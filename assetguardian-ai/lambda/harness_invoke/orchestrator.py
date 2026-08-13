"""Experiment 7: Multi-Agent Workflow Orchestration.

Sequential orchestration with fail-fast identity verification and
conditional branching, reproducing the report's 4 enterprise workflows and
SLA targets:
  - Employee Handover:      Identity -> Vision -> Fraud -> Lifecycle          (24h SLA)
  - Asset Return:           Identity -> Vision -> Physical -> Display -> Fraud -> Lifecycle  (48h SLA, most comprehensive)
  - Annual Self-Declaration: Identity -> User Input -> Vision -> Fraud -> Lifecycle (72h SLA, with attestation)
  - Ad-hoc Inspection:      Identity -> Vision -> Physical/Display (conditional) -> Fraud -> Lifecycle (4h critical / 24h standard SLA)
"""
import logging
import time

from agents import (
    damage_detection,
    display_health,
    evaluators,
    fraud_detection,
    identity_verification,
    lifecycle_decision,
    memory,
    vision_quality_gate,
)

logger = logging.getLogger()

SLA_HOURS = {
    "employee_handover": 24,
    "asset_return": 48,
    "annual_self_declaration": 72,
    "adhoc_inspection_critical": 4,
    "adhoc_inspection_standard": 24,
}

REPLACEMENT_VALUE_BY_DEVICE = {
    "laptop": 1500,
    "monitor": 400,
    "phone": 900,
    "tablet": 700,
}


def _damage_keys(req: dict) -> list[str]:
    """Stills plus any video frames — what the damage agent should look at.

    Video frames widen coverage (a walk-around clip catches edges a few posed
    photos miss) but are deliberately kept out of the quality gate and fraud
    stages: the gate would reject a legitimately motion-blurred frame, and
    fraud_detection's EXIF, GPS and freshness checks read camera metadata that
    extracted frames simply do not carry.
    """
    return list(req.get("image_keys", [])) + list(req.get("video_frame_keys", []))


def _video_evidence_stage(req: dict) -> dict | None:
    frames = req.get("video_frame_keys") or []
    if not req.get("video_key") and not frames:
        return None
    return {
        "agent": "video_evidence",
        "video_key": req.get("video_key"),
        "frames_extracted": len(frames),
        "frames_analysed": len(frames),
        "note": "Frames were sampled in the browser; the clip itself is retained for audit.",
    }


class PipelineHaltedError(Exception):
    def __init__(self, stage: str, detail: dict):
        self.stage = stage
        self.detail = detail
        super().__init__(f"Pipeline halted at {stage}: {detail}")


def _run_identity_stage(req: dict) -> dict:
    result = identity_verification.run(
        bucket=req["bucket"],
        label_image_key=req.get("label_image_key"),
        asset_id=req.get("asset_id"),
        provided_serial_number=req.get("serial_number"),
        employee_id=req.get("employee_id"),
    )
    if not result["verified"] and result.get("cmdb_source") != "auto_registered":
        # Fail-fast: identity failure halts the pipeline — but auto-registered
        # assets always pass since they're self-assigned.
        raise PipelineHaltedError("identity_verification", result)
    return result


def _run_vision_and_fraud(req: dict, image_keys: list[str]) -> tuple[dict, dict]:
    quality = vision_quality_gate.run(req["bucket"], image_keys, req.get("declared_views"))
    if not quality["passed"]:
        raise PipelineHaltedError("vision_quality_gate", quality)

    fraud = fraud_detection.run(
        bucket=req["bucket"],
        image_keys=image_keys,
        expected_otp=req.get("expected_otp"),
        expected_site_coordinates=req.get("expected_site_coordinates"),
        prior_photo_hashes=req.get("prior_photo_hashes", []),
    )
    return quality, fraud


def _run_lifecycle(identity: dict, damage: dict) -> dict:
    record = identity["cmdb_record"]
    device_type = (record.get("deviceModel") or "laptop").lower()
    for key in REPLACEMENT_VALUE_BY_DEVICE:
        if key in device_type:
            device_type = key
            break
    else:
        device_type = "laptop"

    return lifecycle_decision.run(
        damage_severity_score=damage["damage_severity_score"],
        severity_category=damage["severity_category"],
        device_age_months=record.get("deviceAgeMonths", 0) or 0,
        repair_count=record.get("repairCount", 0) or 0,
        warranty_active=bool(record.get("warrantyActive")),
        employee_role=record.get("employeeRole"),
        replacement_value=REPLACEMENT_VALUE_BY_DEVICE.get(device_type, 1500),
    )


