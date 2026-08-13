# AssetGuardian AI — Rebuild in New AWS Account

This is a reverse-engineered rebuild of the **NCS AssetGuardian AI** platform described in
`Hackathon Project Report.docx`. The original source code lived in a CodeCommit repository in a
different AWS account (975050365706, us-east-1) that these credentials cannot reach, so this
rebuild reconstructs the architecture from the report's **Infrastructure Services** table,
**Experiments 1–12**, and **Solution Justification** sections only — no original code was copied.

Deployed to account **418748712186**, region **`ap-southeast-1` (Singapore) — the sole live
deployment.**

> **Current state (2026-08-05).** Beyond the original rollout, seven gaps have since been closed —
> CORS pinned to the portal origin, portal traffic routed through CloudFront so WAF actually
> inspects it, the API Gateway origin sealed against direct callers, the AgentCore Gateway target
> wired so the Cedar policies govern real traffic, employee ID accepting a staff ID or corporate
> email, plain-English error messages with a reference code, and password-reset self-service. See
> **"Hardening round"** below for what changed and how each was verified. Three items remain open,
> and all three are documented under **"Known gaps"** — none is a code defect.

The platform originally launched in `us-east-1`, then was migrated to Singapore per an explicit
decision that this is an internal-organization tool and workloads/models should be distributed
regionally accordingly. **us-east-1 has since been fully decommissioned** (2026-08-01): the
`AssetGuardianAiStack` CloudFormation stack, all 3 S3 buckets, the DynamoDB table, the Cognito
User Pool + Identity Pool, both CloudWatch log groups, and all AgentCore resources (Harness,
Gateway, Policy Engine + Cedar policies) were deleted and verified gone via direct API checks. The
KMS key was scheduled for deletion (7-day pending window, since KMS keys can't be deleted
immediately). The us-east-1 WAFv2 Web ACL and CloudFront distributions were also destroyed — the
only WAFv2 Web ACL now present in `us-east-1` (`ApiWebAcl-JIVUrL0F6lHx`) belongs to the Singapore
deployment, which uses it via a small `AssetGuardianWafStack` pinned there (CloudFront's WAFv2 Web
ACL can only be created in `us-east-1` regardless of which region the distribution's origin lives
in — a hard AWS constraint).

## Singapore (`ap-southeast-1`) deployment — current primary

Deployed as a separate, region-suffixed CloudFormation stack (`AssetGuardianAiStack-ap-southeast-1`)
plus a small `AssetGuardianWafStack` pinned to `us-east-1` (CloudFront's WAFv2 Web ACL can only be
created there regardless of which region the distribution's origin lives in — a hard AWS
constraint, not a design choice). The two regions do not share any other resources.

| Service | Resource |
|---|---|
| **Portal** | **`https://dn40ox0gzb4b1.cloudfront.net`** |
| CloudFront (API edge) | `https://d2xsszrekq4050.cloudfront.net` — **what the portal actually calls**; WAF is attached here |
| API Gateway (origin) | `https://rwgheig8j7.execute-api.ap-southeast-1.amazonaws.com` — sealed, rejects anything not arriving via CloudFront |
| Cognito User Pool | `ap-southeast-1_KDojmmieE` |
| Cognito User Pool Client | `k4gl5k5fb6mb597rcqom5g709` |
| Cognito Identity Pool | `ap-southeast-1:49b648af-ece5-495c-b4e6-19e3b78410cb` |
| S3 (photos) | `assetguardian-asset-photos-418748712186-ap-southeast-1` |
| S3 (portal) | `assetguardian-portal-418748712186-ap-southeast-1` |
| Bedrock model | `global.anthropic.claude-sonnet-4-5-20250929-v1:0` (Claude Sonnet 4.5, global cross-region inference profile) — the original `us.anthropic.claude-sonnet-4-20250514-v1:0` (Claude Sonnet 4) is now provider-flagged **Legacy** and was replaced account-wide going forward; confirmed working via a direct `Converse` call before adopting it |
| AgentCore Memory | `assetguardian_enterprise_memory-hSMythGi1Z` (4-strategy: SEMANTIC, SUMMARIZATION, USER_PREFERENCE, EPISODIC) — succeeded here after fixing the episodic strategy's `reflectionConfiguration.namespaces` (see below); the harness was explicitly re-pointed at this memory via `UpdateHarness` after initially auto-provisioning its own managed memory |
| AgentCore Policy Engine | `assetguardian_policy_engine-767vqraeto` — 3 Cedar policies, ACTIVE, `ENFORCE` mode on the gateway |
| AgentCore Gateway | `assetguardian-enterprise-gateway-kxh7qe0ilc` — `READY`, AWS_IAM authorizer |
| AgentCore Gateway Target | `ODHA9DHQEN` — `READY`; harness Lambda exposed as 4 MCP tools (see "MCP entry point" below) |
| AgentCore Harness | `assetguardian_harness-MF7yRoCxzf` |
| AgentCore Evaluators | 3 custom LLM-as-a-Judge evaluators, all `ACTIVE` |
| DynamoDB CMDB | `assetguardian-cmdb` — seeded with 294 test records (see "Test data" below) |
| Origin-verify secret | Secrets Manager, 48 chars — proves a request came via CloudFront |
| CloudWatch | `/assetguardian/harness-invoke`, `/assetguardian/policy-audit` |
| Admin account | `admin@assetguardian.local` (separate Cognito user from the us-east-1 account; password shared with you separately) |

