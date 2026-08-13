#!/usr/bin/env python3
"""
Provisions the Bedrock AgentCore resources named in the report's
"Infrastructure Services" table that don't yet have CDK L1/L2 constructs:
  - AgentCore Memory        (4-strategy persistent memory)
  - AgentCore Policy Engine + 3 Cedar policies (governance, ENFORCE mode)
  - AgentCore Gateway       (MCP tool routing, AWS_IAM authorizer, policy
                              engine attached)
  - AgentCore Harness       (declarative agent runtime: model + system
                              prompt + memory)
  - 3 AgentCore Evaluators  (LLM-as-a-Judge) + an Online Evaluation Config
                              at 100% sampling

Run this AFTER `cdk deploy` has created the core stack (S3/DynamoDB/Lambda/
API Gateway/IAM). It reads the stack's CloudFormation outputs to get the
Lambda function ARN, then writes its own outputs to
scripts/agentcore_outputs.json and patches the Lambda's environment
variables (AGENTCORE_MEMORY_ID, AGENTCORE_POLICY_ENGINE_ID) so the handler
can use them.

Every resource creation is wrapped in its own try/except: if one preview-
service call fails (these AgentCore APIs are new and evolving), the script
reports it clearly and continues with the rest rather than aborting.
"""
import json
import os
import sys
import time
from pathlib import Path

import boto3

# Overridable via env vars so the same script can target either region's
# stack without duplicating the file. Defaults match the original us-east-1
# rollout for backwards compatibility.
REGION = os.environ.get("AGENTCORE_REGION", "us-east-1")
STACK_NAME = os.environ.get("AGENTCORE_STACK_NAME", "AssetGuardianAiStack")
MODEL_ID = os.environ.get("AGENTCORE_MODEL_ID", "us.anthropic.claude-sonnet-4-20250514-v1:0")
EVALUATOR_MODEL_ID = os.environ.get("AGENTCORE_EVALUATOR_MODEL_ID", "anthropic.claude-sonnet-4-20250514-v1:0")

OUTPUT_FILE_NAME = os.environ.get("AGENTCORE_OUTPUT_FILE", "agentcore_outputs.json")
OUTPUT_FILE = Path(__file__).parent / OUTPUT_FILE_NAME
POLICIES_DIR = Path(__file__).parent.parent / "policies"

cfn = boto3.client("cloudformation", region_name=REGION)
iam = boto3.client("iam", region_name=REGION)
sts = boto3.client("sts", region_name=REGION)
lambda_client = boto3.client("lambda", region_name=REGION)
agentcore = boto3.client("bedrock-agentcore-control", region_name=REGION)

account_id = sts.get_caller_identity()["Account"]
results = {}


def log(msg):
    print(f"[deploy_agentcore] {msg}", flush=True)


def get_stack_outputs() -> dict:
    resp = cfn.describe_stacks(StackName=STACK_NAME)
    outputs = resp["Stacks"][0].get("Outputs", [])
    return {o["OutputKey"]: o["OutputValue"] for o in outputs}


def ensure_role(role_name: str, trust_service: str, inline_policy: dict) -> str:
    """Idempotent: creates the role if missing, otherwise reuses it."""
    trust_policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"Service": trust_service},
                "Action": "sts:AssumeRole",
            }
        ],
    }
    try:
        resp = iam.create_role(
            RoleName=role_name,
            AssumeRolePolicyDocument=json.dumps(trust_policy),
            Description=f"AssetGuardian AI - {role_name}",
        )
        role_arn = resp["Role"]["Arn"]
        log(f"Created IAM role {role_name}")
    except iam.exceptions.EntityAlreadyExistsException:
        role_arn = iam.get_role(RoleName=role_name)["Role"]["Arn"]
        log(f"Reusing existing IAM role {role_name}")

    iam.put_role_policy(
        RoleName=role_name,
        PolicyName=f"{role_name}-inline",
        PolicyDocument=json.dumps(inline_policy),
    )
    # New roles need a few seconds to propagate through STS before they can
    # be assumed by another service.
    time.sleep(8)
    return role_arn


