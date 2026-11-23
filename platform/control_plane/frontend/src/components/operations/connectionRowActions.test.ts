import { afterEach, describe, expect, expectTypeOf, it, vi } from 'vitest';

import type { Connection } from '../../api/client';
import {
  connectionRowActions,
  connectionStatusPill,
  oauthClientSubline,
  templatesReconcilePath,
  type ConnectionRowSource,
} from './connectionRowActions.ts';

const conn = (over: Partial<ConnectionRowSource> = {}): ConnectionRowSource => ({
  status: 'connected',
  auth_type: 'github_app',
  has_oauth_client: true,
  ...over,
});

const keys = (items: { key: string }[]) => items.map((i) => i.key);

describe('ConnectionRowSource is the wire row', () => {
  it("the client's Connection assigns to it, so a row object passes straight in", () => {
    expectTypeOf<Connection>().toExtend<ConnectionRowSource>();
  });
});

describe('connectionRowActions — inline is the row\'s ONE health/setup verb', () => {
  it('gives a pending github_app row Finish setup, and nothing else inline: a token minted against an uninstalled App fails, so Re-test cannot be the verb', () => {
    const { inline } = connectionRowActions(conn({ status: 'pending', auth_type: 'github_app' }));
    expect(keys(inline)).toEqual(['finish-setup']);
    expect(inline[0].label).toBe('Finish setup');
    expect(inline[0].kind).toBe('default');
  });

  it('gives every other row Re-test, and nothing else inline, a pending PAT row included: it has no install to finish', () => {
    for (const c of [
      conn({ status: 'connected', auth_type: 'github_app' }),
      conn({ status: 'error', auth_type: 'github_app' }),
      conn({ status: 'connected', auth_type: 'pat' }),
      conn({ status: 'error', auth_type: 'pat' }),
      conn({ status: 'pending', auth_type: 'pat' }),
    ]) {
      const { inline } = connectionRowActions(c);
      expect(keys(inline)).toEqual(['re-test']);
      expect(inline[0].label).toBe('Re-test');
    }
  });

  it('never renders more than one inline action, for any row shape', () => {
    for (const status of ['connected', 'error', 'pending'] as const) {
      for (const auth_type of ['pat', 'github_app'] as const) {
        for (const has_oauth_client of [true, false]) {
          const { inline } = connectionRowActions(conn({ status, auth_type, has_oauth_client }));
          expect(inline).toHaveLength(1);
        }
      }
    }
  });

  it('keeps "Roll out templates" OUT of the inline slot: the rarest action, opening a provider-backed modal, is no peer of the health verb', () => {
    for (const auth_type of ['pat', 'github_app'] as const) {
      const { inline } = connectionRowActions(conn({ auth_type }));
      expect(keys(inline)).not.toContain('roll-out-templates');
      expect(inline.map((i) => i.label)).not.toContain('Roll out templates');
    }
  });
});

describe('connectionRowActions — the menu carries everything else, destructive last', () => {
  it('orders a github_app row: OAuth client, roll out, then Delete', () => {
    const { menu } = connectionRowActions(conn({ auth_type: 'github_app' }));
    expect(keys(menu)).toEqual(['oauth-client', 'roll-out-templates', 'delete']);
  });

  it('orders a PAT row: Replace token, roll out, then Delete', () => {
    const { menu } = connectionRowActions(conn({ auth_type: 'pat' }));
    expect(keys(menu)).toEqual(['replace-token', 'roll-out-templates', 'delete']);
  });

  it('puts Delete last and marks it destructive, for every row shape', () => {
    for (const status of ['connected', 'error', 'pending'] as const) {
      for (const auth_type of ['pat', 'github_app'] as const) {
        for (const has_oauth_client of [true, false]) {
          const { menu } = connectionRowActions(conn({ status, auth_type, has_oauth_client }));
          const last = menu[menu.length - 1];
          expect(last.key).toBe('delete');
          expect(last.label).toBe('Delete');
          expect(last.kind).toBe('destructive');
        }
      }
    }
  });

  it('separates Delete from the item above it with a divider — and ONLY Delete: a lone irreversible action never sits flush against its neighbour', () => {
    for (const auth_type of ['pat', 'github_app'] as const) {
      const { menu } = connectionRowActions(conn({ auth_type }));
      const divided = menu.filter((i) => i.dividerBefore);
      expect(keys(divided)).toEqual(['delete']);
    }
  });

  it('never offers Delete twice, or in the inline slot', () => {
    const { inline, menu } = connectionRowActions(conn());
    expect(keys(inline)).not.toContain('delete');
    expect(menu.filter((i) => i.key === 'delete')).toHaveLength(1);
  });
});

describe('connectionRowActions — the auth-type gates', () => {
  it('offers the OAuth-client item to github_app rows ONLY: the client is a property of the App, and a PAT connection has none to add', () => {
    expect(keys(connectionRowActions(conn({ auth_type: 'github_app' })).menu)).toContain('oauth-client');
    expect(keys(connectionRowActions(conn({ auth_type: 'pat' })).menu)).not.toContain('oauth-client');
  });

  it('offers Replace token to non-github_app rows ONLY: an App mints its own installation tokens, so there is no stored token to replace', () => {
    expect(keys(connectionRowActions(conn({ auth_type: 'pat' })).menu)).toContain('replace-token');
    expect(keys(connectionRowActions(conn({ auth_type: 'github_app' })).menu)).not.toContain('replace-token');
  });

  it('never offers both token affordances to the same row', () => {
    for (const auth_type of ['pat', 'github_app'] as const) {
      const k = keys(connectionRowActions(conn({ auth_type })).menu);
      expect(k.includes('oauth-client') && k.includes('replace-token')).toBe(false);
    }
  });

  it('flips the OAuth-client label on whether a client is already stored: adding unblocks account linking for the org, replacing rotates a secret that already works', () => {
    const absent = connectionRowActions(conn({ has_oauth_client: false })).menu.find(
      (i) => i.key === 'oauth-client',
    );
    const present = connectionRowActions(conn({ has_oauth_client: true })).menu.find(
      (i) => i.key === 'oauth-client',
    );
    expect(absent?.label).toBe('Add OAuth client');
    expect(present?.label).toBe('Replace OAuth client');
  });

  it('does not let has_oauth_client change the SET of actions, only the label', () => {
    expect(keys(connectionRowActions(conn({ has_oauth_client: false })).menu)).toEqual(
      keys(connectionRowActions(conn({ has_oauth_client: true })).menu),
    );
  });
});

