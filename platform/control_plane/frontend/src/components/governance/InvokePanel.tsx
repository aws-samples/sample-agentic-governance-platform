// Decision: docs/adr/frontend-refusals-classified-by-code.md
import { useEffect, useRef, useState } from 'react';
import { invokeApi } from '../../api/client';
import { isApiFailure } from '../../api/failure';
import type { Agent, AgentRepositoryView, TraceSummary } from '../../api/client';
import AddToDatasetModal from '../operations/repo-tabs/AddToDatasetModal';
import { INVOKE_ADD_COPY, committedFlash, traceFromInvoke } from '../operations/repo-tabs/tracesToDataset';
import { invokeStageChoice } from './invokeStage';
import { toolCallsView } from './invokeReply';
import type { ToolCallView } from './invokeReply';
import ReplyMarkdown from './ReplyMarkdown';
import { classifyInvokeRefusal } from './agent-tabs/containmentLogic';

const CARD = 'bg-white/70 backdrop-blur rounded-xl border border-slate-200/60 shadow-sm';

function Spinner() {
  return (
    <span
      aria-hidden="true"
      className="inline-block h-4 w-4 rounded-full border-2 border-slate-200 border-t-blue-600 animate-spin"
    />
  );
}

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
    return String(response);
  }
}

function ToolsCalled({ tools }: { tools: ToolCallView[] }) {
  const [open, setOpen] = useState<Set<number>>(new Set());
  const toggle = (i: number) =>
    setOpen((prev) => {
      const next = new Set(prev);
      if (next.has(i)) next.delete(i);
      else next.add(i);
      return next;
    });
  return (
    <div className="mt-3">
      <div className="text-[11px] uppercase tracking-wide text-slate-400 font-medium mb-1">
        Tools called
      </div>
      {tools.length === 0 ? (
        <p className="text-xs text-slate-500">No tools called.</p>
      ) : (
        <ul className="space-y-1">
          {tools.map((t, i) => (
            <li key={i} className="font-mono text-xs text-slate-700 break-all">
              <span className="font-semibold text-slate-800">{t.name}</span>{' '}
              <span className={open.has(i) ? 'whitespace-pre-wrap' : ''}>
                {open.has(i) ? t.args : t.short}
              </span>
              {t.truncated && (
                <button
                  type="button"
                  onClick={() => toggle(i)}
                  className="ml-2 font-sans text-[11px] text-blue-600 hover:underline"
                >
                  {open.has(i) ? 'Less' : 'More'}
                </button>
              )}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

export default function InvokePanel({
  agent,
  datasetRepo = null,
}: {
  agent: Agent;
  datasetRepo?: AgentRepositoryView | null;
}) {
  const [prompt, setPrompt] = useState('');

  const [actionPending, setActionPending] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [blocked, setBlocked] = useState(false);
  const [contained, setContained] = useState(false);

  const [result, setResult] = useState<
    { text: string; markdown: boolean; tools: ToolCallView[] | null; trace: TraceSummary | null } | null
  >(null);
  const [adding, setAdding] = useState(false);
  const [addedFlash, setAddedFlash] = useState<string | null>(null);

  const [stage, setStage] = useState('');

  const mountedRef = useRef(true);
  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  if (!(agent.identity_status === 'provisioned' && agent.auth_type === 'entra')) {
    return null;
  }

  const trimmed = prompt.trim();
  const canRun = trimmed.length > 0 && !actionPending;

  const { stages, showSelector, defaultStage } = invokeStageChoice(
    agent.agent_arns,
    agent.agent_arn,
  );
  const selectedStage = showSelector && stages.includes(stage) ? stage : (defaultStage ?? '');

  const runInvoke = async () => {
    if (!canRun) return;
    setActionPending(true);
    setActionError(null);
    setBlocked(false);
    setContained(false);
    setResult(null);
    setAddedFlash(null);
    try {
      const { response, tool_calls, session_id } = await invokeApi.invoke(
        agent.id,
        trimmed,
        selectedStage ? { stage: selectedStage } : undefined,
      );
      if (!mountedRef.current) return;
      setResult({
        text: formatResponse(response),
        markdown: typeof response === 'string' && response.trim() !== '',
        tools: Array.isArray(tool_calls) ? toolCallsView(tool_calls) : null,
        trace: traceFromInvoke(
          { prompt: trimmed, response, toolCalls: tool_calls, sessionId: session_id },
          new Date().toISOString(),
        ),
      });
    } catch (err: unknown) {
      if (!mountedRef.current) return;
      const message = err instanceof Error ? err.message : 'Invoke failed.';
      const failure = isApiFailure(err) ? err : null;
      const refusal = failure === null ? null : classifyInvokeRefusal(failure);
      setBlocked(refusal === 'unassigned');
      setContained(refusal === 'contained');
      setActionError(message);
    } finally {
      if (mountedRef.current) setActionPending(false);
    }
  };

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

      {actionError && blocked && (
        <div className="mt-4 rounded-lg border border-amber-200 bg-amber-50 px-4 py-3" role="alert">
          <p className="text-sm font-semibold text-amber-800">You don’t have access to this agent</p>
          <p className="text-sm text-amber-700 mt-0.5">
            Microsoft Entra blocked this invocation because you’re not assigned to this agent. Ask
            an admin to grant you access on the Access tab.
          </p>
        </div>
      )}

      {actionError && contained && (
        <div className="mt-4 rounded-lg border border-red-200 bg-red-50 px-4 py-3" role="alert">
          <p className="text-sm font-semibold text-red-800">This agent is contained. Restore it on the Containment tab.</p>
        </div>
      )}

      {actionError && !blocked && !contained && (
        <p className="mt-4 text-sm text-red-600" role="alert">{actionError}</p>
      )}

      {result !== null && (
        <div className="mt-4">
          <div className="text-[11px] uppercase tracking-wide text-slate-400 font-medium mb-1">
            Response
          </div>
          {result.markdown ? (
            <ReplyMarkdown
              text={result.text}
              className="max-h-80 overflow-auto rounded-lg border border-slate-200/60 bg-slate-50 px-3 py-2.5 text-sm text-slate-700 space-y-2 break-words"
            />
          ) : (
            <pre className="max-h-80 overflow-auto rounded-lg border border-slate-200/60 bg-slate-50 px-3 py-2.5 font-mono text-xs text-slate-700 whitespace-pre-wrap break-words">
              {result.text}
            </pre>
          )}
          {result.tools !== null && <ToolsCalled tools={result.tools} />}
          {datasetRepo && result.trace && (
            <div className="flex flex-wrap items-center gap-3 mt-3">
              {addedFlash === null && (
                <button
                  type="button"
                  onClick={() => setAdding(true)}
                  className="px-3 py-1.5 rounded-lg border border-slate-300 bg-white text-sm font-medium text-slate-700 hover:bg-slate-50 transition-colors"
                >
                  {INVOKE_ADD_COPY.button}
                </button>
              )}
              <span className="text-[11px] text-slate-400">{addedFlash ?? INVOKE_ADD_COPY.hint}</span>
            </div>
          )}
        </div>
      )}

      {adding && datasetRepo && result?.trace && (
        <AddToDatasetModal
          repo={{ project_id: datasetRepo.project_id, id: datasetRepo.repo_id }}
          traces={[result.trace]}
          onClose={() => setAdding(false)}
          onDone={(n) => {
            setAdding(false);
            setAddedFlash(committedFlash(n));
          }}
        />
      )}
    </div>
  );
}