def _run_warranty(identity: dict, serial_number: str = "") -> dict:
    """Check warranty against vendor's support site.

    Uses the CMDB record to determine vendor, then queries the vendor API.
    Non-blocking with a 5-second timeout to stay within API Gateway limits.
    """
    from agents import warranty_verification

    record = identity.get("cmdb_record", {})
    device_model = (record.get("deviceModel") or "").lower()
    serial = serial_number or record.get("serialNumber", "")

    # Determine vendor from device model
    vendor = ""
    if any(v in device_model for v in ("hp", "elitebook", "probook", "zbook", "pavilion", "envy")):
        vendor = "hp"
    elif any(v in device_model for v in ("dell", "latitude", "optiplex", "precision", "inspiron", "xps")):
        vendor = "dell"
    elif any(v in device_model for v in ("lenovo", "thinkpad", "ideapad", "thinkcentre", "yoga")):
        vendor = "lenovo"
    elif any(v in device_model for v in ("apple", "macbook", "imac", "mac")):
        vendor = "apple"
    elif any(v in device_model for v in ("surface", "microsoft")):
        vendor = "microsoft"

    if not vendor:
        manufacturer = (record.get("manufacturer") or "").lower()
        for v in ("hp", "dell", "lenovo", "apple", "microsoft"):
            if v in manufacturer:
                vendor = v
                break

    if not vendor or not serial:
        return {
            "vendor": vendor or "unknown",
            "serialNumber": serial,
            "warrantyStatus": "UNKNOWN",
            "error": "Could not determine vendor or serial number from device records.",
            "source": "skipped",
        }

    # Run with a 5-second timeout to avoid pushing pipeline over 30s
    import concurrent.futures
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(warranty_verification.run, serial, vendor)
            return future.result(timeout=5)
    except concurrent.futures.TimeoutError:
        return {
            "vendor": vendor,
            "serialNumber": serial,
            "warrantyStatus": "UNKNOWN",
            "warrantyEndDate": record.get("warrantyEndDate"),
            "error": "Warranty check timed out (5s limit). Using cached CMDB data.",
            "source": "timeout_fallback",
            "cmdbWarrantyActive": record.get("warrantyActive", False),
        }
    except Exception as e:
        logger.warning("Warranty check failed (non-blocking): %s", e)
        return {
            "vendor": vendor,
            "serialNumber": serial,
            "warrantyStatus": "UNKNOWN",
            "error": f"Warranty check error: {str(e)[:80]}",
            "source": "error",
        }


def _finish(workflow: str, sla_hours: int, started_at: float, stages: dict) -> dict:
    elapsed_seconds = round(time.time() - started_at, 2)
    return {
        "workflow": workflow,
        "sla_hours": sla_hours,
        "pipeline_elapsed_seconds": elapsed_seconds,
        "stages": stages,
    }


def _record_memory(req: dict, stages: dict) -> None:
    actor_id = req.get("employee_id") or "unknown_employee"
    memory.record_inspection_event(
        actor_id=actor_id,
        session_id=req.get("session_id", actor_id),
        payload=stages,
    )


def employee_handover(req: dict) -> dict:
    started_at = time.time()
    stages = {}
    stages["identity_verification"] = _run_identity_stage(req)
    quality, fraud = _run_vision_and_fraud(req, req["image_keys"])
    stages["vision_quality_gate"] = quality
    stages["fraud_detection"] = fraud
    video_stage = _video_evidence_stage(req)
    if video_stage:
        stages["video_evidence"] = video_stage

    # Run damage detection and warranty check in parallel
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        damage_future = executor.submit(damage_detection.run, req["bucket"], _damage_keys(req), req.get("device_type", "laptop"))
        warranty_future = executor.submit(_run_warranty, stages["identity_verification"], req.get("serial_number", ""))
        damage = damage_future.result()
        stages["warranty_verification"] = warranty_future.result()

    stages["damage_detection"] = damage
    stages["lifecycle_decision"] = _run_lifecycle(stages["identity_verification"], damage)
    stages["quality_evaluation"] = evaluators.run_all(
        damage_result=damage, lifecycle_result=stages["lifecycle_decision"], fraud_result=fraud
    )
    _record_memory(req, stages)
    return _finish("employee_handover", SLA_HOURS["employee_handover"], started_at, stages)


