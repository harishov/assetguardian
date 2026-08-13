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
    if not result["verified"]:
        # Fail-fast: identity failure halts the entire pipeline immediately.
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

    damage = damage_detection.run(req["bucket"], req["image_keys"], req.get("device_type", "laptop"))
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

    damage = damage_detection.run(req["bucket"], req["image_keys"], req.get("device_type", "laptop"))
    stages["damage_detection"] = damage
    stages["display_health"] = display_health.run(
        req["bucket"],
        req.get("white_screen_key"),
        req.get("black_screen_key"),
        req.get("color_pattern_key"),
    )
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

    damage = damage_detection.run(req["bucket"], req["image_keys"], req.get("device_type", "laptop"))
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

    damage = None
    if req.get("check_physical_damage", True):
        damage = damage_detection.run(req["bucket"], req["image_keys"], req.get("device_type", "laptop"))
        stages["damage_detection"] = damage

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
