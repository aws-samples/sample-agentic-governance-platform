import { useEffect, useState, type JSX } from 'react';
import { useNavigate } from 'react-router-dom';

import { projectsApi, type Project, type Repository } from '../../api/client';
import { safeLocalStorage } from '../../api/safeLocalStorage';
import { useUser } from '../../contexts/useUser';
import { findTenantAccount } from '../governance/tenantUi';
import { useTenantDirectory } from '../governance/useTenantDirectory';
import AddRepoModal from './AddRepoModal.tsx';
import { ModalShell } from './ModalShell';
import { LAST_PROJECT_KEY, OBJECT_NOUN_INLINE, defaultProjectId, objectPath } from './objectRoutes';
import { OPS_PRIMARY_BTN } from './opsUi';
import { effectiveRole, mayMaintainProject } from './projectRoles';
import { ARTIFACT_KIND_OPTIONS, tenantPlatformOf, type ArtifactKind } from './templateChain.ts';

const FIELD_LABEL = 'block text-[11px] uppercase tracking-wide text-slate-400 font-medium mb-1';
const FIELD_SELECT =
  'w-full px-3 py-2 text-sm rounded-lg border border-slate-300 bg-white focus:outline-none focus:ring-2 focus:ring-emerald-500/40 disabled:opacity-40';

const KIND_BLURB: Record<ArtifactKind, string> = {
  agent: 'An agent built from a template — no-code (Harness) or code.',
  mcp_server: 'An MCP server that exposes an API as tools, behind a gateway.',
};

interface Resolved {
  projectId: string;
  connectionId: string;
  tenantPlatform: string;
  mayMaintain: boolean;
}

export default function NewObjectFlow({
  presetKind,
  onClose,
}: {
  presetKind?: ArtifactKind;
  onClose: () => void;
}): JSX.Element | null {
  const navigate = useNavigate();
  const { user } = useUser();
  const tenantDirectory = useTenantDirectory((user?.role_level ?? 0) >= 2);

  const [kind, setKind] = useState<ArtifactKind | null>(presetKind ?? null);
  const [projects, setProjects] = useState<Project[] | null>(null);
  const [projectId, setProjectId] = useState('');
  const [resolving, setResolving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [resolved, setResolved] = useState<Resolved | null>(null);

  useEffect(() => {
    let cancelled = false;
    projectsApi
      .list()
      .then((rows) => {
        if (cancelled) return;
        setProjects(rows);
        setProjectId(defaultProjectId(rows, safeLocalStorage.getItem(LAST_PROJECT_KEY)));
      })
      .catch((err: unknown) => {
        if (!cancelled) setError(err instanceof Error ? err.message : 'Failed to load projects.');
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const handleContinue = async () => {
    if (!kind || !projectId || resolving) return;
    setResolving(true);
    setError(null);
    try {
      const detail = await projectsApi.get(projectId);
      const mayMaintain = mayMaintainProject(effectiveRole(detail), user?.role_level ?? 0, detail.ungoverned);
      if (!mayMaintain) {
        setError(`You need the Maintainer role on ${detail.project.name} to create here.`);
        return;
      }
      safeLocalStorage.setItem(LAST_PROJECT_KEY, projectId);
      setResolved({
        projectId,
        connectionId: detail.project.connection_id,
        tenantPlatform: tenantPlatformOf(findTenantAccount(detail.project.tenant_id, user?.tenants, tenantDirectory)),
        mayMaintain,
      });
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : 'Failed to load the project.');
    } finally {
      setResolving(false);
    }
  };

  if (resolved && kind) {
    return (
      <AddRepoModal
        projectId={resolved.projectId}
        connectionId={resolved.connectionId}
        tenantPlatform={resolved.tenantPlatform}
        mayMaintain={resolved.mayMaintain}
        initialKind={kind}
        doneLabel={`Open ${OBJECT_NOUN_INLINE[kind]}`}
        onClose={onClose}
        onCreated={(repo?: Repository) => {
          if (repo) navigate(objectPath(repo));
          else onClose();
        }}
      />
    );
  }

  const noProjects = projects !== null && projects.length === 0;
  const title = kind && presetKind ? `New ${OBJECT_NOUN_INLINE[kind]}` : 'What are you building?';

  return (
    <ModalShell
      title={title}
      description="Pick the project it belongs to. The repository, its pipeline and the governance record are created for you."
      ariaLabel={title}
      actionPending={resolving}
      onClose={onClose}
      footer={
        <>
          <button
            type="button"
            onClick={onClose}
            disabled={resolving}
            className="px-3.5 py-1.5 rounded-lg bg-white border border-slate-300 text-slate-700 text-sm font-medium hover:bg-slate-50 transition-colors disabled:opacity-40"
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={() => void handleContinue()}
            disabled={!kind || !projectId || resolving}
            className={`${OPS_PRIMARY_BTN} disabled:opacity-40`}
          >
            {resolving ? 'Checking…' : 'Continue'}
          </button>
        </>
      }
    >
      {!presetKind && (
        <div role="radiogroup" aria-label="What are you building?" className="grid grid-cols-2 gap-3">
          {ARTIFACT_KIND_OPTIONS.map((o) => {
            const active = kind === o.kind;
            return (
              <button
                key={o.kind}
                type="button"
                role="radio"
                aria-checked={active}
                onClick={() => setKind(o.kind)}
                className={`text-left rounded-xl border p-3 transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-emerald-500/40 ${
                  active ? 'border-emerald-400 bg-emerald-50/60' : 'border-slate-200 hover:border-emerald-300'
                }`}
              >
                <span className="block text-sm font-semibold text-slate-800">{o.label}</span>
                <span className="block text-xs text-slate-500 mt-0.5">{KIND_BLURB[o.kind]}</span>
              </button>
            );
          })}
        </div>
      )}

      <div>
        <label htmlFor="new-object-project" className={FIELD_LABEL}>
          Project
        </label>
        <select
          id="new-object-project"
          value={projectId}
          onChange={(e) => setProjectId(e.target.value)}
          disabled={resolving || projects === null || noProjects}
          className={FIELD_SELECT}
        >
          <option value="">{projects === null ? 'Loading projects…' : 'Select a project'}</option>
          {(projects ?? []).map((p) => (
            <option key={p.id} value={p.id}>
              {p.name}
            </option>
          ))}
        </select>
        {noProjects && (
          <p className="text-[11px] text-slate-400 mt-1">
            No projects yet. Create one under Projects first — it sets the org and tenant.
          </p>
        )}
      </div>

      {error && (
        <p className="text-sm text-red-600" role="alert">
          {error}
        </p>
      )}
    </ModalShell>
  );
}
