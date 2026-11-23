import { useEffect, useMemo, useState, type JSX } from 'react';

import {
  evalsApi,
  type DatasetItemOutput,
  type EvalRun,
  type EvalRuns,
  type ItemResult,
  type Repository,
} from '../../../api/client';
import ReplyMarkdown from '../../governance/ReplyMarkdown';
import { NO_VALUE } from '../opsLabels';
import { OPS_CARD, OPS_TABLE_DIVIDE, OPS_TABLE_HEAD } from '../opsUi';
import { entraActorName, type ActorViewer } from '../repositoryDetailTabs';
import {
  buildLabel,
  errText,
  evaluatorLabel,
  expectedItemFor,
  itemPassed,
  itemsText,
  json,
  judgeExplanation,
  latencyText,
  orderScores,
  runLabels,
  runSummaryText,
  scoreView,
  toolDiffRows,
  when,
} from './evaluationsLogic';
import { DIFF_STYLE, FIELD_LABEL, PILL, SECTION_TITLE, TONE_CLS, runStatusPill } from './evaluationsTabStyles';

export function RunsSection({
  repo,
  runs,
  error,
  openRunId,
  onToggle,
  live,
  viewer,
  items,
}: {
  repo: Repository;
  runs: EvalRuns | null;
  items: Map<string, DatasetItemOutput>;
  error: string | null;
  openRunId: string | null;
  onToggle: (runId: string) => void;
  live: boolean;
  viewer: ActorViewer | null;
}): JSX.Element {
  const labels = useMemo(() => runLabels(runs?.runs ?? []), [runs]);
  const list = runs?.runs ?? [];
  return (
    <section aria-label="Runs" className={`${OPS_CARD} overflow-hidden`}>
      <div className="flex items-baseline justify-between gap-2 px-5 pt-5 pb-3">
        <h3 className={SECTION_TITLE}>Runs</h3>
        <span className="text-[11px] text-slate-400">Checks first, then judges · newest first</span>
      </div>
      {error && runs === null ? (
        <p className="px-5 pb-5 text-sm text-rose-700">Runs could not be read: {error}</p>
      ) : list.length === 0 ? (
        <p className="px-5 pb-5 text-sm text-slate-500">No runs yet. A run starts on its own after each dev build once the dataset has items, or press Run evals.</p>
      ) : (
        <table className="w-full text-sm">
          <thead className={OPS_TABLE_HEAD}>
            <tr>
              <th scope="col" className="text-left font-medium px-5 py-2">Run</th>
              <th scope="col" className="text-left font-medium px-3 py-2">Result</th>
              <th scope="col" className="text-left font-medium px-3 py-2">Checks · judges</th>
              <th scope="col" className="text-left font-medium px-3 py-2 hidden md:table-cell">Build</th>
              <th scope="col" className="text-left font-medium px-3 py-2 hidden lg:table-cell">Started by</th>
            </tr>
          </thead>
          <tbody className={OPS_TABLE_DIVIDE}>
            {list.map((run) => {
              const open = openRunId === run.run_id;
              const pill = runStatusPill(run.status);
              return (
                <RunRow
                  key={run.run_id}
                  repo={repo}
                  run={run}
                  label={labels.get(run.run_id) ?? 'Run'}
                  pillCls={pill.cls}
                  pillLabel={pill.label}
                  open={open}
                  onToggle={() => onToggle(run.run_id)}
                  live={live}
                  viewer={viewer}
                  items={items}
                />
              );
            })}
          </tbody>
        </table>
      )}
      {(runs?.overrides.length ?? 0) > 0 && <Overrides overrides={runs!.overrides} viewer={viewer} />}
    </section>
  );
}

