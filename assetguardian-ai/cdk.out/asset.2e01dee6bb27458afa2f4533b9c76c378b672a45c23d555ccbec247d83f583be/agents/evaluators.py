"""Experiment 10: Continuous Quality Evaluation (LLM-as-a-Judge).

Three specialized judges evaluate every pipeline invocation (100% sampling,
no gaps) and write structured scores to the policy-audit CloudWatch log
group so quality drift can be tracked via Logs Insights, matching the
report's description exactly.
"""
import json
import logging
import os

import boto3

logger = logging.getLogger()

MODEL_ID = os.environ.get("MODEL_ID", "us.anthropic.claude-sonnet-4-20250514-v1:0")
_bedrock = boto3.client("bedrock-runtime")


def _judge(system_prompt: str, instruction: str, evidence: dict) -> dict:
    response = _bedrock.converse(
        modelId=MODEL_ID,
        system=[{"text": system_prompt}],
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "text": (
                            instruction
                            + "\n\nEvidence (JSON):\n"
                            + json.dumps(evidence, default=str)[:6000]
                            + "\n\nRespond with ONLY minified JSON: "
                            '{"score": number (0-100), "pass": bool, "issues": [string], "rationale": string}'
                        )
                    }
                ],
            }
        ],
        inferenceConfig={"maxTokens": 800, "temperature": 0.0},
    )
    text = "".join(
        b.get("text", "") for b in response["output"]["message"]["content"] if "text" in b
    )
    try:
        start, end = text.find("{"), text.rfind("}")
        return json.loads(text[start : end + 1])
    except Exception:
        return {"score": None, "pass": None, "issues": ["judge_response_unparseable"], "rationale": text[:500]}


def evaluate_damage_detection(damage_result: dict) -> dict:
    return _judge(
        system_prompt=(
            "You are the Damage Detection Accuracy Evaluator for AssetGuardian AI. "
            "Validate severity categorization, flag likely false positives/negatives, "
            "and check whether the confidence scores look properly calibrated."
        ),
        instruction="Evaluate this damage-detection agent output for quality.",
        evidence=damage_result,
    )


def evaluate_lifecycle_decision(lifecycle_result: dict, damage_result: dict) -> dict:
    return _judge(
        system_prompt=(
            "You are the Lifecycle Decision Quality Evaluator for AssetGuardian AI. "
            "Validate business-rule adherence and cost-estimate accuracy, and check "
            "for unsafe decisions such as approving continued use or reissue of a "
            "device graded Failed/Poor, or disposing of a lightly-damaged device."
        ),
        instruction="Evaluate this lifecycle decision given the damage assessment it was based on.",
        evidence={"lifecycle_decision": lifecycle_result, "damage_assessment": damage_result},
    )


def evaluate_fraud_detection(fraud_result: dict) -> dict:
    return _judge(
        system_prompt=(
            "You are the Fraud Detection Accuracy Evaluator for AssetGuardian AI. "
            "Assess whether the risk level assigned is proportionate to the number "
            "and severity of failed checks, and whether the escalation threshold "
            "(Critical/High risk should always escalate) was applied appropriately."
        ),
        instruction="Evaluate this fraud-detection agent output for quality.",
        evidence=fraud_result,
    )


def run_all(*, damage_result: dict | None, lifecycle_result: dict | None, fraud_result: dict | None) -> dict:
    """100% sampling: every pipeline invocation that reaches this point gets
    all applicable judges run against it, matching the report's
    'no decisions go unmonitored' guarantee."""
    scores = {}
    if damage_result:
        scores["damage_detection_evaluation"] = evaluate_damage_detection(damage_result)
    if lifecycle_result and damage_result:
        scores["lifecycle_decision_evaluation"] = evaluate_lifecycle_decision(lifecycle_result, damage_result)
    if fraud_result:
        scores["fraud_detection_evaluation"] = evaluate_fraud_detection(fraud_result)

    logger.info(json.dumps({"event": "quality_evaluation", "scores": scores}, default=str))
    return scores
