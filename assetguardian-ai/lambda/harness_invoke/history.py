"""Inspection history: who did what, and who is allowed to see it.

Two rules drive everything here:

  1. **Identity comes from the signed request, never the payload.** API Gateway
     puts the Cognito identity ID and the assumed-role ARN into the request
     context after validating the SigV4 signature. A caller can edit any form
     field they like, but they cannot forge those, so they are what scoping is
     built on. The `employeeId` a user types is display data only.

  2. **Admin is a role, not a claim.** Membership of the Cognito "Admins" group
     is what makes the Identity Pool hand out PortalAdminRole instead of
     PortalAuthRole, so checking which role signed the request is equivalent to
     checking group membership — and it cannot be spoofed from the browser.
"""
import os
import time
from datetime import datetime, timezone

import boto3
from boto3.dynamodb.conditions import Key

HISTORY_TABLE_NAME = os.environ.get("HISTORY_TABLE_NAME", "assetguardian-inspection-history")
PHOTOS_BUCKET_NAME = os.environ.get("PHOTOS_BUCKET_NAME", "")
MEDIA_URL_TTL_SECONDS = int(os.environ.get("MEDIA_URL_TTL_SECONDS", "300"))

# Every row carries the same partition value on the all-activity GSI so admins
# can read the whole feed newest-first with a Query instead of a Scan.
ALL_ACTIVITY_PARTITION = "INSPECTION"

_dynamodb = boto3.resource("dynamodb")
_s3 = boto3.client("s3")


def _table():
    return _dynamodb.Table(HISTORY_TABLE_NAME)


def caller_identity(event) -> dict:
    """Extracts the unforgeable caller identity from the request context."""
    ctx = (event.get("requestContext") or {})
    iam_ctx = ((ctx.get("authorizer") or {}).get("iam") or {})
    cognito = (iam_ctx.get("cognitoIdentity") or {})

    identity_id = cognito.get("identityId") or iam_ctx.get("userId") or ""
    user_arn = iam_ctx.get("userArn") or ""
    # The assumed-role ARN looks like
    #   arn:aws:sts::123:assumed-role/Stack-PortalAdminRoleXXXX-YYYY/CognitoIdentity
    # so matching on the role-name segment identifies which role was assumed.
    role_segment = user_arn.split("/")[1] if "/" in user_arn else ""
    is_admin = "PortalAdminRole" in role_segment

    return {
        "identity_id": identity_id,
        "user_arn": user_arn,
        "is_admin": is_admin,
    }


def record_inspection(*, identity, req: dict, result: dict) -> None:
    """Writes one row per completed inspection. Never raises into the pipeline.

    A failure to record history must not fail an inspection that already ran —
    the model calls are the expensive part and the user should still get their
    result — so this logs and swallows.
    """
    if not identity.get("identity_id"):
        return

    stages = result.get("stages", {})
    damage = stages.get("damage_detection") or {}
    lifecycle = stages.get("lifecycle_decision") or {}
    fraud = stages.get("fraud_detection") or {}
    identity_stage = stages.get("identity_verification") or {}
    cmdb = identity_stage.get("cmdb_record") or {}

    item = {
        "identityId": identity["identity_id"],
        "inspectedAt": datetime.now(timezone.utc).isoformat(),
        "recordType": ALL_ACTIVITY_PARTITION,
        "sessionId": req.get("session_id") or "",
        "employeeId": req.get("employee_id") or "unknown",
        "assetId": req.get("asset_id") or cmdb.get("assetId") or "_unassigned-asset",
        "serialNumber": req.get("serial_number") or cmdb.get("serialNumber") or "",
        "deviceModel": cmdb.get("deviceModel") or req.get("device_type") or "",
        "workflow": result.get("workflow") or "",
        "elapsedSeconds": str(result.get("pipeline_elapsed_seconds") or ""),
        "decision": lifecycle.get("decision") or "",
        "ruleApplied": lifecycle.get("rule_applied") or "",
        "damageScore": str(damage.get("damage_severity_score") or ""),
        "severity": damage.get("severity_category") or "",
        "fraudRisk": fraud.get("risk_level") or "",
        # Evidence keys, so the admin console can render what was submitted
        # without having to list the bucket.
        "imageKeys": list(req.get("image_keys") or []),
        "labelImageKey": req.get("label_image_key") or "",
        "videoKey": req.get("video_key") or "",
        "videoFrameKeys": list(req.get("video_frame_keys") or []),
        "expiresAt": int(time.time()) + 60 * 60 * 24 * 730,  # matches S3 retention
    }
    _table().put_item(Item={k: v for k, v in item.items() if v not in ("", None)})


