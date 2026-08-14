// InvokePanel — the agent "Test invoke" panel (Epic 6, T-FE-INVOKE).
//
// The C-2 demo set-piece. Sends a prompt to POST /agents/{id}/invoke as the
// *signed-in* user — the axios interceptor injects that user's Bearer token, so
// switching users (Maria / Lars / Hans) is what drives success vs. blocked:
// the backend OBO-exchanges the caller's token to the agent's audience (assigned
// → token; NOT assigned → 403), bearer-POSTs the runtime, BUFFERS the agent's
// SSE stream, and returns {"response": <text-or-json>}. So this panel does NOT
// stream — it just sends a prompt and renders the buffered response.
//
// Sibling of AccessTab by design: same CARD chrome, same actionPending /
// actionError idiom, the same CSS-only Spinner, the same mountedRef unmount
// guard, the same bg-blue-600 primary. The one bespoke beat is the 403
// blocked-state — rendered as a calm amber "you don't have access" card (not a
// scary red crash) so Hans's blocked invoke reads as an intentional governance
// moment, not a bug.

import { useEffect, useRef, useState } from 'react';
import { invokeApi } from '../../api/client';
import type { Agent } from '../../api/client';
import { invokeStageChoice } from './invokeStage';

// Card chrome — identical to AgentDetail's CARD / AccessTab's CARD so this panel
// sits in the same surface family as the Overview cards it lives among.
const CARD = 'bg-white/70 backdrop-blur rounded-xl border border-slate-200/60 shadow-sm';

// A small CSS-only spinner (no icon library) — slate ring with a blue arc.
// Copied verbatim from AccessTab so the two E6 panels spin identically.
function Spinner() {
  return (
    <span
      aria-hidden="true"
      className="inline-block h-4 w-4 rounded-full border-2 border-slate-200 border-t-blue-600 animate-spin"
    />
  );
}

// Render the backend's `response` for the mono box. The field is typed
// `unknown` and can be a string OR an object (the backend extracts the terminal
// SSE message, which may be plain text or structured JSON) — render a string
// directly; pretty-print anything else. Empty/whitespace-only → a clear marker.
function formatResponse(response: unknown): string {
  if (typeof response === 'string') {
    return response.trim() === '' ? '(empty response)' : response;
  }
  if (response === null || response === undefined) {
    return '(empty response)';
  }
  try {
    return JSON.stringify(response, null, 2);
  } catch {
    // Circular / non-serialisable — fall back to a coarse stringification rather
    // than throwing inside render.
    return String(response);
  }
}

// Narrow an unknown catch value to an axios-style error to read response.status
// without pulling in `any`. The shared client is axios, so failures carry a
// `response.status` when the server answered; degrade gracefully when it didn't
// (network error / timeout surfaced only as a message).
function errorStatus(err: unknown): number | undefined {
  if (typeof err === 'object' && err !== null && 'response' in err) {
    const resp = (err as { response?: unknown }).response;
    if (typeof resp === 'object' && resp !== null && 'status' in resp) {
      const status = (resp as { status?: unknown }).status;
      if (typeof status === 'number') return status;
    }
  }
  return undefined;
}

