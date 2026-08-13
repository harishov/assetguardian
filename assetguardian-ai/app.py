#!/usr/bin/env python3
import os
import aws_cdk as cdk
from assetguardian.assetguardian_stack import AssetGuardianStack, AssetGuardianWafStack

app = cdk.App()

account = os.environ.get("CDK_DEFAULT_ACCOUNT")
region = os.environ.get("CDK_DEFAULT_REGION", "us-east-1")

env = cdk.Environment(account=account, region=region)

# Claude Sonnet 4 (the original "us." cross-region profile) is a Legacy model
# as of this rollout and gets access-restricted for accounts not actively
# using it. Claude Sonnet 4.5 on the "global." cross-region inference profile
# is the current, active replacement — used everywhere models are picked by
# region below. There's no "apac."-geography profile for 4.5 yet (only for
# the legacy 4-20250514 and Claude 3.x lineage), so "global." is what actually
# works today; confirmed live via a direct Converse call before adopting it.
DEFAULT_MODEL_ID = "global.anthropic.claude-sonnet-4-5-20250929-v1:0"
MODEL_ID_BY_REGION = {
    # Kept for reference / rollback — this is what us-east-1 was deployed
    # with originally. Legacy as of 2026-04-14; left here rather than
    # silently changed since that stack isn't being touched by this app run.
    "us-east-1": "us.anthropic.claude-sonnet-4-20250514-v1:0",
}
bedrock_model_id = MODEL_ID_BY_REGION.get(region, DEFAULT_MODEL_ID)

# CORS on the API and the photos bucket is pinned to the portal's own
# CloudFront domain (resolved inside the stack). Set PORTAL_EXTRA_ORIGINS to a
# comma-separated list of full origins — e.g.
# "https://assets.ncs.com.sg,http://localhost:8000" — to allow a custom domain
# or a local dev server in addition to it.
extra_cors_origins = [
    o.strip() for o in os.environ.get("PORTAL_EXTRA_ORIGINS", "").split(",") if o.strip()
]

if region == "us-east-1":
    AssetGuardianStack(
        app,
        "AssetGuardianAiStack",
        env=env,
        bedrock_model_id=bedrock_model_id,
        extra_cors_origins=extra_cors_origins,
        description="NCS AssetGuardian AI - Multi-Agent Enterprise Asset Lifecycle Platform (rebuilt from architecture report)",
    )
else:
    # CloudFront's WAFv2 Web ACL can only be created in us-east-1 regardless
    # of the main stack's region, so it's a separate stack pinned there.
    # WAF_WEB_ACL_ARN must be supplied (after that stack's first deploy) via
    # env var for every subsequent deploy of the regional stack.
    AssetGuardianWafStack(
        app,
        "AssetGuardianWafStack",
        env=cdk.Environment(account=account, region="us-east-1"),
        description="AssetGuardian AI - CloudFront WAFv2 Web ACL (must live in us-east-1; fronts the regional API distribution)",
    )

    # CDK's default physical names for CloudFront resources (Distributions,
    # Origin Access Controls) are derived from {stack name}+{logical id} and
    # CloudFront is a *global* service — those names collide account-wide,
    # not just within a region. Reusing "AssetGuardianAiStack" as the
    # CloudFormation stack name here collided with the identically-named
    # stack already running in us-east-1 (confirmed live: OriginAccessControl
    # creation failed with AlreadyExists against the us-east-1 stack's
    # resource). Each region therefore needs its own distinct stack name.
    stack_id = f"AssetGuardianAiStack-{region}"
    web_acl_arn = os.environ.get("WAF_WEB_ACL_ARN")
    AssetGuardianStack(
        app,
        stack_id,
        env=env,
        bedrock_model_id=bedrock_model_id,
        web_acl_arn=web_acl_arn,
        extra_cors_origins=extra_cors_origins,
        description=f"NCS AssetGuardian AI - Multi-Agent Enterprise Asset Lifecycle Platform ({region})",
    )

app.synth()
