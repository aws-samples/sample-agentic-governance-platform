// Playground — the multi-model compare surface (Epic 18, Task 9). A supporting
// beat that proves parity with Maria's world: type one prompt, hit Run, and watch
// the SAME claim get answered side-by-side by Claude Opus 4.8 / Gemini / GPT, with
// canned responses STREAMING in (incremental character reveal on a timer) and a
// per-column tokens + $ readout underneath. Because each model carries different
// per-1k pricing (demoData.MODELS), the costs land visibly different — that's the
// whole point of the compare.
//
// The seed models + the cost helper are Task 3 (demoData.ts — import only, no
// re-implementing the cost math); the page frame + emerald-on-glass tokens are
// Task 1 (OpsPage / opsUi.ts). This file is only the React composition + the
// staged streaming interaction on top of those. No backend, no api/client.
//
// Streaming state machine (StrictMode-safe — mirrors the ref-tracked-timer idiom
// Studio.tsx / Experiments.tsx use; this epic already paid for the lesson in T6):
//   idle → streaming → done
//   • On Run: cancel every in-flight timer, reset all reveal counts to 0, phase
//     'streaming'. Each column starts after a STAGGERED setTimeout so they begin —
//     and therefore finish — at different times (visually "live", not lockstep).
//   • Each column's reveal is driven by its own setInterval whose updater is PURE
//     (it only bumps the revealed-char count via a functional update — no stale
//     closures, no side-effects inside the updater). The interval clears itself
//     and drops out of the ref-tracked set once the column is fully revealed.
//   • When the last column finishes, a watcher effect (NOT the updater) flips the
//     phase to 'done' — side-effects live in effects, exactly as T6 established.
//   • Every timer id (stagger setTimeouts + per-column setIntervals) is tracked in
//     a ref and cleared on unmount AND on every re-run, so clicking Run again
//     cancels any in-flight stream and restarts cleanly with no late beats.
//
// House style: emerald-on-glass Ops tokens (opsUi.ts), inline-SVG glyphs (no icon
// lib), Tailwind v4 utility strings, 2-space indent — matching the other Ops pages.

import { useCallback, useEffect, useMemo, useRef, useState, type JSX } from 'react';

import { ComingSoonBanner } from '../shared/comingSoon';
import OpsPage from './OpsPage';
import { OPS_CARD, OPS_BADGE, OPS_PRIMARY_BTN } from './opsUi';
import { MODELS, estimateCost, type ModelOption } from './demoData';

// ───────────────────────────── constants ─────────────────────────────

const DEFAULT_PROMPT = 'Summarize this claim and flag any fraud signals';

// The three models we put head-to-head (Claude Opus 4.8 / Gemini / GPT). Pulled
// from MODELS by id so labels, providers, and pricing stay the single source of
// truth — if MODELS changes, this compare follows it.
const COMPARE_MODEL_IDS = ['claude-opus-4-8', 'gemini-2-5-pro', 'gpt-5'] as const;

// Phase machine that drives the whole compare spectacle.
type Phase = 'idle' | 'streaming' | 'done';

// Reveal cadence (ms). The per-column STAGGER offsets the start so the three
// streams begin — and finish — at different moments; the STEP is the per-tick
// character advance interval.
const STREAM_STEP_MS = 18; // gap between reveal ticks
const STREAM_CHARS_PER_TICK = 3; // characters revealed per tick (smooth but brisk)
const COLUMN_STAGGER_MS = [0, 280, 560]; // staggered start per column index

// Per-model demo metrics. The output token count is fixed + realistic per model
// (different models are "chattier" → different token spend); the input token
// count is shared (same prompt to every model). These feed estimateCost so the $
// readout differs per model from BOTH pricing and token volume.
const SHARED_INPUT_TOKENS = 1180; // the claim context + prompt, tokenized
const OUTPUT_TOKENS_BY_ID: Record<string, number> = {
  'claude-opus-4-8': 612,
  'gemini-2-5-pro': 548,
  'gpt-5': 583,
};