## Hardening round (2026-08-03 → 2026-08-05)

Seven gaps closed after the Singapore rollout. Every item below was verified against the live
deployment with API calls, not assumed from a successful `cdk deploy`.

### 1. CORS pinned to the portal origin

Both the HTTP API and the photos bucket carried `allow_origins=["*"]` with a "tighten before
go-live" note. Both are now pinned to the portal's CloudFront domain, referenced as
`portal_distribution.distribution_domain_name` rather than hardcoded so it survives a distribution
replacement. That required constructing the portal block *before* the API in
`assetguardian_stack.py`, and moving the bucket's CORS to `add_cors_rule()`.

Set `PORTAL_EXTRA_ORIGINS` (comma-separated, full origins with scheme) to allow a custom domain or
a local dev server alongside it.

`allowed_headers=["*"]` is retained **deliberately** on the bucket: a browser SigV4 PUT sends a
variable set of `x-amz-*` headers and pinning that list breaks uploads the moment one changes. The
origin is the control that matters.

Verified: a preflight from the portal origin returns the `Access-Control-Allow-*` headers; one from
`https://evil.example.com` returns a 204 with none.

### 2. Portal traffic now goes through CloudFront, so WAF actually sees it

The portal was calling `execute-api` directly, bypassing the CloudFront distribution and therefore
the WAFv2 Web ACL entirely — the WAF was provisioned, billed, and inspecting nothing.

Repointing `apiBase` alone would have broken every call, because SigV4 signs the `host` header and
the browser sets it from the URL. Two coordinated changes were needed:

- The distribution moved from `ALL_VIEWER` to **`ALL_VIEWER_EXCEPT_HOST_HEADER`**. `ALL_VIEWER`
  forwards the viewer's Host (the CloudFront domain), which API Gateway rejects as a host it does
  not own; the except-host policy lets CloudFront substitute the origin's hostname.
- `sigv4Headers()` in the portal gained a **`signingHost` override** (`CONFIG.apiSigningHost`), so
  the browser signs for the host the *origin* will see. The browser cannot set `Host` itself — it
  is a forbidden header — which is precisely what makes this work.

Verified: signed-for-origin via CloudFront reaches the Lambda; signed-for-CloudFront returns
**403**, confirming the override is load-bearing rather than incidental.

### 3. Origin sealed against direct callers

`execute-api` stays publicly resolvable — HTTP APIs (v2) support neither resource policies nor
CloudFront Origin Access Control, so API Gateway itself cannot refuse non-CloudFront callers.
Instead CloudFront injects a shared secret header (`x-origin-verify`, generated into Secrets
Manager) and the handler rejects anything without it, using `hmac.compare_digest`.

This is **additive**: the route keeps its AWS_IAM authorizer, so a caller needs valid SigV4
credentials *and* the header. A route can only carry one authorizer, which is why the check lives
in the handler rather than in a Lambda authorizer — swapping authorizers would have traded SigV4
validation away.

Verified from four angles:

| Path | Result |
|---|---|
| via CloudFront | reaches the Lambda |
| direct to origin | **403** |
| direct + forged header | **403** |
| via CloudFront + forged header | reaches the Lambda — CloudFront **overwrites** the viewer's value |

That last row is the security property the design rests on: a same-named custom header always wins
over anything the viewer sends.

**Caveat:** CloudFront stores origin custom headers in plaintext, so anyone with
`cloudfront:GetDistribution` in this account can read the secret. That is inherent to the pattern,
not a defect in it. Rotation is the mitigation — and **rotating requires a redeploy**, because the
Lambda reads the secret from Secrets Manager while CloudFront holds a literal copy. Rotate, then
`cdk deploy`, and the two are back in step.

### 4. AgentCore Gateway target wired — the Cedar policies now govern real traffic

Previously the Gateway and Policy Engine were provisioned and ACTIVE but no target was registered,
so the three Cedar policies evaluated nothing. `step_gateway_target()` in
`scripts/deploy_agentcore.py` now registers the harness Lambda as **4 MCP tools**:
`inspect_device`, `employee_handover`, `asset_return`, `annual_self_declaration`.

Two API details that had blocked this, now pinned down:

- **`credentialProviderConfigurations` is required** for a Lambda target despite not being marked
  required in the API model. Omitting it fails with `Credential provider configurations is not
  defined`. `GATEWAY_IAM_ROLE` means the gateway invokes the Lambda as itself.
