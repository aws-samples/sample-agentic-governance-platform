// templatesView.ts — the pure decisions behind the standalone Templates page (E28/T9).
//
// `Templates.tsx` renders; this file DECIDES. The split is not stylistic: vitest collects
// only `src/**/*.test.ts` and there is no jsdom, so any judgement made inside the `.tsx`
// is a judgement no test can reach. `repoRowModel.ts` / `projectRoles.ts` /
// `githubLink.ts` established this idiom; the Templates page follows it.
//
// What lives here is everything that could be WRONG rather than merely ugly: which of the
// six mutually-exclusive body states the page is in, which org stays selected across a
// connections reload, and the copy for the two empty states that must never collapse into
// one another.
//
// Deliberately client-free (structural `ConnectionLike` rather than importing `Connection`
// from `../../api/client`), for the same reason `repoRowModel.ts` declares `RepoRowSource`:
// it keeps this module out of axios's import graph so it stays a plain unit test, and it
// states the contract as "these three fields, nothing else".

// ---------------------------------------------------------------------------
// Org labelling
// ---------------------------------------------------------------------------

/** The three fields the org picker reads off a connection. */
export interface ConnectionLike {
  id: string;
  provider: string;
  org: string;
}

const PROVIDER_LABEL: Record<string, string> = {
  github: 'GitHub',
  gitlab: 'GitLab',
};

/**
 * A connection's picker label: "GitHub · acme-corp".
 *
 * Unknown providers fall through to the raw wire value rather than a placeholder — a
 * provider this build has not been taught about is still a real org the operator picked,
 * and hiding its name behind "—" would make the selected option unidentifiable.
 */
export function orgLabel(connection: ConnectionLike): string {
  const provider = PROVIDER_LABEL[connection.provider] ?? connection.provider;
  return `${provider} · ${connection.org}`;
}

/** `<option>` rows for the org `<select>`, in the API's own order. */
export function orgOptions(connections: readonly ConnectionLike[]): { value: string; label: string }[] {
  return connections.map((c) => ({ value: c.id, label: orgLabel(c) }));
}

/**
 * Which org should be selected after a connections list arrives.
 *
 * STICKY: an org the human already picked wins over the first row, so a connections
 * reload cannot silently snap the page back to org #1 while the cards below still say
 * otherwise. That snap-back is indistinguishable from "the refetch showed me the wrong
 * org's templates", which is the exact confusion a per-org catalog cannot afford.
 * Falls back to the first connection (so the grid isn't empty on open), then to ''.
 */
export function pickSelectedOrg(
  connections: readonly ConnectionLike[],
  current: string,
): string {
  if (current && connections.some((c) => c.id === current)) return current;
  return connections[0]?.id ?? '';
}

// ---------------------------------------------------------------------------
// Who may change the catalog
// ---------------------------------------------------------------------------

/**
 * ADMIN on the backend's role ladder (`core/rbac.py`: VIEWER=0, OPERATOR=1, ADMIN=2).
 *
 * Re-declared rather than imported from `settingsSections.ts`, which already names it. That
 * module mounts `.tsx` section bodies, so importing it here would pull `ConnectionsAdmin` →
 * `api/client` → axios into this module's graph and cost it the property stated at the top of
 * this file: client-free, so its tests stay plain units. `templates.test.ts` imports BOTH
 * constants and asserts they agree, which is the drift guard the import would have been.
 */
const ROLE_LEVEL_ADMIN = 2;

/**
 * May this caller change the template catalog — edit, delete, upload?
 *
 * The backend is the authority and already says no: `github_templates.py` puts
 * `require_role(Role.ADMIN)` on `POST ""`, `PATCH /{name}` and `DELETE /{name}`, while `GET ""`
 * needs only OPERATOR. That gap is the whole finding. An OPERATOR passes the read, sees every
 * card, and every Edit / Delete / Upload click they make returns 403 — three affordances that
 * cannot work, shown to the platform's entire operator population. (A pure VIEWER never gets
 * this far: they fail the OPERATOR read, so the page dead-ends before the grid.)
 *
 * Mirrors `isSectionVisible`'s shape deliberately — one function owning the altitude→number
 * mapping, so a wider ladder later (a per-org template maintainer, say) touches one place.
 *
 * Takes the level rather than the user object so it stays pure and testable: there is no jsdom
 * here, so a hook read inside the `.tsx` is unreachable by any test. `Templates.tsx` supplies
 * `user?.role_level ?? 0` — absent role is the LOWEST rung, never a permissive default, because
 * "still loading" must not render an admin control.
 */