def step_memory():
    role_arn = ensure_role(
        "assetguardian-memory-execution-role",
        "bedrock-agentcore.amazonaws.com",
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Action": ["bedrock:InvokeModel", "bedrock:Converse"],
                    "Resource": "*",
                }
            ],
        },
    )
    try:
        resp = agentcore.create_memory(
            name="assetguardian_enterprise_memory",
            description="AssetGuardian AI - 4-strategy persistent memory (SEMANTIC, SUMMARIZATION, USER_PREFERENCE, EPISODIC)",
            memoryExecutionRoleArn=role_arn,
            eventExpiryDuration=90,
            memoryStrategies=[
                {
                    "semanticMemoryStrategy": {
                        "name": "asset_inspection_semantic",
                        "description": "Vector similarity retrieval for related past inspections",
                        "namespaces": ["actors/{actorId}"],
                    }
                },
                {
                    "summaryMemoryStrategy": {
                        "name": "asset_inspection_summary",
                        "description": "Compresses long session histories to prevent context overflow",
                        "namespaces": ["actors/{actorId}/sessions/{sessionId}"],
                    }
                },
                {
                    "userPreferenceMemoryStrategy": {
                        "name": "employee_interaction_preference",
                        "description": "Tracks interaction patterns and communication preferences per employee",
                        "namespaces": ["actors/{actorId}"],
                    }
                },
                {
                    "episodicMemoryStrategy": {
                        "name": "fleet_episodic_patterns",
                        "description": "Episode-based pattern recognition across the device fleet",
                        "namespaces": ["actors/{actorId}/episodes"],
                        # The API now requires the reflection namespace to be
                        # the same as or a prefix of the episodic namespace
                        # above (default reflection namespace doesn't satisfy
                        # that on its own) — discovered via ValidationException
                        # on the ap-southeast-1 rollout.
                        "reflectionConfiguration": {
                            "namespaces": ["actors/{actorId}"],
                        },
                    }
                },
            ],
        )
        results["memory_id"] = resp["memoryId"]
        results["memory_arn"] = resp["memoryArn"]
        log(f"Created AgentCore Memory: {resp['memoryId']}")
    except Exception as e:
        # CreateMemory raises ValidationException (not ConflictException) when
        # the name is taken, so re-running has to recover the existing IDs here
        # — otherwise patch_lambda_env would blank AGENTCORE_MEMORY_ID and the
        # handler would silently stop recording to memory.
        if "already exists" in str(e):
            for m in agentcore.list_memories().get("memories", []):
                if m.get("id", "").startswith("assetguardian_enterprise_memory"):
                    results["memory_id"] = m["id"]
                    results["memory_arn"] = m.get("arn", "")
                    log(f"Reusing existing AgentCore Memory: {m['id']}")
                    break
            else:
                log(f"Memory exists but could not be listed: {e}")
        else:
            log(f"FAILED to create AgentCore Memory: {e}")


def step_policy_engine():
    try:
        resp = agentcore.create_policy_engine(
            name="assetguardian_policy_engine",
            description="AssetGuardian AI governance guardrails (allow_authenticated_users, deny_unauthenticated, rate_limit_policy)",
        )
        engine_id = resp["policyEngineId"]
        engine_arn = resp["policyEngineArn"]
        results["policy_engine_id"] = engine_id
        results["policy_engine_arn"] = engine_arn
        log(f"Created AgentCore Policy Engine: {engine_id}")
    except agentcore.exceptions.ConflictException:
        for pe in agentcore.list_policy_engines().get("policyEngines", []):
            if pe["name"] == "assetguardian_policy_engine":
                engine_id, engine_arn = pe["policyEngineId"], pe["policyEngineArn"]
                results["policy_engine_id"] = engine_id
                results["policy_engine_arn"] = engine_arn
                log(f"Reusing existing AgentCore Policy Engine: {engine_id}")
                break
        else:
            log("FAILED to create or find AgentCore Policy Engine")
    except Exception as e:
        log(f"FAILED to create AgentCore Policy Engine: {e}")


