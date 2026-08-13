"""
Dell TechDirect Warranty API Integration.

Dell provides warranty lookup through TechDirect APIs.
Requires:
  - client_id and client_secret (obtained via TechDirect onboarding)
  - Register at https://tdm.dell.com

API Flow:
  1. OAuth2 client credentials flow to get access token
  2. GET warranty by service tag (serial number)
  3. Parse entitlements
"""

import os
import json
import requests
from datetime import datetime
from typing import Optional

from .base import VendorWarrantyProvider, WarrantyResult, WarrantyCoverage


class DellWarrantyProvider(VendorWarrantyProvider):
    """Dell TechDirect Warranty API provider."""

    TOKEN_URL = "https://apigw-prod.apiconnect.ibmcloud.com/auth/oauth/v2/token"
    WARRANTY_URL = "https://apigw-prod.apiconnect.ibmcloud.com/PROD/sbil/eapi/v5/asset-entitlements"

    def __init__(self):
        self.client_id = os.environ.get("DELL_API_CLIENT_ID", "")
        self.client_secret = os.environ.get("DELL_API_CLIENT_SECRET", "")
        self._token = None

    @property
    def vendor_name(self) -> str:
        return "Dell"

    def _authenticate(self) -> Optional[str]:
        """Get OAuth2 access token from Dell."""
        if not self.client_id or not self.client_secret:
            return None

        try:
            response = requests.post(
                self.TOKEN_URL,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                data={
                    "grant_type": "client_credentials",
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                },
                timeout=10,
            )
            if response.status_code == 200:
                data = response.json()
                self._token = data.get("access_token")
                return self._token
        except Exception as e:
            print(f"Dell auth error: {e}")
        return None

    def check_warranty(self, serial_number: str, **kwargs) -> WarrantyResult:
        """Check Dell warranty by service tag (serial number)."""

        if not self.client_id or not self.client_secret:
            return self._fallback_result(serial_number)

        token = self._authenticate()
        if not token:
            return WarrantyResult(
                valid=False,
                vendor=self.vendor_name,
                serial_number=serial_number,
                warranty_status="UNKNOWN",
                error="Dell API authentication failed. Check client_id/secret.",
                source="vendor_api",
                checked_at=datetime.utcnow().isoformat() + "Z",
            )

        try:
            response = requests.get(
                self.WARRANTY_URL,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/json",
                },
                params={"servicetags": serial_number},
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
                    error="Service tag not found in Dell records",
                    source="vendor_api",
                    checked_at=datetime.utcnow().isoformat() + "Z",
                )
            else:
                return WarrantyResult(
                    valid=False,
                    vendor=self.vendor_name,
                    serial_number=serial_number,
                    warranty_status="UNKNOWN",
                    error=f"Dell API returned HTTP {response.status_code}",
                    source="vendor_api",
                    checked_at=datetime.utcnow().isoformat() + "Z",
                )

        except requests.Timeout:
            return WarrantyResult(
                valid=False,
                vendor=self.vendor_name,
                serial_number=serial_number,
                warranty_status="UNKNOWN",
                error="Dell API request timed out",
                source="vendor_api",
                checked_at=datetime.utcnow().isoformat() + "Z",
            )
        except Exception as e:
            return WarrantyResult(
                valid=False,
                vendor=self.vendor_name,
                serial_number=serial_number,
                warranty_status="UNKNOWN",
                error=f"Dell API error: {str(e)[:100]}",
                source="vendor_api",
                checked_at=datetime.utcnow().isoformat() + "Z",
            )

    def _parse_response(self, serial_number: str, data: dict) -> WarrantyResult:
        """Parse Dell warranty API response."""
        try:
            # Dell returns array of assets
            assets = data if isinstance(data, list) else data.get("data", data.get("assets", []))
            if not assets:
                return WarrantyResult(
                    valid=False,
                    vendor=self.vendor_name,
                    serial_number=serial_number,
                    warranty_status="NOT_FOUND",
                    source="vendor_api",
                    checked_at=datetime.utcnow().isoformat() + "Z",
                )

            asset = assets[0] if isinstance(assets, list) else assets
            product_name = asset.get("productLineDescription", asset.get("productDescription", ""))
            product_number = asset.get("productCode", "")

            # Parse entitlements
            coverages = []
            latest_end_date = None
            earliest_start_date = None
            service_level = ""

            entitlements = asset.get("entitlements", [])
            for ent in entitlements:
                start = ent.get("startDate", "")
                end = ent.get("endDate", "")
                ent_type = ent.get("serviceLevelDescription", ent.get("entitlementType", "Hardware Support"))

                start_date = self._normalize_date(start)
                end_date = self._normalize_date(end)

                if end_date:
                    if not latest_end_date or end_date > latest_end_date:
                        latest_end_date = end_date
                if start_date:
                    if not earliest_start_date or start_date < earliest_start_date:
                        earliest_start_date = start_date

                if not service_level and ent.get("serviceLevelDescription"):
                    service_level = ent["serviceLevelDescription"]

                cov_status = self._determine_status(end_date)
                coverages.append(WarrantyCoverage(
                    type=ent_type,
                    status=cov_status,
                    start_date=start_date,
                    end_date=end_date,
                    description=ent.get("itemNumber", ""),
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
                error=f"Failed to parse Dell response: {str(e)[:100]}",
                source="vendor_api",
                checked_at=datetime.utcnow().isoformat() + "Z",
            )

    def _normalize_date(self, date_str: str) -> Optional[str]:
        """Normalize date formats to YYYY-MM-DD."""
        if not date_str:
            return None
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f",
                    "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d", "%m/%d/%Y"):
            try:
                return datetime.strptime(date_str.strip()[:19], fmt).strftime("%Y-%m-%d")
            except ValueError:
                continue
        return None

    def _fallback_result(self, serial_number: str) -> WarrantyResult:
        """Return a result pointing user to Dell's manual warranty check."""
        return WarrantyResult(
            valid=False,
            vendor=self.vendor_name,
            serial_number=serial_number,
            warranty_status="UNKNOWN",
            error="Dell API credentials not configured. Check manually at https://www.dell.com/support/home/en-us/product-support/servicetag",
            source="fallback",
            checked_at=datetime.utcnow().isoformat() + "Z",
        )