// Canned per-model responses — deliberately DIFFERENT in voice + structure so the
// side-by-side reads like a real compare (Claude: structured + cautious; Gemini:
// terse + bulleted; GPT: narrative). These are the only "content" in the file.
const RESPONSE_BY_ID: Record<string, string> = {
  'claude-opus-4-8':
    'Summary: First-notice auto claim, single-vehicle collision reported 9 days after the loss date. Claimant requests full repair plus a rental allowance.\n\nFraud signals (2 flagged):\n• Late reporting — the 9-day gap exceeds the policy norm and warrants a recorded statement.\n• Coverage timing — the comprehensive rider was added 6 days before the loss; cross-check the binding date against the incident report.\n\nRecommendation: Route to the SIU desk for a light-touch review before settlement. No hard stop — the documentation is otherwise consistent.',
  'gemini-2-5-pro':
    'Claim summary: auto collision, 1 vehicle, reported late. Repair + rental requested.\n\nFraud flags:\n- Reporting delay (9 days)\n- Recent rider addition (6 days pre-loss)\n- Estimate slightly above book value\n\nDisposition: refer for adjuster review. Confidence: medium.',
  'gpt-5':
    "Here's the rundown. The claimant filed a single-vehicle auto claim a little over a week after the incident, asking for repairs and a rental car while the vehicle is in the shop.\n\nA couple of things stand out as worth a second look. The claim came in later than we'd typically expect, and the coverage that would pay for this was added just days before the loss occurred — that combination is a common pattern we keep an eye on. The repair estimate also runs a touch high relative to the vehicle's value.\n\nNothing here is conclusive, but I'd suggest a quick adjuster review before approving payment.",
};

// Resolve the compare models from MODELS (ordered by COMPARE_MODEL_IDS). Computed
// once — MODELS is a module constant.
const COMPARE_MODELS: ModelOption[] = COMPARE_MODEL_IDS
  .map((id) => MODELS.find((m) => m.id === id))
  .filter((m): m is ModelOption => Boolean(m));

// ───────────────────────────── inline glyphs (no icon lib) ─────────────────────────────

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
    default:
      return (
        <svg viewBox="0 0 20 20" fill="currentColor" aria-hidden="true" className={className}>
          <path d="M10 2.4a4 4 0 0 1 3.5 2.05 4 4 0 0 1 2.2 6.3 4 4 0 0 1-3.5 5.95 4 4 0 0 1-6.9-1.05 4 4 0 0 1-2.2-6.3A4 4 0 0 1 6.6 3.45 4 4 0 0 1 10 2.4Zm0 3-2.9 1.66v3.86L10 12.6l2.9-1.68V7.06L10 5.4Z" />
        </svg>
      );
  }
}

// ───────────────────────────── the page ─────────────────────────────