export function canMutateTemplates(roleLevel: number): boolean {
  return roleLevel >= ROLE_LEVEL_ADMIN;
}

// ---------------------------------------------------------------------------
// Body state
// ---------------------------------------------------------------------------

/**
 * The page body's six mutually-exclusive states. A closed union with no `default` branch
 * downstream, so the compiler is the exhaustiveness test (contract C3's idiom).
 *
 * `no-connection` and `empty-catalog` are BOTH "there are no cards to show" and are
 * deliberately separate members. They are different situations with different fixes: one
 * needs an org connected under Settings, the other needs a `.zip` uploaded here. Merging
 * them would send a user with no connection hunting for an Upload button that cannot work
 * — which is why `EMPTY_STATE_COPY` below is keyed by state and asserted to differ.
 */
export type TemplatesBodyState =
  | 'loading-orgs'
  | 'connections-unreadable'
  | 'no-connection'
  | 'no-selection'
  | 'loading'
  | 'error'
  | 'empty-catalog'
  | 'grid';

export interface TemplatesBodyInput {
  /** The connections request is still in flight. */
  connectionsLoading: boolean;
  /**
   * The connections request FAILED — so how many orgs exist is unknown (E28 final review, FR-4).
   *
   * Without this field the page told ONE failure TWICE, and got it wrong the second time. A
   * rejected `connectionsApi.list()` set the error banner AND left the connections array empty, so
   * `connectionCount === 0` fell straight through to `no-connection`: a rose "couldn't load org
   * connections" sat above a full-page card reading "no org connected yet", offering an operator who
   * already HAS orgs a CTA to go create one. The filter strip was suppressed too, taking away the
   * picker they could have retried from.
   *
   * The two states are not interchangeable — "you have none" needs an org connected on another
   * page, "we could not ask" needs a retry — which is the same argument that already keeps
   * `no-connection` and `empty-catalog` apart.
   */
  connectionsError: boolean;
  /** How many org connections exist (0 ⇒ nothing to scope templates to). */
  connectionCount: number;
  /** The selected connection id ('' ⇒ nothing selected). */
  connectionId: string;
  /** The catalog request for `connectionId` is in flight. */
  catalogLoading: boolean;
  /** The catalog request's failure message, or null. */
  catalogError: string | null;
  /** How many templates the last successful catalog fetch returned. */
  templateCount: number;
}

/**
 * Total: every input maps to exactly one state.
 *
 * Precedence, and the two orderings that carry a decision:
 *
 *  • `connections-unreadable` outranks `no-connection`. An empty connections array after a FAILED
 *    read establishes nothing about how many orgs the operator has, and "you have none" is the
 *    reassuring-but-wrong reading of it. It sits below `loading-orgs` for the same reason
 *    `catalogLoading` sits above `catalogError`: an in-flight request has no verdict yet.
 *
 *  • `no-connection` outranks the catalog entirely. With no org there is no catalog to
 *    have failed, so an error left over from a deleted connection must not outlive it.
 *
 *  • `catalogLoading` outranks `catalogError` — the opposite of the admin tab body this
 *    page replaces, and the one intentional divergence. There, `error` is cleared only on
 *    success, so switching orgs after a failure re-rendered the PREVIOUS org's error on
 *    top of an in-flight fetch: the page accused the newly-picked org of a failure that
 *    had not happened to it. An in-flight request has no verdict yet; it renders as
 *    loading.
 */
export function templatesBodyState(input: TemplatesBodyInput): TemplatesBodyState {
  if (input.connectionsLoading) return 'loading-orgs';
  // A failed read AND nothing to show for it. The `=== 0` is load-bearing: rows already in hand are
  // real data and outrank a later read's failure, so a failed refetch over a populated list keeps
  // showing the list (with the banner above it) rather than replacing it with an error card.
  if (input.connectionsError && input.connectionCount === 0) return 'connections-unreadable';
  if (input.connectionCount === 0) return 'no-connection';
  if (!input.connectionId) return 'no-selection';
  if (input.catalogLoading) return 'loading';
  if (input.catalogError !== null) return 'error';
  if (input.templateCount === 0) return 'empty-catalog';
  return 'grid';
}

