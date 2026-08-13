"""Experiment 4: Vision Quality Gate (image preprocessing).

Validates submitted inspection images before they're fed to damage/display
agents, combining Amazon Rekognition label detection (electronic device
presence + content moderation) with Claude Vision scoring of sharpness,
lighting and completeness.
"""
import os

import boto3

from .bedrock_client import analyze_images

_rekognition = boto3.client("rekognition")

EXPECTED_VIEWS = [
    "front",
    "rear",
    "left_side",
    "right_side",
    "top",
    "bottom",
    "screen_on",
    "screen_off",
    "keyboard",
    "ports",
    "label",
    "hinge",
]

MIN_ACCEPTABLE_SCORE = 60

SYSTEM_PROMPT = (
    "You are the Vision Quality Gate agent for AssetGuardian AI. You grade "
    "whether a submitted device photo is usable for downstream damage and "
    "display inspection. Be strict but fair."
)


def _rekognition_precheck(bucket: str, key: str) -> dict:
    labels = _rekognition.detect_labels(
        Image={"S3Object": {"Bucket": bucket, "Name": key}},
        MaxLabels=20,
        MinConfidence=60,
    )["Labels"]
    label_names = {label["Name"].lower() for label in labels}
    device_present = bool(
        label_names
        & {
            "electronics",
            "laptop",
            "computer",
            "monitor",
            "screen",
            "phone",
            "mobile phone",
            "tablet computer",
            "computer hardware",
        }
    )

    moderation = _rekognition.detect_moderation_labels(
        Image={"S3Object": {"Bucket": bucket, "Name": key}}, MinConfidence=60
    )["ModerationLabels"]

    return {
        "device_detected": device_present,
        "detected_labels": sorted(label_names),
        "moderation_flags": [m["Name"] for m in moderation],
    }


def run(bucket: str, image_keys: list[str], declared_views: list[str] | None = None) -> dict:
    declared_views = declared_views or []

    rekognition_results = {key: _rekognition_precheck(bucket, key) for key in image_keys}
    flagged = [k for k, v in rekognition_results.items() if v["moderation_flags"]]
    no_device = [k for k, v in rekognition_results.items() if not v["device_detected"]]

    vision_scoring = analyze_images(
        bucket=bucket,
        image_keys=image_keys,
        system_prompt=SYSTEM_PROMPT,
        instruction=(
            "Score image quality for enterprise asset inspection across all "
            f"{len(image_keys)} images collectively. Score sharpness (blur), "
            "lighting quality, and device visibility/completeness on 0-100 "
            "scales. Identify which of these expected views each image most "
            f"likely represents: {EXPECTED_VIEWS}."
        ),
        response_schema_hint=(
            '{"sharpness_score": int, "lighting_score": int, '
            '"completeness_score": int, "detected_views": [string], '
            '"notes": string}'
        ),
    )

    sharpness = vision_scoring.get("sharpness_score", 0)
    lighting = vision_scoring.get("lighting_score", 0)
    completeness = vision_scoring.get("completeness_score", 0)
    detected_views = vision_scoring.get("detected_views", [])

    visibility_score = 100 if not no_device else max(0, 100 - (40 * len(no_device)))

    overall_score = round(
        sharpness * 0.30 + lighting * 0.25 + completeness * 0.25 + visibility_score * 0.20
    )

    missing_views = sorted(set(EXPECTED_VIEWS) - set(declared_views or detected_views))

    passed = overall_score >= MIN_ACCEPTABLE_SCORE and not flagged and not no_device

    return {
        "agent": "vision_quality_gate",
        "passed": passed,
        "overall_score": overall_score,
        "threshold": MIN_ACCEPTABLE_SCORE,
        "sharpness_score": sharpness,
        "lighting_score": lighting,
        "completeness_score": completeness,
        "visibility_score": visibility_score,
        "detected_views": detected_views,
        "missing_views": missing_views,
        "content_moderation_flagged_images": flagged,
        "images_without_device_detected": no_device,
        "rekognition_detail": rekognition_results,
    }