def step_cedar_policies(gateway_id: str | None):
    engine_id = results.get("policy_engine_id")
    if not engine_id:
        log("No policy_engine_id available; skipping Cedar policy creation.")
        return
    if not gateway_id:
        log("No gateway_id available yet; skipping Cedar policy creation (policies must be scoped to a specific AgentCore::Gateway resource). Run again after the gateway exists.")
        return

    gateway_arn = agentcore.get_gateway(gatewayIdentifier=gateway_id)["gatewayArn"]

    policy_files = {
        "allow_authenticated_users": "allow_authenticated_users.cedar",
        "deny_unauthenticated": "deny_unauthenticated.cedar",
        "rate_limit_policy": "rate_limit_policy.cedar",
    }
    for name, filename in policy_files.items():
        try:
            statement = (POLICIES_DIR / filename).read_text()
            statement = statement.replace(
                "resource is AgentCore::Gateway",
                f'resource == AgentCore::Gateway::"{gateway_arn}"',
            )
            agentcore.create_policy(
                name=name,
                definition={"cedar": {"statement": statement}},
                policyEngineId=engine_id,
                enforcementMode="ACTIVE",
                validationMode="FAIL_ON_ANY_FINDINGS",
            )
            log(f"Created Cedar policy: {name} (ACTIVE)")
        except agentcore.exceptions.ConflictException:
            log(f"Cedar policy {name} already exists, skipping.")
        except Exception as e:
            log(f"FAILED to create policy {name}: {e}")


def step_gateway(lambda_arn: str):
    role_arn = ensure_role(
        "assetguardian-gateway-role",
        "bedrock-agentcore.amazonaws.com",
        {
            "Version": "2012-10-17",
            "Statement": [
                {"Effect": "Allow", "Action": "lambda:InvokeFunction", "Resource": lambda_arn},
                {"Effect": "Allow", "Action": ["bedrock:InvokeModel", "bedrock:Converse"], "Resource": "*"},
                {"Effect": "Allow", "Action": ["bedrock-agentcore:GetPolicyEngine", "bedrock-agentcore:GetPolicyEngineSummary", "bedrock-agentcore:AuthorizeAction", "bedrock-agentcore:PartiallyAuthorizeActions"], "Resource": "*"},
            ],
        },
    )
    policy_engine_arn = results.get("policy_engine_arn")
    kwargs = dict(
        name="assetguardian-enterprise-gateway",
        description="AssetGuardian AI - MCP tool routing gateway",
        roleArn=role_arn,
        protocolType="MCP",
        protocolConfiguration={
            "mcp": {
                "supportedVersions": ["2025-06-18"],
                "instructions": "Route AssetGuardian AI inspection tool calls with Cedar governance enforced.",
                "searchType": "SEMANTIC",
            }
        },
        authorizerType="AWS_IAM",
    )
    if policy_engine_arn:
        kwargs["policyEngineConfiguration"] = {"arn": policy_engine_arn, "mode": "ENFORCE"}

    try:
        resp = agentcore.create_gateway(**kwargs)
        results["gateway_id"] = resp["gatewayId"]
        results["gateway_arn"] = resp["gatewayArn"]
        results["gateway_url"] = resp.get("gatewayUrl", "")
        log(f"Created AgentCore Gateway: {resp['gatewayId']}")
    except agentcore.exceptions.ConflictException:
        for g in agentcore.list_gateways().get("items", []):
            if g.get("name") == "assetguardian-enterprise-gateway":
                gw_id = g["gatewayId"]
                results["gateway_id"] = gw_id
                # list_gateways omits the ARN and URL, so fetch the full record —
                # the Cedar policies are scoped to the gateway ARN and need it.
                full = agentcore.get_gateway(gatewayIdentifier=gw_id)
                results["gateway_arn"] = full.get("gatewayArn", "")
                results["gateway_url"] = full.get("gatewayUrl", "")
                log(f"Reusing existing AgentCore Gateway: {gw_id}")
                break
    except Exception as e:
        log(f"FAILED to create AgentCore Gateway: {e}")
        log("NOTE: Gateway target wiring (registering the Lambda as an MCP tool) is left as a manual follow-up — the target schema is still evolving in this preview API. The portal can call the Lambda directly via the API Gateway HTTP endpoint (SigV4-authorized) in the meantime.")