/**
 * Does the org picker earn its place in this state?
 *
 * `Record<TemplatesBodyState, boolean>` with no `default` branch, so a new state is a `tsc` error
 * naming this table rather than a state that quietly inherits whichever side of a `!==` chain it
 * happens to fall on. That is not hypothetical — this WAS a `state !== 'no-connection' && state !==
 * 'loading-orgs'` chain, and adding `connections-unreadable` to the union silently landed it on the
 * `true` side, showing a `<select>` whose only option reads "No orgs connected" directly above a
 * card explaining that the org list could not be read. The dead control would have contradicted the
 * card.
 *
 * `false` wherever there is nothing to filter: an org `<select>` shown to someone who has no org (or
 * whose org list is unknown) is a dead control competing with the copy that explains why.
 */
export const SHOW_FILTER_STRIP: Record<TemplatesBodyState, boolean> = {
  'loading-orgs': false,
  'connections-unreadable': false,
  'no-connection': false,
  'no-selection': true,
  loading: true,
  error: true,
  'empty-catalog': true,
  grid: true,
};

// ---------------------------------------------------------------------------
// Empty-state copy
// ---------------------------------------------------------------------------

/** What an empty state offers the user to do about it. */
export type LinkCta = { kind: 'link'; label: string; to: string };
export type UploadCta = { kind: 'upload'; label: string };
/**
 * Re-run the read that failed (E28 final review, FR-4).
 *
 * Distinct from `LinkCta` because the fix is not on another page: an operator whose connections read
 * failed already has whatever orgs they have, and sending them to go create one is an instruction
 * the product cannot honour. It is also why this state needs its own CTA variant rather than
 * borrowing `no-connection`'s — the `EmptyStateCopy<Cta>` generic makes the page read `.to` off a
 * link CTA without a runtime guard, so a state whose action is a retry has to say so in the type.
 */
export type RetryCta = { kind: 'retry'; label: string };
export type NoCta = { kind: 'none' };
export type EmptyStateCta = LinkCta | UploadCta | RetryCta | NoCta;

export interface EmptyStateCopy<Cta extends EmptyStateCta = EmptyStateCta> {
  headline: string;
  body: string;
  cta: Cta;
}

/**
 * The three card-less states' copy, keyed by state.
 *
 * Load-bearing, not decoration: each one is the only thing on screen when it renders, so
 * it is the entire answer to "why is this page blank and what do I do?".
 *
 * Each entry's CTA is typed to its SPECIFIC variant rather than the open union, which is
 * doing real work: it lets `Templates.tsx` read `.to` / `.label` without a runtime narrowing
 * guard — and a guard is the wrong tool here, because the `false` branch of
 * `cta.kind === 'link' && <StateCard/>` renders NOTHING, i.e. a copy edit would silently
 * blank the page instead of failing. With these types the same edit fails to compile.
 */
