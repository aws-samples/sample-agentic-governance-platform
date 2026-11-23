import { useEffect, useRef, useState, type JSX } from 'react';

import {
  agentOpsApi,
  agentsApi,
  connectionsApi,
  githubTemplatesApi,
  mcpServersApi,
  projectsApi,
  pullRequestsApi,
  type Connection,
} from '../../api/client';
import { safeLocalStorage } from '../../api/safeLocalStorage';
import { draftComposition, submitProposal } from '../../api/studio';
import { useUser } from '../../contexts/useUser';
import { findTenantAccount } from '../governance/tenantUi';
import { useTenantDirectory } from '../governance/useTenantDirectory';
import OpsPage from './OpsPage';
import { OPS_CARD, OPS_PRIMARY_BTN } from './opsUi';
import { effectiveRole, mayMaintainProject } from './projectRoles';
import { CreateProjectModal } from './Projects.tsx';
import { AddComponent, AiBox, BlockCard, DraftingSkeleton, RepoTree, UnusedFiles } from './StudioCompose.tsx';
import { Spinner } from './StudioForms.tsx';
import RunView from './StudioRunView.tsx';
import { tenantPlatformOf } from './templateChain.ts';
import {
  assignFile,
  authNoneSummary,
  blockFiles,
  blockName,
  blockers,
  compositionLabel,
  fromDraft,
  newBlock,
  pickTemplates,
  registryFrom,
  removeBlock,
  wiredNames,
  type Attachment,
  type Block,
  type BlockKind,
  type RegistryMcp,
  type RepoFile,
  type StudioTemplates,
} from './studioModel';
import {
  POLL_MS,
  apply,
  clearRun,
  findSavedRun,
  isPoll,
  nextAction,
  planRun,
  runAction,
  saveRun,
  type RunItem,
  type RunState,
  type StudioRunApi,
} from './studioRun';
import { LABEL, SECONDARY_BTN } from './studioStyles';

type Step = 'project' | 'compose' | 'review';
type Mode = 'ai' | 'manual';
interface ProjectOption {
  id: string;
  name: string;
}
interface ProjectContext {
  projectId: string;
  org: string;
  templates: StudioTemplates;
  existingRepos: string[];
}

const LIVE_RUN_API: StudioRunApi = {
  createRepo: (p, body) => projectsApi.addRepo(p, body),
  repoStatus: (p, r) => projectsApi.getRepoStatus(p, r),
  retryRepo: (p, r) => projectsApi.retryRepo(p, r),
  propose: (p, r, body) => submitProposal(p, r, body),
  pullRequests: (r) => pullRequestsApi.list(r),
  deployments: (p, r) => projectsApi.repoDeployments(p, r),
  runtimeStatus: (agentId) => agentOpsApi.runtime(agentId),
  submitAgent: (id) => agentsApi.submit(id),
  submitMcp: (id) => mcpServersApi.submit(id),
};

function ProjectStep(props: {
  projects: ProjectOption[];
  value: string;
  resolving: boolean;
  error: string | null;
  onPick: (id: string) => void;
  onCreate: () => void;
}): JSX.Element {
  return (
    <div className="max-w-xl">
      <h3 className="text-sm font-semibold text-slate-800">Which project is this for?</h3>
      <p className="text-xs text-slate-500 mt-0.5 mb-4">
        Every component you compose lands in this project: its repositories, its secrets, its owners.
      </p>
      <label htmlFor="studio-project" className={LABEL}>
        Project
      </label>
      <div className="flex flex-col sm:flex-row gap-2 mt-1">
        <select
          id="studio-project"
          value={props.value}
          disabled={props.resolving}
          onChange={(e) => props.onPick(e.target.value)}
          className="flex-1 px-3 py-2 text-sm rounded-lg border border-slate-300 bg-white focus:outline-none focus:ring-2 focus:ring-emerald-500/40 disabled:opacity-60"
        >
          <option value="">Select a project</option>
          {props.projects.map((p) => (
            <option key={p.id} value={p.id}>
              {p.name}
            </option>
          ))}
        </select>
        <button type="button" onClick={props.onCreate} className={`${SECONDARY_BTN} inline-flex items-center justify-center gap-1.5`}>
          <span aria-hidden="true" className="text-base leading-none">+</span> Create project
        </button>
      </div>
      {props.resolving && (
        <p className="text-xs text-slate-500 mt-2 flex items-center gap-2" aria-live="polite">
          <Spinner className="w-3.5 h-3.5 text-emerald-600" /> Checking the project and its templates…
        </p>
      )}
      {props.error && (
        <p role="alert" className="text-xs text-rose-700 mt-2">
          {props.error}
        </p>
      )}
    </div>
  );
}

