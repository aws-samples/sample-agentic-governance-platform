import type { Connection, ConnStatus } from '../../api/client';
import { warnOnce } from './warnOnce';

export type ConnectionRowSource = Pick<Connection, 'status' | 'auth_type' | 'has_oauth_client'>;

/** The render contract: `navigate` is a link that leaves the page, `destructive` is rose and divided, `default` wires a handler. */
export type RowActionKind = 'default' | 'destructive' | 'navigate';

export interface RowAction {
  key: string;
  label: string;
  kind: RowActionKind;
  dividerBefore?: boolean;
}

function needsFinish(conn: ConnectionRowSource): boolean {
  return conn.status === 'pending' && conn.auth_type === 'github_app';
}

export function connectionRowActions(conn: ConnectionRowSource): {
  inline: RowAction[];
  menu: RowAction[];
} {
  const inline: RowAction[] = [
    needsFinish(conn)
      ? { key: 'finish-setup', label: 'Finish setup', kind: 'default' }
      : { key: 're-test', label: 'Re-test', kind: 'default' },
  ];

  const menu: RowAction[] = [];

  if (conn.auth_type === 'github_app') {
    menu.push({
      key: 'oauth-client',
      label: conn.has_oauth_client ? 'Replace OAuth client' : 'Add OAuth client',
      kind: 'default',
    });
  } else {
    menu.push({ key: 'replace-token', label: 'Replace token', kind: 'default' });
  }

  menu.push({ key: 'roll-out-templates', label: 'Roll out templates', kind: 'navigate' });

  menu.push({ key: 'delete', label: 'Delete', kind: 'destructive', dividerBefore: true });

  return { inline, menu };
}

const STATUS_PILL: Record<ConnStatus, { cls: string; label: string }> = {
  connected: { cls: 'bg-emerald-50 text-emerald-700', label: 'Connected' },
  error: { cls: 'bg-rose-50 text-rose-700', label: 'Error' },
  pending: { cls: 'bg-amber-50 text-amber-700', label: 'Pending install' },
};

export function connectionStatusPill(status: ConnStatus): { cls: string; label: string } {
  if (Object.prototype.hasOwnProperty.call(STATUS_PILL, status)) return STATUS_PILL[status];
  warnOnce('[connectionRowActions] connection status this frontend does not know; shown as sent:', status);
  return { cls: 'bg-slate-100 text-slate-500', label: status };
}

export function templatesReconcilePath(connectionId: string): string {
  return `/ops/templates?connection=${encodeURIComponent(connectionId)}`;
}

export interface OauthClientSubline {
  label: string;
  title: string;
  tone: 'warn' | 'muted';
  rendersAsButton: boolean;
  actionKey: 'oauth-client';
}

export function oauthClientSubline(conn: ConnectionRowSource): OauthClientSubline | null {
  if (conn.auth_type !== 'github_app') return null;
  return conn.has_oauth_client
    ? {
        label: 'OAuth client: ready',
        title: 'Members of this org can link their personal GitHub account.',
        tone: 'muted',
        rendersAsButton: false,
        actionKey: 'oauth-client',
      }
    : {
        label: 'OAuth client: not set',
        title:
          'Members cannot link their personal GitHub account until an OAuth client is added. Click to add one.',
        tone: 'warn',
        rendersAsButton: true,
        actionKey: 'oauth-client',
      };
}