export const EMPTY_STATE_COPY: {
  'connections-unreadable': EmptyStateCopy<RetryCta>;
  'no-connection': EmptyStateCopy<LinkCta>;
  'no-selection': EmptyStateCopy<NoCta>;
  'empty-catalog': EmptyStateCopy<UploadCta>;
} = {
  // The read FAILED, so how many orgs exist is unknown. Every sentence here is about our read and
  // none of them says the operator has no org — that was the claim the `no-connection` card made
  // over this exact failure, complete with a CTA to go create an org they may already have.
  'connections-unreadable': {
    headline: 'Org connections could not be read',
    body: 'The list of connected orgs failed to load, so the template catalog cannot be scoped to one. This is not a report that no org is connected — the orgs you have are unaffected.',
    cta: { kind: 'retry', label: 'Try again' },
  },
  // A template is a REGISTERED POINTER scoped to a connection, so with no connection there is
  // nothing for a pointer to be scoped to. The fix is one page away — say which one.
  //
  // "live as template repositories inside a connected org" was the same claim D-C2 falsified
  // (see TEMPLATES_SUBTITLE): the `is_template` flag is gone and a template's source may be a
  // repository outside the org. The record is what is connection-scoped; the repository need not be.
  'no-connection': {
    headline: 'No org connected yet',
    body: 'Templates are registered per connected org, so there is nothing to list until an org is connected.',
    cta: { kind: 'link', label: 'Connect an org', to: '/ops/settings' },
  },
  'no-selection': {
    headline: 'No org selected',
    body: 'Pick an org above to browse the agent templates available in it.',
    cta: { kind: 'none' },
  },
  // The org is connected and reachable and answered with zero rows. Nothing is broken;
  // the catalog is simply new. The fix is on THIS page.
  //
  // THE BODY IS PINNED VERBATIM (E28C/T6, design 4a) and its predecessor was false in two ways at
  // once. "Upload a .zip scaffold to create the first template" named ONE of the three ways a
  // template comes to exist and omitted the two this epic makes real (seed and adopt), so the cold
  // start looked like it had a single narrow answer. And "ride on the template repository's
  // topics" described a mechanism D-C1/D-C2 deleted: metadata lives on the AGP record now, so the
  // sentence pointed an operator at GitHub topics to explain fields AGP stores itself.
  'empty-catalog': {
    headline: 'No templates in this org',
    body: 'Seed starter templates into this org, adopt an existing repository, or upload a .zip scaffold.',
    cta: { kind: 'upload', label: 'Upload template' },
  },
};

/**
 * THE PAGE SUBTITLE — pinned verbatim (E28C/T6, design 4a).
 *
 * A CONSTANT rather than a literal in the `.tsx` for this file's whole reason to exist: the
 * predecessor was a false sentence in a file no test could read, so it rotted silently for an
 * epic. Its two claims and why each was wrong:
 *
 *   "Each template is a template repository" — the `is_template` FILTER is gone (D-C1/D-C2). A
 *     template is a REGISTERED POINTER: a record carrying `source_org`/`source_repo`, which may
 *     name a repository outside this org entirely. Nothing about it is a GitHub template repo.
 *   "materializing one into a project creates a repo from it" — materialize no longer generates
 *     from a template repo; it READS the template repository at use-time (D-C2). The distinction
 *     is exactly what makes the D-C2 rebuild safe, so the copy has to carry it.
 *
 * The replacement makes three statements that are all true after this epic, and it is test-pinned
 * so the next mechanism change breaks a test instead of shipping a lie.
 */
export const TEMPLATES_SUBTITLE =
  'Registered template pointers for this connection. Metadata lives on the AGP record; materializing reads the template repository.';

/**
 * The empty catalog, said to someone who cannot fill it (finding #14).
 *
 * The ONE piece of substitute copy this task adds, and it earns its place by replacing an
 * instruction rather than by explaining a removal. `EMPTY_STATE_COPY['empty-catalog'].body` is
 * an imperative — "upload a .zip scaffold" — sitting under a CTA the gate has just taken away.
 * Leaving it would be the same over-promise as the button, only in prose, and it is the one
 * place on this page where a non-admin would otherwise be told to do something they cannot.
 *
 * Everywhere ELSE the gate renders nothing at all: no explanatory line on the cards, no tooltip
 * on the missing Upload. That is this app's established idiom for absent standing — the Settings
 * tab strip does not render a greyed Admin tab or a note about one (`visibleTabs`), and
 * GitHubLink renders no button when there is no action. A per-card "admins only" nag would be a
 * message repeated once per template, telling the operator nothing they can act on.
 */
export const EMPTY_CATALOG_READ_ONLY_BODY =
  'This org has no agent templates yet. An admin adds them; once one exists it appears here and you can materialize it into a project.';

