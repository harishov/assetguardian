// Policy 1: allow_authenticated_users
// Permits authenticated principals to invoke gateway tools.
permit (
    principal,
    action == AssetGuardian::Action::"InvokeTool",
    resource is AgentCore::Gateway
)
when {
    context.authenticated == true
};
