import { useMemo, useState, type JSX } from 'react';

import {
  evalsApi,
  type EvalConfigInput,
  type EvalFiles,
  type Repository,
} from '../../../api/client';
import { COMING_SOON_TITLE } from '../../shared/comingSoonCopy';
import { OPS_CARD, OPS_PRIMARY_BTN } from '../opsUi';
import type { ActorViewer } from '../repositoryDetailTabs';
import { AddItemModal, DatasetSection, UploadModal } from './EvaluationsDataset';
import { RunsSection } from './EvaluationsRuns';
import {
  ARGS_MODE_OPTIONS,
  COMING_SOON,
  COMMIT_NOTE,
  JUDGE_CHOICES,
  TOOL_MODE_OPTIONS,
  commitErrorText,
  gateCard,
  parseThreshold,
} from './evaluationsLogic';
import { FIELD_INPUT, FIELD_LABEL, GATE_STYLE, PILL, SECTION_TITLE } from './evaluationsTabStyles';
import type { RepoEvals } from './useRepoEvals';

export default function EvaluationsTab({
  repo,
  mayWrite,
  viewer,
  evals,
  openRunId,
  onOpenRun,
}: {
  repo: Repository;
  mayWrite: boolean;
  viewer: ActorViewer | null;
  evals: RepoEvals;
  openRunId: string | null;
  onOpenRun: (runId: string | null) => void;
}): JSX.Element {
  const { files, filesError, runs, runsError, now, live, starting, actionError, currentBuild, itemCount, blocked, loadFiles } = evals;
  const [notice, setNotice] = useState<string | null>(null);
  const [modal, setModal] = useState<'add' | 'upload' | null>(null);

  const itemsById = useMemo(() => new Map((files?.items ?? []).map((i) => [i.id, i])), [files]);

  const start = async () => {
    const run = await evals.startManual();
    if (run) onOpenRun(run.run_id);
  };

  const card = gateCard({ currentBuild, currentRun: runs?.current_run ?? null, itemCount, now });

  const committed = async (message: string) => {
    setModal(null);
    setNotice(`${message} ${COMMIT_NOTE}`);
    await loadFiles();
  };

  if (files === null && runs === null && !filesError && !runsError) {
    return (
      <div className={`${OPS_CARD} p-6`}>
        <p className="text-sm text-slate-500">Loading evaluations…</p>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <GateCardView
        card={card}
        blocked={blocked}
        starting={starting}
        onRun={() => void start()}
        error={actionError ?? runsError}
      />

      {notice && (
        <div role="status" className="flex items-start justify-between gap-3 rounded-xl border border-emerald-200 bg-emerald-50/70 px-4 py-3 text-sm text-emerald-800">
          <span>{notice}</span>
          <button type="button" onClick={() => setNotice(null)} className="text-emerald-700 hover:text-emerald-900" aria-label="Dismiss">
            ×
          </button>
        </div>
      )}

      {files && files.errors.length > 0 && <FileErrors errors={files.errors} />}

      <RunsSection
        repo={repo}
        runs={runs}
        error={runsError}
        openRunId={openRunId}
        onToggle={(id) => onOpenRun(openRunId === id ? null : id)}
        live={live}
        viewer={viewer}
        items={itemsById}
      />

      <DatasetSection
        repo={repo}
        files={files}
        error={filesError}
        mayWrite={mayWrite}
        onAdd={() => setModal('add')}
        onUpload={() => setModal('upload')}
      />

      {files && (
        <SettingsSection
          key={files.head_sha}
          repo={repo}
          files={files}
          mayWrite={mayWrite}
          onSaved={() => void committed('Settings saved.')}
        />
      )}

      <ComingSoon />

      {modal === 'add' && files && (
        <AddItemModal
          repo={repo}
          existing={files.items}
          onClose={() => setModal(null)}
          onDone={(id) => void committed(`Item “${id}” added.`)}
        />
      )}
      {modal === 'upload' && files && (
        <UploadModal
          repo={repo}
          current={files.items.length}
          path={files.config.dataset}
          onClose={() => setModal(null)}
          onDone={(n) => void committed(`Dataset replaced with ${n} ${n === 1 ? 'item' : 'items'}.`)}
        />
      )}
    </div>
  );
}

function GateCardView({
  card,
  blocked,
  starting,
  onRun,
  error,
}: {
  card: ReturnType<typeof gateCard>;
  blocked: string | null;
  starting: boolean;
  onRun: () => void;
  error: string | null;
}): JSX.Element {
  const style = GATE_STYLE[card.state];
  return (
    <section aria-label="Promotion gate" className={`${OPS_CARD} relative overflow-hidden`}>
      <span aria-hidden="true" className={`absolute inset-y-0 left-0 w-1 ${style.rail}`} />
      <div className="flex flex-col gap-4 p-5 pl-6 sm:flex-row sm:items-start sm:justify-between">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <span className={`${PILL} ${style.pill}`}>
              <span aria-hidden="true" className={card.state === 'running' ? 'animate-spin inline-block' : ''}>
                {style.glyph}
              </span>
              {style.label}
            </span>
            <span className="text-[11px] uppercase tracking-wide text-slate-400 font-medium">Prod promotion gate · dev build</span>
          </div>
          <p className="mt-2 text-base text-slate-900" aria-live="polite">
            <span className="font-semibold">{card.headline}</span>
            {card.facts.map((f) => (
              <span key={f} className="text-slate-600 tabular-nums">
                <span aria-hidden="true" className="mx-2 text-slate-300">·</span>
                {f}
              </span>
            ))}
          </p>
          {card.detail && <p className="mt-1 text-sm text-slate-500 max-w-2xl">{card.detail}</p>}
          {card.regressions.length > 0 && (
            <p className="mt-2 text-sm text-amber-700 max-w-2xl break-words">
              <span className="font-medium">Regression vs prod:</span> {card.regressions.join('; ')}
            </p>
          )}
          {card.reasons.length > 0 && (
            <details className="mt-3 text-sm">
              <summary className="cursor-pointer text-rose-700 font-medium">
                {card.reasons.length} {card.reasons.length === 1 ? 'reason' : 'reasons'} the gate failed
              </summary>
              <ul className="mt-2 space-y-1 text-slate-600 list-disc pl-5">
                {card.reasons.map((r, i) => (
                  <li key={i} className="break-words">{r}</li>
                ))}
              </ul>
            </details>
          )}
          {error && <p className="mt-2 text-sm text-rose-700" role="alert">{error}</p>}
        </div>
        <div className="shrink-0 flex flex-col items-start sm:items-end gap-1">
          <button
            type="button"
            onClick={onRun}
            disabled={blocked !== null}
            title={blocked ?? undefined}
            className={`${OPS_PRIMARY_BTN} disabled:opacity-50 disabled:cursor-not-allowed whitespace-nowrap`}
          >
            {starting ? 'Starting…' : 'Run evals'}
          </button>
          {blocked && <span className="text-[11px] text-slate-400">{blocked}</span>}
        </div>
      </div>
    </section>
  );
}

function FileErrors({ errors }: { errors: string[] }): JSX.Element {
  return (
    <div role="alert" className="rounded-xl border border-amber-200 bg-amber-50/70 px-4 py-3 text-sm text-amber-900">
      <p className="font-medium">The eval files in the repository have {errors.length} {errors.length === 1 ? 'error' : 'errors'}. Runs are refused until they are fixed.</p>
      <ul className="mt-1 list-disc pl-5 font-mono text-xs space-y-0.5">
        {errors.map((e, i) => (
          <li key={i}>{e}</li>
        ))}
      </ul>
    </div>
  );
}

function SettingsSection({
  repo,
  files,
  mayWrite,
  onSaved,
}: {
  repo: Repository;
  files: EvalFiles;
  mayWrite: boolean;
  onSaved: () => void;
}): JSX.Element {
  const cfg = files.config;
  const [tools, setTools] = useState(cfg.checks.tools);
  const [args, setArgs] = useState(cfg.checks.args);
  const [noError, setNoError] = useState(cfg.checks.no_error);
  const [judges, setJudges] = useState<string[]>(cfg.judges);
  const [threshold, setThreshold] = useState(String(cfg.gate.judge_min_mean));
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const parsed = parseThreshold(threshold);
  const dirty =
    tools !== cfg.checks.tools || args !== cfg.checks.args || noError !== cfg.checks.no_error
    || judges.join() !== cfg.judges.join() || parsed !== cfg.gate.judge_min_mean;

  const save = async () => {
    if (parsed === null) return;
    const next: EvalConfigInput = {
      ...cfg,
      checks: { tools, args, no_error: noError },
      judges: JUDGE_CHOICES.map((j) => j.id).filter((id) => judges.includes(id)).concat(judges.filter((id) => !JUDGE_CHOICES.some((j) => j.id === id))),
      gate: { ...cfg.gate, judge_min_mean: parsed },
    };
    setSaving(true);
    setError(null);
    try {
      await evalsApi.putConfig(repo.project_id, repo.id, next);
      onSaved();
    } catch (e) {
      setError(commitErrorText(e, 'Save settings'));
    } finally {
      setSaving(false);
    }
  };

  const disabled = !mayWrite || saving;
  return (
    <section aria-label="Settings" className={`${OPS_CARD} p-5`}>
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h3 className={SECTION_TITLE}>Settings</h3>
        <span className="text-[11px] text-slate-400 font-mono">evals/evals.yaml{files.configured ? '' : ' · not in the repo yet, defaults shown'}</span>
      </div>
      <div className="mt-4 grid gap-6 md:grid-cols-2">
        <fieldset disabled={disabled} className="space-y-3">
          <legend className="text-xs font-semibold text-slate-700 mb-1">1 · Deterministic checks</legend>
          <label className="block">
            <span className={FIELD_LABEL}>Tool names</span>
            <select className={FIELD_INPUT} value={tools} onChange={(e) => setTools(e.target.value as typeof tools)}>
              {TOOL_MODE_OPTIONS.map((o) => (
                <option key={o.value} value={o.value}>{o.label} — {o.hint}</option>
              ))}
            </select>
          </label>
          <label className="block">
            <span className={FIELD_LABEL}>Tool arguments</span>
            <select className={FIELD_INPUT} value={args} onChange={(e) => setArgs(e.target.value as typeof args)}>
              {ARGS_MODE_OPTIONS.map((o) => (
                <option key={o.value} value={o.value}>{o.label} — {o.hint}</option>
              ))}
            </select>
          </label>
          <label className="flex items-center gap-2 text-sm text-slate-700">
            <input type="checkbox" checked={noError} onChange={(e) => setNoError(e.target.checked)} className="accent-emerald-600" />
            The invoke must succeed
          </label>
          <p className="text-[11px] text-slate-400">Every enabled check must pass on every item.</p>
        </fieldset>
        <fieldset disabled={disabled} className="space-y-3">
          <legend className="text-xs font-semibold text-slate-700 mb-1">2 · LLM judges (AgentCore built-ins)</legend>
          <div className="space-y-1.5">
            {JUDGE_CHOICES.map((j) => (
              <label key={j.id} className="flex items-start gap-2 text-sm">
                <input
                  type="checkbox"
                  className="mt-1 accent-emerald-600"
                  checked={judges.includes(j.id)}
                  onChange={(e) => setJudges((cur) => (e.target.checked ? [...cur, j.id] : cur.filter((x) => x !== j.id)))}
                />
                <span>
                  <span className="font-medium text-slate-700">{j.label}</span>
                  <span className="block text-xs text-slate-500">{j.hint}</span>
                </span>
              </label>
            ))}
          </div>
          <label className="block max-w-[12rem]">
            <span className={FIELD_LABEL}>Minimum mean score (0–1)</span>
            <input
              className={FIELD_INPUT}
              inputMode="decimal"
              value={threshold}
              onChange={(e) => setThreshold(e.target.value)}
              aria-invalid={parsed === null}
            />
          </label>
          {parsed === null && <p className="text-xs text-rose-700">Enter a number from 0 to 1.</p>}
          <p className="text-[11px] text-slate-400">Each judge's mean across items must reach this. No judges = checks only.</p>
        </fieldset>
      </div>
      <div className="mt-4 flex flex-wrap items-center justify-end gap-3">
        {error && <span className="text-sm text-rose-700" role="alert">{error}</span>}
        {!mayWrite && <span className="text-[11px] text-slate-400">Needs the Maintainer role on this project</span>}
        <button
          type="button"
          onClick={() => void save()}
          disabled={disabled || !dirty || parsed === null}
          className={`${OPS_PRIMARY_BTN} disabled:opacity-50 disabled:cursor-not-allowed`}
        >
          {saving ? 'Committing…' : 'Save settings'}
        </button>
      </div>
    </section>
  );
}

function ComingSoon(): JSX.Element {
  return (
    <section aria-label={COMING_SOON_TITLE} className="rounded-xl border border-slate-200/80 bg-white/50 px-5 py-4">
      <h3 className="text-xs font-semibold text-slate-500 uppercase tracking-wide">{COMING_SOON_TITLE}</h3>
      <ul className="mt-2 grid gap-x-6 gap-y-1.5 sm:grid-cols-2">
        {COMING_SOON.map((c) => (
          <li key={c.title} className="text-sm text-slate-500">
            <span className="font-medium text-slate-600">{c.title}</span> — {c.detail}
          </li>
        ))}
      </ul>
    </section>
  );
}
