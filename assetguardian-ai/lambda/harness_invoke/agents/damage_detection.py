"""Experiment 1: AI-Powered Physical Damage Detection.

Uses Claude Sonnet 4 vision to analyze multi-angle device photos and score
physical damage (cracks, scratches, dents, missing keys, hinge damage,
chassis deformation) with a 0-100 severity scale and condition grade.
"""
from .bedrock_client import analyze_images

SYSTEM_PROMPT = (
    "You are the Damage Detection agent for AssetGuardian AI, an enterprise "
    "IT asset inspection platform. You examine device photographs (front, "
    "rear, side, close-ups) and report physical damage precisely and "
    "conservatively, the way a hardware repair technician would."
)

DAMAGE_TYPES_BY_DEVICE = {
    "laptop": ["crack", "scratch", "dent", "missing_key", "hinge_damage", "chassis_deformation", "port_damage"],
    "monitor": ["crack", "scratch", "dent", "chassis_deformation", "port_damage"],
    "phone": ["crack", "scratch", "dent", "chassis_deformation"],
    "tablet": ["crack", "scratch", "dent", "chassis_deformation"],
}


def run(bucket: str, image_keys: list[str], device_type: str = "auto") -> dict:
    device_type = device_type.lower()
    if device_type == "auto" or device_type not in DAMAGE_TYPES_BY_DEVICE:
        # Let the AI determine device type — use full damage type list
        damage_types = list(set(t for types in DAMAGE_TYPES_BY_DEVICE.values() for t in types))
        declared_type = "auto (AI will identify)"
    else:
        damage_types = DAMAGE_TYPES_BY_DEVICE[device_type]
        declared_type = device_type

    result = analyze_images(
        bucket=bucket,
        image_keys=image_keys,
        system_prompt=SYSTEM_PROMPT,
        instruction=(
            f"Examine these {len(image_keys)} images carefully.\n\n"
            "STEP 1 — DEVICE IDENTIFICATION: First, identify what type of device "
            "is actually shown in the photos. Determine: detected_device_type "
            "(laptop/monitor/phone/tablet/desktop/unknown) and detected_device_brand "
            "(HP/Dell/Lenovo/Apple/etc or unknown).\n\n"
            f"STEP 2 — DECLARED vs ACTUAL: The submitter declared this as a '{declared_type}'. "
            "Set device_type_match to true ONLY if the photos genuinely show the "
            "declared device type. If declared as 'auto (AI will identify)' then "
            "device_type_match should be true (no declaration to contradict). "
            "If the photos show a different device than declared (e.g., declared "
            "monitor but photos show laptop), set device_type_match to false and "
            "explain the mismatch.\n\n"
            "STEP 3 — DAMAGE ASSESSMENT: Cross-reference all images of the same "
            f"physical device. Detect and score these damage types where applicable: "
            f"{damage_types}. For each damage type found, give a confidence score "
            "0.0-1.0. Produce an overall damage_severity_score 0-100 (0=pristine, "
            "100=destroyed) and severity_category one of None/Minor/Moderate/Severe/"
            "Critical, plus an overall condition_grade of Excellent/Good/Fair/Poor/Failed.\n\n"
            "IMPORTANT: A severity score of 0-10 means truly minimal/no damage. "
            "A score of 50+ means significant visible damage. Be calibrated."
        ),
        response_schema_hint=(
            '{"detected_device_type": string, "detected_device_brand": string, '
            '"device_type_match": boolean, "device_mismatch_detail": string, '
            '"damage_severity_score": int, "severity_category": string, '
            '"condition_grade": string, '
            '"detected_damage": [{"type": string, "location": string, '
            '"confidence": number, "description": string}], '
            '"summary": string}'
        ),
        max_tokens=2500,
    )

    return {
        "agent": "damage_detection",
        "declared_device_type": declared_type,
        "detected_device_type": result.get("detected_device_type", "unknown"),
        "detected_device_brand": result.get("detected_device_brand", "unknown"),
        "device_type_match": result.get("device_type_match", True),
        "device_mismatch_detail": result.get("device_mismatch_detail", ""),
        "damage_severity_score": result.get("damage_severity_score", 0),
        "severity_category": result.get("severity_category", "None"),
        "condition_grade": result.get("condition_grade", "Good"),
        "detected_damage": result.get("detected_damage", []),
        "summary": result.get("summary", ""),
    }