_COMMON_TOOL_PROPS = {
    "asset_id": {"type": "string", "description": "CMDB asset ID, e.g. ASSET-0174."},
    "serial_number": {"type": "string", "description": "Serial number printed on the device label."},
    "employee_id": {"type": "string", "description": "Staff ID (EMP1219) or corporate email address."},
    "image_keys": {
        "type": "array",
        "items": {"type": "string"},
        "description": "S3 keys of the device photos, under the uploads/ prefix.",
    },
    "label_image_key": {"type": "string", "description": "S3 key of the asset-label photo used for OCR."},
    "device_type": {"type": "string", "description": "laptop | monitor | phone | tablet."},
}

_GATEWAY_TOOLS = [
    ("inspect_device",
     "Runs an ad-hoc device inspection: identity check, photo quality gate, fraud "
     "detection, damage assessment and a lifecycle recommendation."),
    ("employee_handover",
     "Runs the employee handover workflow (24h SLA) when a device is issued to a member of staff."),
    ("asset_return",
     "Runs the most comprehensive workflow (48h SLA) when a device is returned, "
     "including display-health diagnostics."),
    ("annual_self_declaration",
     "Runs the annual self-declaration workflow (72h SLA), including the employee attestation."),
]


def step_gateway_target(lambda_arn: str):
    """Registers the harness Lambda as an MCP tool target on the gateway.

    Without this the gateway exists but routes nothing, so the Cedar policies
    attached to it never evaluate real traffic. The Lambda target schema is
    lambdaArn + an inline tool schema; the handler recognises gateway calls via
    the tool name in the client context (see handler._gateway_tool_name).
    """
    gateway_id = results.get("gateway_id")
    if not gateway_id:
        log("No gateway_id available; skipping gateway target.")
        return

    tools = [
        {
            "name": name,
            "description": description,
            "inputSchema": {
                "type": "object",
                "properties": _COMMON_TOOL_PROPS,
                "required": ["image_keys"],
            },
            "outputSchema": {
                "type": "object",
                "description": "Per-stage inspection results, or an error with a reference code.",
                "properties": {
                    "workflow": {"type": "string"},
                    "sla_hours": {"type": "integer"},
                    "stages": {"type": "object", "description": "Result of each agent stage that ran."},
                },
            },
        }
        for name, description in _GATEWAY_TOOLS
    ]

    # The gateway assumes its own role to call us, but Lambda also needs a
    # resource policy naming the service principal.
    try:
        lambda_client.add_permission(
            FunctionName=lambda_arn,
            StatementId="AllowAgentCoreGatewayInvoke",
            Action="lambda:InvokeFunction",
            Principal="bedrock-agentcore.amazonaws.com",
            SourceAccount=account_id,
        )
        log("Granted bedrock-agentcore permission to invoke the harness Lambda.")
    except lambda_client.exceptions.ResourceConflictException:
        log("Lambda invoke permission for bedrock-agentcore already present.")
    except Exception as e:
        log(f"WARNING: could not add Lambda invoke permission: {e}")

    try:
        resp = agentcore.create_gateway_target(
            gatewayIdentifier=gateway_id,
            name="assetguardian-harness-tools",
            description="AssetGuardian AI inspection workflows exposed as MCP tools",
            targetConfiguration={
                "mcp": {
                    "lambda": {
                        "lambdaArn": lambda_arn,
                        "toolSchema": {"inlinePayload": tools},
                    }
                }
            },
            # Required even for a Lambda target, despite not being marked as
            # such in the API model — CreateGatewayTarget rejects the call with
            # "Credential provider configurations is not defined" without it.
            # GATEWAY_IAM_ROLE means the gateway invokes the Lambda as itself,
            # using assetguardian-gateway-role.
            credentialProviderConfigurations=[
                {"credentialProviderType": "GATEWAY_IAM_ROLE"}
            ],
        )
        results["gateway_target_id"] = resp.get("targetId")
        log(f"Created gateway target with {len(tools)} MCP tools: {resp.get('targetId')}")
    except agentcore.exceptions.ConflictException:
        for t in agentcore.list_gateway_targets(gatewayIdentifier=gateway_id).get("items", []):
            if t.get("name") == "assetguardian-harness-tools":
                results["gateway_target_id"] = t.get("targetId")
                log(f"Reusing existing gateway target: {t.get('targetId')}")
                break
    except Exception as e:
        log(f"FAILED to create gateway target: {e}")


