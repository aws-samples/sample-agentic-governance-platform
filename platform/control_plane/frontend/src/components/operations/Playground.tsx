// Decision: docs/adr/frontend-loading-state.md
import { useCallback, useEffect, useMemo, useRef, useState, type JSX } from 'react';

import { ComingSoonBanner } from '../shared/comingSoon';
import OpsPage from './OpsPage';
import { OPS_CARD, OPS_BADGE, OPS_PRIMARY_BTN } from './opsUi';
import { MODELS, estimateCost, type ModelOption } from './demoData';

const DEFAULT_PROMPT = 'Summarize this claim and flag any fraud signals';

const COMPARE_MODEL_IDS = ['claude-opus-4-8', 'gemini-2-5-pro', 'gpt-5'] as const;

type Phase = 'idle' | 'streaming' | 'done';

const STREAM_STEP_MS = 18;
const STREAM_CHARS_PER_TICK = 3;
const COLUMN_STAGGER_MS = [0, 280, 560];

const SHARED_INPUT_TOKENS = 1180;
const OUTPUT_TOKENS_BY_ID: Record<string, number> = {
  'claude-opus-4-8': 612,
  'gemini-2-5-pro': 548,
  'gpt-5': 583,
};

const RESPONSE_BY_ID: Record<string, string> = {
  'claude-opus-4-8':
    'Summary: First-notice auto claim, single-vehicle collision reported 9 days after the loss date. Claimant requests full repair plus a rental allowance.\n\nFraud signals (2 flagged):\n• Late reporting — the 9-day gap exceeds the policy norm and warrants a recorded statement.\n• Coverage timing — the comprehensive rider was added 6 days before the loss; cross-check the binding date against the incident report.\n\nRecommendation: Route to the SIU desk for a light-touch review before settlement. No hard stop — the documentation is otherwise consistent.',
  'gemini-2-5-pro':
    'Claim summary: auto collision, 1 vehicle, reported late. Repair + rental requested.\n\nFraud flags:\n- Reporting delay (9 days)\n- Recent rider addition (6 days pre-loss)\n- Estimate slightly above book value\n\nDisposition: refer for adjuster review. Confidence: medium.',
  'gpt-5':
    "Here's the rundown. The claimant filed a single-vehicle auto claim a little over a week after the incident, asking for repairs and a rental car while the vehicle is in the shop.\n\nA couple of things stand out as worth a second look. The claim came in later than we'd typically expect, and the coverage that would pay for this was added just days before the loss occurred — that combination is a common pattern we keep an eye on. The repair estimate also runs a touch high relative to the vehicle's value.\n\nNothing here is conclusive, but I'd suggest a quick adjuster review before approving payment.",
};

const COMPARE_MODELS: ModelOption[] = COMPARE_MODEL_IDS
  .map((id) => MODELS.find((m) => m.id === id))
  .filter((m): m is ModelOption => Boolean(m));

function PlayGlyph({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 16 16" fill="currentColor" aria-hidden="true" className={className}>
      <path d="M4.5 3.3a.8.8 0 0 1 1.22-.68l7 4.7a.8.8 0 0 1 0 1.36l-7 4.7A.8.8 0 0 1 4.5 12.7V3.3Z" />
    </svg>
  );
}

function ProviderGlyph({ provider, className }: { provider: ModelOption['provider']; className?: string }) {
  switch (provider) {
    case 'Anthropic':
      return (
        <svg viewBox="0 0 20 20" fill="currentColor" aria-hidden="true" className={className}>
          <path d="M7.4 3h2.6l4.6 14h-2.8l-1-3.1H7.1l-1 3.1H3.3L7.4 3Zm-.4 8.5h3.2L8.6 6.4 7 11.5Z" />
        </svg>
      );
    case 'Google':
      return (
        <svg viewBox="0 0 20 20" fill="currentColor" aria-hidden="true" className={className}>
          <path d="M10 2.2a7.8 7.8 0 1 0 5.3 13.5l-2-1.9A4.9 4.9 0 1 1 10 5.1c1.2 0 2.3.45 3.1 1.2l1.95-1.95A7.75 7.75 0 0 0 10 2.2Zm7.6 6.4h-7.2v2.9h4.1a3.6 3.6 0 0 1-1.3 2l2 1.9a7.7 7.7 0 0 0 2.4-5.8c0-.35-.03-.68-.08-1Z" />
        </svg>
      );
    case 'OpenAI':
      return (
        <svg viewBox="0 0 20 20" fill="currentColor" aria-hidden="true" className={className}>
          <path d="M10 2.4a4 4 0 0 1 3.5 2.05 4 4 0 0 1 2.2 6.3 4 4 0 0 1-3.5 5.95 4 4 0 0 1-6.9-1.05 4 4 0 0 1-2.2-6.3A4 4 0 0 1 6.6 3.45 4 4 0 0 1 10 2.4Zm0 3-2.9 1.66v3.86L10 12.6l2.9-1.68V7.06L10 5.4Z" />
        </svg>
      );
  }
}

