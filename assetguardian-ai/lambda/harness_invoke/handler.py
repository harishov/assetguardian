"""assetguardian-harness-invoke Lambda entrypoint.

Downloads/references photos from S3, sanitizes all caller-supplied text
fields (prompt-injection defense), routes the request to the matching
Experiment-7 workflow, and returns a JSON result. Runs behind API Gateway
HTTP API with AWS_IAM (SigV4) authorization and a WAFv2 Web ACL — see
assetguardian_stack.py.
"""
import hmac
import json
import logging
import os

import boto3

import history
from agents.sanitize import SuspiciousInputError, sanitize_asset_id, sanitize_employee_id, sanitize_s3_key
from orchestrator import PipelineHaltedError, WORKFLOWS

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

PHOTOS_BUCKET_NAME = os.environ["PHOTOS_BUCKET_NAME"]

# Upper bound on still frames pulled out of a video clip. Each one becomes part
# of a multi-modal Bedrock call, so this caps both latency and spend.
MAX_VIDEO_FRAMES = int(os.environ.get("MAX_VIDEO_FRAMES", "6"))

# Origin sealing — see assetguardian_stack.py. CloudFront stamps this header on
# every origin request; a caller hitting execute-api directly cannot produce it,
# so the WAF-bypassing route is refused here. This sits alongside the route's
# AWS_IAM authorizer rather than replacing it.
ORIGIN_VERIFY_HEADER = os.environ.get("ORIGIN_VERIFY_HEADER", "x-origin-verify")
ORIGIN_VERIFY_SECRET_ARN = os.environ.get("ORIGIN_VERIFY_SECRET_ARN", "")

_secrets = boto3.client("secretsmanager") if ORIGIN_VERIFY_SECRET_ARN else None
_origin_secret_cache = None


def _origin_secret() -> str | None:
    """Fetches and caches the shared secret for this execution environment.

    Cached for the container's lifetime, so rotating the secret takes effect as
    old containers age out. Rotation also needs a redeploy so CloudFront sends
    the new value — rotate, redeploy, then the two are back in step.
    """
    global _origin_secret_cache
    if _origin_secret_cache is None and _secrets is not None:
        _origin_secret_cache = _secrets.get_secret_value(
            SecretId=ORIGIN_VERIFY_SECRET_ARN
        )["SecretString"]
    return _origin_secret_cache


def _from_cloudfront(event) -> bool:
    if not ORIGIN_VERIFY_SECRET_ARN:
        # Not configured (e.g. a local or pre-migration deploy) — don't lock the
        # API out of its own front door.
        return True
    # Function URL invocations have a different requestContext structure — allow them
    # (CORS on the Function URL restricts callers to the portal origin).
    request_context = event.get("requestContext", {})
    if "apiId" not in request_context and request_context.get("domainName", "").endswith(".lambda-url.ap-southeast-1.on.aws"):
        return True
    # API Gateway payload format 2.0 lowercases header names.
    presented = (event.get("headers") or {}).get(ORIGIN_VERIFY_HEADER, "")
    expected = _origin_secret() or ""
    return bool(presented) and hmac.compare_digest(presented, expected)

ROUTE_TO_WORKFLOW = {
    "/handover": "handover",
    "/return": "return",
    "/declare": "declare",
    "/inspect": "inspect",
    "/warranty": "warranty",
    "/warranty/vendors": "warranty_vendors",
}


def _response(status: int, body: dict) -> dict:
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body, default=str),
    }


# Plain-English wording for each halt stage. The pipeline stopping is a normal
# business outcome, not a fault, so these read as guidance rather than errors.
_HALT_MESSAGES = {
    "identity_verification": (
        "We couldn't match this device to your records. Check the asset ID and "
        "serial number, and make sure the device is assigned to you."
    ),
    "vision_quality_gate": (
        "The photos weren't clear enough to assess. Please retake them in good "
        "light, with the whole device in frame."
    ),
    "fraud_detection": (
        "These photos don't appear to be genuine photos of your device. "
        "Please take fresh photos of the actual physical device in your "
        "workplace — stock images, internet downloads, and previously used "
        "photos are not accepted."
    ),
}

_GENERIC_HALT_MESSAGE = (
    "We couldn't complete this inspection. Please check the details and photos, "
    "then try again."
)


def _error(status, code, message, request_id, *, log_detail=None, level="warning"):
    """Returns a plain-English error body and writes the full detail to logs.

    The response deliberately carries no internal detail — field names, stack
    traces and stage payloads all go to CloudWatch instead, correlated by the
    reference the caller is shown. An admin pastes that reference into the
    portal's Event Logs tab to retrieve the complete record.
    """
    if log_detail is not None:
        getattr(logger, level)(
            "reference=%s code=%s detail=%s", request_id, code, log_detail
        )
    return _response(
        status,
        {"error": code, "message": message, "reference": request_id},
    )


