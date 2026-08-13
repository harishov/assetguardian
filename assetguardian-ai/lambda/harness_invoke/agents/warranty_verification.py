"""Warranty Verification Agent.

Checks device serial numbers against vendor support APIs (HP, Dell, Lenovo)
to retrieve warranty status, support end dates, and serial number validity.
Caches results in DynamoDB to avoid repeated vendor API calls.
"""
import json
import logging
import os
from datetime import date, datetime, timedelta
from typing import Optional

import boto3
import requests

logger = logging.getLogger(__name__)

REGION = os.environ.get("AWS_REGION", "ap-southeast-1")
WARRANTY_CACHE_TABLE = os.environ.get("WARRANTY_CACHE_TABLE", "assetguardian-warranty-cache")
CACHE_TTL_HOURS = int(os.environ.get("WARRANTY_CACHE_TTL_HOURS", "24"))

# Vendor API credentials (set via Lambda env vars)
HP_API_KEY = os.environ.get("HP_WARRANTY_API_KEY", "")
HP_API_SECRET = os.environ.get("HP_WARRANTY_API_SECRET", "")
DELL_CLIENT_ID = os.environ.get("DELL_API_CLIENT_ID", "")
DELL_CLIENT_SECRET = os.environ.get("DELL_API_CLIENT_SECRET", "")

VENDOR_MANUAL_URLS = {
    "hp": "https://support.hp.com/check-warranty",
    "dell": "https://www.dell.com/support/home/en-us/product-support/servicetag",
    "lenovo": "https://pcsupport.lenovo.com/warranty-lookup",
    "apple": "https://checkcoverage.apple.com/",
    "microsoft": "https://support.microsoft.com/en-us/surface/check-your-surface-warranty",
}


def run(serial_number: str, vendor: str, device_type: str = "", force_refresh: bool = False) -> dict:
    """
    Check warranty for a device.

    Args:
        serial_number: Device serial number / service tag
        vendor: Vendor name (hp, dell, lenovo, etc.)
        device_type: Optional device type hint
        force_refresh: Skip cache

    Returns:
        Standardized warranty result dict
    """
    serial_number = (serial_number or "").strip().upper()
    vendor = (vendor or "").strip().lower()

    if not serial_number:
        return _error_result("", vendor, "Serial number is required")
    if not vendor:
        return _error_result(serial_number, "", "Vendor is required (hp, dell, lenovo)")

    # Normalize vendor aliases
    vendor = _normalize_vendor(vendor)

    # Check cache
    if not force_refresh:
        cached = _get_cache(vendor, serial_number)
        if cached:
            cached["source"] = "cache"
            return cached

    # Route to vendor-specific lookup
    if vendor == "hp":
        result = _check_hp(serial_number)
    elif vendor == "dell":
        result = _check_dell(serial_number)
    elif vendor == "lenovo":
        result = _check_lenovo(serial_number)
    else:
        result = _fallback_result(serial_number, vendor)

    # Cache the result
    if result.get("valid") or result.get("warrantyStatus") == "NOT_FOUND":
        _put_cache(vendor, serial_number, result)

    return result


def get_supported_vendors() -> list:
    """Return list of supported vendors and their configuration status."""
    return [
        {"id": "hp", "name": "HP / Hewlett-Packard", "apiConfigured": bool(HP_API_KEY), "manualUrl": VENDOR_MANUAL_URLS["hp"]},
        {"id": "dell", "name": "Dell Technologies", "apiConfigured": bool(DELL_CLIENT_ID), "manualUrl": VENDOR_MANUAL_URLS["dell"]},
        {"id": "lenovo", "name": "Lenovo", "apiConfigured": True, "manualUrl": VENDOR_MANUAL_URLS["lenovo"]},
        {"id": "apple", "name": "Apple", "apiConfigured": False, "manualUrl": VENDOR_MANUAL_URLS["apple"]},
        {"id": "microsoft", "name": "Microsoft", "apiConfigured": False, "manualUrl": VENDOR_MANUAL_URLS["microsoft"]},
    ]


