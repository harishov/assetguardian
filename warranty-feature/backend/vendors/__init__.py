from .base import VendorWarrantyProvider, WarrantyResult
from .hp import HPWarrantyProvider
from .dell import DellWarrantyProvider
from .lenovo import LenovoWarrantyProvider
from .generic import GenericWarrantyProvider

VENDOR_PROVIDERS = {
    "hp": HPWarrantyProvider,
    "dell": DellWarrantyProvider,
    "lenovo": LenovoWarrantyProvider,
    "apple": GenericWarrantyProvider,
    "microsoft": GenericWarrantyProvider,
}


def get_provider(vendor: str) -> VendorWarrantyProvider:
    """Get the appropriate warranty provider for a vendor."""
    vendor_lower = vendor.lower().strip()
    # Normalize common aliases
    aliases = {
        "hewlett-packard": "hp",
        "hewlett packard": "hp",
        "hp inc": "hp",
        "dell technologies": "dell",
        "dell inc": "dell",
        "lenovo group": "lenovo",
        "thinkpad": "lenovo",
        "surface": "microsoft",
        "macbook": "apple",
        "ipad": "apple",
    }
    vendor_key = aliases.get(vendor_lower, vendor_lower)
    provider_class = VENDOR_PROVIDERS.get(vendor_key, GenericWarrantyProvider)
    return provider_class()
