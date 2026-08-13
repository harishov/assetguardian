"""
HP Warranty API Integration.

HP provides an enterprise Warranty API at https://warranty.api.hp.com
Requires:
  - API Key and API Secret (obtained from HP Developer Portal)
  - Request access via warrantyapi.customers@hp.com

API Flow:
  1. Authenticate with API key/secret to get bearer token
  2. POST serial number to warranty endpoint
  3. Parse response for warranty entitlements
"""

import os
import json
import requests
from datetime import datetime
from typing import Optional

from .base import VendorWarrantyProvider, WarrantyResult, WarrantyCoverage


class HPWarrantyProvider(VendorWarrantyProvider):
    """HP Warranty API provider."""

    API_BASE = "https://warranty.api.hp.com"
    TOKEN_URL = f"{API_BASE}/oauth/v1/token"
    WARRANTY_URL = f"{API_BASE}/productWarranty/v2"

    def __init__(self):
        self.api_key = os.environ.get("HP_WARRANTY_API_KEY", "")
        self.api_secret = os.environ.get("HP_WARRANTY_API_SECRET", "")
        self._token = None
        self._token_expiry = None

    @property
    def vendor_name(self) -> str:
        return "HP"

    def _authenticate(self) -> Optional[str]:
        """Get OAuth bearer token from HP."""
        if not self.api_key or not self.api_secret:
            return None

        try:
            response = requests.post(
                self.TOKEN_URL,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                data={
                    "grant_type": "client_credentials",
                    "client_id": self.api_key,
                    "client_secret": self.api_secret,
                },
                timeout=10,
            )
            if response.status_code == 200:
                data = response.json()
                self._token = data.get("access_token")
                return self._token
        except Exception as e:
            print(f"HP auth error: {e}")
        return None

    def check_warranty(self, serial_number: str, **kwargs) -> WarrantyResult:
        """Check HP warranty by serial number."""

        # If no API credentials, return fallback with HP support URL
        if not self.api_key or not self.api_secret:
            return self._fallback_result(serial_number)

        # Authenticate
        token = self._authenticate()
        if not token:
            return WarrantyResult(
                valid=False,
                vendor=self.vendor_name,
                serial_number=serial_number,
                warranty_status="UNKNOWN",
                error="HP API authentication failed. Check API key/secret.",
                source="vendor_api",
                checked_at=datetime.utcnow().isoformat() + "Z",
            )

        # Query warranty
        try:
            response = requests.post(
                self.WARRANTY_URL,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
                json={
                    "serialNumber": [serial_number],
                },
                timeout=15,
            )

            if response.status_code == 200:
                return self._parse_response(serial_number, response.json())
            elif response.status_code == 404:
                return WarrantyResult(
                    valid=False,
                    vendor=self.vendor_name,
                    serial_number=serial_number,
                    warranty_status="NOT_FOUND",
                    error="Serial number not found in HP records",
                    source="vendor_api",
                    checked_at=datetime.utcnow().isoformat() + "Z",
                )
            else:
                return WarrantyResult(
                    valid=False,
                    vendor=self.vendor_name,
                    serial_number=serial_number,
                    warranty_status="UNKNOWN",
                    error=f"HP API returned HTTP {response.status_code}",
                    source="vendor_api",
                    checked_at=datetime.utcnow().isoformat() + "Z",
                )

        except requests.Timeout:
            return WarrantyResult(
                valid=False,
                vendor=self.vendor_name,
                serial_number=serial_number,
                warranty_status="UNKNOWN",
                error="HP API request timed out",
                source="vendor_api",
                checked_at=datetime.utcnow().isoformat() + "Z",
            )
        except Exception as e:
            return WarrantyResult(
                valid=False,
                vendor=self.vendor_name,
                serial_number=serial_number,
                warranty_status="UNKNOWN",
                error=f"HP API error: {str(e)[:100]}",
                source="vendor_api",
                checked_at=datetime.utcnow().isoformat() + "Z",
            )

    def _parse_response(self, serial_number: str, data: dict) -> WarrantyResult:
        """Parse HP warranty API response into standardized result."""
        try:
            # HP returns array of products
            products = data.get("products", data.get("data", []))
            if not products:
                return WarrantyResult(
                    valid=False,
                    vendor=self.vendor_name,
                    serial_number=serial_number,
                    warranty_status="NOT_FOUND",
                    source="vendor_api",
                    checked_at=datetime.utcnow().isoformat() + "Z",
                )

            product = products[0] if isinstance(products, list) else products
            product_name = product.get("productDescription", product.get("productName", ""))
            product_number = product.get("productNumber", "")

            # Parse entitlements/warranties
            coverages = []
            latest_end_date = None
            earliest_start_date = None
            service_level = ""

            entitlements = product.get("warranties", product.get("entitlements", []))
            for ent in entitlements:
                start = ent.get("startDate", ent.get("warrantyStartDate", ""))
                end = ent.get("endDate", ent.get("warrantyEndDate", ""))
                ent_type = ent.get("warrantyType", ent.get("type", "Hardware Support"))
                status = ent.get("status", "")

                # Normalize dates to YYYY-MM-DD
                start_date = self._normalize_date(start)
                end_date = self._normalize_date(end)

                if end_date:
                    if not latest_end_date or end_date > latest_end_date:
                        latest_end_date = end_date
                if start_date:
                    if not earliest_start_date or start_date < earliest_start_date:
                        earliest_start_date = start_date

                if not service_level and ent.get("serviceLevel"):
                    service_level = ent["serviceLevel"]

                cov_status = self._determine_status(end_date) if end_date else (status or "UNKNOWN")
                coverages.append(WarrantyCoverage(
                    type=ent_type,
                    status=cov_status,
                    start_date=start_date,
                    end_date=end_date,
                    description=ent.get("description", ""),
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
                error=f"Failed to parse HP response: {str(e)[:100]}",
                source="vendor_api",
                checked_at=datetime.utcnow().isoformat() + "Z",
            )

    def _normalize_date(self, date_str: str) -> Optional[str]:
        """Normalize various date formats to YYYY-MM-DD."""
        if not date_str:
            return None
        for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%SZ",
                    "%m/%d/%Y", "%d/%m/%Y", "%Y%m%d"):
            try:
                return datetime.strptime(date_str.strip()[:19], fmt).strftime("%Y-%m-%d")
            except ValueError:
                continue
        return None

    def _fallback_result(self, serial_number: str) -> WarrantyResult:
        """Return a result pointing user to HP's manual warranty check."""
        return WarrantyResult(
            valid=False,
            vendor=self.vendor_name,
            serial_number=serial_number,
            warranty_status="UNKNOWN",
            error="HP API credentials not configured. Please check manually at https://support.hp.com/check-warranty",
            source="fallback",
            checked_at=datetime.utcnow().isoformat() + "Z",
        )
