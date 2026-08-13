"""
AssetGuardian AI - core infrastructure stack.

Reverse-engineered from the "NCS EUSS - AssetGuardian AI" hackathon project
report. The original source (CDK app under @aws/agentcore-cdk, Lambda code,
frontend portal) lived in a CodeCommit repo in a different AWS account that
this rebuild does not have access to, so this stack reconstructs the
architecture from the report's "Infrastructure Services" table only, and is
hardened for production use per the report's own "Security Hardening" gap
list under Future Work.

Deliberate deviations from the report / security hardening applied here
(see README for the full list):
  - Everything (S3, DynamoDB, CloudWatch Logs) is encrypted with a
    customer-managed KMS key, not default AWS-managed keys.
  - The S3 bucket blocks all public access and denies non-TLS requests.
    Browser uploads go through a Cognito unauthenticated identity scoped to
    a single prefix instead of an open public PUT (the report explicitly
    flags the original's public PUT as an unaddressed security gap).
  - The API Gateway HTTP API requires SigV4 (AWS_IAM) authorization on every
    route instead of being open to the internet (also an explicitly flagged
    gap in the report).
  - A WAFv2 Web ACL (managed rule groups + rate limiting) sits in front of
    the API behind a CloudFront distribution for DDoS/injection/bot
    protection (also an explicitly flagged gap in the report). WAFv2 cannot
    be attached directly to an API Gateway HTTP API (v2) — only to REST
    APIs (v1), CloudFront, ALB, AppSync, or Cognito — so CloudFront is the
    standard AWS pattern to get WAF coverage in front of an HTTP API.
  - Bedrock AgentCore Gateway / Memory / Policy Engine / Harness /
    Evaluators are not yet exposed as CDK L1/L2 constructs (very new
    preview service), so they are provisioned by scripts/deploy_agentcore.py
    after this stack deploys, using the IAM role ARNs this stack outputs.
"""

from aws_cdk import (
    Stack,
    Duration,
    RemovalPolicy,
    CfnOutput,
    CfnJson,
    aws_s3 as s3,
    aws_dynamodb as dynamodb,
    aws_iam as iam,
    aws_kms as kms,
    aws_lambda as _lambda,
    aws_apigatewayv2 as apigwv2,
    aws_apigatewayv2_integrations as apigwv2_integrations,
    aws_apigatewayv2_authorizers as apigwv2_authorizers,
    aws_cognito as cognito,
    aws_logs as logs,
    aws_secretsmanager as secretsmanager,
    aws_wafv2 as wafv2,
    aws_cloudfront as cloudfront,
    aws_cloudfront_origins as cloudfront_origins,
    aws_s3_deployment as s3_deployment,
)
from constructs import Construct


CLAUDE_SONNET_4_MODEL_ID = "us.anthropic.claude-sonnet-4-20250514-v1:0"


class AssetGuardianWafStack(Stack):
    """
    CloudFront's WAFv2 Web ACL (scope=CLOUDFRONT) can only be created in
    us-east-1 — this is a hard AWS requirement that applies regardless of
    which region the CloudFront distribution's origin (the rest of this
    stack) lives in, since CloudFront itself is a global/edge service. When
    the main stack deploys outside us-east-1 (e.g. ap-southeast-1 for the
    Singapore rollout), this small stack is deployed separately, pinned to
    us-east-1, and its Web ACL ARN is passed into AssetGuardianStack.
    """

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self.web_acl = wafv2.CfnWebACL(
            self,
            "ApiWebAcl",
            scope="CLOUDFRONT",
            default_action=wafv2.CfnWebACL.DefaultActionProperty(allow={}),
            visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
                sampled_requests_enabled=True,
                cloud_watch_metrics_enabled=True,
                metric_name="AssetGuardianApiWebAcl",
            ),
            rules=[
                wafv2.CfnWebACL.RuleProperty(
                    name="AWS-AWSManagedRulesCommonRuleSet",
                    priority=1,
                    override_action=wafv2.CfnWebACL.OverrideActionProperty(none={}),
                    statement=wafv2.CfnWebACL.StatementProperty(
                        managed_rule_group_statement=wafv2.CfnWebACL.ManagedRuleGroupStatementProperty(
                            vendor_name="AWS", name="AWSManagedRulesCommonRuleSet"
                        )
                    ),
                    visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
                        sampled_requests_enabled=True,
                        cloud_watch_metrics_enabled=True,
                        metric_name="CommonRuleSet",
                    ),
                ),
                wafv2.CfnWebACL.RuleProperty(
                    name="AWS-AWSManagedRulesKnownBadInputsRuleSet",
                    priority=2,
                    override_action=wafv2.CfnWebACL.OverrideActionProperty(none={}),
                    statement=wafv2.CfnWebACL.StatementProperty(
                        managed_rule_group_statement=wafv2.CfnWebACL.ManagedRuleGroupStatementProperty(
                            vendor_name="AWS", name="AWSManagedRulesKnownBadInputsRuleSet"
                        )
                    ),
                    visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
                        sampled_requests_enabled=True,
                        cloud_watch_metrics_enabled=True,
                        metric_name="KnownBadInputs",
                    ),
                ),
                wafv2.CfnWebACL.RuleProperty(
                    name="RateLimitPerIp",
                    priority=3,
                    action=wafv2.CfnWebACL.RuleActionProperty(block={}),
                    statement=wafv2.CfnWebACL.StatementProperty(
                        rate_based_statement=wafv2.CfnWebACL.RateBasedStatementProperty(
                            limit=2000, aggregate_key_type="IP"
                        )
                    ),
                    visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
                        sampled_requests_enabled=True,
                        cloud_watch_metrics_enabled=True,
                        metric_name="RateLimitPerIp",
                    ),
                ),
            ],
        )
        CfnOutput(self, "ApiWebAclArn", value=self.web_acl.attr_arn)


class AssetGuardianStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        bedrock_model_id: str = CLAUDE_SONNET_4_MODEL_ID,
        web_acl_arn: str = None,
        extra_cors_origins: list = None,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        account = self.account
        region = self.region
        # us-east-1 keeps its original (unsuffixed) resource names so the
        # already-live stack there is untouched; any other region gets a
        # region suffix on globally-unique names (S3 bucket names are
        # unique across ALL regions, not just per-account) so it can be
        # deployed and run side by side with the us-east-1 stack.
        name_suffix = "" if region == "us-east-1" else f"-{region}"

        # ------------------------------------------------------------------
        # KMS - customer-managed key for all data-at-rest encryption
        # ------------------------------------------------------------------
        kms_key = kms.Key(
            self,
            "AssetGuardianKey",
            description="AssetGuardian AI - CMK for S3 photos, DynamoDB CMDB, and CloudWatch Logs",
            enable_key_rotation=True,
            removal_policy=RemovalPolicy.RETAIN,
        )

        # ------------------------------------------------------------------
        # S3 - device inspection photo storage + access-log bucket
        # ------------------------------------------------------------------
        access_logs_bucket = s3.Bucket(
            self,
            "AccessLogsBucket",
            bucket_name=f"assetguardian-access-logs-{account}{name_suffix}",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            object_ownership=s3.ObjectOwnership.OBJECT_WRITER,
            lifecycle_rules=[
                s3.LifecycleRule(id="expire-access-logs", enabled=True, expiration=Duration.days(365))
            ],
            removal_policy=RemovalPolicy.RETAIN,
        )

        photos_bucket = s3.Bucket(
            self,
            "AssetPhotosBucket",
            bucket_name=f"assetguardian-asset-photos-{account}{name_suffix}",
            versioned=True,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.KMS,
            encryption_key=kms_key,
            bucket_key_enabled=True,
            enforce_ssl=True,
            server_access_logs_bucket=access_logs_bucket,
            server_access_logs_prefix="asset-photos/",
            # CORS is attached further down via add_cors_rule(), once the portal
            # CloudFront distribution exists and its domain (the only allowed
            # origin) is known.
            lifecycle_rules=[
                s3.LifecycleRule(
                    id="transition-and-expire-inspection-photos",
                    enabled=True,
                    transitions=[
                        s3.Transition(
                            storage_class=s3.StorageClass.INFREQUENT_ACCESS,
                            transition_after=Duration.days(90),
                        )
                    ],
                    expiration=Duration.days(730),
                    noncurrent_version_expiration=Duration.days(90),
                )
            ],
            removal_policy=RemovalPolicy.RETAIN,
        )

        # ------------------------------------------------------------------
        # DynamoDB - CMDB asset table (KMS-encrypted, PITR, deletion
        # protection)
        # ------------------------------------------------------------------
        cmdb_table = dynamodb.TableV2(
            self,
            "AssetCmdbTable",
            table_name="assetguardian-cmdb",
            partition_key=dynamodb.Attribute(
                name="assetId", type=dynamodb.AttributeType.STRING
            ),
            billing=dynamodb.Billing.on_demand(),
            encryption=dynamodb.TableEncryptionV2.customer_managed_key(kms_key),
            point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=True
            ),
            deletion_protection=True,
            removal_policy=RemovalPolicy.RETAIN,
        )
        cmdb_table.add_global_secondary_index(
            index_name="SerialNumberIndex",
            partition_key=dynamodb.Attribute(
                name="serialNumber", type=dynamodb.AttributeType.STRING
            ),
        )
        cmdb_table.add_global_secondary_index(
            index_name="AssignedUserIndex",
            partition_key=dynamodb.Attribute(
                name="assignedUser", type=dynamodb.AttributeType.STRING
            ),
        )

        # ------------------------------------------------------------------
        # DynamoDB - inspection history, the record behind "my activity" for
        # users and "everyone's activity" for admins.
        #
        # Partitioned on the Cognito *identity ID* rather than the employee ID
        # the user typed. The identity ID arrives in the API Gateway request
        # context from the signed SigV4 credentials, so a caller cannot claim
        # to be someone else by editing a form field; employeeId is display
        # data and is deliberately not the isolation boundary.
        # ------------------------------------------------------------------
        history_table = dynamodb.TableV2(
            self,
            "InspectionHistoryTable",
            table_name="assetguardian-inspection-history",
            partition_key=dynamodb.Attribute(
                name="identityId", type=dynamodb.AttributeType.STRING
            ),
            sort_key=dynamodb.Attribute(
                name="inspectedAt", type=dynamodb.AttributeType.STRING
            ),
            billing=dynamodb.Billing.on_demand(),
            encryption=dynamodb.TableEncryptionV2.customer_managed_key(kms_key),
            point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=True
            ),
            deletion_protection=True,
            removal_policy=RemovalPolicy.RETAIN,
        )
        # Admin views: "all activity, newest first" and "everything for this
        # asset". Both are GSIs rather than scans so the console stays usable
        # as history grows.
        history_table.add_global_secondary_index(
            index_name="AllActivityIndex",
            partition_key=dynamodb.Attribute(
                name="recordType", type=dynamodb.AttributeType.STRING
            ),
            sort_key=dynamodb.Attribute(
                name="inspectedAt", type=dynamodb.AttributeType.STRING
            ),
        )
        history_table.add_global_secondary_index(
            index_name="AssetHistoryIndex",
            partition_key=dynamodb.Attribute(
                name="assetId", type=dynamodb.AttributeType.STRING
            ),
            sort_key=dynamodb.Attribute(
                name="inspectedAt", type=dynamodb.AttributeType.STRING
            ),
        )

        # ------------------------------------------------------------------
        # Cognito User Pool - real login. Two admission paths:
        #   - Self-service sign-up restricted to @ncs.com.sg email addresses
        #     (enforced by the PreSignUpDomainCheck Lambda trigger below).
        #   - Admin-provisioned accounts (created via AdminCreateUser, e.g.
        #     scripts/create_admin_user.py), which bypass the domain check
        #     since they don't go through public self sign-up.
        # ------------------------------------------------------------------
        presignup_role = iam.Role(
            self,
            "PreSignUpTriggerRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AWSLambdaBasicExecutionRole"
                )
            ],
        )
        presignup_lambda = _lambda.Function(
            self,
            "PreSignUpDomainCheck",
            function_name="assetguardian-presignup-domain-check",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="index.handler",
            role=presignup_role,
            timeout=Duration.seconds(10),
            code=_lambda.Code.from_inline(
                "ALLOWED_DOMAIN = '@ncs.com.sg'\n"
                "\n"
                "def handler(event, context):\n"
                "    trigger = event.get('triggerSource', '')\n"
                "    # Admin-provisioned accounts (AdminCreateUser) skip the domain check;\n"
                "    # only public self sign-up is restricted to the corporate domain.\n"
                "    if trigger == 'PreSignUp_SignUp':\n"
                "        email = (event.get('request', {}).get('userAttributes', {}).get('email') or '').lower()\n"
                "        if not email.endswith(ALLOWED_DOMAIN):\n"
                "            raise Exception(f'Sign-up is restricted to {ALLOWED_DOMAIN} email addresses.')\n"
                "    return event\n"
            ),
        )

        user_pool = cognito.UserPool(
            self,
            "PortalUserPool",
            user_pool_name="assetguardian-users",
            self_sign_up_enabled=True,
            sign_in_aliases=cognito.SignInAliases(email=True, username=False),
            standard_attributes=cognito.StandardAttributes(
                email=cognito.StandardAttribute(required=True, mutable=False)
            ),
            auto_verify=cognito.AutoVerifiedAttrs(email=True),
            password_policy=cognito.PasswordPolicy(
                min_length=10,
                require_lowercase=True,
                require_uppercase=True,
                require_digits=True,
                require_symbols=False,
            ),
            account_recovery=cognito.AccountRecovery.EMAIL_ONLY,
            lambda_triggers=cognito.UserPoolTriggers(pre_sign_up=presignup_lambda),
            removal_policy=RemovalPolicy.RETAIN,
        )

        user_pool_client = user_pool.add_client(
            "PortalUserPoolClient",
            auth_flows=cognito.AuthFlow(user_password=True, user_srp=True),
            generate_secret=False,  # browser-based (public) client
            id_token_validity=Duration.hours(1),
            access_token_validity=Duration.hours(1),
            refresh_token_validity=Duration.days(30),
        )

        # ------------------------------------------------------------------
        # Cognito Identity Pool - exchanges a Cognito User Pool login (or,
        # for unauthenticated visitors browsing the hero/landing page, an
        # anonymous identity) for short-lived AWS credentials.
        # ------------------------------------------------------------------
        identity_pool = cognito.CfnIdentityPool(
            self,
            "PortalIdentityPool",
            identity_pool_name="assetguardian_portal_identity_pool",
            allow_unauthenticated_identities=True,
            cognito_identity_providers=[
                cognito.CfnIdentityPool.CognitoIdentityProviderProperty(
                    client_id=user_pool_client.user_pool_client_id,
                    provider_name=user_pool.user_pool_provider_name,
                )
            ],
        )

        unauth_role = iam.Role(
            self,
            "PortalUnauthRole",
            assumed_by=iam.FederatedPrincipal(
                "cognito-identity.amazonaws.com",
                conditions={
                    "StringEquals": {"cognito-identity.amazonaws.com:aud": identity_pool.ref},
                    "ForAnyValue:StringLike": {
                        "cognito-identity.amazonaws.com:amr": "unauthenticated"
                    },
                },
                assume_role_action="sts:AssumeRoleWithWebIdentity",
            ),
        )
        unauth_role.add_to_policy(
            iam.PolicyStatement(
                actions=["s3:PutObject"],
                resources=[photos_bucket.arn_for_objects("uploads/*")],
            )
        )
        kms_key.grant_encrypt_decrypt(unauth_role)

        # Logged-in users (admin or any verified @ncs.com.sg employee) get
        # this role instead — same upload scope today, but this is the role
        # to widen (e.g. read-back of their own inspection history) as the
        # portal grows past the "minimal UI" stage.
        auth_role = iam.Role(
            self,
            "PortalAuthRole",
            assumed_by=iam.FederatedPrincipal(
                "cognito-identity.amazonaws.com",
                conditions={
                    "StringEquals": {"cognito-identity.amazonaws.com:aud": identity_pool.ref},
                    "ForAnyValue:StringLike": {
                        "cognito-identity.amazonaws.com:amr": "authenticated"
                    },
                },
                assume_role_action="sts:AssumeRoleWithWebIdentity",
            ),
        )
        # Upload only. Read access is deliberately NOT granted: a blanket
        # s3:GetObject on uploads/* would let any signed-in employee fetch any
        # other employee's evidence by guessing a key. Images are served
        # instead as short-lived presigned URLs from /media, which checks the
        # caller owns the record first.
        auth_role.add_to_policy(
            iam.PolicyStatement(
                actions=["s3:PutObject"],
                resources=[photos_bucket.arn_for_objects("uploads/*")],
            )
        )
        kms_key.grant_encrypt_decrypt(auth_role)

        # ------------------------------------------------------------------
        # Real admin authorization. Membership in the "Admins" User Pool
        # Group (not a client-side email heuristic) is what actually grants
        # elevated access: the ID token's `cognito:groups` claim is matched
        # by an Identity Pool role-mapping rule below, which upgrades the
        # federated session to AdminRole. AdminRole gets everything
        # auth_role has, plus read access to the CloudWatch Logs the portal
        # exposes as "Event Logs" to admins.
        # ------------------------------------------------------------------
        admins_group = cognito.CfnUserPoolGroup(
            self,
            "AdminsGroup",
            user_pool_id=user_pool.user_pool_id,
            group_name="Admins",
            description="AssetGuardian AI administrators (elevated portal access, e.g. event log viewing)",
        )

        admin_role = iam.Role(
            self,
            "PortalAdminRole",
            assumed_by=iam.FederatedPrincipal(
                "cognito-identity.amazonaws.com",
                conditions={
                    "StringEquals": {"cognito-identity.amazonaws.com:aud": identity_pool.ref},
                    "ForAnyValue:StringLike": {
                        "cognito-identity.amazonaws.com:amr": "authenticated"
                    },
                },
                assume_role_action="sts:AssumeRoleWithWebIdentity",
            ),
        )
        # Same as auth_role: admins also read evidence through /media, so that
        # every image fetch is attributable in the audit log rather than being
        # an unlogged direct S3 GET.
        admin_role.add_to_policy(
            iam.PolicyStatement(
                actions=["s3:PutObject"],
                resources=[photos_bucket.arn_for_objects("uploads/*")],
            )
        )
        kms_key.grant_encrypt_decrypt(admin_role)

        # The role-mapping dict key is
        # "cognito-idp.{region}.amazonaws.com/{userPoolId}:{clientId}" —
        # userPoolId/clientId are CloudFormation tokens not known until
        # deploy time, so a plain Python dict (which needs a literal string
        # key at synth time) doesn't work here. CfnJson defers resolution of
        # the whole structure (including the key) to deploy time instead.
        role_mappings_value = CfnJson(
            self,
            "RoleMappingsJson",
            value={
                f"cognito-idp.{region}.amazonaws.com/{user_pool.user_pool_id}:{user_pool_client.user_pool_client_id}": {
                    "Type": "Rules",
                    "AmbiguousRoleResolution": "AuthenticatedRole",
                    "RulesConfiguration": {
                        "Rules": [
                            {
                                "Claim": "cognito:groups",
                                "MatchType": "Contains",
                                "Value": "Admins",
                                "RoleARN": admin_role.role_arn,
                            }
                        ]
                    },
                }
            },
        )

        cognito.CfnIdentityPoolRoleAttachment(
            self,
            "IdentityPoolRoleAttachment",
            identity_pool_id=identity_pool.ref,
            roles={"unauthenticated": unauth_role.role_arn, "authenticated": auth_role.role_arn},
            role_mappings=role_mappings_value,
        )

        # ------------------------------------------------------------------
        # CloudWatch - dedicated, KMS-encrypted log groups for the harness
        # Lambda and for the policy-decision audit trail.
        # ------------------------------------------------------------------
        kms_key.grant_encrypt_decrypt(
            iam.ServicePrincipal(f"logs.{region}.amazonaws.com")
        )
        lambda_log_group = logs.LogGroup(
            self,
            "HarnessInvokeLogGroup",
            log_group_name="/assetguardian/harness-invoke",
            retention=logs.RetentionDays.ONE_YEAR,
            encryption_key=kms_key,
            removal_policy=RemovalPolicy.RETAIN,
        )
        policy_audit_log_group = logs.LogGroup(
            self,
            "PolicyAuditLogGroup",
            log_group_name="/assetguardian/policy-audit",
            retention=logs.RetentionDays.SIX_YEARS,  # SOX/audit-friendly retention
            encryption_key=kms_key,
            removal_policy=RemovalPolicy.RETAIN,
        )

        # Admins can read (not write) both log groups directly from the
        # browser's "Event Logs" panel — FilterLogEvents/GetLogEvents/
        # DescribeLogStreams only, scoped to exactly these two log groups.
        admin_role.add_to_policy(
            iam.PolicyStatement(
                sid="AdminReadEventLogs",
                actions=[
                    "logs:FilterLogEvents",
                    "logs:GetLogEvents",
                    "logs:DescribeLogStreams",
                ],
                resources=[
                    lambda_log_group.log_group_arn,
                    f"{lambda_log_group.log_group_arn}:*",
                    policy_audit_log_group.log_group_arn,
                    f"{policy_audit_log_group.log_group_arn}:*",
                ],
            )
        )
        kms_key.grant_decrypt(admin_role)  # to decrypt KMS-encrypted log events

        # ------------------------------------------------------------------
        # Origin sealing. The execute-api endpoint stays publicly resolvable —
        # HTTP APIs (v2) support neither resource policies nor CloudFront
        # Origin Access Control, so there is no way to make API Gateway itself
        # refuse non-CloudFront callers. Instead CloudFront injects a shared
        # secret header on every origin request and the Lambda rejects any
        # request that doesn't carry it, which makes the WAF-bypassing direct
        # route useless.
        #
        # This is additive, not a replacement: the route keeps its AWS_IAM
        # authorizer, so a caller still needs valid SigV4 credentials AND the
        # header. A route can only carry one authorizer, so validating the
        # header in the handler is what lets both controls coexist.
        #
        # Caveat worth knowing: CloudFront origin custom headers are stored in
        # the distribution config in plaintext, so anyone with
        # cloudfront:GetDistribution in this account can read the secret. That
        # is inherent to the pattern; rotation is the mitigation (rotate the
        # secret, then redeploy so CloudFront picks up the new value).
        # ------------------------------------------------------------------
        origin_verify_secret = secretsmanager.Secret(
            self,
            "OriginVerifySecret",
            description="AssetGuardian AI - shared secret proving a request arrived via CloudFront",
            generate_secret_string=secretsmanager.SecretStringGenerator(
                exclude_punctuation=True,
                password_length=48,
            ),
            removal_policy=RemovalPolicy.RETAIN,
        )

        # ------------------------------------------------------------------
        # IAM role for the harness Lambda (least-privilege, scoped resources)
        # ------------------------------------------------------------------
        lambda_role = iam.Role(
            self,
            "HarnessInvokeRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AWSLambdaBasicExecutionRole"
                )
            ],
        )
        # Cross-region (and "global.") inference profiles fan requests out to
        # whichever region/geography has capacity (observed live: a request
        # against a "us." profile actually executed against a foundation-model
        # resource in a different US region, and got AccessDeniedException when
        # the policy was scoped to one region only). AWS's own guidance for
        # cross-region/global inference profiles is to grant the foundation-model
        # permission across all regions the profile can dispatch to — a region
        # wildcard is the standard, supported way to do that. Foundation-model
        # ARNs never carry the geography prefix ("us."/"apac."/"global.") that
        # inference-profile IDs do, so strip it before building the ARN.
        bare_model_name = bedrock_model_id
        for prefix in ("us.", "apac.", "eu.", "global."):
            if bare_model_name.startswith(prefix):
                bare_model_name = bare_model_name[len(prefix):]
                break
        lambda_role.add_to_policy(
            iam.PolicyStatement(
                sid="BedrockVisionReasoning",
                actions=[
                    "bedrock:InvokeModel",
                    "bedrock:InvokeModelWithResponseStream",
                    "bedrock:Converse",
                    "bedrock:ConverseStream",
                ],
                resources=[
                    f"arn:aws:bedrock:*::foundation-model/{bare_model_name}",
                    # The inference-profile resource itself (what's actually
                    # invoked, and what usage/billing is metered against)
                    # stays scoped to this account and region.
                    f"arn:aws:bedrock:{region}:{account}:inference-profile/{bedrock_model_id}",
                ],
            )
        )
        lambda_role.add_to_policy(
            iam.PolicyStatement(
                sid="RekognitionOcrAndModeration",
                actions=[
                    "rekognition:DetectText",
                    "rekognition:DetectLabels",
                    "rekognition:DetectModerationLabels",
                ],
                resources=["*"],  # Rekognition Detect* APIs do not support resource-level scoping
            )
        )
        lambda_role.add_to_policy(
            iam.PolicyStatement(
                sid="AgentCoreMemoryGatewayAndPolicy",
                actions=[
                    "bedrock-agentcore:GetEvent",
                    "bedrock-agentcore:CreateEvent",
                    "bedrock-agentcore:ListEvents",
                    "bedrock-agentcore:RetrieveMemoryRecords",
                    "bedrock-agentcore:InvokeGateway",
                    "bedrock-agentcore:GetPolicyEngineSummary",
                ],
                resources=["*"],  # AgentCore data-plane APIs do not yet support resource ARNs in policy conditions
            )
        )
        photos_bucket.grant_read(lambda_role)
        cmdb_table.grant_read_write_data(lambda_role)
        history_table.grant_read_write_data(lambda_role)
        kms_key.grant_decrypt(lambda_role)
        origin_verify_secret.grant_read(lambda_role)

        # ------------------------------------------------------------------
        # Lambda - eucd-harness-invoke equivalent (multi-agent orchestrator)
        # ------------------------------------------------------------------
        harness_lambda = _lambda.Function(
            self,
            "HarnessInvokeFunction",
            function_name="assetguardian-harness-invoke",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="handler.lambda_handler",
            code=_lambda.Code.from_asset("lambda/harness_invoke"),
            memory_size=512,
            timeout=Duration.seconds(300),
            role=lambda_role,
            log_group=lambda_log_group,
            tracing=_lambda.Tracing.ACTIVE,
            environment={
                "CMDB_TABLE_NAME": cmdb_table.table_name,
                "HISTORY_TABLE_NAME": history_table.table_name,
                "PHOTOS_BUCKET_NAME": photos_bucket.bucket_name,
                "MODEL_ID": bedrock_model_id,
                "ORIGIN_VERIFY_SECRET_ARN": origin_verify_secret.secret_arn,
                "ORIGIN_VERIFY_HEADER": "x-origin-verify",
                # Still frames sampled from an uploaded video clip. Each frame
                # joins a multi-modal Bedrock call, so raising this raises both
                # inspection latency and per-run spend.
                "MAX_VIDEO_FRAMES": "6",
                "AGENTCORE_POLICY_ENGINE_ID": "",  # populated post-deploy by scripts/deploy_agentcore.py
                "AGENTCORE_MEMORY_ID": "",  # populated post-deploy by scripts/deploy_agentcore.py
                "AGENTCORE_GATEWAY_URL": "",  # populated post-deploy by scripts/deploy_agentcore.py
            },
        )

        # ------------------------------------------------------------------
        # Portal - minimal static web UI (upload photos, call the API,
        # show results), served from a private S3 bucket behind its own
        # CloudFront distribution with Origin Access Control (no public
        # bucket). The page itself calls S3 and API Gateway directly with
        # browser-side SigV4 signing using short-lived Cognito
        # unauthenticated-identity credentials — no secrets are baked into
        # the static files.
        #
        # This block is constructed BEFORE the API and before the photos
        # bucket's CORS rule because the distribution's domain name is the
        # single allowed CORS origin for both — see cors_origins below.
        # ------------------------------------------------------------------
        portal_bucket = s3.Bucket(
            self,
            "PortalBucket",
            bucket_name=f"assetguardian-portal-{account}{name_suffix}",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            removal_policy=RemovalPolicy.RETAIN,
        )

        portal_distribution = cloudfront.Distribution(
            self,
            "PortalDistribution",
            comment="AssetGuardian AI - inspection portal (static UI)",
            default_root_object="index.html",
            default_behavior=cloudfront.BehaviorOptions(
                origin=cloudfront_origins.S3BucketOrigin.with_origin_access_control(portal_bucket),
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                cache_policy=cloudfront.CachePolicy.CACHING_OPTIMIZED,
            ),
        )

        s3_deployment.BucketDeployment(
            self,
            "PortalDeployment",
            sources=[s3_deployment.Source.asset("portal")],
            destination_bucket=portal_bucket,
            distribution=portal_distribution,
            distribution_paths=["/*"],
        )

        # ------------------------------------------------------------------
        # CORS allow-list. The portal is the only browser origin that calls
        # the API or PUTs to the photos bucket, so the allow-list is exactly
        # its CloudFront domain. Referencing the distribution's attribute
        # (rather than hardcoding the current d-name) keeps this correct if
        # the distribution is ever replaced. `extra_cors_origins` is the
        # escape hatch for a custom domain / a local dev origin — pass full
        # origins including scheme, e.g. "https://assets.ncs.com.sg".
        # ------------------------------------------------------------------
        cors_origins = [f"https://{portal_distribution.distribution_domain_name}"]
        cors_origins += list(extra_cors_origins or [])

        photos_bucket.add_cors_rule(
            allowed_methods=[s3.HttpMethods.PUT, s3.HttpMethods.GET],
            allowed_origins=cors_origins,
            # Header wildcard is retained deliberately: a browser SigV4 PUT
            # sends a variable set of x-amz-* headers (date, security-token,
            # content-sha256, server-side-encryption, ...) and pinning that
            # list breaks uploads the moment one changes. The origin is the
            # control that matters here.
            allowed_headers=["*"],
            max_age=3000,  # seconds; add_cors_rule takes a plain int, not a Duration
        )

        # ------------------------------------------------------------------
        # API Gateway HTTP API - portal -> Lambda backend, SigV4-only
        # ------------------------------------------------------------------
        http_api = apigwv2.HttpApi(
            self,
            "AssetGuardianHttpApi",
            api_name="assetguardian-api",
            cors_preflight=apigwv2.CorsPreflightOptions(
                allow_origins=cors_origins,
                allow_methods=[apigwv2.CorsHttpMethod.POST, apigwv2.CorsHttpMethod.OPTIONS],
                # NOTE: a bare "*" mixed with named headers causes API Gateway's automatic
                # CORS preflight response to come back with NO Access-Control-Allow-* headers
                # at all (verified live — confirmed by curl against the OPTIONS route), which
                # browsers then hard-fail as "Failed to fetch". Use an explicit list instead.
                allow_headers=[
                    "Authorization",
                    "Content-Type",
                    "X-Amz-Date",
                    "X-Amz-Security-Token",
                    "X-Amz-Content-Sha256",
                ],
            ),
            create_default_stage=True,
        )
        integration = apigwv2_integrations.HttpLambdaIntegration(
            "HarnessIntegration", harness_lambda
        )
        iam_authorizer = apigwv2_authorizers.HttpIamAuthorizer()
        # /history returns the caller's own inspections, or everyone's when the
        # caller assumed PortalAdminRole. /media issues a short-lived presigned
        # URL for one evidence object, but only if that object belongs to a
        # record the caller is allowed to see.
        for route in ["/inspect", "/handover", "/return", "/declare", "/history", "/media"]:
            http_api.add_routes(
                path=route,
                methods=[apigwv2.HttpMethod.POST],
                integration=integration,
                authorizer=iam_authorizer,
            )

        # Cognito-authenticated (logged-in), admin, and unauthenticated
        # (upload-only, pre-login) identities are all allowed to sign and
        # invoke the API.
        harness_lambda.grant_invoke(unauth_role)
        harness_lambda.grant_invoke(auth_role)
        harness_lambda.grant_invoke(admin_role)
        for role in (unauth_role, auth_role, admin_role):
            role.add_to_policy(
                iam.PolicyStatement(
                    actions=["execute-api:Invoke"],
                    resources=[
                        f"arn:aws:execute-api:{region}:{account}:{http_api.api_id}/*/POST/*"
                    ],
                )
            )

        # ------------------------------------------------------------------
        # WAFv2 (CLOUDFRONT scope) + CloudFront in front of the HTTP API -
        # managed rule groups + rate limiting (addresses the report's
        # flagged "No WAF rules on API Gateway" gap). HTTP APIs (v2) can't
        # take a WAF association directly, so CloudFront is the distribution
        # layer that WAF attaches to.
        #
        # CloudFront's WAFv2 Web ACL can only be *created* in us-east-1
        # (a hard AWS requirement, independent of where this stack itself
        # deploys). In us-east-1 that's this stack's own region, so it's
        # created inline as before; anywhere else, AssetGuardianWafStack
        # must be deployed separately to us-east-1 first and its ARN passed
        # in here via the web_acl_arn constructor argument.
        # ------------------------------------------------------------------
        if region == "us-east-1":
            web_acl = wafv2.CfnWebACL(
                self,
                "ApiWebAcl",
                scope="CLOUDFRONT",
                default_action=wafv2.CfnWebACL.DefaultActionProperty(allow={}),
                visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
                    sampled_requests_enabled=True,
                    cloud_watch_metrics_enabled=True,
                    metric_name="AssetGuardianApiWebAcl",
                ),
                rules=[
                    wafv2.CfnWebACL.RuleProperty(
                        name="AWS-AWSManagedRulesCommonRuleSet",
                        priority=1,
                        override_action=wafv2.CfnWebACL.OverrideActionProperty(none={}),
                        statement=wafv2.CfnWebACL.StatementProperty(
                            managed_rule_group_statement=wafv2.CfnWebACL.ManagedRuleGroupStatementProperty(
                                vendor_name="AWS", name="AWSManagedRulesCommonRuleSet"
                            )
                        ),
                        visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
                            sampled_requests_enabled=True,
                            cloud_watch_metrics_enabled=True,
                            metric_name="CommonRuleSet",
                        ),
                    ),
                    wafv2.CfnWebACL.RuleProperty(
                        name="AWS-AWSManagedRulesKnownBadInputsRuleSet",
                        priority=2,
                        override_action=wafv2.CfnWebACL.OverrideActionProperty(none={}),
                        statement=wafv2.CfnWebACL.StatementProperty(
                            managed_rule_group_statement=wafv2.CfnWebACL.ManagedRuleGroupStatementProperty(
                                vendor_name="AWS", name="AWSManagedRulesKnownBadInputsRuleSet"
                            )
                        ),
                        visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
                            sampled_requests_enabled=True,
                            cloud_watch_metrics_enabled=True,
                            metric_name="KnownBadInputs",
                        ),
                    ),
                    wafv2.CfnWebACL.RuleProperty(
                        name="RateLimitPerIp",
                        priority=3,
                        action=wafv2.CfnWebACL.RuleActionProperty(block={}),
                        statement=wafv2.CfnWebACL.StatementProperty(
                            rate_based_statement=wafv2.CfnWebACL.RateBasedStatementProperty(
                                limit=2000, aggregate_key_type="IP"
                            )
                        ),
                        visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
                            sampled_requests_enabled=True,
                            cloud_watch_metrics_enabled=True,
                            metric_name="RateLimitPerIp",
                        ),
                    ),
                ],
            )
            resolved_web_acl_arn = web_acl.attr_arn
        else:
            if not web_acl_arn:
                raise ValueError(
                    "web_acl_arn is required when deploying AssetGuardianStack outside "
                    "us-east-1 — deploy AssetGuardianWafStack to us-east-1 first and pass "
                    "its ApiWebAclArn output here."
                )
            resolved_web_acl_arn = web_acl_arn

        api_domain_name = f"{http_api.api_id}.execute-api.{region}.amazonaws.com"
        distribution = cloudfront.Distribution(
            self,
            "ApiDistribution",
            comment="AssetGuardian AI - WAF-protected edge in front of the SigV4-only HTTP API",
            web_acl_id=resolved_web_acl_arn,
            default_behavior=cloudfront.BehaviorOptions(
                origin=cloudfront_origins.HttpOrigin(
                    api_domain_name,
                    protocol_policy=cloudfront.OriginProtocolPolicy.HTTPS_ONLY,
                    # Added by CloudFront on the origin request only — a viewer
                    # cannot forge it, because ALL_VIEWER_EXCEPT_HOST_HEADER
                    # forwards viewer headers but CloudFront's own custom
                    # headers overwrite any same-named header from the viewer.
                    custom_headers={
                        "x-origin-verify": origin_verify_secret.secret_value.unsafe_unwrap()
                    },
                ),
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.HTTPS_ONLY,
                allowed_methods=cloudfront.AllowedMethods.ALLOW_ALL,
                cache_policy=cloudfront.CachePolicy.CACHING_DISABLED,
                # ALL_VIEWER would forward the viewer's Host header (the
                # CloudFront domain) to API Gateway, which rejects a Host it
                # doesn't own. EXCEPT_HOST_HEADER forwards everything else and
                # lets CloudFront set Host to the origin — so API Gateway sees
                # its own execute-api hostname.
                #
                # The portal therefore signs SigV4 against the *origin* host
                # rather than the CloudFront one (portal/index.html,
                # CONFIG.apiSigningHost): the browser cannot set Host itself, so
                # the value API Gateway recomputes the signature over is the one
                # CloudFront substitutes here. Signing the CloudFront host
                # instead yields a signature mismatch at the origin.
                origin_request_policy=cloudfront.OriginRequestPolicy.ALL_VIEWER_EXCEPT_HOST_HEADER,
            ),
        )

        # ------------------------------------------------------------------
        # Outputs consumed by scripts/deploy_agentcore.py and by operators
        # ------------------------------------------------------------------
        CfnOutput(self, "PortalUrl", value=f"https://{portal_distribution.distribution_domain_name}")
        CfnOutput(self, "KmsKeyArn", value=kms_key.key_arn)
        CfnOutput(self, "PhotosBucketName", value=photos_bucket.bucket_name)
        CfnOutput(self, "CmdbTableName", value=cmdb_table.table_name)
        CfnOutput(self, "HarnessFunctionName", value=harness_lambda.function_name)
        CfnOutput(self, "HarnessFunctionArn", value=harness_lambda.function_arn)
        CfnOutput(self, "HarnessRoleArn", value=lambda_role.role_arn)
        CfnOutput(self, "ApiEndpoint", value=http_api.api_endpoint)
        CfnOutput(self, "ApiId", value=http_api.api_id)
        CfnOutput(self, "IdentityPoolId", value=identity_pool.ref)
        CfnOutput(self, "CloudFrontDomainName", value=distribution.distribution_domain_name)
        CfnOutput(self, "UserPoolId", value=user_pool.user_pool_id)
        CfnOutput(self, "UserPoolClientId", value=user_pool_client.user_pool_client_id)