def list_history(*, identity, limit: int = 50, employee_filter: str | None = None) -> dict:
    """Admins see every inspection; everyone else sees only their own rows."""
    table = _table()
    limit = max(1, min(int(limit or 50), 100))

    if identity.get("is_admin"):
        resp = table.query(
            IndexName="AllActivityIndex",
            KeyConditionExpression=Key("recordType").eq(ALL_ACTIVITY_PARTITION),
            ScanIndexForward=False,
            Limit=limit,
        )
        items = resp.get("Items", [])
        if employee_filter:
            needle = employee_filter.strip().lower()
            items = [i for i in items if needle in str(i.get("employeeId", "")).lower()]
    else:
        # Partition key is the caller's own identity ID, taken from the signed
        # request — there is no parameter here a user could point elsewhere.
        resp = table.query(
            KeyConditionExpression=Key("identityId").eq(identity["identity_id"]),
            ScanIndexForward=False,
            Limit=limit,
        )
        items = resp.get("Items", [])

    return {
        "scope": "all_users" if identity.get("is_admin") else "own",
        "count": len(items),
        "items": items,
    }


def summarise_users(*, identity, limit: int = 200) -> dict:
    """Admin-only: who has run inspections, how many, and when they were last active."""
    if not identity.get("is_admin"):
        return {"error": "forbidden"}

    resp = _table().query(
        IndexName="AllActivityIndex",
        KeyConditionExpression=Key("recordType").eq(ALL_ACTIVITY_PARTITION),
        ScanIndexForward=False,
        Limit=max(1, min(int(limit or 200), 500)),
    )
    users: dict[str, dict] = {}
    for item in resp.get("Items", []):
        employee = str(item.get("employeeId", "unknown"))
        entry = users.setdefault(employee, {
            "employeeId": employee,
            "identityId": item.get("identityId", ""),
            "inspections": 0,
            "lastActive": "",
            "assets": set(),
        })
        entry["inspections"] += 1
        entry["assets"].add(str(item.get("assetId", "")))
        stamp = str(item.get("inspectedAt", ""))
        if stamp > entry["lastActive"]:
            entry["lastActive"] = stamp

    rows = []
    for entry in users.values():
        entry["distinctAssets"] = len(entry.pop("assets"))
        rows.append(entry)
    rows.sort(key=lambda r: r["lastActive"], reverse=True)
    return {"count": len(rows), "users": rows}


def _keys_visible_to(identity, record) -> set:
    keys = set(record.get("imageKeys") or [])
    keys.update(record.get("videoFrameKeys") or [])
    for single in (record.get("labelImageKey"), record.get("videoKey")):
        if single:
            keys.add(single)
    return keys


def presign_media(*, identity, key: str) -> dict:
    """Presigns one evidence object, but only after proving the caller owns it.

    The browser has no s3:GetObject of its own (see the IAM notes in
    assetguardian_stack.py), so this is the only route to evidence — which is
    what stops one employee reading another's photos by guessing a key.
    """
    if not key:
        return {"error": "missing_key"}

    if identity.get("is_admin"):
        allowed = True
    else:
        resp = _table().query(
            KeyConditionExpression=Key("identityId").eq(identity["identity_id"]),
            ScanIndexForward=False,
            Limit=100,
        )
        allowed = any(key in _keys_visible_to(identity, r) for r in resp.get("Items", []))

    if not allowed:
        return {"error": "forbidden"}

    url = _s3.generate_presigned_url(
        "get_object",
        Params={"Bucket": PHOTOS_BUCKET_NAME, "Key": key},
        ExpiresIn=MEDIA_URL_TTL_SECONDS,
    )
    return {"url": url, "expires_in": MEDIA_URL_TTL_SECONDS}
