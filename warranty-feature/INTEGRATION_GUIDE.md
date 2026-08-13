# Warranty Verification — Integration Guide

## Quick Start

### 1. Deploy Backend (AWS CloudShell)

```bash
# Upload the warranty-feature folder to CloudShell, then:
cd warranty-feature
chmod +x infra/deploy-warranty.sh
./infra/deploy-warranty.sh
```

This creates:
- Lambda function: `assetguardian-warranty-checker`
- DynamoDB table: `assetguardian-warranty-cache` (with TTL)
- API Gateway routes: `/api/warranty/check`, `/api/warranty/batch`, `/api/warranty/vendors`

### 2. Integrate Frontend

Copy the frontend files into your existing AssetGuardian frontend:

```bash
# From the assetguardian-ai project root:
cp warranty-feature/frontend/warranty-api.ts frontend/src/lib/warranty-api.ts
cp warranty-feature/frontend/components/WarrantyChecker.tsx frontend/src/pages/warranty.tsx
cp warranty-feature/frontend/components/WarrantyResult.tsx frontend/src/components/WarrantyResult.tsx
```

Then add the route to your App.tsx:
```tsx
import { WarrantyChecker } from './pages/warranty';

// In your routes:
<Route path="/warranty" element={<WarrantyChecker />} />
```

Add navigation link:
```tsx
<NavLink to="/warranty">Warranty Check</NavLink>
```

### 3. Configure Vendor API Keys

Set Lambda environment variables:

```bash
aws lambda update-function-configuration \
  --function-name assetguardian-warranty-checker \
  --environment "Variables={
    WARRANTY_CACHE_TABLE=assetguardian-warranty-cache,
    HP_WARRANTY_API_KEY=your-hp-key,
    HP_WARRANTY_API_SECRET=your-hp-secret,
    DELL_API_CLIENT_ID=your-dell-client-id,
    DELL_API_CLIENT_SECRET=your-dell-secret,
    LENOVO_WARRANTY_API_KEY=optional
  }" \
  --region ap-southeast-1
```

---

## Vendor API Access

### HP (Enterprise Warranty API)

| Item | Detail |
|------|--------|
| Portal | https://developers.hp.com/hp-warranty-api |
| Contact | warrantyapi.customers@hp.com |
| Requires | HP Account Manager email |
| Keys | API Key + API Secret |
| Key Expiry | 90 days (request custom) |
| Rate Limit | ~100 req/min |

**Steps:**
1. Email warrantyapi.customers@hp.com with your HP Account Manager's email
2. Wait for approval (1-3 business days)
3. Login to HP Developer Portal
4. Copy API Key and Secret
5. Set as Lambda env vars: `HP_WARRANTY_API_KEY`, `HP_WARRANTY_API_SECRET`

### Dell (TechDirect API)

| Item | Detail |
|------|--------|
| Portal | https://tdm.dell.com |
| Onboarding | TechDirect API request form |
| Keys | client_id + client_secret (OAuth2) |
| Auth | Client Credentials flow |
| Rate Limit | ~50 req/min |

**Steps:**
1. Register at https://tdm.dell.com
2. Go to Services > API Onboarding
3. Complete the request form
4. Wait for approval (3-5 business days)
5. Receive client_id and client_secret
6. Set as Lambda env vars: `DELL_API_CLIENT_ID`, `DELL_API_CLIENT_SECRET`

### Lenovo (Public Warranty Lookup)

| Item | Detail |
|------|--------|
| Endpoint | https://pcsupport.lenovo.com/warranty-lookup |
| Auth | None required (public) |
| Batch | Up to 1000 serial numbers |
| Rate Limit | Generous (public service) |

**Steps:**
- No configuration needed — works out of the box
- Optional: Set `LENOVO_WARRANTY_API_KEY` for enterprise batch endpoint

---

## API Reference

### POST /api/warranty/check

Check warranty for a single device.

**Request:**
```json
{
  "serialNumber": "5CG01523C7",
  "vendor": "hp",
  "deviceType": "laptop",
  "forceRefresh": false
}
```