def step_harness(lambda_arn: str):
    role_arn = ensure_role(
        "assetguardian-harness-execution-role",
        "bedrock-agentcore.amazonaws.com",
        {
            "Version": "2012-10-17",
            "Statement": [
                {"Effect": "Allow", "Action": ["bedrock:InvokeModel", "bedrock:Converse", "bedrock:ConverseStream"], "Resource": "*"},
                {"Effect": "Allow", "Action": "lambda:InvokeFunction", "Resource": lambda_arn},
            ],
        },
    )
    memory_arn = results.get("memory_arn")
    system_prompt = (
        "You are AssetGuardian AI, an enterprise IT asset lifecycle orchestrator. "
        "You coordinate identity verification, visual damage inspection, display "
        "health diagnostics, fraud detection, and lifecycle decisions for "
        "enterprise device fleets, enforcing Cedar governance policies and "
        "recording outcomes to persistent memory for future sessions."
    )
    kwargs = dict(
        harnessName="assetguardian_harness",
        executionRoleArn=role_arn,
        model={
            "bedrockModelConfig": {
                "modelId": MODEL_ID,
                "maxTokens": 4096,
                "temperature": 0.1,
            }
        },
        systemPrompt=[{"text": system_prompt}],
        maxIterations=10,
        timeoutSeconds=300,
    )
    if memory_arn:
        kwargs["memory"] = {"agentCoreMemoryConfiguration": {"arn": memory_arn}}

    try:
        resp = agentcore.create_harness(**kwargs)
        # API response nests fields under "harness" now (discovered via KeyError
        # on the ap-southeast-1 rollout — differs from the flat shape the
        # us-east-1 script was originally written against).
        harness = resp.get("harness", resp)
        results["harness_id"] = harness["harnessId"]
        results["harness_arn"] = harness["arn"]
        log(f"Created AgentCore Harness: {harness['harnessId']}")
    except agentcore.exceptions.ConflictException:
        for h in agentcore.list_harnesses().get("harnesses", []):
            if h["harnessName"] == "assetguardian_harness":
                results["harness_id"] = h["harnessId"]
                results["harness_arn"] = h["arn"]
                log(f"Reusing existing AgentCore Harness: {h['harnessId']}")
                break
    except Exception as e:
        log(f"FAILED to create AgentCore Harness: {e}")


