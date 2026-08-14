// connectionRowActions.ts — the pure decisions behind a CONNECTIONS-ADMIN ROW (E28D/T4).
//
// `ConnectionsAdmin.tsx` renders; this file DECIDES. The split is the one `reconcileView.ts`,
// `repoRowModel.ts` and `projectRoles.ts` established, and here it is not stylistic but overdue:
// vitest collects only `src/**/*.test.ts` in a node environment with NO jsdom, and
// `ConnectionsAdmin.tsx` is 1471 lines with ZERO tests of its own. Every judgement made inside that
// file is a judgement no test can reach — which is precisely how the row's action layout drifted
// into the state this module fixes.
//
// WHAT DRIFTED. The row shipped three inline slots and a kebab with no stated rule behind the
// split, and the two ended up inverted on the axis that matters — how often the action is wanted,
// and how much it costs to click by accident:
//
//   • `Roll out templates` was UNCONDITIONALLY inline, a visual peer of the row's health verb. It
//     is the rarest action on the row and the heaviest: it opens a provider-backed modal that
//     reads the whole org.
//   • `Add OAuth client` hid in the kebab — while the Status cell's own amber sub-line called it
//     out as the thing that "blocks every human in the org from linking their account". The
//     operator read the problem in column 3 and had to hunt for the verb in column 6.
//   • `Delete` sat FLUSH against its only neighbour in a two-item menu. One irreversible action,
//     no separation, ~28px of travel.
//
// THE RULE, stated once so it can be tested:
//
//   INLINE = the row's own health/setup verb, AT MOST ONE.
//   MENU   = everything else, in escalating consequence, DESTRUCTIVE LAST behind a divider.
//
// "At most one" is enforced structurally (see `connectionRowActions`) rather than left to whoever
// adds the next action, because "one health verb" is the property that makes the inline slot
// meaningful: the moment it holds two things, it is a toolbar again and the rule is back to being
// prose nobody reads.
//
// WHY ROLL-OUT IS A `navigate` AND NOT A MENU ITEM THAT OPENS THE MODAL. The reconcile modal had
// TWO mount sites — this page and the Templates page — under two different labels ("Roll out
// templates" here, "Reconcile org" there) for one component that titles itself a third way. The
// duplicate mount was also the one that never refetched the template catalog after a write, so an
// operator who rolled out from this page saw a stale catalog on the page that owns template state.
// Deleting the mount and pointing at the Templates page leaves exactly one surface that renders
// the modal and one surface that owns the state it mutates. `kind: 'navigate'` is what tells the
// `.tsx` to render a link rather than wire a handler — the distinction is in the model, not in the
// renderer's memory.

// ---------------------------------------------------------------------------
// The fields a row actually reads, as a STRUCTURAL shape rather than an import of `Connection`.
// Same reason `repoRowModel.ts` declares `RepoRowSource`: it keeps this module out of the
// axios-importing client (so it stays unit-testable with no network shim) and makes the contract
// explicit — these three fields, nothing else. Names match the wire
// (`api/client.ts:1553-1572`), so a row object passes straight in.
// ---------------------------------------------------------------------------
export interface ConnectionRowSource {
  status: 'connected' | 'error' | 'pending';
  auth_type: 'pat' | 'github_app';
  has_oauth_client: boolean;
}

/**
 * `kind` is the RENDER CONTRACT, not decoration:
 *   • `default`     — a normal in-place action; the `.tsx` wires a handler.
 *   • `destructive` — irreversible; rose text, divider above, always last.
 *   • `navigate`    — leaves the page; the `.tsx` renders a link, not a button.
 */
export type RowActionKind = 'default' | 'destructive' | 'navigate';

export interface RowAction {
  key: string;
  label: string;
  kind: RowActionKind;
  /** Draw a separator above this item. Reserved for the destructive tail. */
  dividerBefore?: boolean;
}

// ---------------------------------------------------------------------------
// needsFinish — WHY the two health verbs mutually exclude, and why it is an `&&`.
//
// A pending GitHub App connection cannot be Re-tested into shape: minting a token against
// `installation_id = None` fails, so the test would report an error the operator cannot act on. It
// needs finalize instead. Both halves of the condition are load-bearing — a PAT row can be
// `pending` with no install to resolve, and for that row Re-test IS the honest verb.
// ---------------------------------------------------------------------------
function needsFinish(conn: ConnectionRowSource): boolean {
  return conn.status === 'pending' && conn.auth_type === 'github_app';
}

/**
 * The row's actions, split by the rule in this module's header.
 *
 * `inline` is a one-element array rather than a single value on purpose: the caller renders a list
 * either way, and a rule stated as "at most one" is better checked by a test counting a list than
 * asserted in a comment above a ternary.
 */