const STEPS: { key: Step; label: string }[] = [
  { key: 'project', label: 'Project' },
  { key: 'compose', label: 'Compose' },
  { key: 'review', label: 'Review and create' },
];

const repoPath = (i: RunItem): string => {
  try {
    return i.repoUrl ? new URL(i.repoUrl).pathname.replace(/^\/|\.git$/g, '') : i.name;
  } catch {
    return i.name;
  }
};

export default function Studio(): JSX.Element {
  const { user } = useUser();
  const tenantDirectory = useTenantDirectory((user?.role_level ?? 0) >= 2);
  const [saved] = useState<RunState | null>(() => findSavedRun(safeLocalStorage));
  const [projects, setProjects] = useState<ProjectOption[]>([]);
  const [connections, setConnections] = useState<Connection[]>([]);
  const [registry, setRegistry] = useState<RegistryMcp[]>([]);
  const [creatingProject, setCreatingProject] = useState(false);
  const [projectId, setProjectId] = useState('');
  const [ctx, setCtx] = useState<ProjectContext | null>(null);
  const [resolving, setResolving] = useState(false);
  const [projectError, setProjectError] = useState<string | null>(null);
  const [step, setStep] = useState<Step>('project');
  const [mode, setMode] = useState<Mode>('ai');
  const [useCase, setUseCase] = useState('');
  const [files, setFiles] = useState<Attachment[]>([]);
  const [drafting, setDrafting] = useState(false);
  const [draftError, setDraftError] = useState<string | null>(null);
  const [blocks, setBlocks] = useState<Block[]>([]);
  const [unused, setUnused] = useState<Attachment[]>([]);
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [preview, setPreview] = useState('');
  const [run, setRun] = useState<RunState | null>(saved);
  const keysRef = useRef<Record<string, string>>({});
  const runRef = useRef<RunState | null>(saved);
  const pickSeq = useRef(0);
  const topRef = useRef<HTMLDivElement>(null);
  const manualRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    let cancelled = false;
    projectsApi
      .list()
      .then((rows) => !cancelled && setProjects(rows.map((p) => ({ id: p.id, name: p.name }))))
      .catch((e: unknown) => !cancelled && setProjectError(e instanceof Error ? e.message : 'Failed to load projects.'));
    connectionsApi
      .list()
      .then((rows) => !cancelled && setConnections(rows))
      .catch(() => undefined);
    mcpServersApi
      .list()
      .then((rows) => !cancelled && setRegistry(registryFrom(rows)))
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    runRef.current = run;
    if (!run) return;
    const action = nextAction(run);
    if (action.type === 'stopped' || action.type === 'done') return;
    const t = setTimeout(
      async () => {
        const result = await runAction(run, action, LIVE_RUN_API, keysRef.current);
        if (runRef.current !== run) return;
        const next = apply(run, result);
        runRef.current = next;
        saveRun(safeLocalStorage, next);
        setRun(next);
      },
      isPoll(action) ? POLL_MS : 0,
    );
    return () => clearTimeout(t);
  }, [run]);

  useEffect(() => {
    if (draftError && !drafting) manualRef.current?.focus();
  }, [draftError, drafting]);

  const projectName = projects.find((p) => p.id === projectId)?.name ?? '';
  const issues = blockers(ctx?.projectId ?? '', blocks, { registry, templates: ctx?.templates, existingRepos: ctx?.existingRepos });
  const blockIssues = (id: string) => issues.filter((b) => b.blockId === id).length;
  const noAuth = authNoneSummary(blocks);

  const pickProject = async (id: string) => {
    const seq = ++pickSeq.current;
    setProjectId(id);
    setCtx(null);
    setProjectError(null);
    if (!id) return;
    setResolving(true);
    try {
      const detail = await projectsApi.get(id);
      if (!mayMaintainProject(effectiveRole(detail), user?.role_level ?? 0, detail.ungoverned)) {
        if (seq === pickSeq.current) setProjectError(`You need the Maintainer role on ${detail.project.name} to create here.`);
        return;
      }
      const platform = tenantPlatformOf(findTenantAccount(detail.project.tenant_id, user?.tenants, tenantDirectory));
      const templates = pickTemplates(await githubTemplatesApi.list(detail.project.connection_id), platform);
      if (seq !== pickSeq.current) return;
      const org = connections.find((c) => c.id === detail.project.connection_id)?.org ?? '';
      setCtx({ projectId: id, org, templates, existingRepos: detail.repositories.map((r) => r.name) });
      setStep('compose');
    } catch (e) {
      if (seq === pickSeq.current) setProjectError(e instanceof Error ? e.message : 'Failed to load the project.');
    } finally {
      if (seq === pickSeq.current) setResolving(false);
    }
  };
  const reveal = (id: string) => {
    setExpanded((s) => new Set(s).add(id));
    setTimeout(() => document.getElementById(id)?.scrollIntoView({ behavior: 'smooth', block: 'start' }), 0);
  };
  const addBlock = (kind: BlockKind) => {
    const b = newBlock(kind);
    setBlocks((bs) => [...bs, b]);
    reveal(b.id);
  };
  const updateBlock = (b: Block) => setBlocks((bs) => bs.map((x) => (x.id === b.id ? b : x)));
  const toggle = (id: string) =>
    setExpanded((s) => {
      const next = new Set(s);
      if (!next.delete(id)) next.add(id);
      return next;
    });

  const onDraft = async () => {
    if (!ctx) return;
    setDrafting(true);
    setDraftError(null);
    try {
      const resp = await draftComposition({
        project_id: ctx.projectId,
        description: useCase.trim(),
        files: files.map((f) => ({ name: f.name, text: f.text })),
      });
      const d = fromDraft(resp, files, registry);
      if (d.blocks.length === 0) {
        setDraftError('The draft came back empty. Compose manually or describe it differently.');
        return;
      }
      setBlocks(d.blocks);
      setUnused(d.unused);
      setExpanded(new Set(d.blocks.map((b) => b.id)));
    } catch (e) {
      setDraftError(e instanceof Error ? e.message : 'The draft failed. Compose manually or try again.');
    } finally {
      setDrafting(false);
    }
  };

  const goTo = (blockId: string | null) => {
    if (!blockId) return setStep(ctx ? 'compose' : 'project');
    setStep('compose');
    reveal(blockId);
  };

  const startRun = () => {
    if (!ctx) return;
    keysRef.current = Object.fromEntries(blocks.flatMap((b) => (b.kind === 'mcp' && b.draft.keyMode === 'new' ? [[b.id, b.draft.apiKey]] : [])));
    const r = planRun(ctx.projectId, blocks, ctx.templates);
    saveRun(safeLocalStorage, r);
    setRun(r);
    topRef.current?.scrollIntoView({ behavior: 'smooth', block: 'start' });
  };
  const retry = () => {
    if (!run) return;
    const next = { ...run, error: null };
    saveRun(safeLocalStorage, next);
    setRun(next);
  };
  const reset = () => {
    if (run) clearRun(safeLocalStorage, run.projectId);
    keysRef.current = {};
    setRun(null);
    setStep(ctx ? 'compose' : 'project');
    setMode('ai');
    setUseCase('');
    setFiles([]);
    setDraftError(null);
    setBlocks([]);
    setUnused([]);
    setExpanded(new Set());
  };

  const enabled = (s: Step) => s === 'project' || (s === 'compose' ? Boolean(ctx) : Boolean(ctx) && blocks.length > 0);
  const stepIndex = STEPS.findIndex((s) => s.key === step);
  const next = STEPS[stepIndex + 1];

  const org = ctx?.org ? `${ctx.org}/` : '';
  const repos = blocks.map((b, i) => ({ b, repo: `${org}${b.draft.name || `unnamed-${b.kind}-${i + 1}`}`, files: blockFiles(b) }));
  const allFiles: [string, RepoFile][] = repos.flatMap((r) => r.files.map((f) => [`${r.repo}:${f.path}`, f] as [string, RepoFile]));
  const shown = allFiles.find(([k]) => k === preview) ?? allFiles[0];

  return (
    <OpsPage
      title="Studio"
      eyebrow="Build"
      subtitle="Compose the MCP servers and Strands agents a project needs. Studio writes the repositories and opens pull requests. Governance decides who may use them."
    >
      <div ref={topRef} className="scroll-mt-6" />

      {run ? (
        <RunView
          run={run}
          repoPath={repoPath}
          registry={registry}
          onRetry={retry}
          onReset={reset}
          needsKey={(rowKey) => {
            const it = run.items.find((i) => `pr:${i.blockId}` === rowKey);
            return Boolean(it?.mcp?.needsKey && !keysRef.current[it.blockId]);
          }}
          onKey={(rowKey, key) => {
            keysRef.current = { ...keysRef.current, [rowKey.slice('pr:'.length)]: key };
            retry();
          }}
        />
      ) : (
        <div className={OPS_CARD}>
          <div role="tablist" aria-label="Studio steps" className="flex items-stretch border-b border-emerald-100/70 overflow-x-auto">
            {STEPS.map((s, i) => {
              const active = s.key === step;
              const on = enabled(s.key);
              const composeIssues = issues.filter((b) => b.blockId).length;
              return (
                <button
                  key={s.key}
                  type="button"
                  role="tab"
                  aria-selected={active}
                  disabled={!on}
                  title={on ? undefined : s.key === 'compose' ? 'Pick a project first' : 'Add a component first'}
                  onClick={() => setStep(s.key)}
                  className={`flex items-center gap-2 px-5 py-3 text-sm border-b-2 -mb-px transition-colors whitespace-nowrap disabled:cursor-not-allowed disabled:opacity-40 ${
                    active ? 'border-emerald-600 text-slate-900 font-semibold' : 'border-transparent text-slate-500 hover:text-slate-700'
                  }`}
                >
                  <span
                    className={`inline-flex items-center justify-center w-5 h-5 rounded-full text-[11px] font-semibold ${
                      active ? 'bg-emerald-600 text-white' : 'bg-slate-100 text-slate-500'
                    }`}
                  >
                    {i + 1}
                  </span>
                  {s.label}
                  {s.key === 'project' && ctx && projectName && <span className="font-mono text-xs font-normal text-slate-500">{projectName}</span>}
                  {s.key === 'compose' && blocks.length > 0 && (
                    <span
                      className={`text-[10px] font-semibold px-1.5 rounded-full ${composeIssues ? 'bg-rose-50 text-rose-700' : 'bg-emerald-50 text-emerald-700'}`}
                      title={composeIssues ? `${composeIssues} to fix` : 'All components ready'}
                    >
                      {composeIssues || blocks.length}
                    </span>
                  )}
                </button>
              );
            })}
          </div>

          <div className="p-5">
            {step === 'project' && (
              <ProjectStep
                projects={projects}
                value={projectId}
                resolving={resolving}
                error={projectError}
                onPick={(id) => void pickProject(id)}
                onCreate={() => setCreatingProject(true)}
              />
            )}

            {step === 'compose' &&
              (drafting ? (
                <DraftingSkeleton />
              ) : blocks.length === 0 && mode === 'ai' ? (
                <AiBox
                  useCase={useCase}
                  setUseCase={setUseCase}
                  files={files}
                  setFiles={setFiles}
                  error={draftError}
                  manualRef={manualRef}
                  onDraft={() => void onDraft()}
                  onManual={() => setMode('manual')}
                />
              ) : blocks.length === 0 ? (
                <div className="max-w-xl mx-auto text-center py-8">
                  <h3 className="text-sm font-semibold text-slate-800">No components yet</h3>
                  <p className="text-xs text-slate-500 mt-1 mb-4">Add the MCP servers and agents this project needs. Each one becomes its own repository.</p>
                  <AddComponent primary onAdd={addBlock} />
                  <p className="mt-4">
                    <button type="button" onClick={() => setMode('ai')} className="text-xs font-medium text-emerald-700 hover:underline">
                      Compose with AI instead
                    </button>
                  </p>
                </div>
              ) : (
                <div>
                  <div className="flex items-start justify-between gap-3 mb-3">
                    <div>
                      <h3 className="text-sm font-semibold text-slate-800">Components</h3>
                      <p className="text-xs text-slate-500 mt-0.5">
                        {compositionLabel(blocks)} in <span className="font-mono">{projectName}</span>. Each becomes its own repository and pull request.
                      </p>
                    </div>
                  </div>
                  <div className="space-y-3">
                    {blocks.map((b) => (
                      <BlockCard
                        key={b.id}
                        block={b}
                        blocks={blocks}
                        registry={registry}
                        open={expanded.has(b.id)}
                        issues={blockIssues(b.id)}
                        onToggle={() => toggle(b.id)}
                        onChange={updateBlock}
                        onRemove={() => setBlocks((bs) => removeBlock(bs, b.id))}
                      />
                    ))}
                  </div>
                  <div className="mt-3">
                    <AddComponent onAdd={addBlock} />
                  </div>
                  {unused.length > 0 && (
                    <div className="mt-4">
                      <UnusedFiles
                        files={unused}
                        blocks={blocks}
                        onAssign={(f, target) => {
                          setBlocks((bs) => assignFile(bs, f, target));
                          setUnused((u) => u.filter((x) => x.id !== f.id));
                          reveal(target.split(':')[0]);
                        }}
                        onRemove={(f) => setUnused((u) => u.filter((x) => x.id !== f.id))}
                      />
                    </div>
                  )}
                </div>
              ))}

            {step === 'review' && (
              <div>
                <div className="mb-4">
                  <h3 className="text-sm font-semibold text-slate-800">What gets committed</h3>
                  <p className="text-xs text-slate-500 mt-0.5">
                    {repos.length} {repos.length === 1 ? 'repository' : 'repositories'} in <span className="font-mono">{projectName}</span>, each on a
                    new <span className="font-mono">studio/</span> branch with one pull request. Never the default branch. Click a file to read it.
                  </p>
                </div>
                <div className="grid grid-cols-1 lg:grid-cols-[19rem_1fr] gap-4">
                  <div className="space-y-3">
                    {repos.map(({ b, repo, files: fs }) => (
                      <RepoTree
                        key={b.id}
                        repo={repo}
                        kind={b.kind === 'mcp' ? 'MCP server' : 'Strands agent'}
                        branch="studio/…"
                        files={fs}
                        selected={shown?.[0] ?? ''}
                        onSelect={setPreview}
                        footer={
                          b.kind === 'mcp'
                            ? b.draft.keyMode === 'none'
                              ? 'Authentication: none (public endpoint, not for production)'
                              : b.draft.keyMode === 'existing'
                                ? 'API key: existing secret, not in the repo'
                                : 'API key: stored in Secrets Manager at Create, not in the repo'
                            : `Declares: ${wiredNames(b.draft, blocks, registry).join(', ') || 'no MCP servers'} (usable after a Governance grant)`
                        }
                      />
                    ))}
                  </div>
                  {shown && (
                    <div className="min-w-0">
                      <div className="flex items-center justify-between gap-2 mb-1">
                        <span className="font-mono text-xs text-slate-600 truncate">{shown[0].replace(':', ' / ')}</span>
                        {shown[1].note && <span className="text-[11px] text-slate-400 shrink-0">{shown[1].note}</span>}
                      </div>
                      <pre className="max-h-[28rem] overflow-auto rounded-lg border border-slate-200/60 bg-slate-50 px-3 py-2.5 font-mono text-xs text-slate-700 whitespace-pre-wrap break-words">
                        {shown[1].content}
                      </pre>
                    </div>
                  )}
                </div>

                <div className="mt-5 pt-5 border-t border-emerald-100/70">
                  {noAuth && (
                    <p role="note" className="flex items-start gap-2 rounded-lg bg-amber-50/70 border border-amber-200/70 px-3 py-2 mb-3 text-xs text-amber-800">
                      <svg className="w-4 h-4 shrink-0 text-amber-500" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5} aria-hidden="true">
                        <path strokeLinecap="round" strokeLinejoin="round" d="M12 9v3.75m9-.75a9 9 0 11-18 0 9 9 0 0118 0zm-9 3.75h.008v.008H12v-.008z" />
                      </svg>
                      <span>
                        <span className="font-semibold">{noAuth}.</span> {noAuth.startsWith('1 ') ? 'Its gateway calls the API' : 'Their gateways call the APIs'} without credentials: public endpoints only, not for production.
                      </span>
                    </p>
                  )}
                  {issues.length > 0 && (
                    <div className="rounded-lg bg-rose-50 border border-rose-200/70 px-3 py-2 mb-4">
                      <p className="text-xs font-semibold text-rose-800 mb-1">Fix these before you create</p>
                      <ul className="space-y-0.5">
                        {issues.map((x, i) => {
                          const b = blocks.find((y) => y.id === x.blockId);
                          return (
                            <li key={`${x.blockId}-${i}`} className="text-xs text-rose-700 flex flex-wrap items-center gap-x-2">
                              {b && <span className="font-mono font-semibold">{blockName(b)}:</span>}
                              <span>{x.text}</span>
                              <button type="button" onClick={() => goTo(x.blockId)} className="text-[11px] font-medium text-rose-800 underline">
                                Go to {b ? blockName(b) : 'project'}
                              </button>
                            </li>
                          );
                        })}
                      </ul>
                    </div>
                  )}
                  <div className="flex flex-col md:flex-row md:items-center justify-between gap-3">
                    <p className="text-xs text-slate-500 max-w-2xl">
                      Create makes every repository, stores any API keys, and opens one pull request per component. Builds start after they are all
                      merged in GitHub. Studio never grants access or approves a listing.
                    </p>
                    <button type="button" onClick={startRun} disabled={issues.length > 0} className={`${OPS_PRIMARY_BTN} shrink-0 disabled:opacity-40`}>
                      Create {compositionLabel(blocks)}
                    </button>
                  </div>
                </div>
              </div>
            )}
          </div>

          {step !== 'review' && !(step === 'compose' && (drafting || blocks.length === 0)) && (
            <div className="flex items-center justify-between gap-2 px-5 py-4 border-t border-emerald-100/70">
              <button type="button" onClick={() => setStep(STEPS[stepIndex - 1]?.key ?? 'project')} disabled={stepIndex === 0} className={SECONDARY_BTN}>
                Back
              </button>
              <button type="button" onClick={() => setStep(next.key)} disabled={!enabled(next.key)} className={`${OPS_PRIMARY_BTN} disabled:opacity-40`}>
                Next: {next.label}
              </button>
            </div>
          )}
        </div>
      )}

      {creatingProject && (
        <CreateProjectModal
          connections={connections}
          onClose={() => setCreatingProject(false)}
          onCreated={(p) => {
            setCreatingProject(false);
            setProjects((ps) => [...ps.filter((x) => x.id !== p.id), { id: p.id, name: p.name }]);
            void pickProject(p.id);
          }}
        />
      )}
    </OpsPage>
  );
}
