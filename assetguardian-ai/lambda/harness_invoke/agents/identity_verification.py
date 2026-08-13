"""Experiment 5: Asset Identity Verification (OCR + CMDB cross-reference).

Cross-references a photographed asset tag/serial number against the
DynamoDB CMDB table, using Rekognition DetectText for OCR and falling back
to a small local record set if DynamoDB is unavailable (mirrors the
report's "Fallback strategy testing" experiment).
"""
import os

import boto3
from boto3.dynamodb.conditions import Key

_rekognition = boto3.client("rekognition")
_dynamodb = boto3.resource("dynamodb")

CMDB_TABLE_NAME = os.environ.get("CMDB_TABLE_NAME", "assetguardian-cmdb")

# Used only if DynamoDB is unreachable, matching the report's fallback test.
_LOCAL_FALLBACK_RECORDS: dict[str, dict] = {}


def _ocr_asset_label(bucket: str, key: str) -> dict:
    detected = _rekognition.detect_text(Image={"S3Object": {"Bucket": bucket, "Name": key}})
    lines = [
        d["DetectedText"]
        for d in detected["TextDetections"]
        if d["Type"] == "LINE"
    ]
    confidences = [d["Confidence"] for d in detected["TextDetections"] if d["Type"] == "LINE"]
    avg_confidence = round(sum(confidences) / len(confidences), 1) if confidences else 0.0
    return {"detected_lines": lines, "avg_confidence": avg_confidence}


def _lookup_cmdb(asset_id: str | None, serial_number: str | None) -> tuple[dict | None, str]:
    table = _dynamodb.Table(CMDB_TABLE_NAME)
    try:
        if asset_id:
            resp = table.get_item(Key={"assetId": asset_id})
            if "Item" in resp:
                return resp["Item"], "dynamodb"
        if serial_number:
            resp = table.query(
                IndexName="SerialNumberIndex",
                KeyConditionExpression=Key("serialNumber").eq(serial_number),
                Limit=1,
            )
            items = resp.get("Items", [])
            if items:
                return items[0], "dynamodb"
        return None, "dynamodb"
    except Exception:
        record = _LOCAL_FALLBACK_RECORDS.get(asset_id or serial_number or "")
        return record, "local_fallback"


def _auto_register_asset(asset_id: str | None, serial_number: str | None, employee_id: str | None) -> dict:
    """Auto-register an unknown asset in the CMDB and return the new record.

    When a device is submitted for inspection but doesn't exist in the CMDB,
    we create an entry rather than blocking the inspection. The record is
    flagged as auto-registered so admins can review later.
    """
    import uuid
    from datetime import datetime

    table = _dynamodb.Table(CMDB_TABLE_NAME)

    # Use provided asset_id or generate one
    new_asset_id = asset_id if asset_id else f"AUTO-{uuid.uuid4().hex[:8].upper()}"
    new_serial = serial_number or ""
    new_employee = employee_id or "UNKNOWN"

    record = {
        "assetId": new_asset_id,
        "serialNumber": new_serial,
        "assignedUser": new_employee,
        "assignedUserName": "",
        "assignedUserEmail": "",
        "deviceModel": "Unknown (pending inspection)",
        "manufacturer": "",
        "warrantyActive": False,
        "deviceAgeMonths": 0,
        "repairCount": 0,
        "employeeRole": "standard",
        "status": "AutoRegistered",
        "modelInferred": False,
        "registeredAt": datetime.utcnow().isoformat() + "Z",
        "autoRegistered": True,
    }

    try:
        table.put_item(Item=record)
    except Exception as e:
        # Non-blocking — if write fails, still return the record for the pipeline
        import logging
        logging.getLogger().warning("Auto-register CMDB write failed: %s", e)

    return record


def run(
    *,
    bucket: str,
    label_image_key: str | None,
    asset_id: str | None,
    provided_serial_number: str | None,
    employee_id: str | None,
) -> dict:
    ocr_result = _ocr_asset_label(bucket, label_image_key) if label_image_key else {
        "detected_lines": [],
        "avg_confidence": 0.0,
    }

    record, source = _lookup_cmdb(asset_id, provided_serial_number)

    if record is None:
        # Auto-create CMDB entry for unregistered assets
        record = _auto_register_asset(
            asset_id=asset_id,
            serial_number=provided_serial_number,
            employee_id=employee_id,
        )
        source = "auto_registered"

    cmdb_serial = record.get("serialNumber")
    serial_match = bool(
        provided_serial_number
        and cmdb_serial
        and provided_serial_number.strip().upper() == str(cmdb_serial).strip().upper()
    )
    ocr_serial_match = any(
        cmdb_serial and cmdb_serial.upper() in line.upper().replace(" ", "")
        for line in ocr_result["detected_lines"]
    )
    # An employee can be identified by either their staff ID (EMP1001) or their
    # corporate email — both are aliases for the same principal, and the portal
    # offers one free-text field for whichever the user has to hand. Matching
    # only assignedUser would reject a legitimate submission typed as an email.
    submitted = (employee_id or "").strip().upper()
    known_identifiers = {
        str(record.get(field)).strip().upper()
        for field in ("assignedUser", "assignedUserEmail")
        if record.get(field)
    }
    ownership_match = bool(submitted and submitted in known_identifiers)

    verified = (serial_match or ocr_serial_match) and (ownership_match or employee_id is None)

    return {
        "agent": "identity_verification",
        "verified": verified,
        "cmdb_status": record.get("status", "Unknown"),
        "cmdb_source": source,
        "serial_match": serial_match,
        "ocr_serial_match": ocr_serial_match,
        "ownership_match": ownership_match,
        "cmdb_record": {
            "assetId": record.get("assetId"),
            "serialNumber": cmdb_serial,
            "assignedUser": record.get("assignedUser"),
            "assignedUserEmail": record.get("assignedUserEmail"),
            "deviceModel": record.get("deviceModel"),
            "status": record.get("status"),
            "deviceAgeMonths": record.get("deviceAgeMonths"),
            "warrantyActive": record.get("warrantyActive"),
            "repairCount": record.get("repairCount", 0),
            "employeeRole": record.get("employeeRole"),
        },
        "ocr": ocr_result,
    }
