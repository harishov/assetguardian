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


def run(bucket: str, image_keys: list[str], device_type: str = "laptop") -> dict:
    device_type = device_type.lower()
    damage_types = DAMAGE_TYPES_BY_DEVICE.get(device_type, DAMAGE_TYPES_BY_DEVICE["laptop"])

    result = analyze_images(
        bucket=bucket,
        image_keys=image_keys,
        system_prompt=SYSTEM_PROMPT,
        instruction=(
            f"Device type: {device_type}. Cross-reference all {len(image_keys)} "
            f"images of the same physical device. Detect and score these damage "
            f"types where applicable: {damage_types}. For each damage type found, "
            "give a confidence score 0.0-1.0. Produce an overall damage_severity_score "
            "0-100 (0=pristine, 100=destroyed) and severity_category one of "
            "None/Minor/Moderate/Severe/Critical, plus an overall condition_grade "
            "one of Excellent/Good/Fair/Poor/Failed."
        ),
        response_schema_hint=(
            '{"damage_severity_score": int, "severity_category": string, '
            '"condition_grade": string, '
            '"detected_damage": [{"type": string, "location": string, '
            '"confidence": number, "description": string}], '
            '"summary": string}'
        ),
        max_tokens=2500,
    )

    return {
        "agent": "damage_detection",
        "device_type": device_type,
        "damage_severity_score": result.get("damage_severity_score", 0),
        "severity_category": result.get("severity_category", "None"),
        "condition_grade": result.get("condition_grade", "Good"),
        "detected_damage": result.get("detected_damage", []),
        "summary": result.get("summary", ""),
    }
