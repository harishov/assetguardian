/**
 * Warranty Verification API Client
 * Integrates with the AssetGuardian warranty Lambda
 */

// Use the same API base as the rest of the app
const API_BASE = import.meta.env.VITE_API_URL || '';

export interface WarrantyCoverage {
  type: string;
  status: string;
  startDate: string | null;
  endDate: string | null;
  description: string;
}

export interface WarrantyResult {
  valid: boolean;
  vendor: string;
  serialNumber: string;
  productName: string;
  productNumber: string;
  warrantyStatus: 'ACTIVE' | 'EXPIRED' | 'UNKNOWN' | 'NOT_FOUND';
  warrantyStartDate: string | null;
  warrantyEndDate: string | null;
  daysRemaining: number;
  serviceLevel: string;
  coverages: WarrantyCoverage[];
  checkedAt: string;
  source: 'vendor_api' | 'cache' | 'fallback' | 'manual';
  error?: string;
}

export interface VendorInfo {
  id: string;
  name: string;
  apiConfigured: boolean;
  manualUrl: string;
}

export interface WarrantyCheckRequest {
  serialNumber: string;
  vendor: string;
  deviceType?: string;
  forceRefresh?: boolean;
}

/**
 * Check warranty for a single device
 */
export async function checkWarranty(request: WarrantyCheckRequest): Promise<WarrantyResult> {
  const response = await fetch(`${API_BASE}/api/warranty/check`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'Authorization': `Bearer ${getToken()}`,
    },
    body: JSON.stringify(request),
  });

  if (!response.ok) {
    const error = await response.json().catch(() => ({ error: `HTTP ${response.status}` }));
    throw new Error(error.error || `Warranty check failed (${response.status})`);
  }

  return response.json();
}

/**
 * Check warranty for multiple devices (batch)
 */
export async function batchCheckWarranty(
  items: Array<{ serialNumber: string; vendor: string; deviceType?: string }>
): Promise<{ results: WarrantyResult[]; count: number }> {
  const response = await fetch(`${API_BASE}/api/warranty/batch`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'Authorization': `Bearer ${getToken()}`,
    },
    body: JSON.stringify({ items }),
  });

  if (!response.ok) {
    throw new Error(`Batch warranty check failed (${response.status})`);
  }

  return response.json();
}

/**
 * Get list of supported vendors
 */
export async function getSupportedVendors(): Promise<VendorInfo[]> {
  const response = await fetch(`${API_BASE}/api/warranty/vendors`, {
    headers: {
      'Authorization': `Bearer ${getToken()}`,
    },
  });

  if (!response.ok) {
    throw new Error(`Failed to fetch vendors (${response.status})`);
  }

  const data = await response.json();
  return data.vendors;
}

/**
 * Get auth token from storage (integrate with existing auth)
 */
function getToken(): string {
  return localStorage.getItem('assetguardian_token') || sessionStorage.getItem('idToken') || '';
}