# ─── HP ───────────────────────────────────────────────────────────────────────

def _check_hp(serial_number: str) -> dict:
    """Check HP warranty via HP Warranty API."""
    if not HP_API_KEY or not HP_API_SECRET:
        return _fallback_result(serial_number, "hp")

    try:
        # Get token
        token_resp = requests.post(
            "https://warranty.api.hp.com/oauth/v1/token",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={"grant_type": "client_credentials", "client_id": HP_API_KEY, "client_secret": HP_API_SECRET},
            timeout=10,
        )
        if token_resp.status_code != 200:
            return _error_result(serial_number, "HP", "HP API authentication failed")

        token = token_resp.json().get("access_token")

        # Query warranty
        resp = requests.post(
            "https://warranty.api.hp.com/productWarranty/v2",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"serialNumber": [serial_number]},
            timeout=15,
        )

        if resp.status_code == 200:
            return _parse_hp_response(serial_number, resp.json())
        elif resp.status_code == 404:
            return _not_found_result(serial_number, "HP")
        else:
            return _error_result(serial_number, "HP", f"HP API returned HTTP {resp.status_code}")

    except requests.Timeout:
        return _error_result(serial_number, "HP", "HP API request timed out")
    except Exception as e:
        logger.warning("HP warranty check failed: %s", e)
        return _error_result(serial_number, "HP", f"HP API error: {str(e)[:80]}")


def _parse_hp_response(serial_number: str, data: dict) -> dict:
    """Parse HP warranty API response."""
    products = data.get("products", data.get("data", []))
    if not products:
        return _not_found_result(serial_number, "HP")

    product = products[0] if isinstance(products, list) else products
    product_name = product.get("productDescription", product.get("productName", ""))

    coverages = []
    latest_end = None
    earliest_start = None
    service_level = ""

    for ent in product.get("warranties", product.get("entitlements", [])):
        start = _normalize_date(ent.get("startDate", ent.get("warrantyStartDate", "")))
        end = _normalize_date(ent.get("endDate", ent.get("warrantyEndDate", "")))
        if end and (not latest_end or end > latest_end):
            latest_end = end
        if start and (not earliest_start or start < earliest_start):
            earliest_start = start
        if not service_level:
            service_level = ent.get("serviceLevel", "")
        coverages.append({
            "type": ent.get("warrantyType", "Hardware Support"),
            "status": _status_from_date(end),
            "startDate": start,
            "endDate": end,
        })

    return _build_result(serial_number, "HP", product_name, earliest_start, latest_end, service_level, coverages)


# ─── Dell ─────────────────────────────────────────────────────────────────────

def _check_dell(serial_number: str) -> dict:
    """Check Dell warranty via TechDirect API."""
    if not DELL_CLIENT_ID or not DELL_CLIENT_SECRET:
        return _fallback_result(serial_number, "dell")

    try:
        token_resp = requests.post(
            "https://apigw-prod.apiconnect.ibmcloud.com/auth/oauth/v2/token",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={"grant_type": "client_credentials", "client_id": DELL_CLIENT_ID, "client_secret": DELL_CLIENT_SECRET},
            timeout=10,
        )
        if token_resp.status_code != 200:
            return _error_result(serial_number, "Dell", "Dell API authentication failed")

        token = token_resp.json().get("access_token")

        resp = requests.get(
            "https://apigw-prod.apiconnect.ibmcloud.com/PROD/sbil/eapi/v5/asset-entitlements",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            params={"servicetags": serial_number},
            timeout=15,
        )

        if resp.status_code == 200:
            return _parse_dell_response(serial_number, resp.json())
        elif resp.status_code == 404:
            return _not_found_result(serial_number, "Dell")
        else:
            return _error_result(serial_number, "Dell", f"Dell API returned HTTP {resp.status_code}")

    except requests.Timeout:
        return _error_result(serial_number, "Dell", "Dell API request timed out")
    except Exception as e:
        logger.warning("Dell warranty check failed: %s", e)
        return _error_result(serial_number, "Dell", f"Dell API error: {str(e)[:80]}")


