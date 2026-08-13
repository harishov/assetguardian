// Policy 2: deny_unauthenticated
// Blocks all unauthenticated access to gateway tools, regardless of any
// permit statement. Forbid always wins over permit in Cedar evaluation.
forbid (
    principal,
    action == AssetGuardian::Action::"InvokeTool",
    resource is AgentCore::Gateway
)
when {
    context.authenticated == false
};