export default function Playground(): JSX.Element {
  const [prompt, setPrompt] = useState(DEFAULT_PROMPT);

  const [hasRun, setHasRun] = useState(false);
  const [revealed, setRevealed] = useState<Record<string, number>>(() =>
    Object.fromEntries(COMPARE_MODELS.map((m) => [m.id, 0])),
  );

  const timers = useRef<ReturnType<typeof setTimeout>[]>([]);
  const intervals = useRef<ReturnType<typeof setInterval>[]>([]);
  const clearTimers = useCallback(() => {
    timers.current.forEach(clearTimeout);
    intervals.current.forEach(clearInterval);
    timers.current = [];
    intervals.current = [];
  }, []);
  useEffect(() => clearTimers, [clearTimers]);

  const fullLengths = useMemo(
    () => Object.fromEntries(COMPARE_MODELS.map((m) => [m.id, RESPONSE_BY_ID[m.id]?.length ?? 0])),
    [],
  );

  const allDone = COMPARE_MODELS.every((m) => revealed[m.id] >= fullLengths[m.id]);
  const phase: Phase = !hasRun ? 'idle' : allDone ? 'done' : 'streaming';

  const onRun = useCallback(() => {
    clearTimers();
    setRevealed(Object.fromEntries(COMPARE_MODELS.map((m) => [m.id, 0])));
    setHasRun(true);

    COMPARE_MODELS.forEach((model, i) => {
      const target = RESPONSE_BY_ID[model.id]?.length ?? 0;
      const startDelay = COLUMN_STAGGER_MS[i] ?? 0;

      const startTimer = setTimeout(() => {
        const id = setInterval(() => {
          setRevealed((prev) => {
            const current = prev[model.id] ?? 0;
            if (current >= target) return prev;
            const next = Math.min(current + STREAM_CHARS_PER_TICK, target);
            return { ...prev, [model.id]: next };
          });
        }, STREAM_STEP_MS);
        intervals.current.push(id);
      }, startDelay);
      timers.current.push(startTimer);
    });
  }, [clearTimers]);

  return (
    <OpsPage
      backTo="/ops"
      title="Playground"
      subtitle="Send one prompt to Claude, Gemini, and GPT side-by-side — and watch the tokens and cost diverge in real time."
    >
      <ComingSoonBanner />

      <div className={`${OPS_CARD} p-5 mb-6`}>
        <label htmlFor="pg-prompt" className="block text-[11px] font-medium text-slate-500 uppercase tracking-wide mb-2">
          Prompt
        </label>
        <textarea
          id="pg-prompt"
          value={prompt}
          onChange={(e) => setPrompt(e.target.value)}
          rows={2}
          className="w-full resize-none rounded-lg border border-emerald-200/70 bg-white/70 px-3.5 py-2.5 text-sm text-slate-800 placeholder:text-slate-400 focus:outline-none focus:ring-2 focus:ring-emerald-400/50 focus:border-emerald-400 transition-shadow"
          placeholder="Ask the same thing of every model…"
        />

        <div className="mt-4 flex flex-wrap items-center justify-between gap-3">
          <p className="text-xs text-slate-500">
            Same prompt, {COMPARE_MODELS.length} models. Token spend and price differ per model.
          </p>
          <button
            type="button"
            onClick={onRun}
            disabled={!prompt.trim()}
            className={`${OPS_PRIMARY_BTN} inline-flex items-center gap-1.5 disabled:opacity-60 disabled:cursor-not-allowed`}
          >
            <PlayGlyph className="h-3.5 w-3.5" />
            {phase === 'idle' ? 'Run' : phase === 'streaming' ? 'Running…' : 'Re-run'}
          </button>
        </div>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4 items-start">
        {COMPARE_MODELS.map((model) => (
          <ModelColumn
            key={model.id}
            model={model}
            text={RESPONSE_BY_ID[model.id] ?? ''}
            revealed={revealed[model.id] ?? 0}
            hasRun={hasRun}
            outputTokens={OUTPUT_TOKENS_BY_ID[model.id] ?? 0}
          />
        ))}
      </div>
    </OpsPage>
  );
}