// ---------------------------------------------------------------------------
// THE DELETE CONFIRMATION — what DELETE actually does, after E28B/T2.
//
// THIS COPY WAS THE EPIC'S OWN DEFECT CLASS, SHIPPED. The confirm read "Its GitHub repository
// will be permanently removed from the org", and T2 changed the verb underneath it: the route
// DEREGISTERS. It deletes the catalog record and leaves the repository completely alone
// (`github_templates.py`: "A 204 here means 'no longer in the catalog', never 'the repository was
// removed'"). So the platform was asking an admin to authorise an irreversible teardown, doing
// something else, and reporting nothing about the difference — a verb whose meaning moved while
// its message stayed put, which is precisely what this epic exists to stop.
//
// WHY DEREGISTER IS THE RIGHT BACKEND BEHAVIOUR, because the copy has to convey it: a registered
// template is a POINTER. Its `source_url` may name a public repository or a mirror living in
// another org, so deleting the repository behind it would be AGP destroying something it does not
// own, on the strength of a catalog row.
//
// The sentence therefore does three things and the tests pin all three: it names the act
// (deregister/remove from the catalog), it states the repository is LEFT IN PLACE, and it never
// promises a deletion of anything on the provider. The last one is asserted as an ABSENCE, because
// the failure mode here is a reassuring word nobody checked.
//
// A FUNCTION rather than a constant because the name is interpolated — and it lives here rather
// than inline in the `.tsx` for the reason at the top of this file: `window.confirm` is called
// from a component vitest never mounts, so a sentence written there is a sentence no test reads.
// ---------------------------------------------------------------------------

export function deleteTemplateConfirm(name: string): string {
  return (
    `Deregister template “${name}”? This removes it from the catalog so it can no longer be ` +
    'materialized into a project. Its repository is left in place — AGP does not delete it, ' +
    'because a template’s source may be a public repository or a mirror in another org.'
  );
}

// ---------------------------------------------------------------------------
// classifyTemplateError — the routes' FIXED `detail` literals → a sentence and a retry verdict.
//
// E28B/T2 gave these routes a status they did not have before: a registry fault is now
// `store_error` → **503**, and validation faults correctly return 422 instead of sharing one
// bucket with it. The console could not tell them apart, because the axios interceptor replaces
// the AxiosError with `new Error(response.data.detail)` (`api/client.ts:66-67`) — by the time a
// component holds the rejection THE STATUS CODE IS GONE. The `detail` literal is fixed per
// `.kind` server-side (never `str(err)`) precisely so it can be classified, which is what makes
// this table possible at all.
//
// WHY IT MATTERS THAT THESE TWO READ DIFFERENTLY. An unreadable catalog partition is TRANSIENT and
// the remedy is to wait; malformed metadata is the caller's and the remedy is to fix the input.
// Showing the store fault as a bare failure invites an admin to conclude the catalog is gone and
// re-upload templates that already exist — the exact confusion `list_templates` refuses when it
// raises `store_error` rather than returning an empty list ("you have no templates" would invite a
// re-upload). Showing it as "invalid input" is worse: it accuses the operator of a fault that is
// ours.
//
// A `Map`, not an object literal — `githubLink.ts`'s reasoning applies unchanged: the lookup key
// is a SERVER-SUPPLIED string, and an object would resolve `'toString'` / `'constructor'` to
// `Object.prototype`'s member, classifying a `detail` of "toString" as a recognized kind whose
// value is a function.
//
// `kind` is returned as the discriminator between "matched a pinned literal" and "fell through":
// `message` and `retryable` alone cannot tell those apart for a terminal detail, which is exactly
// how a backend reword would silently degrade this copy with every test still green.
// ---------------------------------------------------------------------------

/** The five `.kind` values `github_templates.py`'s `_ERROR_DETAIL` is keyed by. */
export type TemplateErrorKind =
  | 'invalid_zip'
  | 'invalid_input'
  | 'not_found'
  | 'github_error'
  | 'store_error';

/** Appended to a RETRYABLE failure, so the human is told to wait rather than to give up. */
export const TEMPLATE_RETRY_HINT = 'Try again in a moment.';

// Detail sentence → kind. Keep these BYTE-IDENTICAL to the route's `_ERROR_DETAIL` values.
const TEMPLATE_DETAIL_KINDS: ReadonlyMap<string, TemplateErrorKind> = new Map([
  ['Invalid template zip', 'invalid_zip'],
  ['Invalid template metadata', 'invalid_input'],
  ['Template not found', 'not_found'],
  ['GitHub template operation failed', 'github_error'],
  ['Template catalog is temporarily unavailable', 'store_error'],
] as const);