- The Gateway prefixes tool names as **`target___tool`**, so the handler splits on `___`.

The Gateway invokes the Lambda **directly**, not through API Gateway — no `routeKey`, no CloudFront
header. Registering the target alone would have made every MCP call 403 then 404. `handler.py`
therefore dispatches on invocation style: `_gateway_tool_name()` reads the tool name from the
client context, and gateway calls return the bare result with no HTTP envelope and skip the origin
check (they are already authorized by the gateway's IAM role plus the Cedar engine in `ENFORCE`).

### 5. Employee ID accepts a staff ID or a corporate email

`identity_verification` matched `employee_id` against `assignedUser` only, so an email would have
failed ownership. It now matches `assignedUser` **or** `assignedUserEmail` — two aliases for the
same principal.

The domain is enforced twice: the portal highlights an off-domain address in the Employee ID field
and blocks submit, and `sanitize_employee_id` rejects it server-side for callers bypassing the
page. The check uses `endswith`, so `attacker@ncs.com.sg.evil.com` is rejected — a `contains` check
would have let it through.

### 6. Plain-English errors, full detail in Event Logs

Responses previously returned raw internals (`{"error":"pipeline_halted","detail":{…}}`) and the
portal rendered `JSON.stringify(data.detail)` straight into the page.

Every error now returns exactly three fields — `error` (code), `message` (plain English), and
`reference` (the Lambda request ID). The complete technical record goes to CloudWatch under that
same reference; an admin pastes it into the portal's Event Logs tab to retrieve it.

| Situation | What the user sees |
|---|---|
| Device doesn't match CMDB | We couldn't match this device to your records. Check the asset ID and serial number, and make sure the device is assigned to you. |
| Photos too poor to assess | The photos weren't clear enough to assess. Please retake them in good light, with the whole device in frame. |
| Non-corporate email | Please use your @ncs.com.sg work email address, or your employee ID instead. |
| Bad asset ID characters | Asset IDs and serial numbers can only contain letters, numbers, hyphens and underscores. |
| Unexpected failure | Something went wrong on our side… quote the reference below to your IT team. |

Pipeline halts read as guidance rather than failure, because a halt is a *correct* business
outcome. Validation errors keep an actionable hint — only the internal field names were removed.
`SuspiciousInputError` carries both messages: the exception text for logs, `user_message` for the
screen. Browser-side errors were softened too, with technical text still going to the console.

`escapeHtml()` was added to the portal, since these server-supplied strings reach `innerHTML`.

### 7. Forgotten-password self-service

Cognito `ForgotPassword` / `ConfirmForgotPassword` wired into the portal, so a locked-out user no
longer needs an admin running `AdminSetUserPassword`. It shows identical wording whether or not the
address exists — Cognito's `UserNotFound` would otherwise let anyone enumerate valid accounts.

### Bugs found and fixed during this round

1. **`deploy_agentcore.py` blanked `AGENTCORE_MEMORY_ID` on re-run.** `CreateMemory` raises
   `ValidationException` (not `ConflictException`) on a duplicate name, so the existing-resource
   recovery never ran; `patch_lambda_env` then wrote `results.get(key, "")` unconditionally,
   overwriting a working ID with empty. Because `agents/memory.py` fails soft, memory recording
   stopped **silently**. Both halves fixed: the duplicate-name path now recovers the existing IDs,
   and `patch_lambda_env` only writes values it actually resolved, logging when it preserves one.
   The script is safe to re-run now; it was not before this fix.
2. **`list_gateways` omits the gateway ARN and URL**, so the reuse path left them blank — and the
   Cedar policies are scoped to that ARN. Now fetched via `get_gateway`.
3. **`scripts/seed_cmdb.py` pointed at decommissioned `us-east-1`.** Now defaults to
   `ap-southeast-1`, overridable via `CMDB_REGION`.
4. **`showAuthPanel()` had a hardcoded three-panel list**, so the new password-reset panels would
   never have hidden.
5. **`MK893ZP/A` was unreachable.** The slash is outside the character set `sanitize_asset_id`
   accepts, so the record could exist in the CMDB but never be inspected. Stored normalised as
   `MK893ZP-A`, with `printed as MK893ZP/A` kept in `sourceNote` so the physical label still traces
   back.

## Test data

`scripts/seed_test_assets.py` builds a CMDB from a real two-column store-room inventory
(`EquipmentSerialNo.` + free-text `STATUS`) — 294 unique records from 326 rows. It derives the
seven attributes the CMDB needs that the source doesn't carry: device model from vendor hints in
the status text (falling back to serial-format inference, flagged `modelInferred`), status mapped
onto an enum, and assignedUser / email / age / warranty / repairs / role hashed from the serial so
re-runs are byte-identical.

Six serials are overridden so every branch of `lifecycle_decision.py` is reachable. The damage
score comes from the photo, so the record controls only half of each rule — the last column says
what to pair it with:

| Rule | Asset | Serial | Employee ID | Pair with |
|---|---|---|---|---|
| `rule_2_severe_and_aged` | ASSET-0001 | 5CG01523C7 | EMP1004 / arjun.teo@ncs.com.sg | Severe photo |
| `rule_3_excessive_repairs` | ASSET-0050 | PC0MLJ58 | EMP1054 / kelvin.ho@ncs.com.sg | any photo |
| `rule_4a_repair` | ASSET-0174 | 6355NW2 | EMP1219 / arjun.yap@ncs.com.sg | Moderate photo |
| `rule_4b_repair_uneconomical` | ASSET-0046 | 5CD0128PHV | EMP1365 / daniel.yap@ncs.com.sg | Severe photo |
| `rule_5_minor_continue` | ASSET-0178 | NXGGMSG001704052807600 | EMP1295 / kelvin.sim2@ncs.com.sg | Minor photo |
| `rule_6_age_based_refresh` | ASSET-0183 | DMPVG7S3HLF9 | EMP1043 / farid.yeo@ncs.com.sg | pristine photo |

`rule_1_critical_damage` needs a damage score > 75 and ignores every CMDB attribute, so any record
reaches it.

```bash
python scripts/seed_test_assets.py                      # dry run + data-quality report
python scripts/seed_test_assets.py --csv assets.csv     # review the derived records
python scripts/seed_test_assets.py --directory emp.csv  # 199 employees holding assets
python scripts/seed_test_assets.py --apply              # write to DynamoDB
```

**The source inventory has real data-quality faults**, and the script reports rather than silently
normalising them — these are exactly the CMDB-quality risk called out at the bottom of this README:

- **31 duplicate serials** (mostly the `0F3…24…BF` block). `serialNumber` backs the lookup GSI with
  `Limit=1`, so a duplicate means the query returns an arbitrary one of the two.
- **Two confusable pairs where both variants are present**: `PC0MLG8G` ↔ `PCOMLG8G` (letter O for
  zero) and `PC0MLF8C` ↔ `PC0M1F8C` (1 for L). Rekognition OCR will produce these inconsistently
  against a physical label. Deciding which is correct needs someone with the devices.
- One 15-character `0F33H88J24023BF` among 44 otherwise-14-character siblings, likely a typo.

## MCP entry point

The harness Lambda is reachable two ways, and `handler.py` dispatches on which:

| | HTTP | MCP |
|---|---|---|
| Caller | Portal via CloudFront | AgentCore Gateway |
| Auth | SigV4 (AWS_IAM) + `x-origin-verify` | Gateway IAM role + Cedar `ENFORCE` |
| Event | API Gateway v2 payload | Tool args; tool name in client context |
| Returns | `{statusCode, headers, body}` | Bare result object |

**Preview-API quirks hit during this rollout** (the AgentCore control API changed shape/behavior
somewhat between the us-east-1 and Singapore rollouts — all fixed in `scripts/deploy_agentcore.py`,
which now supports both regions via `AGENTCORE_REGION`/`AGENTCORE_STACK_NAME`/`AGENTCORE_MODEL_ID`
env vars):
- `CreateMemory` now requires the episodic strategy's `reflectionConfiguration.namespaces` to be
  explicitly set as a prefix of the episodic namespace — the default reflection namespace no
  longer satisfies this on its own. Fixed by adding `reflectionConfiguration.namespaces:
  ["actors/{actorId}"]` alongside the episodic namespace `["actors/{actorId}/episodes"]`.
- `CreateHarness`'s response shape changed from flat (`resp["harnessId"]`) to nested
  (`resp["harness"]["harnessId"]`).
- `CreateEvaluator` rejects the new model's bare on-demand ID the same way it rejected the old one
  — needs the cross-region inference profile ID, same as the harness model.
- **Still unresolved:** `CreateOnlineEvaluationConfig` rejects the execution role with "does not
  have permissions to access the specified log groups" despite both an IAM policy on the role and
  a CloudWatch Logs resource policy explicitly granting `bedrock-agentcore.amazonaws.com` read
  access to the log group. Same class of gap as the Memory/Evaluator issues hit in the original
  us-east-1 rollout — a preview-API rough edge, not a configuration error we could find. The
  Evaluators themselves are live and usable directly; only the automatic 100%-sampling online
  wiring is affected.

**End-to-end verification performed** (per the standing "double check, don't just trust the
console" instruction — all done via live API calls, not assumed): Cognito login as the Singapore
admin → confirmed `cognito:groups: ["Admins"]` in the ID token → Identity Pool credential exchange
→ confirmed the assumed role is `PortalAdminRole` (not just `PortalAuthRole`) → S3 photo upload
under those signed credentials → SigV4-signed `POST /inspect` call against the live API → Lambda →
DynamoDB CMDB lookup → Rekognition → **live Bedrock Claude Sonnet 4.5 vision scoring call, whose
real (non-zero, non-error) output was returned in the response** → correctly halted with a
structured 422 because the synthetic test photo wasn't a real device photo (a legitimate
business-logic gate, not an infra failure) → admin CloudWatch Logs read via SigV4-signed
`FilterLogEvents`, confirming the invocation's log line. CORS preflight on the new API endpoint
also reconfirmed fixed. All test data (CMDB record, uploaded photo) was cleaned up after
verification.

## Historical: original us-east-1 deployment (now decommissioned)

Everything in this section and the two below it (**"What's live right now"** through **"Bugs
found and fixed after initial deploy"**) describes the **original us-east-1 deployment**, which
has since been fully torn down (see top of README). Kept here as a historical record of what was
built and debugged along the way — none of the resource IDs below still exist. For the current
live system, see the **Singapore deployment** section above.

The Bedrock "Anthropic model use case" form blocker described below was specific to us-east-1 and
was superseded by the region migration before it was ever fully resolved there.

Verified via `aws cloudformation describe-stacks` and direct resource checks after the original
us-east-1 deployment (historical):

| Service | Resource | Notes |
|---|---|---|
| **Portal** | **`https://d2mbc1mlfcw9rk.cloudfront.net`** | **Static web UI — see "Web Portal" section below** |
| S3 | `assetguardian-asset-photos-418748712186` | KMS-encrypted, versioned, private (no public PUT) |
| S3 | `assetguardian-portal-418748712186` | Portal static assets, private, served only via CloudFront OAC |
| DynamoDB | `assetguardian-cmdb` | 2 GSIs (SerialNumberIndex, AssignedUserIndex), PITR, deletion protection, KMS-encrypted, seeded with 3 sample records |
| Lambda | `assetguardian-harness-invoke` | Python 3.12, 512MB, 300s timeout, implements all 7 agents from the report |
| API Gateway | `https://376s8azv87.execute-api.us-east-1.amazonaws.com` | HTTP API, routes: `/inspect` `/handover` `/return` `/declare`, **AWS_IAM (SigV4) authorization required** |
| CloudFront | `d2sizdk7psq0yv.cloudfront.net` | Sits in front of the API with WAFv2 attached |
| CloudFront | `d2mbc1mlfcw9rk.cloudfront.net` | Sits in front of the portal S3 bucket (Origin Access Control, no WAF needed — static assets only) |
| WAFv2 | `AssetGuardianApiWebAcl` (CLOUDFRONT scope) | AWS Managed Common Rule Set + Known Bad Inputs + 2000 req/5min rate limit per IP |
| Cognito Identity Pool | `us-east-1:1dcf225e-22eb-4763-9d7a-48d2867318e3` | Unauthenticated role (legacy, minimal S3 scope) + **authenticated role** tied to the User Pool below |
| **Cognito User Pool** | `us-east-1_lZjNBe0Qx` | Real login. Self-service sign-up gated to **@ncs.com.sg** by a Pre-SignUp Lambda trigger; admin accounts created out-of-band via `AdminCreateUser` bypass that gate |
| Cognito User Pool Client | `16qecg3cclvdk48cj6qpuot8pm` | Public browser client (no secret), `USER_PASSWORD_AUTH` + `USER_SRP_AUTH`, 1h token validity, 30d refresh |
| KMS | Customer-managed key | Encrypts S3, DynamoDB, CloudWatch Logs |
| Bedrock | Claude Sonnet 4 (`us.anthropic.claude-sonnet-4-20250514-v1:0`) | Vision analysis for all agents |
| **Bedrock AgentCore Policy Engine** | `assetguardian_policy_engine-mru_4gvch5` | 3 Cedar policies, **ACTIVE enforcement mode**, matches the report exactly |
| **Bedrock AgentCore Gateway** | `assetguardian-enterprise-gateway-ojercetxjl` | MCP protocol, AWS_IAM authorizer, policy engine attached |
| **Bedrock AgentCore Harness** | `assetguardian_harness-YjKUJodC1G` | Declarative agent runtime: Claude Sonnet 4 + system prompt |
| CloudWatch | `/assetguardian/harness-invoke`, `/assetguardian/policy-audit` | 1yr / 6yr retention, KMS-encrypted |

The Lambda has been smoke-tested (unknown-route 404, missing-field validation, prompt-injection
rejection all confirmed working end-to-end against the live function). The full browser auth chain
(Cognito login → ID token → Identity Pool credential exchange → SigV4-signed API call) has also
been verified end-to-end (see "Web Portal" below).

## Web Portal

**Live at: https://d2mbc1mlfcw9rk.cloudfront.net**

A single-page static site (`portal/index.html`), deployed via S3 + CloudFront (Origin Access
Control — the bucket itself is fully private). No build step, no external JS dependencies —
Cognito calls and AWS SigV4 request signing are both hand-rolled in vanilla JS using the Web
Crypto API, to avoid pulling in a third-party CDN library for a page that signs AWS credentials.

**Auth model — two tiers, one login form:**
- **NCS employees** self-register with an `@ncs.com.sg` email address via "Create NCS Account" on
  the hero page. A Pre-SignUp Lambda trigger (`assetguardian-presignup-domain-check`) rejects any
  other domain at signup time — verified live (a `someone@gmail.com` signup attempt is rejected
  with `UserLambdaValidationException`). New accounts require email verification (6-digit code)
  before first login.
- **Admins** are provisioned out-of-band by IT via `scripts/create_admin_user.py`, which uses
  `AdminCreateUser` + `AdminSetUserPassword(Permanent=True)` — this bypasses the Pre-SignUp domain
  check entirely (that trigger only fires on the public `SignUp` API) and skips the forced
  first-login password reset. One admin account already exists:
  `admin@assetguardian.local` (password shared with you separately — rotate it after first login).
- Admin status is **real IAM-enforced authorization, not a client-side heuristic**: membership in
  the Cognito User Pool Group `Admins` puts `"Admins"` in the ID token's `cognito:groups` claim,
  which an Identity Pool role-mapping rule matches to upgrade the federated session from
  `PortalAuthRole` to `PortalAdminRole` — a separate IAM role that additionally grants
  `logs:FilterLogEvents`/`GetLogEvents`/`DescribeLogStreams` scoped to exactly the two AssetGuardian
  log groups. Verified live: an admin session assumes `PortalAdminRole` and can read both log
  groups; a plain `@ncs.com.sg` test account assumes `PortalAuthRole` and gets `AccessDeniedException`
  on the same log calls. `scripts/create_admin_user.py` adds new admins to this group automatically.

**Credential flow:** browser → `InitiateAuth` (Cognito User Pool, `USER_PASSWORD_AUTH`) → ID token
→ `GetId` + `GetCredentialsForIdentity` against the Identity Pool with a `Logins` map referencing
the User Pool → short-lived AWS credentials scoped to `PortalAuthRole` or, for group members,
`PortalAdminRole` → SigV4-signed S3 upload (photos), SigV4-signed inspection call **through the
CloudFront API edge** (signed against the origin host — see "Hardening round" §2), and — admins
only — SigV4-signed CloudWatch Logs `FilterLogEvents` calls straight from the browser (no separate
backend query endpoint). Tokens are cached in `sessionStorage` (cleared when the tab closes) and
silently refreshed via `REFRESH_TOKEN_AUTH` when within 4 minutes of expiry.

Only the inspection call crosses WAF and the IAM authorizer; login, photo upload and admin log
reads are signed in the page and hit those AWS services directly, with IAM as the sole gate.

**Event Logs (admin-only):** a second tab in the portal, visible only when `cognito:groups`
contains `Admins`. Lets an admin pick a log group (`harness-invoke` or `policy-audit`) and a time
window (1h/24h/7d) and see recent events — reads CloudWatch Logs directly, no new Lambda/API
endpoint was added for this.

To create additional admin accounts (adds to the `Admins` group automatically):
```bash
python3 scripts/create_admin_user.py <email> <password>   # reads UserPoolId from the stack outputs
```

### Bugs found and fixed after initial deploy

1. **"Failed to fetch" on every inspection submit.** Root cause: the API Gateway CORS config
   mixed a bare `"*"` with named headers in `allow_headers`. Verified live via `curl -X OPTIONS`
   that this combination made API Gateway's automatic CORS preflight response come back with
   **no** `Access-Control-Allow-*` headers at all, which browsers hard-fail as "Failed to fetch"
   before the request is even sent. Fixed by using an explicit header list (`Authorization`,
   `Content-Type`, `X-Amz-Date`, `X-Amz-Security-Token`, `X-Amz-Content-Sha256`) instead of a
   wildcard. Confirmed fixed: the OPTIONS response now returns all four `Access-Control-Allow-*`
   headers.
2. **Bedrock `AccessDeniedException` on every inspection.** The Lambda's IAM policy scoped
   `bedrock:Converse`/`InvokeModel` to the `us-east-1` foundation-model ARN only. The `us.`
   cross-region inference profile actually dispatches requests to whichever US region has
   capacity — observed live, a call against the `us-east-1` profile executed against the
   `us-east-2` foundation-model resource and was denied. Fixed by widening the foundation-model
   resource ARN to a region wildcard (the inference-profile resource itself, which is what's
   actually metered/billed, stays scoped to this account and region). Confirmed fixed: the
   `AccessDeniedException` is gone.

## Known gaps (honest accounting)

A few pieces of the report's architecture could not be provisioned in this pass, either because
the AgentCore preview API rejected every configuration tried, or because they were out of scope
for a backend rebuild:

1. ~~**AgentCore Memory (4-strategy persistent memory)**~~ — **resolved during the Singapore
   rollout** by setting `reflectionConfiguration.namespaces` explicitly (see "Preview-API quirks").
   Live as `assetguardian_enterprise_memory-hSMythGi1Z`. Note that `agents/memory.py` fails soft:
   if `AGENTCORE_MEMORY_ID` is unset, memory read/writes are skipped **silently** rather than
   breaking the pipeline — which is why the `patch_lambda_env` bug in the hardening round was
   invisible until the env var was inspected directly.
2. **AgentCore Evaluators + Online Evaluation Config (LLM-as-a-Judge, 100% sampling)** —
   `CreateEvaluator` rejects Claude Sonnet 4 for on-demand invocation ("Invocation of model ID ...
   with on-demand throughput isn't supported") both as a bare model ID and as the standard
   cross-region inference-profile ARN. This likely needs a customer-created **Application
   Inference Profile** (`AWS::Bedrock::ApplicationInferenceProfile`) rather than the system
   cross-region profile — not yet created. `agents/evaluators.py` in the Lambda still runs its own
   lightweight LLM-as-a-Judge evaluation via direct Bedrock Converse calls and logs scores to
   `/assetguardian/policy-audit`, so quality monitoring exists, just not through the native
   AgentCore Evaluator/Online-Eval resource types.
3. ~~**Gateway Target (Lambda wired in as an MCP tool)**~~ — **done**, see "Hardening round" §4.
   The harness Lambda is registered as 4 MCP tools and the Cedar policies now evaluate real gateway
   traffic. The two things that had made it "not reliably scriptable" were
   `credentialProviderConfigurations` being required despite not being marked so in the API model,
   and the `target___tool` name prefix.
4. ~~**Frontend portal**~~ — **built**, and password-reset self-service is now wired in too
   (§7). No known portal gaps outstanding.
5. **AgentCore Online Evaluation Config (Singapore only)** — see "Preview-API quirks" under the
   Singapore section above; `CreateOnlineEvaluationConfig` rejects the execution role's log-group
   access despite both IAM and CloudWatch Logs resource-policy grants.
6. ~~**us-east-1 decommissioning**~~ — **done.** See the top of this README for what was torn down
   and verified. The KMS key (`7091b452-4a18-4be6-9799-8f24b48ba3f3`) is in `PendingDeletion`
   until ~2026-08-08; cancel via `aws kms cancel-key-deletion` before then if it turns out to be
   needed again.
7. Everything under **"Future Work / What the POC did not look at"** in the original report
   (ServiceNow/Intune/HR integration, 200K-scale load testing, offline mode, multi-tenancy,
   advanced forensic fraud detection, compliance certification, human-in-the-loop UI, continuous
   learning loops) remains out of scope here too, same as it was in the original POC.

## Security hardening applied beyond the original report

The report's own "Security Hardening" section flagged several gaps in the original POC. This
rebuild closes them by default:

- S3 bucket blocks all public access; browser uploads go through a Cognito unauthenticated role
  scoped to a single prefix, not an open public PUT.
- API Gateway requires AWS_IAM (SigV4) authorization on every route — not open to the internet.
- CloudFront + WAFv2 (managed rule sets + rate limiting) sit in front of the API, **and the portal
  actually routes through them** — see "Hardening round" §2, which is what made the WAF load-bearing
  rather than decorative.
- The API Gateway origin is **sealed**: a shared secret header injected by CloudFront is required
  in addition to SigV4, so the WAF cannot be bypassed by calling `execute-api` directly (§3).
- CORS on the API and the photos bucket is pinned to the portal's origin, not `*` (§1).
- KMS customer-managed key encrypts S3, DynamoDB, and CloudWatch Logs.
- All Lambda-side user input (`assetId`, `employeeId`, S3 keys, free-text fields) is sanitized in
  `agents/sanitize.py` — length-capped, control-character-stripped, and checked against a
  prompt-injection phrase list — before it ever reaches a prompt or a log line.
- Error responses carry no internal detail: a plain-English message plus a reference code, with the
  full record in CloudWatch under that reference (§6).
- Least-privilege IAM throughout (scoped Bedrock model ARN, scoped S3/DynamoDB grants).

## Repository layout

```
assetguardian-ai/
  app.py                          CDK entrypoint
  assetguardian/assetguardian_stack.py   Core infra stack (S3, DynamoDB, Lambda, API GW, CloudFront, WAF, KMS, Cognito)
  lambda/harness_invoke/
    handler.py                    Lambda entrypoint — dual-mode (HTTP + MCP), origin check,
                                  sanitizes, dispatches to orchestrator, plain-English errors
    orchestrator.py                Experiment 7: 4 workflows (handover/return/declare/inspect), fail-fast identity check
    agents/
      bedrock_client.py            Shared Claude Sonnet 4 Converse helper
      vision_quality_gate.py       Experiment 4
      identity_verification.py     Experiment 5 (Rekognition OCR + DynamoDB CMDB)
      damage_detection.py          Experiment 1
      display_health.py            Experiment 2
      fraud_detection.py           Experiment 3 (EXIF parsing, freshness, liveness, dup-hash, GPS, AI forensics)
      exif_utils.py                Dependency-free JPEG EXIF reader
      lifecycle_decision.py        Experiment 6 (deterministic 6-rule business engine)
      memory.py                    Experiment 8 (AgentCore Memory client, fails soft)
      evaluators.py                Experiment 10 (LLM-as-a-Judge, direct Bedrock calls)
      sanitize.py                  Prompt-injection / input-validation defense + corporate
                                   email-domain enforcement; carries user_message for the UI
  policies/*.cedar                 The 3 governance policies, matching the report's names exactly
  portal/
    index.html                    Single-page web portal — hero, login/signup, password reset,
                                  inspection form + results, admin event logs (see "Web Portal")
  scripts/
    deploy_agentcore.py            Provisions AgentCore Memory/PolicyEngine/Gateway/GatewayTarget/
                                   Harness/Evaluators (idempotent, safe to re-run)
    seed_test_assets.py            Builds a 294-record CMDB from a real store-room inventory,
                                   with deliberate lifecycle-rule coverage (see "Test data")
    seed_cmdb.py                   Seeds 3 minimal smoke-test records
    create_admin_user.py           Provisions/updates an admin account (AdminCreateUser + permanent password)
    agentcore_outputs_ap-southeast-1.json  Resource IDs/ARNs from the last deploy_agentcore.py run
```

The portal's brand mark is an inline SVG sprite (`#brandMark` for display sizes, `#brandMarkSm`
below ~40px where the interior mesh would merge into a blob), referenced with `<use>` from the
hero, auth card and app top bar. It is defined once rather than duplicated, and the favicon is an
inline `data:` URI — the portal ships no external assets by design.

## Setup / redeploy instructions

```bash
cd assetguardian-ai
pip install -r requirements.txt
npm install -g aws-cdk   # if not already installed

export AWS_ACCESS_KEY_ID=...
export AWS_SECRET_ACCESS_KEY=...
export AWS_SESSION_TOKEN=...       # if using temporary credentials
export CDK_DEFAULT_ACCOUNT=418748712186
export AWS_DEFAULT_REGION=ap-southeast-1       # CDK's region resolution follows this, not a
export CDK_DEFAULT_REGION=ap-southeast-1       # manually-exported CDK_DEFAULT_REGION alone

# WAFv2 Web ACL for CloudFront can only be created in us-east-1 (hard AWS constraint), so it's a
# separate stack pinned there regardless of the main stack's region:
cdk bootstrap aws://418748712186/us-east-1
cdk bootstrap aws://418748712186/ap-southeast-1
cdk deploy AssetGuardianWafStack --app "python3 app.py"
export WAF_WEB_ACL_ARN=<ApiWebAclArn output from the stack above>
cdk deploy AssetGuardianAiStack-ap-southeast-1 --app "python3 app.py"

AGENTCORE_REGION=ap-southeast-1 \
AGENTCORE_STACK_NAME=AssetGuardianAiStack-ap-southeast-1 \
AGENTCORE_MODEL_ID=global.anthropic.claude-sonnet-4-5-20250929-v1:0 \
AGENTCORE_EVALUATOR_MODEL_ID=global.anthropic.claude-sonnet-4-5-20250929-v1:0 \
AGENTCORE_OUTPUT_FILE=agentcore_outputs_ap-southeast-1.json \
  python3 scripts/deploy_agentcore.py          # AgentCore resources (re-runnable / idempotent)

python3 scripts/seed_test_assets.py --apply    # 294-record test CMDB (or seed_cmdb.py for 3 rows)
```

After any deploy that touches CloudFront or the origin secret, confirm the two things that fail
quietly rather than loudly:

```bash
# 1. The origin-verify secret must be a real 48-char value, not an unresolved
#    {{resolve:secretsmanager:...}} literal — if it is literal, every request 403s.
aws cloudfront get-distribution --id E37XX5O7AZAE4M \
  --query "Distribution.DistributionConfig.Origins.Items[0].CustomHeaders.Items[0].HeaderValue"

# 2. AgentCore IDs must still be populated on the Lambda (see bug 1 above).
aws lambda get-function-configuration --function-name assetguardian-harness-invoke \
  --query "Environment.Variables.{MEM:AGENTCORE_MEMORY_ID,POL:AGENTCORE_POLICY_ENGINE_ID,GW:AGENTCORE_GATEWAY_URL}"
```

## The single biggest risk (per the report)

Same as the original: **CMDB data quality**. `identity_verification.py`'s fail-fast check will
reject legitimate submissions at scale if the CMDB has bad serial numbers or wrong assignments.
Run an accuracy audit (sample 500 records against physical assets) before pointing this at a real
employee population — this rebuild only seeded 3 synthetic test records.