def _parse_dell_response(serial_number: str, data: dict) -> dict:
    """Parse Dell warranty API response."""
    assets = data if isinstance(data, list) else data.get("data", data.get("assets", []))
    if not assets:
        return _not_found_result(serial_number, "Dell")

    asset = assets[0] if isinstance(assets, list) else assets
    product_name = asset.get("productLineDescription", asset.get("productDescription", ""))

    coverages = []
    latest_end = None
    earliest_start = None
    service_level = ""

    for ent in asset.get("entitlements", []):
        start = _normalize_date(ent.get("startDate", ""))
        end = _normalize_date(ent.get("endDate", ""))
        if end and (not latest_end or end > latest_end):
            latest_end = end
        if start and (not earliest_start or start < earliest_start):
            earliest_start = start
        if not service_level:
            service_level = ent.get("serviceLevelDescription", "")
        coverages.append({
            "type": ent.get("serviceLevelDescription", "Hardware Support"),
            "status": _status_from_date(end),
            "startDate": start,
            "endDate": end,
        })

    return _build_result(serial_number, "Dell", product_name, earliest_start, latest_end, service_level, coverages)


# ─── Lenovo ───────────────────────────────────────────────────────────────────

def _check_lenovo(serial_number: str) -> dict:
    """Check Lenovo warranty via public support API."""
    try:
        url = f"https://pcsupport.lenovo.com/us/en/api/v4/upsell/redport/getIbaseInfo?serialNumber={serial_number}"
        resp = requests.get(url, headers={"Accept": "application/json", "User-Agent": "AssetGuardian/1.0"}, timeout=15)

        if resp.status_code == 200:
            data = resp.json()
            product_name = data.get("machineType", "") or data.get("productName", "")
            if not product_name and not data.get("warranty"):
                return _not_found_result(serial_number, "Lenovo")

            coverages = []
            latest_end = None
            earliest_start = None
            service_level = ""

            for w in (data.get("warranty", data.get("warranties", [])) if isinstance(data.get("warranty", []), list) else [data.get("warranty", {})]):
                start = _normalize_date(w.get("Start", w.get("startDate", "")))
                end = _normalize_date(w.get("End", w.get("endDate", "")))
                if end and (not latest_end or end > latest_end):
                    latest_end = end
                if start and (not earliest_start or start < earliest_start):
                    earliest_start = start
                if not service_level:
                    service_level = w.get("Type", "")
                coverages.append({
                    "type": w.get("Type", w.get("warrantyType", "Hardware Support")),
                    "status": _status_from_date(end),
                    "startDate": start,
                    "endDate": end,
                })

            if not coverages and data.get("baseWarrantyEnd"):
                end = _normalize_date(data["baseWarrantyEnd"])
                start = _normalize_date(data.get("baseWarrantyStart", ""))
                latest_end = end
                earliest_start = start
                coverages.append({"type": "Base Warranty", "status": _status_from_date(end), "startDate": start, "endDate": end})

            return _build_result(serial_number, "Lenovo", product_name, earliest_start, latest_end, service_level, coverages)
        else:
            return _fallback_result(serial_number, "lenovo")

    except Exception as e:
        logger.warning("Lenovo warranty check failed: %s", e)
        return _fallback_result(serial_number, "lenovo")


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _normalize_vendor(vendor: str) -> str:
    aliases = {"hewlett-packard": "hp", "hewlett packard": "hp", "hp inc": "hp",
               "dell technologies": "dell", "dell inc": "dell",
               "lenovo group": "lenovo", "thinkpad": "lenovo"}
    return aliases.get(vendor, vendor)


