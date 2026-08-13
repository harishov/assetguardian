"""
DynamoDB cache layer for warranty lookups.
Caches results to avoid hitting vendor APIs repeatedly for the same serial number.
Cache TTL: 24 hours (warranty data doesn't change frequently).
"""

import os
import json
import boto3
from datetime import datetime, timedelta
from typing import Optional
from decimal import Decimal


TABLE_NAME = os.environ.get("WARRANTY_CACHE_TABLE", "assetguardian-warranty-cache")
CACHE_TTL_HOURS = int(os.environ.get("WARRANTY_CACHE_TTL_HOURS", "24"))
REGION = os.environ.get("AWS_REGION", "ap-southeast-1")


class WarrantyCache:
    """DynamoDB-backed cache for warranty lookup results."""

    def __init__(self):
        self.table = boto3.resource("dynamodb", region_name=REGION).Table(TABLE_NAME)

    def get(self, vendor: str, serial_number: str) -> Optional[dict]:
        """
        Get cached warranty result.
        Returns None if not found or expired.
        """
        try:
            cache_key = f"{vendor.lower()}#{serial_number.upper()}"
            response = self.table.get_item(Key={"cacheKey": cache_key})
            item = response.get("Item")

            if not item:
                return None

            # Check TTL
            cached_at = item.get("cachedAt", "")
            if cached_at:
                cached_time = datetime.fromisoformat(cached_at.replace("Z", "+00:00"))
                if datetime.now(cached_time.tzinfo) - cached_time > timedelta(hours=CACHE_TTL_HOURS):
                    return None  # Expired

            # Return cached result
            result = json.loads(item.get("resultJson", "{}"))
            result["source"] = "cache"
            return result

        except Exception as e:
            print(f"Cache get error: {e}")
            return None

    def put(self, vendor: str, serial_number: str, result: dict) -> None:
        """Cache a warranty result."""
        try:
            cache_key = f"{vendor.lower()}#{serial_number.upper()}"
            now = datetime.utcnow().isoformat() + "Z"
            ttl_epoch = int((datetime.utcnow() + timedelta(hours=CACHE_TTL_HOURS)).timestamp())

            self.table.put_item(Item={
                "cacheKey": cache_key,
                "vendor": vendor,
                "serialNumber": serial_number.upper(),
                "resultJson": json.dumps(result, default=str),
                "cachedAt": now,
                "ttl": ttl_epoch,
                "warrantyStatus": result.get("warrantyStatus", "UNKNOWN"),
                "warrantyEndDate": result.get("warrantyEndDate", ""),
            })
        except Exception as e:
            print(f"Cache put error: {e}")

    def invalidate(self, vendor: str, serial_number: str) -> None:
        """Remove a cached entry (force refresh on next lookup)."""
        try:
            cache_key = f"{vendor.lower()}#{serial_number.upper()}"
            self.table.delete_item(Key={"cacheKey": cache_key})
        except Exception as e:
            print(f"Cache invalidate error: {e}")