export default function Playground(): JSX.Element {
  // --- input --------------------------------------------------------------
  const [prompt, setPrompt] = useState(DEFAULT_PROMPT);

  // --- streaming state ----------------------------------------------------
  const [phase, setPhase] = useState<Phase>('idle');
  // Revealed-character count per model id. The visible substring is derived
  // (response.slice(0, revealed[id])) so the timers only ever touch this number —
  // the source string never changes.
  const [revealed, setRevealed] = useState<Record<string, number>>(() =>
    Object.fromEntries(COMPARE_MODELS.map((m) => [m.id, 0])),
  );

  // Every pending timer (stagger setTimeouts + per-column setIntervals) lives here
  // so we can cancel ALL of them on unmount OR on a fresh Run — no late beats from
  // a previous stream once the user re-runs. Mirrors Studio/Experiments' idiom.
  const timers = useRef<ReturnType<typeof setTimeout>[]>([]);
  const intervals = useRef<ReturnType<typeof setInterval>[]>([]);
  const clearTimers = useCallback(() => {
    timers.current.forEach(clearTimeout);
    intervals.current.forEach(clearInterval);
    timers.current = [];
    intervals.current = [];
  }, []);
  // Cancel everything in flight on unmount.
  useEffect(() => clearTimers, [clearTimers]);

  // Full-text lengths per model — the reveal targets.
  const fullLengths = useMemo(
    () => Object.fromEntries(COMPARE_MODELS.map((m) => [m.id, RESPONSE_BY_ID[m.id]?.length ?? 0])),
    [],
  );

  // Completion watcher: when EVERY column has revealed its full string, flip to
  // 'done'. Side-effect lives in an effect (NOT in a setState updater) so it fires
  // cleanly under StrictMode's double-invoked updaters — the exact lesson T6 paid
  // for. Guard on phase so it only fires while a stream is actually in flight.
  useEffect(() => {
    if (phase !== 'streaming') return;
    const allDone = COMPARE_MODELS.every((m) => revealed[m.id] >= fullLengths[m.id]);
    if (allDone) setPhase('done');
  }, [phase, revealed, fullLengths]);

  // --- the staged stream --------------------------------------------------
  const onRun = useCallback(() => {
    // Re-running cancels any in-flight stream first, then restarts from zero.
    clearTimers();
    setRevealed(Object.fromEntries(COMPARE_MODELS.map((m) => [m.id, 0])));
    setPhase('streaming');

    COMPARE_MODELS.forEach((model, i) => {
      const target = RESPONSE_BY_ID[model.id]?.length ?? 0;
      const startDelay = COLUMN_STAGGER_MS[i] ?? 0;

      // Stagger each column's START so the three streams are visibly out of step.
      const startTimer = setTimeout(() => {
        const id = setInterval(() => {
          // PURE updater — only advance this column's count. No closures over
          // stale state, no side-effects (completion is handled by the effect).
          setRevealed((prev) => {
            const current = prev[model.id] ?? 0;
            if (current >= target) return prev; // already full — leave untouched
            const next = Math.min(current + STREAM_CHARS_PER_TICK, target);
            return { ...prev, [model.id]: next };
          });
        }, STREAM_STEP_MS);
        intervals.current.push(id);
      }, startDelay);
      timers.current.push(startTimer);
    });
  }, [clearTimers]);

  const hasRun = phase !== 'idle';

  return (
    <OpsPage
      backTo="/ops"
      title="Playground"
      subtitle="Send one prompt to Claude, Gemini, and GPT side-by-side — and watch the tokens and cost diverge in real time."
    >
      <ComingSoonBanner />

      {/* ── Prompt + Run ──────────────────────────────────────────────── */}
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

      {/* ── Model columns ─────────────────────────────────────────────── */}
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

// ───────────────────────────── sub-components ─────────────────────────────

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

  // Cost via the shared helper — never re-implement the math here. Input tokens are
  // shared (same prompt); output tokens are the per-model demo figure. Different
  // per-1k pricing × different output volume → a visibly different $ per column.
  const cost = estimateCost(model, SHARED_INPUT_TOKENS, outputTokens);
  const totalTokens = SHARED_INPUT_TOKENS + outputTokens;

  return (
    <div className={`${OPS_CARD} flex flex-col overflow-hidden`}>
      {/* header — provider glyph + label + live/done badge */}
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

      {/* response area */}
      <div className="px-4 py-3.5 min-h-[224px] text-sm text-slate-700 leading-relaxed">
        {hasRun ? (
          <p className="whitespace-pre-wrap">
            {visible}
            {streaming && (
              // blinking caret — the "typing" affordance while characters land.
              <span aria-hidden="true" className="inline-block w-[2px] h-[1.05em] -mb-[0.18em] ml-px bg-emerald-500 animate-pulse" />
            )}
          </p>
        ) : (
          <p className="text-slate-400 italic">Run the prompt to stream this model's response.</p>
        )}
      </div>

      {/* readout — tokens + $ (cost from estimateCost) */}
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

// Three pulsing dots — the in-progress "typing" affordance in the column header.
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
