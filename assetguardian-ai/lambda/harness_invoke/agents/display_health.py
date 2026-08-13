"""Experiment 2: Display Health Diagnostics.

Analyzes solid white / solid black / color pattern screen test images to
detect dead pixels, backlight bleed, burn-in and color accuracy issues.
"""
from .bedrock_client import analyze_images

SYSTEM_PROMPT = (
    "You are the Display Health agent for AssetGuardian AI. You inspect "
    "screen test photographs (solid white, solid black, color pattern) and "
    "report display defects precisely."
)


def run(bucket: str, white_screen_key: str | None, black_screen_key: str | None, color_pattern_key: str | None) -> dict:
    image_keys = [k for k in [white_screen_key, black_screen_key, color_pattern_key] if k]
    if not image_keys:
        return {
            "agent": "display_health",
            "skipped": True,
            "reason": "no_screen_test_images_provided",
        }

    result = analyze_images(
        bucket=bucket,
        image_keys=image_keys,
        system_prompt=SYSTEM_PROMPT,
        instruction=(
            "The images provided are, in order supplied, a subset of: solid "
            "white screen test, solid black screen test, color pattern test. "
            "From the white screen image estimate dead pixel count. From the "
            "black screen image estimate bright/stuck pixel count. Assess "
            "backlight bleed severity (None/Minimal/Moderate/Severe), detect "
            "screen burn-in / ghost images, and evaluate color accuracy "
            "(Excellent/Good/Fair/Poor)."
        ),
        response_schema_hint=(
            '{"dead_pixel_estimate": int, "bright_stuck_pixel_estimate": int, '
            '"backlight_bleed_severity": string, "burn_in_detected": bool, '
            '"color_accuracy": string, "display_health_score": int, '
            '"health_classification": string, "notes": string}'
        ),
    )

    return {
        "agent": "display_health",
        "skipped": False,
        "dead_pixel_estimate": result.get("dead_pixel_estimate", 0),
        "bright_stuck_pixel_estimate": result.get("bright_stuck_pixel_estimate", 0),
        "backlight_bleed_severity": result.get("backlight_bleed_severity", "None"),
        "burn_in_detected": result.get("burn_in_detected", False),
        "color_accuracy": result.get("color_accuracy", "Good"),
        "display_health_score": result.get("display_health_score", 100),
        "health_classification": result.get("health_classification", "Good"),
        "notes": result.get("notes", ""),
    }