/**
 * The two kinds whose remedy is "wait", derived from the kind rather than by re-listing the
 * sentences — so the retry verdict cannot drift from the table above.
 *
 * `store_error` is the 503 this task is here for. `github_error` (502) joins it because a provider
 * blip is equally transient and equally not the caller's fault. The three 4xx kinds are terminal:
 * retrying the same zip, the same metadata or the same missing name returns the same answer, and
 * inviting a retry on one would be advice the product cannot honour.
 */
const TEMPLATE_RETRYABLE_KINDS: readonly TemplateErrorKind[] = ['store_error', 'github_error'];

/** What each recognized kind SAYS. Fixed sentences, so a `Record` with no default branch. */
const TEMPLATE_MESSAGES: Record<TemplateErrorKind, string> = {
  // Not "the catalog is empty" and not "your input is invalid": ours, transient, and nothing was
  // lost. The reassurance is load-bearing — an admin who reads this as data loss re-uploads.
  store_error:
    'The template catalog is temporarily unavailable, so this could not be completed. Nothing was changed, and the templates you have are unaffected.',
  github_error:
    'The template operation failed at the provider rather than here, so nothing was changed on the catalog.',
  invalid_zip:
    'That file isn’t a valid template zip. Check it unzips and carries the scaffold at its root, then upload it again.',
  invalid_input:
    'The template metadata isn’t valid. A name must be lowercase letters, digits and hyphens, starting with a letter.',
  not_found:
    'That template is no longer in this org’s catalog — someone may have removed it. Refresh the catalog.',
};

export interface TemplateErrorView {
  /** The sentence to show. Never a raw store/provider body — the route pins fixed literals. */
  message: string;
  /**
   * Is the remedy to WAIT?
   *
   * The SINGLE authority on that, and every channel reads it: the card's tint, its headline, and
   * whether a Retry button is rendered at all. A terminal failure gets no Retry — re-reading the same
   * missing template returns the same 404, and an affordance whose every click is refused is worse
   * than an absent one. (The org filter strip's own Refresh still renders in the error state, so an
   * operator is never stranded; `SHOW_FILTER_STRIP.error` is `true`.)
   */
  retryable: boolean;
  /** The matched route `.kind`, or null when nothing matched (see the note above). */
  kind: TemplateErrorKind | null;
}

/**
 * Turn a rejected `githubTemplatesApi` call into a sentence, a retry verdict and its kind.
 *
 * An UNRECOGNIZED error falls back to its own message (or `fallback` when it carries none) and is
 * treated as TERMINAL — an unknown failure is more likely a bug than contention, so inviting a
 * retry on it would be a guess presented as advice. Same rule, and the same reasoning, as
 * `classifyLinkError`.
 */
export function classifyTemplateError(err: unknown, fallback: string): TemplateErrorView {
  const raw = err instanceof Error && err.message ? err.message.trim() : '';
  const kind = TEMPLATE_DETAIL_KINDS.get(raw) ?? null;
  if (kind === null) return { message: raw || fallback, retryable: false, kind: null };
  const retryable = TEMPLATE_RETRYABLE_KINDS.includes(kind);
  return {
    message: retryable ? `${TEMPLATE_MESSAGES[kind]} ${TEMPLATE_RETRY_HINT}` : TEMPLATE_MESSAGES[kind],
    retryable,
    kind,
  };
}

// ---------------------------------------------------------------------------
// Effect contract
// ---------------------------------------------------------------------------

/**
 * The reactive inputs of the catalog fetch in `Templates.tsx`.
 *
 * The page's whole reason to exist is that picking a different org shows that org's
 * templates, and the ONLY thing that makes that true is `connectionId` appearing in the
 * catalog effect's dependency array. Drop it and the page keeps rendering org #1's cards
 * under org #2's name — a silent wrong-answer, with no error and no empty state, i.e. the
 * failure mode this page is least able to survive.
 *
 * No test in this project can observe that: there is no jsdom, so the effect never runs.
 * `templates.test.ts` therefore parses the dep array out of the `.tsx` source and asserts
 * it against this list. Keep the names identical to the component's local state.
 */
export const CATALOG_EFFECT_DEPS = ['connectionId', 'reloadNonce'] as const;