def asset_return(req: dict) -> dict:
    started_at = time.time()
    stages = {}
    stages["identity_verification"] = _run_identity_stage(req)
    quality, fraud = _run_vision_and_fraud(req, req["image_keys"])
    stages["vision_quality_gate"] = quality
    stages["fraud_detection"] = fraud
    video_stage = _video_evidence_stage(req)
    if video_stage:
        stages["video_evidence"] = video_stage

    # Run damage, display, and warranty in parallel
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
        damage_future = executor.submit(damage_detection.run, req["bucket"], _damage_keys(req), req.get("device_type", "laptop"))
        display_future = executor.submit(display_health.run, req["bucket"], req.get("white_screen_key"), req.get("black_screen_key"), req.get("color_pattern_key"))
        warranty_future = executor.submit(_run_warranty, stages["identity_verification"], req.get("serial_number", ""))
        damage = damage_future.result()
        stages["display_health"] = display_future.result()
        stages["warranty_verification"] = warranty_future.result()

    stages["damage_detection"] = damage
    stages["lifecycle_decision"] = _run_lifecycle(stages["identity_verification"], damage)
    stages["quality_evaluation"] = evaluators.run_all(
        damage_result=damage, lifecycle_result=stages["lifecycle_decision"], fraud_result=fraud
    )
    _record_memory(req, stages)
    return _finish("asset_return", SLA_HOURS["asset_return"], started_at, stages)


def annual_self_declaration(req: dict) -> dict:
    started_at = time.time()
    stages = {}
    stages["identity_verification"] = _run_identity_stage(req)
    stages["employee_attestation"] = {
        "attested": bool(req.get("attestation_confirmed")),
        "attestation_text": req.get("attestation_text", ""),
    }
    quality, fraud = _run_vision_and_fraud(req, req["image_keys"])
    stages["vision_quality_gate"] = quality
    stages["fraud_detection"] = fraud
    video_stage = _video_evidence_stage(req)
    if video_stage:
        stages["video_evidence"] = video_stage

    # Run damage and warranty in parallel
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        damage_future = executor.submit(damage_detection.run, req["bucket"], _damage_keys(req), req.get("device_type", "laptop"))
        warranty_future = executor.submit(_run_warranty, stages["identity_verification"], req.get("serial_number", ""))
        damage = damage_future.result()
        stages["warranty_verification"] = warranty_future.result()

    stages["damage_detection"] = damage
    stages["lifecycle_decision"] = _run_lifecycle(stages["identity_verification"], damage)
    stages["quality_evaluation"] = evaluators.run_all(
        damage_result=damage, lifecycle_result=stages["lifecycle_decision"], fraud_result=fraud
    )
    _record_memory(req, stages)
    return _finish("annual_self_declaration", SLA_HOURS["annual_self_declaration"], started_at, stages)


def adhoc_inspection(req: dict) -> dict:
    started_at = time.time()
    critical = bool(req.get("is_critical"))
    stages = {}
    stages["identity_verification"] = _run_identity_stage(req)
    quality, fraud = _run_vision_and_fraud(req, req["image_keys"])
    stages["vision_quality_gate"] = quality
    stages["fraud_detection"] = fraud
    video_stage = _video_evidence_stage(req)
    if video_stage:
        stages["video_evidence"] = video_stage

    # Run damage and warranty in parallel
    import concurrent.futures
    damage = None
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        if req.get("check_physical_damage", True):
            damage_future = executor.submit(damage_detection.run, req["bucket"], _damage_keys(req), req.get("device_type", "laptop"))
        warranty_future = executor.submit(_run_warranty, stages["identity_verification"], req.get("serial_number", ""))

        if req.get("check_physical_damage", True):
            damage = damage_future.result()
            stages["damage_detection"] = damage
        stages["warranty_verification"] = warranty_future.result()

    if req.get("check_display_health"):
        stages["display_health"] = display_health.run(
            req["bucket"],
            req.get("white_screen_key"),
            req.get("black_screen_key"),
            req.get("color_pattern_key"),
        )

    if damage is not None:
        stages["lifecycle_decision"] = _run_lifecycle(stages["identity_verification"], damage)

    stages["quality_evaluation"] = evaluators.run_all(
        damage_result=damage, lifecycle_result=stages.get("lifecycle_decision"), fraud_result=fraud
    )
    _record_memory(req, stages)
    sla = SLA_HOURS["adhoc_inspection_critical"] if critical else SLA_HOURS["adhoc_inspection_standard"]
    return _finish("adhoc_inspection", sla, started_at, stages)


WORKFLOWS = {
    "handover": employee_handover,
    "return": asset_return,
    "declare": annual_self_declaration,
    "inspect": adhoc_inspection,
}