function ModelColumn({
  model,
  text,
  revealed,
  hasRun,
  outputTokens,
}: {
  model: ModelOption;
  text: string;
  revealed: number;
  hasRun: boolean;
  outputTokens: number;
}): JSX.Element {
  const visible = text.slice(0, revealed);
  const streaming = hasRun && revealed < text.length;
  const done = hasRun && revealed >= text.length;

  const cost = estimateCost(model, SHARED_INPUT_TOKENS, outputTokens);
  const totalTokens = SHARED_INPUT_TOKENS + outputTokens;

  return (
    <div className={`${OPS_CARD} flex flex-col overflow-hidden`}>
      <div className="flex items-center justify-between gap-3 px-4 py-3 border-b border-emerald-100/70">
        <div className="flex items-center gap-2.5 min-w-0">
          <span
            aria-hidden="true"
            className="shrink-0 h-8 w-8 rounded-lg bg-emerald-50 text-emerald-700 flex items-center justify-center"
          >
            <ProviderGlyph provider={model.provider} className="h-4.5 w-4.5" />
          </span>
          <div className="min-w-0">
            <div className="text-sm font-semibold text-slate-900 leading-tight truncate">{model.label}</div>
            <div className="text-[11px] text-slate-400 leading-tight">{model.provider}</div>
          </div>
        </div>
        {streaming ? (
          <span
            className={`inline-flex items-center gap-1.5 text-[11px] font-semibold px-2 py-0.5 rounded-full shrink-0 ${OPS_BADGE.running}`}
          >
            <TypingDots />
            Streaming
          </span>
        ) : done ? (
          <span
            className={`inline-flex items-center gap-1.5 text-[11px] font-semibold px-2 py-0.5 rounded-full shrink-0 ${OPS_BADGE.ok}`}
          >
            <span aria-hidden="true">●</span>
            Done
          </span>
        ) : null}
      </div>

      <div className="px-4 py-3.5 min-h-[224px] text-sm text-slate-700 leading-relaxed">
        {hasRun ? (
          <p className="whitespace-pre-wrap">
            {visible}
            {streaming && (
              <span aria-hidden="true" className="inline-block w-[2px] h-[1.05em] -mb-[0.18em] ml-px bg-emerald-500 animate-pulse" />
            )}
          </p>
        ) : (
          <p className="text-slate-400 italic">Run the prompt to stream this model's response.</p>
        )}
      </div>

      <div className="mt-auto grid grid-cols-2 divide-x divide-emerald-100/70 border-t border-emerald-100/70 bg-white/50">
        <Readout label="Tokens" value={totalTokens.toLocaleString()} hint={`${SHARED_INPUT_TOKENS.toLocaleString()} in · ${outputTokens.toLocaleString()} out`} />
        <Readout label="Cost / run" value={`$${cost.toFixed(4)}`} hint={`$${model.outputPer1k.toFixed(3)}/1k out`} />
      </div>
    </div>
  );
}

function Readout({ label, value, hint }: { label: string; value: string; hint: string }): JSX.Element {
  return (
    <div className="px-4 py-3">
      <div className="text-[10px] font-medium text-slate-400 uppercase tracking-wide">{label}</div>
      <div className="text-base font-semibold text-slate-900 tabular-nums mt-0.5">{value}</div>
      <div className="text-[10px] text-slate-400 tabular-nums mt-0.5">{hint}</div>
    </div>
  );
}

function TypingDots(): JSX.Element {
  return (
    <span aria-hidden="true" className="inline-flex items-center gap-0.5">
      {[0, 1, 2].map((i) => (
        <span
          key={i}
          className="h-1 w-1 rounded-full bg-current animate-pulse"
          style={{ animationDelay: `${i * 160}ms` }}
        />
      ))}
    </span>
  );
}
