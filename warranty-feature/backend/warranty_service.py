"""
Warranty Service — Main orchestrator.
Routes requests to the appropriate vendor provider, manages caching.
"""

from datetime import datetime
from typing import Optional

from .vendors import get_provider, WarrantyResult
from .cache import WarrantyCache


class WarrantyService:
    """Orchestrates vendor warranty lookups with caching."""

    def __init__(self):
        self.cache = WarrantyCache()

    def check_warranty(
        self,
        serial_number: str,
        vendor: str,
        device_type: str = "",
        force_refresh: bool = False,
    ) -> dict:
        """
        Check warranty for a device.

        Args:
            serial_number: Device serial number / service tag
            vendor: Vendor name (hp, dell, lenovo, etc.)
            device_type: Optional device type hint
            force_refresh: Skip cache and query vendor API directly

        Returns:
            Standardized warranty result dict
        """
        serial_number = serial_number.strip().upper()
        vendor = vendor.strip().lower()

        if not serial_number:
            return {
                "valid": False,
                "error": "Serial number is required",
                "vendor": vendor,
                "serialNumber": serial_number,
                "warrantyStatus": "UNKNOWN",
                "checkedAt": datetime.utcnow().isoformat() + "Z",
            }

        if not vendor:
            return {
                "valid": False,
                "error": "Vendor is required. Supported: hp, dell, lenovo",
                "vendor": "",
                "serialNumber": serial_number,
                "warrantyStatus": "UNKNOWN",
                "checkedAt": datetime.utcnow().isoformat() + "Z",
            }

        # Check cache first (unless force refresh)
        if not force_refresh:
            cached = self.cache.get(vendor, serial_number)
            if cached:
                return cached

        # Get the appropriate vendor provider
        provider = get_provider(vendor)

        # Perform the warranty lookup
        result: WarrantyResult = provider.check_warranty(
            serial_number,
            device_type=device_type,
            vendor=vendor,
        )

        # Convert to dict
        result_dict = result.to_dict()

        # Cache successful results (and NOT_FOUND to avoid repeated lookups)
        if result.valid or result.warranty_status == "NOT_FOUND":
            self.cache.put(vendor, serial_number, result_dict)

        return result_dict

    def batch_check(self, items: list) -> list:
        """
        Check warranty for multiple devices.

        Args:
            items: List of dicts with 'serialNumber' and 'vendor' keys

        Returns:
            List of warranty result dicts
        """
        results = []
        for item in items[:20]:  # Limit to 20 per batch
            serial = item.get("serialNumber", "")
            vendor = item.get("vendor", "")
            device_type = item.get("deviceType", "")
            result = self.check_warranty(serial, vendor, device_type)
            results.append(result)
        return results

    def get_supported_vendors(self) -> list:
        """Return list of supported vendors with their API status."""
        import os
        return [
            {
                "id": "hp",
                "name": "HP / Hewlett-Packard",
                "apiConfigured": bool(os.environ.get("HP_WARRANTY_API_KEY")),
                "manualUrl": "https://support.hp.com/check-warranty",
            },
            {
                "id": "dell",
                "name": "Dell Technologies",
                "apiConfigured": bool(os.environ.get("DELL_API_CLIENT_ID")),
                "manualUrl": "https://www.dell.com/support/home/en-us/product-support/servicetag",
            },
            {
                "id": "lenovo",
                "name": "Lenovo",
                "apiConfigured": True,  # Public API, no credentials needed
                "manualUrl": "https://pcsupport.lenovo.com/warranty-lookup",
            },
            {
                "id": "apple",
                "name": "Apple",
                "apiConfigured": False,
                "manualUrl": "https://checkcoverage.apple.com/",
            },
            {
                "id": "microsoft",
                "name": "Microsoft",
                "apiConfigured": False,
                "manualUrl": "https://support.microsoft.com/en-us/surface/check-your-surface-warranty",
            },
        ]