function RunRow({
  repo,
  run,
  label,
  pillCls,
  pillLabel,
  open,
  onToggle,
  live,
  viewer,
  items,
}: {
  repo: Repository;
  run: EvalRun;
  items: Map<string, DatasetItemOutput>;
  label: string;
  pillCls: string;
  pillLabel: string;
  open: boolean;
  onToggle: () => void;
  live: boolean;
  viewer: ActorViewer | null;
}): JSX.Element {
  return (
    <>
      <tr className={`hover:bg-emerald-50/40 ${open ? 'bg-emerald-50/30' : ''}`}>
        <td className="px-5 py-2.5">
          <button
            type="button"
            onClick={onToggle}
            aria-expanded={open}
            title={run.run_id}
            className="inline-flex items-center gap-1.5 font-medium text-slate-800 hover:text-emerald-700"
          >
            <span aria-hidden="true" className={`text-slate-400 transition-transform ${open ? 'rotate-90' : ''}`}>▸</span>
            <time dateTime={run.started_at}>{label}</time>
            {run.trigger === 'auto' && <span className="text-[11px] font-normal text-slate-400">auto</span>}
          </button>
        </td>
        <td className="px-3 py-2.5">
          <span className={`${PILL} ${pillCls}`}>{pillLabel}</span>
        </td>
        <td className="px-3 py-2.5 text-slate-600 tabular-nums">{runSummaryText(run)}</td>
        <td className="px-3 py-2.5 text-slate-500 font-mono text-xs hidden md:table-cell">{buildLabel(run)?.replace(/^build /, '') ?? NO_VALUE}</td>
        <td className="px-3 py-2.5 text-slate-500 hidden lg:table-cell" title={`${run.started_by} · ${when(run.started_at)}`}>
          {entraActorName(run.started_by, viewer)}
        </td>
      </tr>
      {open && (
        <tr>
          <td colSpan={5} className="bg-slate-50/60 px-5 py-4">
            <RunDetail repo={repo} run={run} live={live} items={items} />
          </td>
        </tr>
      )}
    </>
  );
}

