"""
Base class for vendor warranty providers.
All vendor integrations implement this interface.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional, List


@dataclass
class WarrantyCoverage:
    """Individual warranty coverage/entitlement."""
    type: str                    # e.g., "Hardware Support", "Next Business Day"
    status: str                  # ACTIVE, EXPIRED, UNKNOWN
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    description: str = ""


@dataclass
class WarrantyResult:
    """Standardized warranty check result across all vendors."""
    valid: bool                          # Serial number is recognized
    vendor: str                          # HP, Dell, Lenovo, etc.
    serial_number: str
    product_name: str = ""               # e.g., "HP EliteBook 840 G6"
    product_number: str = ""             # Vendor product/model number
    warranty_status: str = "UNKNOWN"     # ACTIVE, EXPIRED, UNKNOWN, NOT_FOUND
    warranty_start_date: Optional[str] = None
    warranty_end_date: Optional[str] = None
    days_remaining: int = 0
    service_level: str = ""              # e.g., "Next Business Day Onsite"
    coverages: List[WarrantyCoverage] = field(default_factory=list)
    checked_at: str = ""
    source: str = "vendor_api"           # vendor_api, cache, manual, fallback
    error: Optional[str] = None

    def to_dict(self) -> dict:
        """Convert to API response dict."""
        result = {
            "valid": self.valid,
            "vendor": self.vendor,
            "serialNumber": self.serial_number,
            "productName": self.product_name,
            "productNumber": self.product_number,
            "warrantyStatus": self.warranty_status,
            "warrantyStartDate": self.warranty_start_date,
            "warrantyEndDate": self.warranty_end_date,
            "daysRemaining": self.days_remaining,
            "serviceLevel": self.service_level,
            "coverages": [
                {
                    "type": c.type,
                    "status": c.status,
                    "startDate": c.start_date,
                    "endDate": c.end_date,
                    "description": c.description,
                }
                for c in self.coverages
            ],
            "checkedAt": self.checked_at or datetime.utcnow().isoformat() + "Z",
            "source": self.source,
        }
        if self.error:
            result["error"] = self.error
        return result


class VendorWarrantyProvider(ABC):
    """Abstract base class for vendor warranty lookups."""

    @property
    @abstractmethod
    def vendor_name(self) -> str:
        """Return the canonical vendor name."""
        pass

    @abstractmethod
    def check_warranty(self, serial_number: str, **kwargs) -> WarrantyResult:
        """
        Check warranty status for a given serial number.
        Returns a standardized WarrantyResult.
        """
        pass

    def _calculate_days_remaining(self, end_date_str: Optional[str]) -> int:
        """Calculate days remaining from an end date string (YYYY-MM-DD)."""
        if not end_date_str:
            return 0
        try:
            end = datetime.strptime(end_date_str, "%Y-%m-%d").date()
            delta = (end - date.today()).days
            return max(0, delta)
        except (ValueError, TypeError):
            return 0

    def _determine_status(self, end_date_str: Optional[str]) -> str:
        """Determine warranty status from end date."""
        if not end_date_str:
            return "UNKNOWN"
        days = self._calculate_days_remaining(end_date_str)
        if days > 0:
            return "ACTIVE"
        return "EXPIRED"