def step_evaluators():
    evaluator_defs = {
        "damage_detection_accuracy_evaluator": (
            "Given the conversation context {context} and the assistant's output "
            "{assistant_turn}, validate severity categorization, identify likely "
            "false positives/negatives, and check confidence calibration on the "
            "device damage assessment."
        ),
        "lifecycle_decision_quality_evaluator": (
            "Given the conversation context {context} and the assistant's output "
            "{assistant_turn}, validate business rule adherence and cost estimate "
            "accuracy, and check for unsafe decisions such as reissuing a Grade F "
            "device."
        ),
        "fraud_detection_accuracy_evaluator": (
            "Given the conversation context {context} and the assistant's output "
            "{assistant_turn}, assess whether the risk level is proportionate to "
            "the failed checks and whether escalation thresholds were applied "
            "appropriately."
        ),
    }
    evaluator_short_descriptions = {
        "damage_detection_accuracy_evaluator": "Validates damage severity categorization and confidence calibration.",
        "lifecycle_decision_quality_evaluator": "Validates lifecycle decision business-rule adherence and cost accuracy.",
        "fraud_detection_accuracy_evaluator": "Validates fraud risk-level calibration and escalation thresholds.",
    }
    evaluator_arns = []
    for name, instructions in evaluator_defs.items():
        try:
            resp = agentcore.create_evaluator(
                evaluatorName=name,
                description=evaluator_short_descriptions[name],
                level="TRACE",
                evaluatorConfig={
                    "llmAsAJudge": {
                        "instructions": instructions,
                        "modelConfig": {
                            "bedrockEvaluatorModelConfig": {
                                "modelId": EVALUATOR_MODEL_ID,
                                "inferenceConfig": {"maxTokens": 800, "temperature": 0.0},
                            }
                        },
                        "ratingScale": {
                            "numerical": [
                                {"value": 0.0, "label": "fail", "definition": "Output fails quality bar (score 0-69)."},
                                {"value": 100.0, "label": "pass", "definition": "Output meets quality bar (score 70-100)."},
                            ]
                        },
                    }
                },
            )
            arn = resp.get("evaluatorArn")
            evaluator_arns.append(arn)
            log(f"Created evaluator: {name}")
        except Exception as e:
            log(f"FAILED to create evaluator {name}: {e}")

    results["evaluator_arns"] = evaluator_arns

    if not evaluator_arns or "harness_arn" not in results:
        log("Skipping online evaluation config (no evaluators or harness available).")
        return

    role_arn = ensure_role(
        "assetguardian-eval-execution-role",
        "bedrock-agentcore.amazonaws.com",
        {
            "Version": "2012-10-17",
            "Statement": [
                {"Effect": "Allow", "Action": ["bedrock:InvokeModel", "bedrock:Converse", "logs:GetLogEvents", "logs:FilterLogEvents"], "Resource": "*"},
            ],
        },
    )
    try:
        agentcore.create_online_evaluation_config(
            onlineEvaluationConfigName="assetguardian_quality_monitoring",
            description="100% sampling continuous quality evaluation of every AssetGuardian AI harness invocation",
            rule={"samplingConfig": {"samplingPercentage": 100}},
            dataSourceConfig={"harnessArn": results["harness_arn"]},
            evaluators=evaluator_arns,
            evaluationExecutionRoleArn=role_arn,
            enableOnCreate=True,
        )
        log("Created online evaluation config at 100% sampling, ENABLED.")
    except Exception as e:
        log(f"FAILED to create online evaluation config: {e}")


def patch_lambda_env():
    try:
        current = lambda_client.get_function_configuration(FunctionName="assetguardian-harness-invoke")
        env = current.get("Environment", {}).get("Variables", {})
        # Only write values we actually resolved this run. Assigning
        # results.get(key, "") unconditionally would wipe a working ID whenever
        # a step failed or was skipped, silently disabling the feature.
        for env_key, result_key in (
            ("AGENTCORE_MEMORY_ID", "memory_id"),
            ("AGENTCORE_POLICY_ENGINE_ID", "policy_engine_id"),
            ("AGENTCORE_GATEWAY_URL", "gateway_url"),
        ):
            value = results.get(result_key)
            if value:
                env[env_key] = value
            elif env.get(env_key):
                log(f"Keeping existing {env_key} (this run did not resolve one).")
        lambda_client.update_function_configuration(
            FunctionName="assetguardian-harness-invoke", Environment={"Variables": env}
        )
        log("Patched Lambda environment variables with AgentCore resource IDs.")
    except Exception as e:
        log(f"FAILED to patch Lambda environment: {e}")


def main():
    log(f"Deploying AgentCore resources to account {account_id}, region {REGION}")
    outputs = get_stack_outputs()
    lambda_arn = outputs.get("HarnessFunctionArn")
    if not lambda_arn:
        log("ERROR: HarnessFunctionArn not found in stack outputs. Run `cdk deploy` first.")
        sys.exit(1)

    step_memory()
    step_policy_engine()
    step_gateway(lambda_arn)
    step_gateway_target(lambda_arn)
    step_cedar_policies(results.get("gateway_id"))
    step_harness(lambda_arn)
    step_evaluators()
    patch_lambda_env()

    OUTPUT_FILE.write_text(json.dumps(results, indent=2, default=str))
    log(f"Wrote results to {OUTPUT_FILE}")
    log(json.dumps(results, indent=2, default=str))


if __name__ == "__main__":
    main()
