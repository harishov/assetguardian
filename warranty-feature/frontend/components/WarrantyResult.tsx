/**
 * WarrantyResultCard — Displays warranty check results
 * Shows warranty status, dates, coverages, and visual indicators.
 */

import React from 'react';
import type { WarrantyResult } from '../warranty-api';

interface Props {
  result: WarrantyResult;
}

export function WarrantyResultCard({ result }: Props) {
  const statusConfig = {
    ACTIVE: { color: 'green', icon: '✅', label: 'Active', bg: 'bg-green-50 border-green-200' },
    EXPIRED: { color: 'red', icon: '❌', label: 'Expired', bg: 'bg-red-50 border-red-200' },
    NOT_FOUND: { color: 'gray', icon: '❓', label: 'Not Found', bg: 'bg-gray-50 border-gray-200' },
    UNKNOWN: { color: 'amber', icon: '⚠️', label: 'Unknown', bg: 'bg-amber-50 border-amber-200' },
  };

  const status = statusConfig[result.warrantyStatus] || statusConfig.UNKNOWN;

  return (
    <div className={`rounded-xl border-2 ${status.bg} p-6 mb-6`} role="region" aria-label="Warranty check result">
      {/* Header */}
      <div className="flex items-start justify-between mb-4">
        <div>
          <div className="flex items-center gap-2 mb-1">
            <span className="text-2xl">{status.icon}</span>
            <h2 className="text-xl font-bold text-gray-900">
              Warranty {status.label}
            </h2>
          </div>
          {result.productName && (
            <p className="text-sm text-gray-600">{result.productName}</p>
          )}
        </div>
        <div className="text-right">
          <span className={`inline-block px-3 py-1 rounded-full text-sm font-semibold
            ${result.warrantyStatus === 'ACTIVE' ? 'bg-green-100 text-green-800' :
              result.warrantyStatus === 'EXPIRED' ? 'bg-red-100 text-red-800' :
              'bg-gray-100 text-gray-700'}`}>
            {result.warrantyStatus}
          </span>
          {result.daysRemaining > 0 && (
            <p className="text-xs text-gray-500 mt-1">{result.daysRemaining} days remaining</p>
          )}
        </div>
      </div>

      {/* Error message */}
      {result.error && (
        <div className="mb-4 p-3 bg-white/70 rounded-lg border border-gray-200">
          <p className="text-sm text-gray-700">{result.error}</p>
        </div>
      )}

      {/* Details Grid */}
      {result.valid && (
        <>
          <div className="grid grid-cols-2 md:grid-cols-4 gap-4 mb-4">
            <DetailItem label="Vendor" value={result.vendor} />
            <DetailItem label="Serial Number" value={result.serialNumber} mono />
            <DetailItem label="Warranty Start" value={formatDate(result.warrantyStartDate)} />
            <DetailItem label="Warranty End" value={formatDate(result.warrantyEndDate)} highlight={result.warrantyStatus === 'ACTIVE'} />
          </div>

          {result.serviceLevel && (
            <div className="mb-4 p-3 bg-white/70 rounded-lg">
              <p className="text-xs text-gray-500">Service Level</p>
              <p className="text-sm font-medium text-gray-800">{result.serviceLevel}</p>
            </div>
          )}

          {/* Coverages */}
          {result.coverages.length > 0 && (
            <div className="mt-4">
              <h3 className="text-sm font-semibold text-gray-700 mb-2">Coverage Details</h3>
              <div className="space-y-2">
                {result.coverages.map((cov, i) => (
                  <div key={i} className="flex items-center justify-between p-2.5 bg-white/80 rounded-lg border border-gray-100">
                    <div className="flex items-center gap-2">
                      <span className={`w-2 h-2 rounded-full ${
                        cov.status === 'ACTIVE' ? 'bg-green-500' :
                        cov.status === 'EXPIRED' ? 'bg-red-400' : 'bg-gray-400'
                      }`} />
                      <div>
                        <p className="text-sm font-medium text-gray-700">{cov.type}</p>
                        {cov.description && <p className="text-xs text-gray-500">{cov.description}</p>}
                      </div>
                    </div>
                    <div className="text-right">
                      <p className="text-xs text-gray-500">
                        {cov.startDate && formatDate(cov.startDate)} — {cov.endDate && formatDate(cov.endDate)}
                      </p>
                      <span className={`text-xs font-medium ${
                        cov.status === 'ACTIVE' ? 'text-green-600' :
                        cov.status === 'EXPIRED' ? 'text-red-600' : 'text-gray-500'
                      }`}>
                        {cov.status}
                      </span>
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* Days remaining progress bar (for active warranties) */}
          {result.warrantyStatus === 'ACTIVE' && result.warrantyStartDate && result.warrantyEndDate && (
            <div className="mt-4">
              <WarrantyProgressBar
                startDate={result.warrantyStartDate}
                endDate={result.warrantyEndDate}
                daysRemaining={result.daysRemaining}
              />
            </div>
          )}
        </>
      )}

      {/* Footer */}
      <div className="mt-4 pt-3 border-t border-gray-200/50 flex items-center justify-between">
        <p className="text-xs text-gray-400">
          Source: {result.source} | Checked: {formatDateTime(result.checkedAt)}
        </p>
        {result.productNumber && (
          <p className="text-xs text-gray-400">Product: {result.productNumber}</p>
        )}
      </div>
    </div>
  );
}

function DetailItem({ label, value, mono, highlight }: { label: string; value: string; mono?: boolean; highlight?: boolean }) {
  return (
    <div>
      <p className="text-xs text-gray-500">{label}</p>
      <p className={`text-sm font-medium ${highlight ? 'text-green-700' : 'text-gray-800'} ${mono ? 'font-mono' : ''}`}>
        {value || '—'}
      </p>
    </div>
  );
}

function WarrantyProgressBar({ startDate, endDate, daysRemaining }: { startDate: string; endDate: string; daysRemaining: number }) {
  const start = new Date(startDate).getTime();
  const end = new Date(endDate).getTime();
  const now = Date.now();
  const total = end - start;
  const elapsed = now - start;
  const progress = Math.min(100, Math.max(0, (elapsed / total) * 100));

  return (
    <div>
      <div className="flex justify-between text-xs text-gray-500 mb-1">
        <span>{formatDate(startDate)}</span>
        <span>{daysRemaining} days left</span>
        <span>{formatDate(endDate)}</span>
      </div>
      <div className="h-2 bg-gray-200 rounded-full overflow-hidden">
        <div
          className={`h-full rounded-full transition-all ${
            daysRemaining > 90 ? 'bg-green-500' :
            daysRemaining > 30 ? 'bg-amber-500' : 'bg-red-500'
          }`}
          style={{ width: `${progress}%` }}
          role="progressbar"
          aria-valuenow={Math.round(progress)}
          aria-valuemin={0}
          aria-valuemax={100}
          aria-label={`Warranty ${Math.round(progress)}% elapsed`}
        />
      </div>
    </div>
  );
}

function formatDate(dateStr: string | null): string {
  if (!dateStr) return '—';
  try {
    return new Date(dateStr).toLocaleDateString('en-SG', { day: '2-digit', month: 'short', year: 'numeric' });
  } catch {
    return dateStr;
  }
}

function formatDateTime(dateStr: string): string {
  if (!dateStr) return '';
  try {
    return new Date(dateStr).toLocaleString('en-SG', { day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit' });
  } catch {
    return dateStr;
  }
}