def _build_request(body: dict) -> dict:
    """Sanitizes and normalizes the inbound request payload."""
    image_keys = body.get("image_keys", [])
    if not isinstance(image_keys, list) or not image_keys:
        raise SuspiciousInputError(
            "image_keys must be a non-empty list",
            user_message="Please add at least one photo of the device before submitting.",
        )
    image_keys = [sanitize_s3_key(k) for k in image_keys]

    # Video evidence. Bedrock's vision models take images, not video, so the
    # browser extracts still frames before upload and sends those for analysis;
    # video_key is the original clip, kept for the audit trail rather than
    # analysed directly. Capped so a long clip can't fan out into dozens of
    # model calls.
    raw_frames = body.get("video_frame_keys") or []
    if not isinstance(raw_frames, list):
        raise SuspiciousInputError(
            "video_frame_keys must be a list",
            user_message="The video evidence couldn't be read. Please record it again.",
        )
    video_frame_keys = [sanitize_s3_key(k) for k in raw_frames[:MAX_VIDEO_FRAMES]]

    req = {
        "bucket": PHOTOS_BUCKET_NAME,
        "image_keys": image_keys,
        "video_key": sanitize_s3_key(body.get("video_key")),
        "video_frame_keys": video_frame_keys,
        "label_image_key": sanitize_s3_key(body.get("label_image_key")),
        "white_screen_key": sanitize_s3_key(body.get("white_screen_key")),
        "black_screen_key": sanitize_s3_key(body.get("black_screen_key")),
        "color_pattern_key": sanitize_s3_key(body.get("color_pattern_key")),
        "asset_id": sanitize_asset_id(body.get("asset_id")),
        "serial_number": sanitize_asset_id(body.get("serial_number")),
        "employee_id": sanitize_employee_id(body.get("employee_id")),
        "session_id": sanitize_employee_id(body.get("session_id")) or body.get("employee_id"),
        "device_type": (body.get("device_type") or "laptop").lower()[:32],
        "declared_views": body.get("declared_views", []),
        "expected_otp": body.get("expected_otp"),
        "is_critical": bool(body.get("is_critical")),
        "check_physical_damage": body.get("check_physical_damage", True),
        "check_display_health": bool(body.get("check_display_health")),
        "attestation_confirmed": bool(body.get("attestation_confirmed")),
        "attestation_text": (body.get("attestation_text") or "")[:1000],
        "prior_photo_hashes": body.get("prior_photo_hashes", []),
    }
    return req


ROUTE_TO_WORKFLOW_BY_TOOL = {
    "inspect_device": "inspect",
    "employee_handover": "handover",
    "asset_return": "return",
    "annual_self_declaration": "declare",
}


def _gateway_tool_name(context) -> str | None:
    """Returns the MCP tool name when AgentCore Gateway invoked us, else None.

    The Gateway calls the function directly rather than through API Gateway, so
    there is no routeKey and no CloudFront header — it passes the tool name in
    the client context instead. Gateway calls are already authorized twice over
    (the gateway's IAM role, plus the Cedar policy engine attached to it in
    ENFORCE mode), which is why they don't go through the origin check.
    """
    client_context = getattr(context, "client_context", None)
    custom = getattr(client_context, "custom", None) or {}
    for key in ("bedrockAgentCoreToolName", "bedrockagentcoretoolname", "toolName"):
        if custom.get(key):
            # Gateway prefixes tool names with the target: "target___tool".
            return str(custom[key]).split("___")[-1]
    return None


def _handle_gateway_invocation(event, context, tool_name, request_id):
    """MCP tool call: returns the bare result, not an HTTP envelope."""
    workflow_key = ROUTE_TO_WORKFLOW_BY_TOOL.get(tool_name)
    if workflow_key is None:
        logger.warning("reference=%s unknown MCP tool %r", request_id, tool_name)
        return {"error": "unknown_tool", "message": f"No such tool: {tool_name}."}

    try:
        req = _build_request(event or {})
    except SuspiciousInputError as e:
        logger.warning("reference=%s code=invalid_input detail=%s", request_id, e)
        return {
            "error": "invalid_input",
            "message": e.user_message or "Some of the details supplied couldn't be accepted.",
            "reference": request_id,
        }

    try:
        return WORKFLOWS[workflow_key](req)
    except PipelineHaltedError as e:
        logger.info(
            "reference=%s code=pipeline_halted detail=halted at %s: %s",
            request_id, e.stage, json.dumps(e.detail, default=str),
        )
        return {
            "error": "pipeline_halted",
            "message": _HALT_MESSAGES.get(e.stage, _GENERIC_HALT_MESSAGE),
            "reference": request_id,
        }
    except Exception:
        logger.exception(
            "reference=%s code=internal_error workflow=%s", request_id, workflow_key
        )
        return {
            "error": "internal_error",
            "message": "Something went wrong while running this inspection.",
            "reference": request_id,
        }


