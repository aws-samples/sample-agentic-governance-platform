import { useCallback, useEffect, useMemo, useRef, useState, type JSX } from 'react';
import { useNavigate } from 'react-router-dom';

import {
  connectionsApi,
  projectsApi,
  type Connection,
  type Project,
} from '../../api/client';
import { useLoad } from '../../api/useLoad';
import { useUser } from '../../contexts/useUser';
import { tenantSelectOptions } from '../governance/tenantUi';
import { useTenantDirectory } from '../governance/useTenantDirectory';
import { ModalShell } from './ModalShell';
import OpsPage from './OpsPage';
import { orgLabel } from './opsLabels';
import { OPS_CARD, OPS_PRIMARY_BTN, OPS_TABLE_DIVIDE, OPS_TABLE_HEAD } from './opsUi';

const FIELD_LABEL = 'block text-[11px] uppercase tracking-wide text-slate-400 font-medium mb-1';
const FIELD_INPUT =
  'w-full px-3 py-2 text-sm rounded-lg border border-slate-300 focus:outline-none focus:ring-2 focus:ring-emerald-500/40';
const FIELD_SELECT =
  'w-full px-3 py-2 text-sm rounded-lg border border-slate-300 bg-white focus:outline-none focus:ring-2 focus:ring-emerald-500/40 disabled:opacity-40';

function formatDate(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' });
}

function connectionLabel(connections: Map<string, Connection>, connectionId: string): string {
  return orgLabel(connections.get(connectionId), connectionId);
}