**Response (success):**
```json
{
  "valid": true,
  "vendor": "HP",
  "serialNumber": "5CG01523C7",
  "productName": "HP EliteBook 840 G6",
  "productNumber": "7KK26UT",
  "warrantyStatus": "ACTIVE",
  "warrantyStartDate": "2023-03-15",
  "warrantyEndDate": "2026-03-15",
  "daysRemaining": 215,
  "serviceLevel": "Next Business Day Onsite",
  "coverages": [
    {
      "type": "Hardware Support",
      "status": "ACTIVE",
      "startDate": "2023-03-15",
      "endDate": "2026-03-15",
      "description": "3 Year NBD Onsite"
    }
  ],
  "checkedAt": "2026-08-12T15:30:00Z",
  "source": "vendor_api"
}
```

**Response (not found):**
```json
{
  "valid": false,
  "vendor": "HP",
  "serialNumber": "INVALID123",
  "warrantyStatus": "NOT_FOUND",
  "error": "Serial number not found in HP records",
  "checkedAt": "2026-08-12T15:30:00Z",
  "source": "vendor_api"
}
```

### POST /api/warranty/batch

Check warranty for up to 20 devices.

**Request:**
```json
{
  "items": [
    { "serialNumber": "5CG01523C7", "vendor": "hp" },
    { "serialNumber": "ABCDEF1", "vendor": "dell" },
    { "serialNumber": "PF1234XY", "vendor": "lenovo" }
  ]
}
```

**Response:**
```json
{
  "results": [...],
  "count": 3
}
```

### GET /api/warranty/vendors

List supported vendors and their configuration status.

**Response:**
```json
{
  "vendors": [
    { "id": "hp", "name": "HP / Hewlett-Packard", "apiConfigured": true, "manualUrl": "..." },
    { "id": "dell", "name": "Dell Technologies", "apiConfigured": false, "manualUrl": "..." },
    { "id": "lenovo", "name": "Lenovo", "apiConfigured": true, "manualUrl": "..." }
  ]
}
```

---

## Caching

- Results are cached in DynamoDB for 24 hours (configurable via `WARRANTY_CACHE_TTL_HOURS`)
- Cache key: `{vendor}#{serialNumber}`
- TTL auto-deletes expired entries
- Use `forceRefresh: true` to bypass cache

---

## Testing

### Test Lambda locally (CloudShell):
```bash
aws lambda invoke \
  --function-name assetguardian-warranty-checker \
  --payload '{"httpMethod":"POST","path":"/api/warranty/check","body":"{\"serialNumber\":\"5CG01523C7\",\"vendor\":\"hp\"}"}' \
  /tmp/out.json \
  --region ap-southeast-1 && cat /tmp/out.json | python3 -m json.tool
```

### Test vendors endpoint:
```bash
aws lambda invoke \
  --function-name assetguardian-warranty-checker \
  --payload '{"httpMethod":"GET","path":"/api/warranty/vendors"}' \
  /tmp/out.json \
  --region ap-southeast-1 && cat /tmp/out.json | python3 -m json.tool
```

---

## Revert / Rollback

This feature is fully isolated. To remove:

```bash
# Remove Lambda
aws lambda delete-function --function-name assetguardian-warranty-checker --region ap-southeast-1

# Remove DynamoDB table
aws dynamodb delete-table --table-name assetguardian-warranty-cache --region ap-southeast-1

# Remove API routes (if added)
# Get route IDs and delete them from API Gateway

# Remove frontend files
rm frontend/src/lib/warranty-api.ts
rm frontend/src/pages/warranty.tsx
rm frontend/src/components/WarrantyResult.tsx
# Remove the route from App.tsx
```

---

## Integration with Inspection Flow

To auto-check warranty during device inspection, add this to the inspection Lambda:

```python
from warranty_service import WarrantyService

# During inspection, after device is identified:
warranty_svc = WarrantyService()
warranty_result = warranty_svc.check_warranty(
    serial_number=device_serial,
    vendor=device_vendor,
)

# Include in inspection result:
inspection_result["warrantyStatus"] = warranty_result
```

This adds warranty info to the inspection result without blocking the main flow.