describe('connectionRowActions — roll out is a NAVIGATION, not a second modal mount', () => {
  it('marks the roll-out item navigate, on every row shape', () => {
    for (const auth_type of ['pat', 'github_app'] as const) {
      const item = connectionRowActions(conn({ auth_type })).menu.find(
        (i) => i.key === 'roll-out-templates',
      );
      expect(item?.kind).toBe('navigate');
      expect(item?.label).toBe('Roll out templates');
    }
  });

  it('is the only navigate item — every other action mutates in place', () => {
    for (const auth_type of ['pat', 'github_app'] as const) {
      const { inline, menu } = connectionRowActions(conn({ auth_type }));
      const nav = [...inline, ...menu].filter((i) => i.kind === 'navigate');
      expect(keys(nav)).toEqual(['roll-out-templates']);
    }
  });
});

describe('templatesReconcilePath — the one URL the navigate item points at', () => {
  it('targets the Templates page with the row\'s connection id, the one param that both scopes that page and opens its reconcile surface', () => {
    expect(templatesReconcilePath('conn-123')).toBe('/ops/templates?connection=conn-123');
  });

  it('encodes an id that would otherwise break the query string', () => {
    expect(templatesReconcilePath('a&b=c d')).toBe('/ops/templates?connection=a%26b%3Dc%20d');
  });

  it('omits `seed=1`: the seed consent belongs to the post-install arrival, and an admin browsing a row did not ask for a write', () => {
    expect(templatesReconcilePath('conn-123')).not.toContain('seed');
  });
});

describe('oauthClientSubline — the amber line becomes the affordance', () => {
  it('is absent on a PAT row: the line reports on a GitHub App\'s OAuth client, and a PAT connection has no App', () => {
    expect(oauthClientSubline(conn({ auth_type: 'pat', has_oauth_client: false }))).toBeNull();
    expect(oauthClientSubline(conn({ auth_type: 'pat', has_oauth_client: true }))).toBeNull();
  });

  it('is ACTIONABLE and warn-toned when the client is missing: the operator reads the blocking state in the Status cell and acts on it there', () => {
    const line = oauthClientSubline(conn({ auth_type: 'github_app', has_oauth_client: false }));
    expect(line).not.toBeNull();
    expect(line?.rendersAsButton).toBe(true);
    expect(line?.tone).toBe('warn');
    expect(line?.label).toBe('OAuth client: not set');
  });

  it('is inert and muted once a client is stored: a button there would invite an accidental secret rotation, which stays in the kebab', () => {
    const line = oauthClientSubline(conn({ auth_type: 'github_app', has_oauth_client: true }));
    expect(line).not.toBeNull();
    expect(line?.rendersAsButton).toBe(false);
    expect(line?.tone).toBe('muted');
    expect(line?.label).toBe('OAuth client: ready');
  });

  it('opens the SAME action the kebab item opens when it renders as a button: two affordances for one modal may not diverge', () => {
    const c = conn({ auth_type: 'github_app', has_oauth_client: false });
    const line = oauthClientSubline(c);
    const kebab = connectionRowActions(c).menu.find((i) => i.key === 'oauth-client');
    expect(line?.actionKey).toBe(kebab?.key);
  });

  it('carries a title that explains the consequence in both states', () => {
    const missing = oauthClientSubline(conn({ has_oauth_client: false }));
    const ready = oauthClientSubline(conn({ has_oauth_client: true }));
    expect(missing?.title).toContain('cannot link');
    expect(ready?.title).toContain('can link');
    expect(missing?.title).not.toBe(ready?.title);
  });

  it('never claims readiness it cannot see — status does not enter the line: an App works without a client, and an errored connection can still hold a good one', () => {
    for (const status of ['connected', 'error', 'pending'] as const) {
      expect(oauthClientSubline(conn({ status, has_oauth_client: true }))?.tone).toBe('muted');
      expect(oauthClientSubline(conn({ status, has_oauth_client: false }))?.tone).toBe('warn');
    }
  });
});

describe('connectionStatusPill', () => {
  afterEach(() => vi.restoreAllMocks());

  it('labels every declared status', () => {
    expect(connectionStatusPill('connected').label).toBe('Connected');
    expect(connectionStatusPill('error').label).toBe('Error');
    expect(connectionStatusPill('pending').label).toBe('Pending install');
  });

  it('renders the raw word for a connection status outside the union, without throwing', () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => undefined);
    const pill = connectionStatusPill('suspended' as never);
    expect(pill.label).toBe('suspended');
    expect(pill.cls).toMatch(/slate/);
    connectionStatusPill('suspended' as never);
    expect(warn).toHaveBeenCalledTimes(1);
  });
});
