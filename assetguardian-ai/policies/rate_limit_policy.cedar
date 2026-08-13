// Policy 3: rate_limit_policy
// Blocks requests once a principal has exceeded 1000 calls within the
// current context window. requestCountInWindow is populated by the
// gateway/authorizer before evaluation.
forbid (
    principal,
    action == AssetGuardian::Action::"InvokeTool",
    resource is AgentCore::Gateway
)
when {
    context.requestCountInWindow > 1000
};
