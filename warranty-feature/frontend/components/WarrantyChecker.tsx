/**
 * WarrantyChecker — Main warranty lookup component
 * Allows users to enter a serial number and vendor to check warranty status.
 */

import React, { useState, useEffect } from 'react';
import { checkWarranty, getSupportedVendors, type WarrantyResult, type VendorInfo, type WarrantyCheckRequest } from '../warranty-api';
import { WarrantyResultCard } from './WarrantyResult';

export function WarrantyChecker() {
  const [serialNumber, setSerialNumber] = useState('');
  const [vendor, setVendor] = useState('');
  const [deviceType, setDeviceType] = useState('');
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState<WarrantyResult | null>(null);
  const [error, setError] = useState('');
  const [vendors, setVendors] = useState<VendorInfo[]>([]);
  const [history, setHistory] = useState<WarrantyResult[]>([]);

  // Load supported vendors on mount
  useEffect(() => {
    loadVendors();
  }, []);

  async function loadVendors() {
    try {
      const data = await getSupportedVendors();
      setVendors(data);
    } catch {
      // Fallback vendor list if API unavailable
      setVendors([
        { id: 'hp', name: 'HP / Hewlett-Packard', apiConfigured: false, manualUrl: 'https://support.hp.com/check-warranty' },
        { id: 'dell', name: 'Dell Technologies', apiConfigured: false, manualUrl: 'https://www.dell.com/support/home' },
        { id: 'lenovo', name: 'Lenovo', apiConfigured: true, manualUrl: 'https://pcsupport.lenovo.com/warranty-lookup' },
        { id: 'apple', name: 'Apple', apiConfigured: false, manualUrl: 'https://checkcoverage.apple.com/' },
        { id: 'microsoft', name: 'Microsoft', apiConfigured: false, manualUrl: 'https://support.microsoft.com' },
      ]);
    }
  }

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!serialNumber.trim() || !vendor) return;

    setLoading(true);
    setError('');
    setResult(null);

    try {
      const request: WarrantyCheckRequest = {
        serialNumber: serialNumber.trim(),
        vendor,
        deviceType: deviceType || undefined,
      };
      const data = await checkWarranty(request);
      setResult(data);
      // Add to history (most recent first, max 10)
      setHistory(prev => [data, ...prev.filter(h => h.serialNumber !== data.serialNumber)].slice(0, 10));
    } catch (err: any) {
      setError(err.message || 'Warranty check failed');
    } finally {
      setLoading(false);
    }
  }

  const selectedVendor = vendors.find(v => v.id === vendor);

  return (
    <div className="max-w-4xl mx-auto p-6">
      {/* Header */}
      <div className="mb-6">
        <h1 className="text-2xl font-bold text-gray-900">Warranty Verification</h1>
        <p className="text-sm text-gray-500 mt-1">
          Check device warranty status against vendor support systems
        </p>
      </div>

      {/* Lookup Form */}
      <form onSubmit={handleSubmit} className="bg-white rounded-xl shadow-sm border border-gray-200 p-6 mb-6">
        <div className="grid grid-cols-1 md:grid-cols-3 gap-4 mb-4">
          {/* Vendor Select */}
          <div>
            <label htmlFor="vendor" className="block text-sm font-medium text-gray-700 mb-1">
              Vendor *
            </label>
            <select
              id="vendor"
              value={vendor}
              onChange={(e) => setVendor(e.target.value)}
              className="w-full border border-gray-300 rounded-lg px-3 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
              required
              aria-label="Select device vendor"
            >
              <option value="">Select vendor...</option>
              {vendors.map(v => (
                <option key={v.id} value={v.id}>
                  {v.name} {v.apiConfigured ? '(API)' : '(Manual)'}
                </option>
              ))}
            </select>
          </div>

          {/* Serial Number */}
          <div>
            <label htmlFor="serialNumber" className="block text-sm font-medium text-gray-700 mb-1">
              Serial Number / Service Tag *
            </label>
            <input
              id="serialNumber"
              type="text"
              value={serialNumber}
              onChange={(e) => setSerialNumber(e.target.value.toUpperCase())}
              placeholder="e.g., 5CG01523C7"
              className="w-full border border-gray-300 rounded-lg px-3 py-2.5 text-sm font-mono focus:outline-none focus:ring-2 focus:ring-blue-500"
              required
              aria-label="Device serial number"
            />
          </div>

          {/* Device Type (optional) */}
          <div>
            <label htmlFor="deviceType" className="block text-sm font-medium text-gray-700 mb-1">
              Device Type (optional)
            </label>
            <select
              id="deviceType"
              value={deviceType}
              onChange={(e) => setDeviceType(e.target.value)}
              className="w-full border border-gray-300 rounded-lg px-3 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
              aria-label="Device type"
            >
              <option value="">Any</option>
              <option value="laptop">Laptop</option>
              <option value="desktop">Desktop</option>
              <option value="monitor">Monitor</option>
              <option value="tablet">Tablet</option>
              <option value="phone">Phone</option>
              <option value="printer">Printer</option>
              <option value="server">Server</option>
            </select>
          </div>
        </div>

        {/* API status hint */}
        {selectedVendor && !selectedVendor.apiConfigured && (
          <div className="mb-4 p-3 bg-amber-50 border border-amber-200 rounded-lg">
            <p className="text-xs text-amber-700">
              <span className="font-medium">Note:</span> Automated API lookup is not configured for {selectedVendor.name}.
              You can check manually at:{' '}
              <a href={selectedVendor.manualUrl} target="_blank" rel="noopener noreferrer" className="underline">
                {selectedVendor.manualUrl}
              </a>
            </p>
          </div>
        )}

        {/* Submit */}
        <button
          type="submit"
          disabled={loading || !serialNumber.trim() || !vendor}
          className="w-full md:w-auto bg-blue-600 hover:bg-blue-700 disabled:bg-gray-300 text-white font-medium rounded-lg px-6 py-2.5 text-sm transition-colors"
          aria-label="Check warranty"
        >
          {loading ? (
            <span className="flex items-center gap-2">
              <svg className="animate-spin h-4 w-4" viewBox="0 0 24 24">
                <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" fill="none" />
                <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
              </svg>
              Checking...
            </span>
          ) : (
            'Check Warranty'
          )}
        </button>
      </form>

      {/* Error */}
      {error && (
        <div className="mb-6 p-4 bg-red-50 border border-red-200 rounded-lg" role="alert">
          <p className="text-sm text-red-700 font-medium">Error</p>
          <p className="text-sm text-red-600 mt-1">{error}</p>
        </div>
      )}

      {/* Result */}
      {result && <WarrantyResultCard result={result} />}

      {/* History */}
      {history.length > 0 && !result && (
        <div className="mt-6">
          <h2 className="text-lg font-semibold text-gray-700 mb-3">Recent Lookups</h2>
          <div className="space-y-2">
            {history.map((h, i) => (
              <div
                key={`${h.serialNumber}-${i}`}
                className="flex items-center justify-between p-3 bg-white rounded-lg border border-gray-100 hover:border-gray-200 cursor-pointer"
                onClick={() => { setSerialNumber(h.serialNumber); setVendor(h.vendor.toLowerCase()); setResult(h); }}
                role="button"
                tabIndex={0}
                aria-label={`View ${h.serialNumber} result`}
              >
                <div className="flex items-center gap-3">
                  <span className={`w-2.5 h-2.5 rounded-full ${
                    h.warrantyStatus === 'ACTIVE' ? 'bg-green-500' :
                    h.warrantyStatus === 'EXPIRED' ? 'bg-red-500' : 'bg-gray-400'
                  }`} />
                  <div>
                    <p className="text-sm font-medium text-gray-800">{h.serialNumber}</p>
                    <p className="text-xs text-gray-500">{h.vendor} — {h.productName || 'Unknown'}</p>
                  </div>
                </div>
                <span className={`text-xs font-medium px-2 py-0.5 rounded-full ${
                  h.warrantyStatus === 'ACTIVE' ? 'bg-green-100 text-green-700' :
                  h.warrantyStatus === 'EXPIRED' ? 'bg-red-100 text-red-700' : 'bg-gray-100 text-gray-600'
                }`}>
                  {h.warrantyStatus}
                </span>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