function RunDetail({ repo, run, live, items }: { repo: Repository; run: EvalRun; live: boolean; items: Map<string, DatasetItemOutput> }): JSX.Element {
  const [results, setResults] = useState<ItemResult[] | null>(null);
  const [snapshot, setSnapshot] = useState<DatasetItemOutput[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [openItem, setOpenItem] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    evalsApi
      .getRun(repo.project_id, repo.id, run.run_id)
      .then((d) => !cancelled && (setResults(d.results), setSnapshot(d.dataset ?? []), setError(null)))
      .catch((e) => !cancelled && setError(errText(e)));
    return () => {
      cancelled = true;
    };
  }, [repo.project_id, repo.id, run.run_id, run.status, run.finished_at, live]);

  const columns = useMemo(() => {
    const seen = new Map<string, ItemResult['scores'][number]>();
    for (const r of results ?? []) for (const s of r.scores) if (!seen.has(s.evaluator)) seen.set(s.evaluator, s);
    return orderScores([...seen.values()]);
  }, [results]);
  const threshold = run.config.gate.judge_min_mean;

  if (error) return <p className="text-sm text-rose-700">The run could not be read: {error}</p>;
  if (results === null) return <p className="text-sm text-slate-500">Loading run…</p>;
  if (results.length === 0) {
    return (
      <p className="text-sm text-slate-500">
        {run.status === 'running' ? 'Items are being invoked on dev. Results appear when the run finishes.' : 'This run recorded no item results.'}
      </p>
    );
  }

  return (
    <div className="space-y-3">
      <p className="text-xs text-slate-500">
        {itemsText(results)} · judges pass at a mean of {threshold.toFixed(2)} or more · checks: tools {run.config.checks.tools}, arguments {run.config.checks.args}
      </p>
      {(run.regressions ?? []).length > 0 && (
        <p className="text-xs text-amber-700 break-words">
          <span className="font-medium">Regression vs prod:</span> {(run.regressions ?? []).join('; ')}
        </p>
      )}
      <div className="overflow-x-auto rounded-lg border border-slate-200 bg-white">
        <table className="w-full text-sm">
          <thead className={OPS_TABLE_HEAD}>
            <tr>
              <th scope="col" className="text-left font-medium px-3 py-2">Item</th>
              {columns.map((c) => (
                <th key={c.evaluator} scope="col" className={`text-center font-medium px-2 py-2 ${c.kind === 'judge' ? 'bg-slate-50/80' : ''}`}>
                  <span className="block text-[10px] text-slate-400 normal-case">{c.kind === 'check' ? 'check' : 'judge'}</span>
                  {evaluatorLabel(c)}
                </th>
              ))}
              <th scope="col" className="text-right font-medium px-3 py-2">Latency</th>
            </tr>
          </thead>
          <tbody className={OPS_TABLE_DIVIDE}>
            {results.map((r) => {
              const open = openItem === r.item_id;
              const byEval = new Map(r.scores.map((s) => [s.evaluator, s]));
              const ok = itemPassed(r);
              const expected = expectedItemFor(r.item_id, snapshot, items);
              return (
                <ItemRows
                  key={r.item_id}
                  result={r}
                  run={run}
                  item={expected.item}
                  fromSnapshot={expected.fromSnapshot}
                  open={open}
                  ok={ok}
                  cells={columns.map((c) => {
                    const s = byEval.get(c.evaluator);
                    return s ? scoreView(s, threshold) : null;
                  })}
                  colSpan={columns.length + 2}
                  onToggle={() => setOpenItem(open ? null : r.item_id)}
                />
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function ItemRows({
  result,
  run,
  item,
  fromSnapshot,
  open,
  ok,
  cells,
  colSpan,
  onToggle,
}: {
  result: ItemResult;
  run: EvalRun;
  item: DatasetItemOutput | undefined;
  fromSnapshot: boolean;
  open: boolean;
  ok: boolean;
  cells: (ReturnType<typeof scoreView> | null)[];
  colSpan: number;
  onToggle: () => void;
}): JSX.Element {
  const threshold = run.config.gate.judge_min_mean;
  return (
    <>
      <tr className="hover:bg-emerald-50/30">
        <td className="px-3 py-2">
          <button type="button" onClick={onToggle} aria-expanded={open} className="inline-flex items-center gap-1.5 font-mono text-xs text-slate-800 hover:text-emerald-700">
            <span aria-hidden="true" className={`text-slate-400 transition-transform ${open ? 'rotate-90' : ''}`}>▸</span>
            <span aria-hidden="true" className={ok ? 'text-emerald-600' : 'text-rose-600'}>●</span>
            {result.item_id}
          </button>
          {result.error && <span className="ml-2 text-[11px] text-amber-700">invoke failed</span>}
        </td>
        {cells.map((c, i) => (
          <td key={i} className={`px-2 py-2 text-center tabular-nums font-semibold ${c ? TONE_CLS[c.tone] : 'text-slate-300'}`} title={c?.title ?? undefined}>
            {c ? c.text : NO_VALUE}
          </td>
        ))}
        <td className="px-3 py-2 text-right text-slate-500 tabular-nums">{latencyText(result.latency_ms) ?? NO_VALUE}</td>
      </tr>
      {open && (
        <tr>
          <td colSpan={colSpan} className="px-4 py-4 bg-slate-50/50">
            <ItemDetail result={result} item={item} fromSnapshot={fromSnapshot} threshold={threshold} />
          </td>
        </tr>
      )}
    </>
  );
}

function ItemDetail({
  result,
  item,
  fromSnapshot,
  threshold,
}: {
  result: ItemResult;
  item: DatasetItemOutput | undefined;
  fromSnapshot: boolean;
  threshold: number;
}): JSX.Element {
  const scores = orderScores(result.scores);
  return (
    <div className="grid gap-4 lg:grid-cols-2">
      <div className="space-y-3 min-w-0">
        <div>
          <span className={FIELD_LABEL}>Scores</span>
          <ul className="space-y-1.5">
            {scores.map((s) => {
              const v = scoreView(s, threshold);
              const why = s.error ? { text: s.error, unused: null } : judgeExplanation(s.explanation);
              return (
                <li key={s.evaluator} className="flex gap-2 text-sm">
                  <span className={`w-16 shrink-0 font-semibold tabular-nums ${TONE_CLS[v.tone]}`}>{v.text}</span>
                  <span className="min-w-0">
                    <span className="font-medium text-slate-700">{evaluatorLabel(s)}</span>
                    <span className="ml-1 text-[11px] text-slate-400">{s.kind}</span>
                    {s.kind === 'judge' && s.label && !s.error && (
                      <span className="ml-2 rounded bg-slate-100 px-1.5 py-0.5 text-[11px] font-medium text-slate-700">{s.label}</span>
                    )}
                    {why.text && <span className="block text-slate-500 break-words">{why.text}</span>}
                    {why.unused && <span className="block text-[11px] text-slate-400">{why.unused}</span>}
                  </span>
                </li>
              );
            })}
          </ul>
        </div>
        {result.error && (
          <div>
            <span className={FIELD_LABEL}>Invoke error</span>
            <p className="text-sm text-amber-800 break-words">{result.error}</p>
          </div>
        )}
        <div>
          <span className={FIELD_LABEL}>Agent output</span>
          {result.output ? (
            <ReplyMarkdown text={result.output} className="text-sm text-slate-700 space-y-2 break-words max-h-48 overflow-auto" />
          ) : (
            <p className="text-sm text-slate-700">{NO_VALUE}</p>
          )}
        </div>
        {result.session_id && (
          <p className="text-[11px] text-slate-400">
            Session <span className="font-mono">{result.session_id}</span>
          </p>
        )}
      </div>
      <ToolDiff item={item} fromSnapshot={fromSnapshot} actual={result.tool_calls} />
    </div>
  );
}

function ToolDiff({
  item,
  fromSnapshot,
  actual,
}: {
  item: DatasetItemOutput | undefined;
  fromSnapshot: boolean;
  actual: ItemResult['tool_calls'];
}): JSX.Element {
  const expected = item?.expected_tools ?? [];
  const rows = toolDiffRows(expected, actual);
  return (
    <div className="min-w-0">
      <span className={FIELD_LABEL}>Expected vs actual tools</span>
      {!item && <p className="text-[11px] text-slate-400 mb-1">This run saved no copy of its dataset and the item is no longer in it, so only the actual calls are shown.</p>}
      {item && !fromSnapshot && <p className="text-[11px] text-slate-400 mb-1">Expected tools are from the current dataset; this run saved no copy of its own.</p>}
      {rows.length === 0 ? (
        <p className="text-sm text-slate-500">
          {actual === null || actual === undefined ? 'No tool-call data for this item.' : 'No tools expected and none called.'}
        </p>
      ) : (
        <table className="w-full text-xs">
          <thead>
            <tr className="text-left text-slate-400">
              <th scope="col" className="font-medium py-1 pr-2">Expected</th>
              <th scope="col" className="font-medium py-1 pr-2">Actual</th>
              <th scope="col" className="font-medium py-1">&nbsp;</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-200/70 font-mono">
            {rows.map((r, i) => (
              <tr key={i} className="align-top">
                <td className="py-1.5 pr-2 break-all">
                  {r.expected ? (
                    <>
                      <span className="text-slate-800">{r.expected.name}</span>
                      {r.expected.args && <span className="block text-slate-500">{json(r.expected.args)}</span>}
                    </>
                  ) : (
                    <span className="text-slate-300">{NO_VALUE}</span>
                  )}
                </td>
                <td className="py-1.5 pr-2 break-all">
                  {r.actual ? (
                    <>
                      <span className="text-slate-800">{r.actual.name}</span>
                      <span className="block text-slate-500">{json(r.actual.args)}</span>
                    </>
                  ) : (
                    <span className="text-slate-300">{NO_VALUE}</span>
                  )}
                </td>
                <td className={`py-1.5 font-sans whitespace-nowrap ${DIFF_STYLE[r.status].cls}`}>{DIFF_STYLE[r.status].label}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

function Overrides({ overrides, viewer }: { overrides: EvalRuns['overrides']; viewer: ActorViewer | null }): JSX.Element {
  return (
    <div className="border-t border-emerald-100/70 px-5 py-4">
      <h4 className="text-xs font-semibold text-slate-700">Promotion overrides</h4>
      <p className="text-[11px] text-slate-400 mb-2">Prod promotions made without a passing run, with the reason given.</p>
      <ul className="space-y-1.5">
        {overrides.map((o, i) => (
          <li key={i} className="text-sm text-slate-600">
            <span className="font-medium text-slate-800" title={o.by}>{o.by_name || entraActorName(o.by, viewer)}</span>
            <span className="text-slate-400"> · {when(o.at)} · {buildLabel({ digest: o.digest, build_tag: o.build_tag }) ?? 'no build'}</span>
            <span className="block text-slate-600 break-words">“{o.reason}”</span>
          </li>
        ))}
      </ul>
    </div>
  );
}
