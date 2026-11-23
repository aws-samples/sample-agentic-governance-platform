import { useState, type JSX } from 'react';

import { maskArn } from '../shared/arnDisplay';
import { NO_VALUE } from './opsLabels';

export const FIELD_LABEL = 'block text-[11px] uppercase tracking-wide text-slate-400 font-medium mb-1';

export function CopyField({ label, value }: { label: string; value?: string | null }): JSX.Element {
  return (
    <Field label={label}>
      {value ? (
        <span className="flex items-center gap-1.5">
          <code className="text-xs font-mono text-slate-700 break-all" title={value}>{maskArn(value)}</code>
          <CopyButton value={value} label={label} />
        </span>
      ) : (
        NO_VALUE
      )}
    </Field>
  );
}

function CopyButton({ value, label }: { value: string; label: string }): JSX.Element {
  const [copied, setCopied] = useState(false);
  const onCopy = async () => {
    try {
      await navigator.clipboard.writeText(value);
    } catch {
      return;
    }
    setCopied(true);
    window.setTimeout(() => setCopied(false), 1500);
  };
  return (
    <button
      type="button"
      onClick={() => void onCopy()}
      aria-label={copied ? `Copied ${label}` : `Copy ${label}`}
      className="shrink-0 inline-flex items-center justify-center h-6 w-6 rounded-md text-slate-400 hover:text-slate-700 hover:bg-slate-100 transition-colors"
    >
      {copied ? (
        <svg viewBox="0 0 20 20" fill="none" className="h-3.5 w-3.5" aria-hidden="true">
          <path d="M5 10.5l3.5 3.5L15 6" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
      ) : (
        <svg viewBox="0 0 20 20" fill="none" className="h-3.5 w-3.5" aria-hidden="true">
          <rect x="7" y="7" width="9" height="9" rx="1.5" stroke="currentColor" strokeWidth="1.5" />
          <path d="M13 4.5H5.5A1.5 1.5 0 004 6v7.5" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
        </svg>
      )}
    </button>
  );
}

export function Field({ label, children }: { label: string; children: React.ReactNode }): JSX.Element {
  return (
    <div>
      <dt className={FIELD_LABEL}>{label}</dt>
      <dd className="text-sm text-slate-700">{children}</dd>
    </div>
  );
}

export function HeaderName({ text }: { text: string | null }): JSX.Element {
  if (text !== null) return <>{text}</>;
  return (
    <span
      className="inline-block align-middle h-3.5 w-24 rounded bg-slate-200/80 animate-pulse"
      aria-label="Loading name"
      role="status"
    />
  );
}
