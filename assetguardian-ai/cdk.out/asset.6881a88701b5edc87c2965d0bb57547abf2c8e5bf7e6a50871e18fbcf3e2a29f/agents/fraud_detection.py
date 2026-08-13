"""Experiment 3: Fraud Detection & Anti-Tampering Analysis.

5-check risk framework: EXIF metadata validity, image freshness, liveness
(visible one-time code), duplicate/similarity detection (sha256 exact-match
proxy for perceptual hashing), and GPS-vs-site consistency. Combined with a
Claude Vision pass for AI-powered manipulation/consistency checks.
"""
import hashlib
import math
from datetime import datetime, timezone

import boto3

from .bedrock_client import analyze_images
from .exif_utils import extract_exif

_s3 = boto3.client("s3")

SYSTEM_PROMPT = (
    "You are the Fraud Detection agent for AssetGuardian AI. You assess "
    "whether submitted device photos are genuine, freshly captured images "
    "of the physical device in question, as opposed to stock photos, "
    "reused old photos, or edited/composited images. Look for lighting "
    "inconsistencies, mismatched shadows, screen bezels typical of stock "
    "photography, watermark remnants, and unnatural edges consistent with "
    "photo editing."
)

FRESHNESS_MAX_AGE_HOURS = 48
GPS_MAX_DISTANCE_KM = 50


def _haversine_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1 = a
    lat2, lon2 = b
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    h = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def run(
    *,
    bucket: str,
    image_keys: list[str],
    expected_otp: str | None = None,
    expected_site_coordinates: tuple[float, float] | None = None,
    prior_photo_hashes: list[str] | None = None,
) -> dict:
    prior_photo_hashes = prior_photo_hashes or []

    checks = {}
    hashes = []
    exif_by_image = {}

    for key in image_keys:
        body = _s3.get_object(Bucket=bucket, Key=key)["Body"].read()
        digest = hashlib.sha256(body).hexdigest()
        hashes.append(digest)
        exif_by_image[key] = extract_exif(body)

    # Check 1: EXIF metadata presence/validity
    has_exif_count = sum(1 for e in exif_by_image.values() if e["has_exif"])
    checks["exif_metadata_present"] = has_exif_count == len(image_keys)

    # Check 2: image freshness
    freshness_ok = True
    for e in exif_by_image.values():
        if not e["datetime_original"]:
            freshness_ok = False
            continue
        try:
            captured = datetime.strptime(e["datetime_original"], "%Y:%m:%d %H:%M:%S").replace(
                tzinfo=timezone.utc
            )
            age_hours = (datetime.now(timezone.utc) - captured).total_seconds() / 3600
            if age_hours > FRESHNESS_MAX_AGE_HOURS or age_hours < -1:
                freshness_ok = False
        except ValueError:
            freshness_ok = False
    checks["freshness_ok"] = freshness_ok

    # Check 3: liveness via visible one-time code (delegated to vision model)
    liveness_result = {"otp_required": expected_otp is not None, "otp_matched": None}
    if expected_otp:
        vision_check = analyze_images(
            bucket=bucket,
            image_keys=image_keys[:1],
            system_prompt=SYSTEM_PROMPT,
            instruction=(
                "A one-time verification code should be visibly written on paper "
                "next to the device in this photo. Read any handwritten or "
                "printed short alphanumeric code you can find."
            ),
            response_schema_hint='{"detected_code": string}',
            max_tokens=300,
        )
        detected_code = str(vision_check.get("detected_code", "")).strip().upper()
        liveness_result["detected_code"] = detected_code
        liveness_result["otp_matched"] = detected_code == expected_otp.strip().upper()
    checks["liveness_ok"] = liveness_result["otp_matched"] is not False

    # Check 4: duplicate/similarity detection (exact-hash proxy for pHash)
    duplicate_hashes = [h for h in hashes if h in prior_photo_hashes]
    checks["no_duplicates"] = len(duplicate_hashes) == 0

    # Check 5: GPS location consistency
    gps_consistent = True
    gps_distance_km = None
    if expected_site_coordinates:
        gps_points = [e["gps"] for e in exif_by_image.values() if e["gps"]]
        if gps_points:
            gps_distance_km = min(
                _haversine_km(p, expected_site_coordinates) for p in gps_points
            )
            gps_consistent = gps_distance_km <= GPS_MAX_DISTANCE_KM
        else:
            gps_consistent = None  # no GPS data to evaluate
    checks["gps_consistent"] = gps_consistent

    # AI-powered manipulation / stock-photo detection
    vision_result = analyze_images(
        bucket=bucket,
        image_keys=image_keys,
        system_prompt=SYSTEM_PROMPT,
        instruction=(
            "Assess whether these photos appear to be genuine, freshly-captured "
            "photos of a physically present device, versus a stock photo, an "
            "old/reused photo, or a manipulated/composited image. Give a "
            "confidence score 0.0-1.0 that the submission is legitimate."
        ),
        response_schema_hint=(
            '{"legitimacy_confidence": number, "manipulation_indicators": [string], '
            '"looks_like_stock_photo": bool, "notes": string}'
        ),
    )

    failed_checks = sum(
        1
        for v in [
            checks["exif_metadata_present"],
            checks["freshness_ok"],
            checks["liveness_ok"],
            checks["no_duplicates"],
            checks["gps_consistent"] is not False,
        ]
        if v is False
    )
    legitimacy_confidence = vision_result.get("legitimacy_confidence", 0.5)

    if failed_checks >= 3 or legitimacy_confidence < 0.3:
        risk_level = "Critical"
    elif failed_checks == 2 or legitimacy_confidence < 0.5:
        risk_level = "High"
    elif failed_checks == 1 or legitimacy_confidence < 0.75:
        risk_level = "Medium"
    else:
        risk_level = "Low"

    return {
        "agent": "fraud_detection",
        "risk_level": risk_level,
        "failed_check_count": failed_checks,
        "checks": checks,
        "liveness": liveness_result,
        "gps_distance_km": gps_distance_km,
        "photo_hashes": hashes,
        "legitimacy_confidence": legitimacy_confidence,
        "manipulation_indicators": vision_result.get("manipulation_indicators", []),
        "looks_like_stock_photo": vision_result.get("looks_like_stock_photo", False),
        "notes": vision_result.get("notes", ""),
    }