def lambda_handler(event, context):
    request_id = getattr(context, "aws_request_id", "unavailable")

    tool_name = _gateway_tool_name(context)
    if tool_name is not None:
        return _handle_gateway_invocation(event, context, tool_name, request_id)

    if not _from_cloudfront(event):
        return _error(
            403,
            "forbidden",
            "This request didn't come through the AssetGuardian portal. "
            "Please open the portal and try again.",
            request_id,
            log_detail="request did not carry the CloudFront origin-verify header",
        )

    route_key = event.get("routeKey", "")
    path = route_key.split(" ")[-1] if " " in route_key else event.get("rawPath", "")

    # Read-only routes. Scope comes from the signed request, not the payload —
    # see history.caller_identity.
    if path in ("/history", "/media"):
        try:
            body = json.loads(event.get("body") or "{}")
        except json.JSONDecodeError:
            body = {}
        identity = history.caller_identity(event)
        try:
            if path == "/media":
                result = history.presign_media(
                    identity=identity, key=sanitize_s3_key(body.get("key"))
                )
                if result.get("error") == "forbidden":
                    return _error(
                        403, "forbidden",
                        "That evidence belongs to another employee's inspection.",
                        request_id,
                        log_detail=f"identity={identity.get('identity_id')} key={body.get('key')!r}",
                    )
                return _response(200, result)

            if body.get("view") == "users":
                result = history.summarise_users(identity=identity)
                if result.get("error") == "forbidden":
                    return _error(
                        403, "forbidden",
                        "Only administrators can view the list of users.",
                        request_id,
                        log_detail=f"non-admin user list attempt by {identity.get('identity_id')}",
                    )
                return _response(200, result)

            return _response(200, history.list_history(
                identity=identity,
                limit=body.get("limit", 50),
                employee_filter=body.get("employee_filter"),
            ))
        except SuspiciousInputError as e:
            return _error(400, "invalid_input",
                          e.user_message or "That request couldn't be read.",
                          request_id, log_detail=str(e))
        except Exception:
            logger.exception("reference=%s code=internal_error path=%s", request_id, path)
            return _response(500, {
                "error": "internal_error",
                "message": "We couldn't load that just now. Please try again.",
                "reference": request_id,
            })
    # ─── Warranty routes (don't use the inspection pipeline) ─────────────────
    if path in ("/warranty", "/warranty/vendors"):
        from agents import warranty_verification
        try:
            body = json.loads(event.get("body") or "{}")
        except json.JSONDecodeError:
            body = {}
        try:
            if path == "/warranty/vendors":
                return _response(200, {"vendors": warranty_verification.get_supported_vendors()})
            # POST /warranty — check a serial number
            serial_number = body.get("serialNumber", "")
            vendor = body.get("vendor", "")
            device_type = body.get("deviceType", "")
            force_refresh = body.get("forceRefresh", False)
            if not serial_number:
                return _error(400, "invalid_input", "Serial number is required.", request_id)
            if not vendor:
                return _error(400, "invalid_input", "Vendor is required (hp, dell, lenovo).", request_id)
            result = warranty_verification.run(serial_number, vendor, device_type, force_refresh)
            return _response(200, result)
        except Exception:
            logger.exception("reference=%s code=internal_error path=%s", request_id, path)
            return _response(500, {
                "error": "internal_error",
                "message": "Warranty check failed. Please try again.",
                "reference": request_id,
            })

    workflow_key = ROUTE_TO_WORKFLOW.get(path)

    if workflow_key is None:
        return _error(
            404,
            "unknown_route",
            "That inspection type isn't available.",
            request_id,
            log_detail=f"unknown path {path!r}",
        )

    try:
        body = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError as e:
        return _error(
            400,
            "invalid_json_body",
            "We couldn't read the details sent with your request. Please try again.",
            request_id,
            log_detail=f"JSON decode failed: {e}",
        )

    try:
        req = _build_request(body)
    except SuspiciousInputError as e:
        return _error(
            400,
            "invalid_input",
            e.user_message or "Some of the details you entered couldn't be accepted. "
            "Please check them and try again.",
            request_id,
            log_detail=str(e),
        )

    workflow_fn = WORKFLOWS[workflow_key]

    try:
        result = workflow_fn(req)
        # History is written after the pipeline succeeds and never blocks the
        # response — the model calls are the expensive part, so a bookkeeping
        # failure must not cost the user their result.
        try:
            history.record_inspection(
                identity=history.caller_identity(event), req=req, result=result
            )
        except Exception:
            logger.exception("reference=%s failed to record inspection history", request_id)
        return _response(200, result)
    except PipelineHaltedError as e:
        # The full stage payload is logged, not returned — it contains model
        # scores and CMDB internals that mean nothing to the person submitting.
        return _error(
            422,
            "pipeline_halted",
            _HALT_MESSAGES.get(e.stage, _GENERIC_HALT_MESSAGE),
            request_id,
            log_detail=f"halted at {e.stage}: {json.dumps(e.detail, default=str)}",
            level="info",
        )
    except Exception:
        logger.exception(
            "reference=%s code=internal_error workflow=%s", request_id, workflow_key
        )
        return _response(
            500,
            {
                "error": "internal_error",
                "message": "Something went wrong on our side. Please try again in a "
                           "moment. If it keeps happening, quote the reference below "
                           "to your IT team.",
                "reference": request_id,
            },
        )