export function connectionRowActions(conn: ConnectionRowSource): {
  inline: RowAction[];
  menu: RowAction[];
} {
  // ONE health verb. Built as a single expression so there is no code path that appends a second.
  const inline: RowAction[] = [
    needsFinish(conn)
      ? { key: 'finish-setup', label: 'Finish setup', kind: 'default' }
      : { key: 're-test', label: 'Re-test', kind: 'default' },
  ];

  const menu: RowAction[] = [];

  // The credential affordance, exactly one of two — they are mutually exclusive by auth type, and
  // offering the wrong one is offering a click with nothing behind it. An App mints its own
  // installation tokens (no stored token to replace); a PAT connection has no App (no OAuth client
  // to add).
  if (conn.auth_type === 'github_app') {
    // The label names WHICH of the two things the admin is doing, because the consequence differs:
    // adding UNBLOCKS account linking for the whole org, replacing rotates a secret that already
    // works.
    menu.push({
      key: 'oauth-client',
      label: conn.has_oauth_client ? 'Replace OAuth client' : 'Add OAuth client',
      kind: 'default',
    });
  } else {
    menu.push({ key: 'replace-token', label: 'Replace token', kind: 'default' });
  }

  // Demoted from the inline slot. Keeps the words "Roll out templates" because that is the verb the
  // operator is heading toward (and the one the wire calls `POST /{id}/rollout`) — the destination
  // page titles the surface itself.
  menu.push({ key: 'roll-out-templates', label: 'Roll out templates', kind: 'navigate' });

  // Destructive tail. The divider is the ONLY one in the menu: a separator that appears more than
  // once stops meaning "past here, be careful".
  menu.push({ key: 'delete', label: 'Delete', kind: 'destructive', dividerBefore: true });

  return { inline, menu };
}

/**
 * Where the `navigate` action goes.
 *
 * `?connection=<id>` is plumbing the Templates page ALREADY reads, twice: it seeds the catalog's
 * org (`Templates.tsx:115`) and it resolves the row the reconcile surface opens for
 * (`Templates.tsx:257`). So one param both scopes the page and opens the dialog.
 *
 * `&seed=1` is deliberately ABSENT. `ConnectionCallback` deep-links with it because that operator
 * just completed an install and IS being asked to consent to seeding. An admin clicking a row
 * action is inspecting — prompting them for consent to a write they never asked for is a
 * mis-prompt, and `Templates.tsx:263` gates the prompt on that param precisely so callers can
 * choose.
 */
export function templatesReconcilePath(connectionId: string): string {
  return `/ops/templates?connection=${encodeURIComponent(connectionId)}`;
}

export interface OauthClientSubline {
  label: string;
  title: string;
  /** `warn` = amber, the state that blocks org members. `muted` = a quiet fact. */
  tone: 'warn' | 'muted';
  /** True ⇒ render the line as a button. See the note below on why only one state is. */
  actionable: boolean;
  /** The menu key this line's click shares, so the two affordances cannot diverge. */
  actionKey: 'oauth-client';
}

/**
 * The Status cell's OAuth-client sub-line — and whether it is itself the affordance.
 *
 * `null` for a PAT row: the line makes a claim about a GitHub App's OAuth client, and a connection
 * with no App has none to report on.
 *
 * ACTIONABLE ONLY WHEN MISSING, which is the whole point of the change. "Not set" is a blocking
 * state, and the operator reads it here — so the fix is to let them act here, rather than promoting
 * the action into the row's inline slot where it would compete with the health verb on every row
 * that is already fine. Once a client IS stored nothing is blocked, and turning that line into a
 * button would invite an accidental secret rotation from a cell nobody clicked deliberately; the
 * rotation stays in the kebab, where a deliberate act belongs.
 *
 * `status` DOES NOT ENTER THIS FUNCTION. The OAuth client is not part of the connection's health —
 * an App works fine without one, and a connection in `error` can still have a perfectly good client
 * stored. Letting the pill's state recolor this line would conflate two independent facts, which is
 * exactly why the line sits under the pill instead of recoloring it.
 */
export function oauthClientSubline(conn: ConnectionRowSource): OauthClientSubline | null {
  if (conn.auth_type !== 'github_app') return null;
  return conn.has_oauth_client
    ? {
        label: 'OAuth client: ready',
        title: 'Members of this org can link their personal GitHub account.',
        tone: 'muted',
        actionable: false,
        actionKey: 'oauth-client',
      }
    : {
        label: 'OAuth client: not set',
        title:
          'Members cannot link their personal GitHub account until an OAuth client is added. Click to add one.',
        tone: 'warn',
        actionable: true,
        actionKey: 'oauth-client',
      };
}
