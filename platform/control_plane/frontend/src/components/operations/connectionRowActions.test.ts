// connectionRowActions.test.ts — the connections-admin row's action split, as UNITS (E28D/T4).
//
// WHY THIS FILE EXISTS AT ALL. `ConnectionsAdmin.tsx` is 1471 lines with ZERO tests of its own,
// and vitest here collects `src/**/*.test.ts` in a node environment with NO jsdom — so nothing in
// that file can be mounted, clicked or rendered by any test in this project. The row-action split
// it shipped had no stated rule (an unconditional "Roll out templates" sitting inline as a peer of
// the row's health verb, while the OAuth-client action that unblocks every human in the org hid
// behind the kebab), and an unstated rule is a rule that drifts. Moving the decision into
// `connectionRowActions.ts` is what makes it assertable; this file is the assertion.
//
// E28B's lesson (nine vacuous guards) is the shape rule: no test below reads the module's own
// constants back to itself or greps a `.tsx` for a class name. Every case calls the function and
// asserts its OUTPUT for a given connection.

import { describe, expect, it } from 'vitest';

import {
  connectionRowActions,
  oauthClientSubline,
  templatesReconcilePath,
  type ConnectionRowSource,
} from './connectionRowActions.ts';

// --- Fixtures --------------------------------------------------------------
// Shaped as the three fields of `Connection` (api/client.ts:1553-1572) this module reads.

const conn = (over: Partial<ConnectionRowSource> = {}): ConnectionRowSource => ({
  status: 'connected',
  auth_type: 'github_app',
  has_oauth_client: true,
  ...over,
});

const keys = (items: { key: string }[]) => items.map((i) => i.key);

describe('connectionRowActions — inline is the row\'s ONE health/setup verb', () => {
  it('gives a pending github_app row Finish setup, and nothing else inline', () => {
    // A pending App connection cannot be Re-tested into shape (minting against
    // installation_id=None fails), so the two verbs must mutually exclude.
    const { inline } = connectionRowActions(conn({ status: 'pending', auth_type: 'github_app' }));
    expect(keys(inline)).toEqual(['finish-setup']);
    expect(inline[0].label).toBe('Finish setup');
    expect(inline[0].kind).toBe('default');
  });

  it('gives every other row Re-test, and nothing else inline', () => {
    for (const c of [
      conn({ status: 'connected', auth_type: 'github_app' }),
      conn({ status: 'error', auth_type: 'github_app' }),
      conn({ status: 'connected', auth_type: 'pat' }),
      conn({ status: 'error', auth_type: 'pat' }),
      // A PAT row can be pending without needing finalize — there is no install to resolve,
      // so Re-test is the honest verb. This is the case the `&&` in the rule exists for.
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

  it('keeps "Roll out templates" OUT of the inline slot', () => {
    // The regression this task closes: the rarest action, opening a heavy provider-backed modal,
    // was an unconditional inline peer of the health verb.
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

  it('separates Delete from the item above it with a divider — and ONLY Delete', () => {
    // There was no divider before this change: Delete sat flush against its neighbour in a
    // two-item menu, which is a mis-click waiting to happen on the one irreversible action.
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
  it('offers the OAuth-client item to github_app rows ONLY', () => {
    // The OAuth client is a property of a GitHub App; a PAT connection has none to add.
    expect(keys(connectionRowActions(conn({ auth_type: 'github_app' })).menu)).toContain('oauth-client');
    expect(keys(connectionRowActions(conn({ auth_type: 'pat' })).menu)).not.toContain('oauth-client');
  });

  it('offers Replace token to non-github_app rows ONLY', () => {
    // An App mints its own installation tokens — there is no stored token to replace, so the
    // affordance would be a click with nothing behind it.
    expect(keys(connectionRowActions(conn({ auth_type: 'pat' })).menu)).toContain('replace-token');
    expect(keys(connectionRowActions(conn({ auth_type: 'github_app' })).menu)).not.toContain('replace-token');
  });

  it('never offers both token affordances to the same row', () => {
    for (const auth_type of ['pat', 'github_app'] as const) {
      const k = keys(connectionRowActions(conn({ auth_type })).menu);
      expect(k.includes('oauth-client') && k.includes('replace-token')).toBe(false);
    }
  });

  it('flips the OAuth-client label on whether a client is already stored', () => {
    // The two labels name DIFFERENT consequences: adding unblocks account linking for the whole
    // org, replacing rotates a secret that already works.
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
  it('targets the Templates page with the row\'s connection id', () => {
    // `?connection=` is the plumbing the Templates page ALREADY reads twice: it seeds the
    // catalog's org (Templates.tsx:115) and it opens the reconcile surface for that row
    // (Templates.tsx:257). So the link needs no second param to arrive in the right place.
    expect(templatesReconcilePath('conn-123')).toBe('/ops/templates?connection=conn-123');
  });

  it('encodes an id that would otherwise break the query string', () => {
    expect(templatesReconcilePath('a&b=c d')).toBe('/ops/templates?connection=a%26b%3Dc%20d');
  });

  it('omits `seed=1` — the seed prompt belongs to the post-install arrival, not to a browse', () => {
    // ConnectionCallback deep-links with `&seed=1` because that operator just finished an
    // install and IS being asked for consent to seed. An admin clicking a row action is
    // inspecting, so asking them to consent to a write they did not ask for is a mis-prompt.
    expect(templatesReconcilePath('conn-123')).not.toContain('seed');
  });
});

describe('oauthClientSubline — the amber line becomes the affordance', () => {
  it('is absent on a PAT row', () => {
    // The line makes a claim about a GitHub App's OAuth client. A PAT row has no App, so the
    // line would be reporting on something that does not exist.
    expect(oauthClientSubline(conn({ auth_type: 'pat', has_oauth_client: false }))).toBeNull();
    expect(oauthClientSubline(conn({ auth_type: 'pat', has_oauth_client: true }))).toBeNull();
  });

  it('is ACTIONABLE and warn-toned when the client is missing', () => {
    // "Not set" is the state that blocks every human in the org from linking their account.
    // The operator reads the problem in the Status cell — they should be able to act on it there,
    // instead of hunting for the verb in the Actions column.
    const line = oauthClientSubline(conn({ auth_type: 'github_app', has_oauth_client: false }));
    expect(line).not.toBeNull();
    expect(line?.actionable).toBe(true);
    expect(line?.tone).toBe('warn');
    expect(line?.label).toBe('OAuth client: not set');
  });

  it('is inert and muted once a client is stored', () => {
    // Nothing is blocked, so a button here would invite an accidental secret rotation. The
    // rotation stays in the kebab, where a deliberate act belongs.
    const line = oauthClientSubline(conn({ auth_type: 'github_app', has_oauth_client: true }));
    expect(line).not.toBeNull();
    expect(line?.actionable).toBe(false);
    expect(line?.tone).toBe('muted');
    expect(line?.label).toBe('OAuth client: ready');
  });

  it('opens the SAME action the kebab item opens when actionable', () => {
    // Two affordances for one modal is fine; two affordances that diverge is the bug this
    // whole task is about.
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

  it('never claims readiness it cannot see — status does not enter the line', () => {
    // The OAuth client is NOT part of the connection's health: an App works fine without one,
    // and a connection in `error` can still have a perfectly good client stored. Letting
    // `status` recolor this line would conflate two independent facts.
    for (const status of ['connected', 'error', 'pending'] as const) {
      expect(oauthClientSubline(conn({ status, has_oauth_client: true }))?.tone).toBe('muted');
      expect(oauthClientSubline(conn({ status, has_oauth_client: false }))?.tone).toBe('warn');
    }
  });
});
