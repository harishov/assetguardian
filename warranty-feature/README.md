# AssetGuardian - Vendor Warranty Verification Feature

## Overview
Multi-vendor warranty verification that checks device serial numbers against vendor support APIs to retrieve warranty status, support end dates, and serial number validity.

## Supported Vendors
- **HP** — HP Warranty API (enterprise key required)
- **Dell** — TechDirect API (OAuth2 client credentials)
- **Lenovo** — Warranty Lookup API (serial-based query)
- **Apple** — Stub (no public API; manual only)
- **Microsoft** — Stub (no public API; manual only)

## Project Structure
```
warranty-feature/
├── backend/
│   ├── handler.py              # Lambda handler (API entry point)
│   ├── warranty_service.py     # Main service orchestrator
│   ├── vendors/
│   │   ├── __init__.py
│   │   ├── base.py             # Abstract vendor interface
│   │   ├── hp.py               # HP Warranty API integration
│   │   ├── dell.py             # Dell TechDirect API integration
│   │   ├── lenovo.py           # Lenovo Warranty API integration
│   │   └── generic.py          # Fallback/manual lookup
│   ├── cache.py                # DynamoDB cache layer
│   └── requirements.txt
├── frontend/
│   ├── components/
│   │   ├── WarrantyChecker.tsx  # Main warranty lookup component
│   │   └── WarrantyResult.tsx   # Result display component
│   └── warranty-api.ts          # API client
├── infra/
│   ├── deploy-warranty.sh       # AWS deployment script
│   └── warranty-table.json      # DynamoDB table definition
├── backup/                      # Backup copies before integration
└── README.md
```

## Backup / Revert Strategy
- All new code is isolated in `warranty-feature/`
- Existing AssetGuardian code is NOT modified
- To integrate: copy files into main project
- To revert: remove the integrated files

## API Endpoint
```
POST /api/warranty/check
{
  "serialNumber": "5CG01523C7",
  "vendor": "hp",           // hp | dell | lenovo | apple | microsoft
  "deviceType": "laptop"    // optional
}

Response:
{
  "valid": true,
  "vendor": "HP",
  "serialNumber": "5CG01523C7",
  "productName": "HP EliteBook 840 G6",
  "warrantyStatus": "ACTIVE",
  "warrantyStartDate": "2023-03-15",
  "warrantyEndDate": "2026-03-15",
  "daysRemaining": 215,
  "serviceLevel": "Next Business Day Onsite",
  "coverages": [...],
  "checkedAt": "2026-08-12T15:30:00Z",
  "source": "vendor_api"    // vendor_api | cache | manual
}
```

## Prerequisites
1. HP: Email warrantyapi.customers@hp.com for API key
2. Dell: Register at TechDirect, complete API onboarding
3. Lenovo: No special access needed (public warranty lookup)
