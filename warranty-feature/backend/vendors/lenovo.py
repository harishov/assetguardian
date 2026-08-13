"""
Lenovo Warranty API Integration.

Lenovo provides warranty lookup via their support API.
Less restrictive than HP/Dell — no special API credentials required
for basic serial number lookups.

API Flow:
  1. POST serial number to Lenovo warranty endpoint
  2. Parse response for warranty details
"""

import os
import json
import requests
from datetime import datetime
from typing import Optional

from .base import VendorWarrantyProvider, WarrantyResult, WarrantyCoverage


class LenovoWarrantyProvider(VendorWarrantyProvider):
    """Lenovo Warranty API provider."""

    # Lenovo's public warranty check endpoint
    WARRANTY_URL = "https://pcsupport.lenovo.com/us/en/api/v4/upsell/red498/getIbaseInfo"
    # Alternative endpoint for batch/enterprise
    BATCH_URL = "https://supportapi.lenovo.com/v2.5/warranty"

    def __init__(self):
        self.api_key = os.environ.get("LENOVO_WARRANTY_API_KEY", "")

    @property
    def vendor_name(self) -> str:
        return "Lenovo"

    def check_warranty(self, serial_number: str, **kwargs) -> WarrantyResult:
        """Check Lenovo warranty by serial number."""

        # Try the support API endpoint
        try:
            result = self._check_via_support_api(serial_number)
            if result and result.valid:
                return result
        except Exception:
            pass

        # Try alternative endpoint
        try:
            result = self._check_via_batch_api(serial_number)
            if result:
                return result
        except Exception:
            pass

        # Fallback
        return self._fallback_result(serial_number)

    def _check_via_support_api(self, serial_number: str) -> Optional[WarrantyResult]:
        """Check warranty via Lenovo PC Support API."""
        try:
            # Lenovo's public endpoint for warranty lookup
            url = f"https://pcsupport.lenovo.com/us/en/api/v4/upsell/redport/getIbaseInfo?serialNumber={serial_number}"

            response = requests.get(
                url,
                headers={
                    "Accept": "application/json",
                    "User-Agent": "AssetGuardian/1.0",
                },
                timeout=15,
            )

            if response.status_code == 200:
                data = response.json()
                return self._parse_support_response(serial_number, data)

        except requests.Timeout:
            return WarrantyResult(
                valid=False,
                vendor=self.vendor_name,
                serial_number=serial_number,
                warranty_status="UNKNOWN",
                error="Lenovo API request timed out",
                source="vendor_api",
                checked_at=datetime.utcnow().isoformat() + "Z",
            )
        except Exception as e:
            return None

    def _check_via_batch_api(self, serial_number: str) -> Optional[WarrantyResult]:
        """Check warranty via Lenovo batch/enterprise API."""
        if not self.api_key:
            return None

        try:
            response = requests.post(
                self.BATCH_URL,
                headers={
                    "Content-Type": "application/json",
                    "ClientID": self.api_key,
                },
                json={"Serial": serial_number},
                timeout=15,
            )

            if response.status_code == 200:
                data = response.json()
                return self._parse_batch_response(serial_number, data)

        except Exception:
            return None

    def _parse_support_response(self, serial_number: str, data: dict) -> WarrantyResult:
        """Parse Lenovo PC Support API response."""
        try:
            # Extract product info
            product_name = data.get("machineType", "") or data.get("productName", "")
            product_number = data.get("machineModel", "") or data.get("productNumber", "")

            if not product_name and not data.get("warranty"):
                return WarrantyResult(
                    valid=False,
                    vendor=self.vendor_name,
                    serial_number=serial_number,
                    warranty_status="NOT_FOUND",
                    error="Serial number not found in Lenovo records",
                    source="vendor_api",
                    checked_at=datetime.utcnow().isoformat() + "Z",
                )

            # Parse warranty info
            coverages = []
            latest_end_date = None
            earliest_start_date = None
            service_level = ""

            warranties = data.get("warranty", data.get("warranties", []))
            if isinstance(warranties, dict):
                warranties = [warranties]

            for w in warranties:
                start = w.get("Start", w.get("startDate", w.get("warrantyStartDate", "")))
                end = w.get("End", w.get("endDate", w.get("warrantyEndDate", "")))
                w_type = w.get("Type", w.get("warrantyType", w.get("description", "Hardware Support")))

                start_date = self._normalize_date(start)
                end_date = self._normalize_date(end)

                if end_date:
                    if not latest_end_date or end_date > latest_end_date:
                        latest_end_date = end_date
                if start_date:
                    if not earliest_start_date or start_date < earliest_start_date:
                        earliest_start_date = start_date

                if not service_level:
                    service_level = w.get("serviceLevel", w.get("Type", ""))

                cov_status = self._determine_status(end_date)
                coverages.append(WarrantyCoverage(
                    type=w_type,
                    status=cov_status,
                    start_date=start_date,
                    end_date=end_date,
                    description=w.get("Description", ""),
                ))

            # If no warranties parsed, check for BaseWarranty fields
            if not coverages and data.get("baseWarrantyEnd"):
                end_date = self._normalize_date(data["baseWarrantyEnd"])
                start_date = self._normalize_date(data.get("baseWarrantyStart", ""))
                latest_end_date = end_date
                earliest_start_date = start_date
                coverages.append(WarrantyCoverage(
                    type="Base Warranty",
                    status=self._determine_status(end_date),
                    start_date=start_date,
                    end_date=end_date,
                ))

            overall_status = self._determine_status(latest_end_date)
            days_remaining = self._calculate_days_remaining(latest_end_date)

            return WarrantyResult(
                valid=True,
                vendor=self.vendor_name,
                serial_number=serial_number,
                product_name=product_name,
                product_number=product_number,
                warranty_status=overall_status,
                warranty_start_date=earliest_start_date,
                warranty_end_date=latest_end_date,
                days_remaining=days_remaining,
                service_level=service_level,
                coverages=coverages,
                source="vendor_api",
                checked_at=datetime.utcnow().isoformat() + "Z",
            )

        except Exception as e:
            return WarrantyResult(
                valid=False,
                vendor=self.vendor_name,
                serial_number=serial_number,
                warranty_status="UNKNOWN",
                error=f"Failed to parse Lenovo response: {str(e)[:100]}",
                source="vendor_api",
                checked_at=datetime.utcnow().isoformat() + "Z",
            )

    def _parse_batch_response(self, serial_number: str, data: dict) -> WarrantyResult:
        """Parse Lenovo batch API response (similar structure)."""
        return self._parse_support_response(serial_number, data)

    def _normalize_date(self, date_str) -> Optional[str]:
        """Normalize date formats to YYYY-MM-DD."""
        if not date_str:
            return None
        if isinstance(date_str, (int, float)):
            # Epoch timestamp
            try:
                return datetime.fromtimestamp(date_str / 1000).strftime("%Y-%m-%d")
            except Exception:
                return None
        date_str = str(date_str).strip()
        for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%SZ",
                    "%m/%d/%Y", "%d/%m/%Y", "%Y%m%d", "%b %d, %Y"):
            try:
                return datetime.strptime(date_str[:19], fmt).strftime("%Y-%m-%d")
            except ValueError:
                continue
        return None

    def _fallback_result(self, serial_number: str) -> WarrantyResult:
        """Return result pointing to Lenovo's manual warranty check."""
        return WarrantyResult(
            valid=False,
            vendor=self.vendor_name,
            serial_number=serial_number,
            warranty_status="UNKNOWN",
            error="Could not reach Lenovo API. Check manually at https://pcsupport.lenovo.com/warranty-lookup",
            source="fallback",
            checked_at=datetime.utcnow().isoformat() + "Z",
        )