export default function InvokePanel({ agent }: { agent: Agent }) {
  const [prompt, setPrompt] = useState('');

  // Mirrors AgentDetail / AccessTab's actionPending + actionError idiom.
  const [actionPending, setActionPending] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  // True only for the demo's blocked-state (403 "not assigned") so the error is
  // rendered as a calm amber governance card rather than the plain red line used
  // for every other failure.
  const [blocked, setBlocked] = useState(false);

  // The buffered response text/JSON to show in the mono box. null = no run yet.
  const [result, setResult] = useState<string | null>(null);

  // WHICH runtime the prompt reaches (E36/T2). Since E28A an agent owns one runtime PER STAGE,
  // and without a stage the backend invokes whichever stage deployed last — so an operator who
  // believes they are testing dev can reach prod. This is the panel half of the fix.
  const [stage, setStage] = useState('');

  // Guards against setState after unmount — the run is awaited, so it can land
  // after the user navigates away from the agent (mirror AccessTab's pattern).
  const mountedRef = useRef(true);
  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  // Visibility gate: a non-provisioned or non-Entra agent has no working invoke
  // path, so the panel renders nothing at all.
  if (!(agent.identity_status === 'provisioned' && agent.auth_type === 'entra')) {
    return null;
  }

  const trimmed = prompt.trim();
  const canRun = trimmed.length > 0 && !actionPending;

  // WHICH RUNTIME — decided in `invokeStage.ts`, not here, because the decision has consequences
  // (one of the reachable runtimes is prod) and only a `.ts` module is test-gated by vitest.
  // It reads `agent_arns` for the options and the `agent_arn` scalar for the default, and returns
  // an absent default whenever there is nothing to attribute — see that module for why.
  const { stages, showSelector, defaultStage } = invokeStageChoice(
    agent.agent_arns,
    agent.agent_arn,
  );
  // DERIVED, not stored: AgentDetail keeps this component mounted across a change of `agent`, and
  // a stage carried over from the previous agent would 404 — so a pick only counts while it is
  // still one of THIS agent's stages, and only while a selector is on screen to have made it.
  // `''` means "send no stage", which is the honest request when nothing was chosen and nothing
  // could be derived.
  const selectedStage = showSelector && stages.includes(stage) ? stage : (defaultStage ?? '');

  const runInvoke = async () => {
    if (!canRun) return;
    // Clear any prior result/error so a new Run starts clean.
    setActionPending(true);
    setActionError(null);
    setBlocked(false);
    setResult(null);
    try {
      const { response } = await invokeApi.invoke(
        agent.id,
        trimmed,
        selectedStage ? { stage: selectedStage } : undefined,
      );
      if (!mountedRef.current) return;
      setResult(formatResponse(response));
    } catch (err: unknown) {
      if (!mountedRef.current) return;
      const message = err instanceof Error ? err.message : 'Invoke failed.';
      // The C-2 blocked-state: a 403 means the OBO exchange refused because the
      // signed-in user isn't assigned to this agent. Prefer the status code, but
      // degrade to the backend detail text when no status is readable.
      // NOTE: the regex is coupled to the exact `detail` wording produced by the
      // T-ROUTES invoke route: `"<email> is not assigned to this agent"`. Because
      // the axios response interceptor flattens errors into err.message (often
      // losing the HTTP status), the regex is frequently the *actual* 403
      // detector — not just a fallback. If T-ROUTES ever rephrases that string,
      // the blocked-state silently degrades to the generic red error line (no
      // crash, but a less-polished experience). Keep these two in sync.
      const status = errorStatus(err);
      const looksUnassigned =
        status === 403 || /not assigned to this agent/i.test(message);
      setBlocked(looksUnassigned);
      setActionError(message);
    } finally {
      if (mountedRef.current) setActionPending(false);
    }
  };

  // Cmd/Ctrl+Enter submits from the textarea — a small power-user nicety that
  // also makes the demo snappier without crowding the UI with hint text.
  const onKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') {
      e.preventDefault();
      void runInvoke();
    }
  };

  return (
    <div className={`${CARD} p-5`}>
      <h2 className="text-sm font-semibold text-slate-800 mb-1">Test invoke</h2>
      <p className="text-xs text-slate-400 mb-3">
        Sends a prompt as the signed-in user — Microsoft Entra decides if you’re allowed.
      </p>

      <label htmlFor="invoke-prompt" className="sr-only">
        Prompt to send to the agent
      </label>
      <textarea
        id="invoke-prompt"
        value={prompt}
        onChange={(e) => setPrompt(e.target.value)}
        onKeyDown={onKeyDown}
        rows={3}
        disabled={actionPending}
        placeholder="Ask the agent something…"
        className="w-full px-3 py-2 text-sm rounded-lg border border-slate-300 focus:outline-none focus:ring-2 focus:ring-blue-500/40 disabled:bg-slate-50 disabled:text-slate-400 resize-y"
      />

      {/* Stage picker — only when the agent owns more than one runtime. Same select chrome as
          the other governance selects (CedarPoliciesTab). */}
      {showSelector && (
        <div className="mt-3">
          <label
            htmlFor="invoke-stage"
            className="block text-[11px] uppercase tracking-wide text-slate-400 font-medium mb-1"
          >
            Stage
          </label>
          <select
            id="invoke-stage"
            value={selectedStage}
            onChange={(e) => setStage(e.target.value)}
            disabled={actionPending}
            className="px-3 py-1.5 text-sm rounded-lg border border-slate-300 bg-white focus:outline-none focus:ring-2 focus:ring-blue-500/40 disabled:bg-slate-50 disabled:text-slate-400"
          >
            {/* Only when the scalar attributes no stage: an explicit option for "send no
                ?stage=", so the unattributable default is a legible choice rather than a
                blank-looking select. The backend then resolves the scalar, as it did pre-T2. */}
            {defaultStage === undefined && <option value="">Last deployed</option>}
            {stages.map((s) => (
              <option key={s} value={s}>{s}</option>
            ))}
          </select>
          <p className="text-[11px] text-slate-400 mt-1">
            This agent has one runtime per stage — the prompt goes to the stage selected here.
          </p>
        </div>
      )}

      <div className="flex items-center gap-3 mt-3">
        <button
          type="button"
          onClick={runInvoke}
          disabled={!canRun}
          aria-busy={actionPending}
          className="inline-flex items-center gap-2 px-3.5 py-1.5 rounded-lg bg-blue-600 text-white text-sm font-medium hover:bg-blue-700 transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
        >
          {actionPending && <Spinner />}
          {actionPending ? 'Running…' : 'Run'}
        </button>
        <span className="text-[11px] text-slate-400 hidden sm:inline">⌘↵ to run</span>
      </div>

      {/* Blocked-state (403 not-assigned) — the C-2 set-piece. Calm amber
          governance card, distinct from the plain-red error line below. */}
      {actionError && blocked && (
        <div className="mt-4 rounded-lg border border-amber-200 bg-amber-50 px-4 py-3" role="alert">
          <p className="text-sm font-semibold text-amber-800">You don’t have access to this agent</p>
          <p className="text-sm text-amber-700 mt-0.5">
            Microsoft Entra blocked this invocation because you’re not assigned to this agent. Ask
            an admin to grant you access on the Access tab.
          </p>
        </div>
      )}

      {/* Any other failure (409 not-provisioned, 500 misconfigured, 502 agent
          rejected, 504 timeout, network) — surface the message plainly. */}
      {actionError && !blocked && (
        <p className="mt-4 text-sm text-red-600" role="alert">{actionError}</p>
      )}

      {/* Buffered response — mono, pre-wrapped, scrollable when long. */}
      {result !== null && (
        <div className="mt-4">
          <div className="text-[11px] uppercase tracking-wide text-slate-400 font-medium mb-1">
            Response
          </div>
          <pre className="max-h-80 overflow-auto rounded-lg border border-slate-200/60 bg-slate-50 px-3 py-2.5 font-mono text-xs text-slate-700 whitespace-pre-wrap break-words">
            {result}
          </pre>
        </div>
      )}
    </div>
  );
}
