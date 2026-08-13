"""
Generic/Fallback Warranty Provider.

Used for vendors without API access (Apple, Microsoft, etc.)
Returns a helpful message directing user to manual lookup.
"""

from datetime import datetime
from .base import VendorWarrantyProvider, WarrantyResult


VENDOR_URLS = {
    "apple": "https://checkcoverage.apple.com/",
    "microsoft": "https://support.microsoft.com/en-us/surface/check-your-surface-warranty-fd498e4d-6cd2-e0c3-89d5-38cd0a5a4d44",
    "asus": "https://www.asus.com/support/warranty-status-inquiry/",
    "acer": "https://www.acer.com/ac/en/US/content/warranty-info",
    "samsung": "https://www.samsung.com/us/support/warranty/",
    "toshiba": "https://support.dynabook.com/warranty",
}


class GenericWarrantyProvider(VendorWarrantyProvider):
    """Generic fallback provider for vendors without API access."""

    def __init__(self, vendor: str = "Unknown"):
        self._vendor = vendor

    @property
    def vendor_name(self) -> str:
        return self._vendor.title()

    def check_warranty(self, serial_number: str, **kwargs) -> WarrantyResult:
        """Return fallback result with manual check URL."""
        vendor_lower = kwargs.get("vendor", self._vendor).lower()
        check_url = VENDOR_URLS.get(vendor_lower, "")

        message = f"Automated warranty lookup is not available for {self.vendor_name}."
        if check_url:
            message += f" Please check manually at: {check_url}"
        else:
            message += " Please check the vendor's support website directly."

        return WarrantyResult(
            valid=False,
            vendor=self.vendor_name,
            serial_number=serial_number,
            warranty_status="UNKNOWN",
            error=message,
            source="fallback",
            checked_at=datetime.utcnow().isoformat() + "Z",
        )