def _normalize_date(date_str) -> Optional[str]:
    if not date_str:
        return None
    if isinstance(date_str, (int, float)):
        try:
            return datetime.fromtimestamp(date_str / 1000).strftime("%Y-%m-%d")
        except Exception:
            return None
    date_str = str(date_str).strip()
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%SZ", "%m/%d/%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(date_str[:19], fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def _status_from_date(end_date: Optional[str]) -> str:
    if not end_date:
        return "UNKNOWN"
    try:
        end = datetime.strptime(end_date, "%Y-%m-%d").date()
        return "ACTIVE" if end >= date.today() else "EXPIRED"
    except ValueError:
        return "UNKNOWN"


def _days_remaining(end_date: Optional[str]) -> int:
    if not end_date:
        return 0
    try:
        end = datetime.strptime(end_date, "%Y-%m-%d").date()
        return max(0, (end - date.today()).days)
    except ValueError:
        return 0


def _build_result(serial_number: str, vendor: str, product_name: str,
                  start_date: Optional[str], end_date: Optional[str],
                  service_level: str, coverages: list) -> dict:
    return {
        "valid": True,
        "vendor": vendor,
        "serialNumber": serial_number,
        "productName": product_name,
        "warrantyStatus": _status_from_date(end_date),
        "warrantyStartDate": start_date,
        "warrantyEndDate": end_date,
        "daysRemaining": _days_remaining(end_date),
        "serviceLevel": service_level,
        "coverages": coverages,
        "checkedAt": datetime.utcnow().isoformat() + "Z",
        "source": "vendor_api",
    }


def _error_result(serial_number: str, vendor: str, error: str) -> dict:
    return {
        "valid": False, "vendor": vendor, "serialNumber": serial_number,
        "warrantyStatus": "UNKNOWN", "error": error,
        "checkedAt": datetime.utcnow().isoformat() + "Z", "source": "vendor_api",
    }


def _not_found_result(serial_number: str, vendor: str) -> dict:
    return {
        "valid": False, "vendor": vendor, "serialNumber": serial_number,
        "warrantyStatus": "NOT_FOUND", "error": "Serial number not found in vendor records",
        "checkedAt": datetime.utcnow().isoformat() + "Z", "source": "vendor_api",
    }


def _fallback_result(serial_number: str, vendor: str) -> dict:
    url = VENDOR_MANUAL_URLS.get(vendor, "")
    msg = f"Automated lookup not available for {vendor.title()}."
    if url:
        msg += f" Check manually: {url}"
    return {
        "valid": False, "vendor": vendor.title(), "serialNumber": serial_number,
        "warrantyStatus": "UNKNOWN", "error": msg,
        "checkedAt": datetime.utcnow().isoformat() + "Z", "source": "fallback",
        "manualUrl": url,
    }


# ─── DynamoDB Cache ───────────────────────────────────────────────────────────

def _get_cache(vendor: str, serial_number: str) -> Optional[dict]:
    try:
        table = boto3.resource("dynamodb", region_name=REGION).Table(WARRANTY_CACHE_TABLE)
        resp = table.get_item(Key={"cacheKey": f"{vendor}#{serial_number}"})
        item = resp.get("Item")
        if not item:
            return None
        cached_at = item.get("cachedAt", "")
        if cached_at:
            cached_time = datetime.fromisoformat(cached_at.replace("Z", "+00:00"))
            if (datetime.now(cached_time.tzinfo) - cached_time) > timedelta(hours=CACHE_TTL_HOURS):
                return None
        return json.loads(item.get("resultJson", "{}"))
    except Exception as e:
        logger.debug("Cache get error: %s", e)
        return None


def _put_cache(vendor: str, serial_number: str, result: dict) -> None:
    try:
        table = boto3.resource("dynamodb", region_name=REGION).Table(WARRANTY_CACHE_TABLE)
        table.put_item(Item={
            "cacheKey": f"{vendor}#{serial_number}",
            "vendor": vendor,
            "serialNumber": serial_number,
            "resultJson": json.dumps(result, default=str),
            "cachedAt": datetime.utcnow().isoformat() + "Z",
            "ttl": int((datetime.utcnow() + timedelta(hours=CACHE_TTL_HOURS)).timestamp()),
        })
    except Exception as e:
        logger.debug("Cache put error: %s", e)
