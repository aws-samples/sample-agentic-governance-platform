// Model Catalog — every model available on the platform (Epic 18, Task 11). A
// polished, STATIC supporting page: a card grid over the shared MODELS seed
// (Task 3) showing each model's provider, per-1k input/output pricing, the
// regions it's served in, and a one-line capability blurb. No state, no timers,
// no backend — it just renders the data nicely.
//
// House style: emerald-on-glass Ops tokens (opsUi.ts), Tailwind v4 utility
// strings, 2-space indent — mirrors the Templates.tsx card-grid idiom.

import { type JSX } from 'react';

import { ComingSoonBanner } from '../shared/comingSoon';
import OpsPage from './OpsPage';
import { OPS_CARD } from './opsUi';
import { MODELS, type ModelOption } from './demoData';

// ───────────────────────────── content maps ─────────────────────────────

/**
 * Provider pill colors — one per vendor. Distinct from OPS_BADGE (which is
 * keyed by health/status); these give each provider its own brand-ish tint
 * while staying within the Ops palette.
 */
const PROVIDER_PILL: Record<ModelOption['provider'], string> = {
  Anthropic: 'bg-orange-50 text-orange-700',
  Google: 'bg-sky-50 text-sky-700',
  OpenAI: 'bg-emerald-50 text-emerald-700',
};

/** One-line capability blurb per model id. */
const CAPABILITY: Record<string, string> = {
  'claude-opus-4-8':
    'Frontier reasoning and long-horizon agentic workflows — the default for complex tool-using triage chains.',
  'gemini-2-5-pro':
    'Long-context multimodal model — strong on large document bundles and mixed text/image claim attachments.',
  'gpt-5':
    'General-purpose workhorse with broad tool support — a cost-effective default for high-volume classification.',
};

/** Regions each model is served in — drawn from the same region values used across the demo (TENANTS / Experiments / Access Keys). */
const REGIONS: Record<string, string[]> = {
  'claude-opus-4-8': ['us-east-1', 'eu-west-1'],
  'gemini-2-5-pro': ['us-east-1', 'us-west-2'],
  'gpt-5': ['us-east-1'],
};

// ───────────────────────────── helpers ─────────────────────────────

/**
 * Format a per-1k-token price as currency. Pricing here runs from $0.001 to a
 * few cents, so show enough precision to keep sub-cent values legible (up to 5
 * fraction digits, trailing zeros trimmed).
 */
function fmtPrice(per1k: number): string {
  return `$${per1k.toLocaleString('en-US', { minimumFractionDigits: 3, maximumFractionDigits: 5 })}`;
}

// ───────────────────────────── the page ─────────────────────────────

export default function ModelCatalog(): JSX.Element {
  return (
    <OpsPage
      backTo="/ops"
      title="Model Catalog"
      subtitle="Every model available on the platform, with regions and pricing."
    >
      <ComingSoonBanner />

      <p className="text-sm text-slate-500 mb-6">
        {MODELS.length} {MODELS.length === 1 ? 'model' : 'models'} across{' '}
        {new Set(MODELS.map((m) => m.provider)).size} providers · per-1K-token pricing
      </p>

      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
        {MODELS.map((m) => (
          <ModelCard key={m.id} model={m} />
        ))}
      </div>
    </OpsPage>
  );
}

// ───────────────────────────── sub-components ─────────────────────────────

function ModelCard({ model }: { model: ModelOption }): JSX.Element {
  const regions = REGIONS[model.id] ?? [];
  return (
    <div className={`${OPS_CARD} p-5 flex flex-col`}>
      <div className="flex items-start justify-between gap-3">
        <h2 className="text-base font-semibold text-slate-900">{model.label}</h2>
        <span
          className={`text-[11px] font-semibold px-2 py-0.5 rounded-full shrink-0 ${PROVIDER_PILL[model.provider]}`}
        >
          {model.provider}
        </span>
      </div>

      <p className="text-sm text-slate-500 mt-2 flex-1">{CAPABILITY[model.id] ?? ''}</p>

      <div className="mt-4 flex flex-wrap gap-1.5">
        {regions.map((r) => (
          <span
            key={r}
            className="inline-flex items-center text-[11px] font-medium px-2 py-0.5 rounded-full bg-emerald-50/70 text-emerald-700 font-mono"
          >
            {r}
          </span>
        ))}
      </div>

      <div className="mt-4 pt-3 border-t border-emerald-100/70 text-xs text-slate-500 tabular-nums">
        <span className="font-medium text-slate-700">{fmtPrice(model.inputPer1k)}</span> / 1K in ·{' '}
        <span className="font-medium text-slate-700">{fmtPrice(model.outputPer1k)}</span> / 1K out
      </div>
    </div>
  );
}
