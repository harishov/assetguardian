#!/usr/bin/env python3
"""Seeds a handful of sample CMDB records into the assetguardian-cmdb table
so the pipeline can be smoke-tested end-to-end without a live ServiceNow/
Intune sync (which is explicitly out of scope for this rebuild — see
README "Integration with Enterprise Systems" gap)."""
import os

import boto3

# us-east-1 was decommissioned 2026-08-01 — ap-southeast-1 is the sole live
# deployment. Override with CMDB_REGION if that ever stops being true.
REGION = os.environ.get("CMDB_REGION", "ap-southeast-1")
TABLE_NAME = os.environ.get("CMDB_TABLE_NAME", "assetguardian-cmdb")

SAMPLE_RECORDS = [
    {
        "assetId": "ASSET-0001",
        "serialNumber": "PF3K9QRX",
        "assignedUser": "EMP1001",
        "deviceModel": "Lenovo ThinkPad X1 Carbon",
        "status": "Active",
        "deviceAgeMonths": 18,
        "warrantyActive": True,
        "repairCount": 0,
        "employeeRole": "standard",
    },
    {
        "assetId": "ASSET-0002",
        "serialNumber": "C02F1234",
        "assignedUser": "EMP1002",
        "deviceModel": "Apple MacBook Pro 14",
        "status": "PendingReturn",
        "deviceAgeMonths": 42,
        "warrantyActive": False,
        "repairCount": 2,
        "employeeRole": "executive",
    },
    {
        "assetId": "ASSET-0003",
        "serialNumber": "DL7788XZ",
        "assignedUser": "EMP1003",
        "deviceModel": "Dell Latitude 5440",
        "status": "Active",
        "deviceAgeMonths": 51,
        "warrantyActive": False,
        "repairCount": 3,
        "employeeRole": "field",
    },
]


def main():
    table = boto3.resource("dynamodb", region_name=REGION).Table(TABLE_NAME)
    for record in SAMPLE_RECORDS:
        table.put_item(Item=record)
        print(f"Seeded {record['assetId']} ({record['deviceModel']})")


if __name__ == "__main__":
    main()
