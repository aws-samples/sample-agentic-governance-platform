// Projects — the API-backed list of project CONTAINERS (Epic 20 / Task 16). A
// Project is an empty container scoped to one org (connection); the repos it
// holds are materialized from ops-templates on the detail page. Rows come from
// projectsApi.list(); the Org column resolves connection_id → provider·org via
// connectionsApi.list(). A row navigates to /ops/projects/:id; "New project"
// opens a small create modal (name + connection + optional description).
//
// House style: emerald-on-glass Ops tokens (opsUi.ts), the OpsPage frame,
// Tailwind v4 utility strings, 2-space indent — mirroring the Deployments.tsx /
// Tenants.tsx table idiom and the ConnectionsAdmin ModalShell form idiom.

import { useCallback, useEffect, useMemo, useRef, useState, type JSX } from 'react';
import { useNavigate } from 'react-router-dom';

import {
  connectionsApi,
  projectsApi,
  type Connection,
  type Project,
} from '../../api/client';
import { useUser } from '../../contexts/UserContext';
import { tenantSelectOptions } from '../governance/tenantUi';
import { useTenantDirectory } from '../governance/useTenantDirectory';
import { ModalShell } from './ConnectionsAdmin';
import OpsPage from './OpsPage';
import { orgLabel } from './opsLabels';
import { OPS_CARD, OPS_PRIMARY_BTN, OPS_TABLE_DIVIDE, OPS_TABLE_HEAD } from './opsUi';

const FIELD_LABEL = 'block text-[11px] uppercase tracking-wide text-slate-400 font-medium mb-1';
const FIELD_INPUT =
  'w-full px-3 py-2 text-sm rounded-lg border border-slate-300 focus:outline-none focus:ring-2 focus:ring-emerald-500/40';
const FIELD_SELECT =
  'w-full px-3 py-2 text-sm rounded-lg border border-slate-300 bg-white focus:outline-none focus:ring-2 focus:ring-emerald-500/40 disabled:opacity-40';

// Human date for created_at; falls back to the raw string when unparseable.
function formatDate(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' });
}

// Resolve a connection_id → "Provider · org"; falls back to the raw id when the connection
// is missing (deleted, or not visible to this caller).
//
// E28/T10: the label itself now comes from the shared `opsLabels.orgLabel`. This copy
// rendered the RAW wire enum (`github · acme`) while `TemplatesAdmin` rendered the brand
// casing (`GitHub · acme`) — one fact, one surface, two spellings, visible when an operator
// moves between the two pages. Only the Map lookup stays local; the formatting is pinned.
function connectionLabel(connections: Map<string, Connection>, connectionId: string): string {
  return orgLabel(connections.get(connectionId), connectionId);
}

export default function Projects(): JSX.Element {
  const navigate = useNavigate();

  const [projects, setProjects] = useState<Project[]>([]);
  const [connections, setConnections] = useState<Connection[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [reloadNonce, setReloadNonce] = useState(0);

  const [showCreate, setShowCreate] = useState(false);

  // Load projects (primary — its failure is the error state), then resolve connections
  // best-effort (they feed the cosmetic Org column + the create-modal picker). A
  // connections failure — e.g. a 403 for a true OPERATOR — must NOT blank the projects
  // list; the Org column just falls back to the raw connection_id. Mirrors ProjectDetail.
  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    projectsApi
      .list()
      .then(async (projectRows) => {
        if (cancelled) return;
        setProjects(projectRows);
        setError(null);
        try {
          const connectionRows = await connectionsApi.list();
          if (!cancelled) setConnections(connectionRows);
        } catch {
          if (!cancelled) setConnections([]);
        }
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        setError(err instanceof Error ? err.message : 'Failed to load projects.');
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [reloadNonce]);

  const refetch = useCallback(() => setReloadNonce((n) => n + 1), []);

  const connectionsById = useMemo(
    () => new Map(connections.map((c) => [c.id, c])),
    [connections],
  );

  return (
    <OpsPage
      backTo="/ops"
      title="Projects"
      subtitle="Projects group related agents and repositories under a single owner for provisioning and governance."
      action={
        <button type="button" onClick={() => setShowCreate(true)} className={OPS_PRIMARY_BTN}>
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
            onClick={refetch}
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
            refetch();
          }}
        />
      )}
    </OpsPage>
  );
}

// --- Create-project modal ---------------------------------------------------
// Reuses the shared ModalShell (ConnectionsAdmin) + the AddConnectionModal
// mechanics (mountedRef guard, actionPending, canSubmit, inline <p role="alert">).
// Fields: name, connection (from the passed-in list), optional description →
// projectsApi.create. The parent closes + refetches on success.
function CreateProjectModal({
  connections,
  onClose,
  onCreated,
}: {
  connections: Connection[];
  onClose: () => void;
  onCreated: () => void;
}) {
  // Tenant select source (E24/T6): the caller's memberships, or the full admin
  // directory for role_level >= 2 (degrades to memberships while it loads).
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

  // Unmount guard — create resolves async and can land after the modal closes.
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
      await projectsApi.create({
        name: name.trim(),
        connection_id: connectionId,
        tenant_id: tenantId,
        description: description.trim() || undefined,
      });
      onCreated();
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
          >
            {actionPending ? 'Creating…' : 'Create project'}
          </button>
        </>
      }
    >
      {/* Project name. */}
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

      {/* Connection (org). */}
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

      {/* Tenant (required, E24/T6) — options from the caller's memberships, or
          the full directory for admins. */}
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

      {/* Description (optional). */}
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