export default function Projects(): JSX.Element {
  const navigate = useNavigate();

  const [showCreate, setShowCreate] = useState(false);

  const { state, reload } = useLoad(
    () =>
      projectsApi.list().then(async (projects) => ({
        projects,
        connections: await connectionsApi.list().catch(() => [] as Connection[]),
      })),
    [],
    'Failed to load projects.',
  );
  const loading = state.kind === 'loading';
  const error = state.kind === 'error' ? state.message : null;
  const data = state.kind === 'ready' ? state.data : null;
  const projects = data?.projects ?? [];
  const connections = data?.connections ?? [];

  const connectionsById = useMemo(
    () => new Map((data?.connections ?? []).map((c) => [c.id, c])),
    [data],
  );

  return (
    <OpsPage
      backTo="/ops"
      title="Projects"
      subtitle="Projects group related agents and repositories under a single owner for provisioning and governance."
      action={
        <button
          type="button"
          onClick={() => setShowCreate(true)}
          className={OPS_PRIMARY_BTN}
          data-testid="project-new"
        >
          New project
        </button>
      }
    >
      {error ? (
        <div className="bg-white/80 backdrop-blur rounded-xl border border-rose-200/70 shadow-sm p-6">
          <h3 className="text-sm font-semibold text-rose-700">Couldn’t load projects</h3>
          <p className="text-sm text-slate-600 mt-1">{error}</p>
          <button
            type="button"
            onClick={reload}
            className="mt-3 px-3 py-1.5 rounded-lg bg-white border border-slate-300 text-slate-700 text-xs font-medium hover:bg-slate-50 transition-colors"
          >
            Retry
          </button>
        </div>
      ) : (
        <div className={`${OPS_CARD} overflow-hidden`}>
          <table className="w-full text-sm">
            <thead className={OPS_TABLE_HEAD}>
              <tr>
                <th className="text-left font-medium px-4 py-2.5">Project</th>
                <th className="text-left font-medium px-4 py-2.5">Org</th>
                <th className="text-left font-medium px-4 py-2.5">Created</th>
              </tr>
            </thead>
            <tbody className={OPS_TABLE_DIVIDE}>
              {loading && (
                <tr>
                  <td colSpan={3} className="px-4 py-8 text-center text-slate-400 text-sm">
                    Loading projects…
                  </td>
                </tr>
              )}

              {!loading &&
                projects.map((p) => (
                  <tr
                    key={p.id}
                    onClick={() => navigate(`/ops/projects/${p.id}`)}
                    role="button"
                    tabIndex={0}
                    aria-label={`Open ${p.name}`}
                    onKeyDown={(e) => {
                      if (e.key === 'Enter' || e.key === ' ') {
                        e.preventDefault();
                        navigate(`/ops/projects/${p.id}`);
                      }
                    }}
                    className="hover:bg-emerald-50/40 transition-colors cursor-pointer"
                  >
                    <td className="px-4 py-3">
                      <div className="font-medium text-slate-900">{p.name}</div>
                      {p.description && (
                        <div className="text-xs text-slate-500 truncate max-w-md">
                          {p.description}
                        </div>
                      )}
                    </td>
                    <td className="px-4 py-3 text-slate-600">
                      {connectionLabel(connectionsById, p.connection_id)}
                    </td>
                    <td className="px-4 py-3 text-slate-500 whitespace-nowrap">
                      {formatDate(p.created_at)}
                    </td>
                  </tr>
                ))}

              {!loading && projects.length === 0 && (
                <tr>
                  <td colSpan={3} className="px-4 py-8 text-center text-slate-400 text-sm">
                    No projects yet. Create one to group repositories under an org.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      )}

      {showCreate && (
        <CreateProjectModal
          connections={connections}
          onClose={() => setShowCreate(false)}
          onCreated={() => {
            setShowCreate(false);
            reload();
          }}
        />
      )}
    </OpsPage>
  );
}

export function CreateProjectModal({
  connections,
  onClose,
  onCreated,
}: {
  connections: Connection[];
  onClose: () => void;
  onCreated: (project: Project) => void;
}) {
  const { user } = useUser();
  const isAdmin = (user?.role_level ?? 0) >= 2;
  const tenantDirectory = useTenantDirectory(isAdmin);
  const tenantOptions = tenantSelectOptions(user?.tenants ?? [], tenantDirectory, isAdmin);

  const [name, setName] = useState('');
  const [connectionId, setConnectionId] = useState('');
  const [tenantId, setTenantId] = useState('');
  const [description, setDescription] = useState('');

  const [actionPending, setActionPending] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);

  const nameRef = useRef<HTMLInputElement>(null);

  const mountedRef = useRef(true);
  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  useEffect(() => {
    nameRef.current?.focus();
  }, []);

  const canSubmit =
    name.trim().length > 0 && connectionId.length > 0 && tenantId.length > 0 && !actionPending;

  const handleSubmit = useCallback(async () => {
    if (!canSubmit) return;
    setActionPending(true);
    setActionError(null);
    try {
      const project = await projectsApi.create({
        name: name.trim(),
        connection_id: connectionId,
        tenant_id: tenantId,
        description: description.trim() || undefined,
      });
      onCreated(project);
    } catch (err: unknown) {
      if (mountedRef.current) {
        setActionError(err instanceof Error ? err.message : 'Failed to create the project.');
      }
    } finally {
      if (mountedRef.current) setActionPending(false);
    }
  }, [canSubmit, name, connectionId, tenantId, description, onCreated]);

  return (
    <ModalShell
      title="New project"
      description="A project is an empty container scoped to one org. Add repositories from enrolled templates once it exists."
      ariaLabel="New project"
      actionPending={actionPending}
      onClose={onClose}
      footer={
        <>
          <button
            type="button"
            onClick={onClose}
            disabled={actionPending}
            className="px-3.5 py-1.5 rounded-lg bg-white border border-slate-300 text-slate-700 text-sm font-medium hover:bg-slate-50 transition-colors disabled:opacity-40"
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={handleSubmit}
            disabled={!canSubmit}
            className={`${OPS_PRIMARY_BTN} disabled:opacity-40`}
            data-testid="project-create"
          >
            {actionPending ? 'Creating…' : 'Create project'}
          </button>
        </>
      }
    >
      <div>
        <label htmlFor="new-proj-name" className={FIELD_LABEL}>
          Project name
        </label>
        <input
          id="new-proj-name"
          ref={nameRef}
          type="text"
          value={name}
          onChange={(e) => setName(e.target.value)}
          disabled={actionPending}
          placeholder="my-agent-project"
          className={`${FIELD_INPUT} disabled:opacity-40`}
          autoComplete="off"
        />
      </div>

      <div>
        <label htmlFor="new-proj-connection" className={FIELD_LABEL}>
          Org connection
        </label>
        <select
          id="new-proj-connection"
          value={connectionId}
          onChange={(e) => setConnectionId(e.target.value)}
          disabled={actionPending}
          className={FIELD_SELECT}
        >
          <option value="">Select a connection</option>
          {connections.map((c) => (
            <option key={c.id} value={c.id}>
              {c.provider} · {c.org}
            </option>
          ))}
        </select>
        {connections.length === 0 && (
          <p className="text-[11px] text-slate-400 mt-1">
            No org connections yet — add one under Admin › Org Connections.
          </p>
        )}
      </div>

      <div>
        <label htmlFor="new-proj-tenant" className={FIELD_LABEL}>
          Tenant
        </label>
        <select
          id="new-proj-tenant"
          value={tenantId}
          onChange={(e) => setTenantId(e.target.value)}
          disabled={actionPending}
          className={FIELD_SELECT}
        >
          <option value="">Select a tenant</option>
          {tenantOptions.map((t) => (
            <option key={t.id} value={t.id}>
              {t.name}
            </option>
          ))}
        </select>
        {tenantOptions.length === 0 && (
          <p className="text-[11px] text-slate-400 mt-1">
            You aren’t a member of any tenant yet — ask an admin to link your Entra group.
          </p>
        )}
      </div>

      <div>
        <label htmlFor="new-proj-description" className={FIELD_LABEL}>
          Description <span className="text-slate-300 normal-case">(optional)</span>
        </label>
        <textarea
          id="new-proj-description"
          value={description}
          onChange={(e) => setDescription(e.target.value)}
          disabled={actionPending}
          rows={2}
          placeholder="What this project groups together."
          className={`${FIELD_INPUT} disabled:opacity-40`}
          autoComplete="off"
        />
      </div>

      {actionError && (
        <p className="text-sm text-red-600" role="alert">
          {actionError}
        </p>
      )}
    </ModalShell>
  );
}
