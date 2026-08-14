// Templates — the agent-template catalog as a first-class Ops page (E28/T9).
//
// Templates used to be a TAB BODY inside the Ops Admin console, which is why the interim
// `/ops/templates` route rendered frame-less and heading-less: OperationsAdmin owned the
// OpsPage frame, the title and the back link. This page owns them. The cards themselves are
// the SHARED `TemplateCardGrid` — the same component the admin tab still renders, so the two
// surfaces cannot drift.
//
// THE DESIGN CRUX — templates are CONNECTION-SCOPED. A template IS an `is_template`
// repository living inside one connected org, so "the template catalog" is not a thing that
// exists platform-wide (that is why the Ops Overview's Templates KPI is a permanent
// em-dash). A standalone page must therefore make the org visible and switchable without
// pretending to be global.
//
// The org picker sits in a FILTER STRIP directly above the cards rather than up in the
// heading row, because from there re-picking visibly re-filters the grid beneath it —
// switching orgs should feel like changing a filter, not like navigating to another page.
// The heading row keeps only the page-level primary action (Upload template), which is where
// every other Ops page puts its primary.
//
// Every judgement this page makes — which of six body states it is in, which org survives a
// connections reload, and the copy for the two distinct empty states — lives in the pure
// `templatesView.ts`, because vitest here collects only `src/**/*.test.ts` with no jsdom, so
// logic left in this file would be logic no test can reach. Same split as `repoRowModel.ts`.
//
// House style: emerald-on-glass Ops tokens (opsUi.ts), the OpsPage frame, Tailwind v4
// utility strings, inline SVG only, 2-space indent — mirroring Projects.tsx / ProjectDetail.tsx.

import { useCallback, useEffect, useMemo, useState, type JSX } from 'react';
import { Link, useSearchParams } from 'react-router-dom';

import { connectionsApi, githubTemplatesApi } from '../../api/client';
import type { Connection, TemplateView } from '../../api/client';
// The role, from the same context Projects.tsx and Settings.tsx read it from.
import { useUser } from '../../contexts/UserContext';
// Explicit `.tsx` on every component import, load-bearing rather than noise: with
// `allowImportingTsExtensions` on a case-insensitive filesystem (macOS, Windows), an
// extensionless specifier resolves to a `.ts` sibling FIRST, and a sibling whose name
// differs only in casing then imports as `undefined` with NO error — a blank page at
// runtime and a clean `tsc`. This page is the live example: `./Templates` resolves toward
// `templatesView.ts`. Same trap documented at `githubLinkApi.test.ts:18-20` and
// `App.tsx:41-45`. The pure-logic import below keeps its `.ts` for the same reason.
import OpsPage from './OpsPage.tsx';
import RolloutTemplatesModal from './RolloutTemplatesModal.tsx';
import TemplateCardGrid from './TemplateCardGrid.tsx';
import { EditTemplateModal, UploadTemplateModal } from './TemplateModals.tsx';
import { OPS_CARD, OPS_PRIMARY_BTN } from './opsUi.ts';
import {
  EMPTY_CATALOG_READ_ONLY_BODY,
  EMPTY_STATE_COPY,
  SHOW_FILTER_STRIP,
  TEMPLATES_SUBTITLE,
  canMutateTemplates,
  classifyTemplateError,
  deleteTemplateConfirm,
  orgLabel,
  orgOptions,
  pickSelectedOrg,
  templatesBodyState,
  type TemplateErrorView,
} from './templatesView.ts';

// Inline SVG paths (Heroicons outline, 24px) — no icon dependency in this project.
const ICON_FUNNEL =
  'M12 3c2.755 0 5.455.232 8.083.678.533.09.917.556.917 1.096v1.044a2.25 2.25 0 01-.659 1.591l-5.432 5.432a2.25 2.25 0 00-.659 1.591v2.927a2.25 2.25 0 01-1.244 2.013L9.75 21v-6.568a2.25 2.25 0 00-.659-1.591L3.659 7.409A2.25 2.25 0 013 5.818V4.774c0-.54.384-1.006.917-1.096A48.32 48.32 0 0112 3z';
const ICON_PLUG =
  'M13.19 8.688a4.5 4.5 0 011.242 7.244l-4.5 4.5a4.5 4.5 0 01-6.364-6.364l1.757-1.757m13.35-.622l1.757-1.757a4.5 4.5 0 00-6.364-6.364l-4.5 4.5a4.5 4.5 0 001.242 7.244';
const ICON_UPLOAD =
  'M12 16.5V9.75m0 0l3 3m-3-3l-3 3M6.75 19.5a4.5 4.5 0 01-1.41-8.775 5.25 5.25 0 0110.233-2.33 3 3 0 013.758 3.848A3.752 3.752 0 0118 19.5H6.75z';

/** Slate-tinted empty/CTA state card. Centered, generous — it is the only thing on screen. */
function StateCard(props: { iconPath: string; headline: string; body: string; children?: JSX.Element }) {
  const { iconPath, headline, body, children } = props;
  return (
    <div className={`${OPS_CARD} px-6 py-14 flex flex-col items-center text-center`}>
      <div className="w-11 h-11 rounded-xl bg-emerald-50 flex items-center justify-center text-emerald-600">
        <svg
          className="w-5 h-5"
          fill="none"
          viewBox="0 0 24 24"
          strokeWidth={1.6}
          stroke="currentColor"
          aria-hidden="true"
        >
          <path strokeLinecap="round" strokeLinejoin="round" d={iconPath} />
        </svg>
      </div>
      <h2 className="mt-4 text-base font-semibold text-slate-900">{headline}</h2>
      <p className="mt-1.5 text-sm text-slate-500 max-w-md">{body}</p>
      {children && <div className="mt-5">{children}</div>}
    </div>
  );
}

export default function Templates(): JSX.Element {
  // --- Standing (finding #14) ----------------------------------------------
  // The catalog is READABLE by an operator and WRITABLE only by an admin — `GET
  // /github-templates` is `require_role(Role.OPERATOR)`, while POST / PATCH / DELETE are all
  // ADMIN. This page used to ignore that entirely and show Edit, Delete and Upload to
  // everyone who could see a card, so an operator got a 403 from all three. The predicate
  // lives in `templatesView.ts` because there is no jsdom here: a comparison written in this
  // file is a comparison no test can reach. `?? 0` is the loading case — no role yet is the
  // lowest rung, never a permissive default.
  const { user } = useUser();
  const canMutate = canMutateTemplates(user?.role_level ?? 0);

  // --- Org connections (templates are per-org) -----------------------------
  const [connections, setConnections] = useState<Connection[]>([]);
  const [connLoading, setConnLoading] = useState(true);
  const [connError, setConnError] = useState<string | null>(null);
  // Seeded from `?connection=` so the catalog behind a post-finalize arrival is the org the prompt
  // is about. `pickSelectedOrg` (in the connections effect) then either KEEPS this value — it
  // matches a real row — or falls back to the first connection, which is the silent, correct
  // degradation for a URL naming a row this operator cannot see.
  const [connectionId, setConnectionId] = useState<string>(
    () => new URLSearchParams(window.location.search).get('connection') ?? '',
  );
  // A SECOND nonce, for the connections read specifically. The catalog nonce below cannot re-run it,
  // and the catalog read it does re-run is gated on a selected org — which is exactly what a failed
  // connections read leaves the page without, so the existing Refresh could not recover this state.
  const [connNonce, setConnNonce] = useState(0);

  // --- Catalog (scoped to the selected org) --------------------------------
  const [templates, setTemplates] = useState<TemplateView[]>([]);
  const [loading, setLoading] = useState(false);
  // The CLASSIFIED failure, not the raw message (E28B/T6, item 4). T2 gave these routes a new
  // status — a registry fault is `store_error` → 503, and validation faults now correctly answer
  // 422 instead of sharing one bucket with it — but the axios interceptor discards the status and
  // hands the component only the `detail` literal, so the page could not tell "ours, transient,
  // wait" from "yours, fix the input". The whole view is kept rather than only its sentence because
  // `retryable` drives THREE channels — the card's tint, its headline, and whether the Retry button
  // renders at all — and a page that re-derived any of them from the message would be back to
  // matching on prose.
  const [error, setError] = useState<TemplateErrorView | null>(null);
  const [reloadNonce, setReloadNonce] = useState(0);

  // Per-card mutation state (Delete). Single-flight, with the error pinned to the
  // offending card rather than raised as a page banner.
  const [actionPendingName, setActionPendingName] = useState<string | null>(null);
  const [cardError, setCardError] = useState<{ name: string; message: string } | null>(null);

  const [showUpload, setShowUpload] = useState(false);
  const [editTarget, setEditTarget] = useState<TemplateView | null>(null);

  // --- The reconcile surface (E28C/T6, design D-C3) -------------------------
  // Hosted HERE as well as from the Org Connections table, for two reasons the design names: the
  // cold start needs a pointer rather than an empty page, and the post-finalize consent route needs
  // a real URL to land on (`ConnectionCallback` sends the operator to
  // `/ops/templates?connection=<id>&seed=1`).
  //
  // `?seed=1` ONLY switches the prompt on. It never makes the surface act: reconcile is a read, and
  // "Nothing is created until you confirm below" is a promise that would be a lie if arriving here
  // executed anything.
  //
  // DERIVED, NOT SYNCHRONISED BY AN EFFECT. The obvious shape — an effect watching `connections`
  // that calls three setters once the row it needs has arrived — is the cascading-render pattern
  // `react-hooks/set-state-in-effect` exists to refuse, and it would need a ran-once ref to stop an
  // unrelated refetch re-opening a surface the operator had closed. Both problems disappear when the
  // URL is read as what it is: an INITIAL VALUE plus a render-time lookup.
  //   • `connectionId` takes the URL's id as its initial state. The connections effect already
  //     resolves it through `pickSelectedOrg`, which KEEPS a current selection that matches a real
  //     row and otherwise falls back to the first — so a `?connection=` naming a deleted or
  //     unreadable row degrades to "the first org", silently, which is right: it is not the
  //     operator's error and there is nothing for them to fix.
  //   • the modal's target is looked up during render and suppressed by ONE piece of state, the
  //     dismissal. That makes "closed" a fact about what the operator did rather than a race
  //     against the next refetch.
  const [searchParams] = useSearchParams();
  const seedParam = searchParams.get('seed') === '1';
  // Which surface the operator has closed, or asked for by hand. `null` = neither yet, so the URL
  // (if any) decides; a connection id = show that one; `''` = dismissed, show none.
  const [surfaceOverride, setSurfaceOverride] = useState<string | null>(null);

  // The connections fetch is the PRIMARY one here — unlike Repositories.tsx, where the
  // connection join is cosmetic. Without an org there is no catalog to scope, so its
  // failure is a real error state, not a quiet degrade.
  //
  // `connNonce` in the deps is what makes the failure's own CTA work: the read is the thing that
  // failed, so the retry has to be able to re-run it. Before this the page's only refresh nonce
  // re-ran the CATALOG read, which cannot run at all while there is no selected org.
  useEffect(() => {
    let cancelled = false;
    setConnLoading(true);
    connectionsApi
      .list()
      .then((rows) => {
        if (cancelled) return;
        setConnections(rows);
        setConnError(null);
        setConnectionId((prev) => pickSelectedOrg(rows, prev));
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        setConnError(err instanceof Error ? err.message : 'Failed to load org connections.');
      })
      .finally(() => {
        if (!cancelled) setConnLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [connNonce]);

  // The catalog fetch. `connectionId` in the deps IS the org switch — drop it and this
  // page renders one org's cards under another org's name, with no error and no empty
  // state to give it away. `templates.test.ts` asserts this dep array against
  // `CATALOG_EFFECT_DEPS` for exactly that reason.
  //
  // `setError(null)` before the request (not only on success) is the fix for the tab
  // body's stale-error bug: leaving the previous org's failure up while a new org's fetch
  // is in flight accuses the new org of something that never happened to it.
  useEffect(() => {
    if (!connectionId) {
      setTemplates([]);
      setLoading(false);
      setError(null);
      return;
    }
    let cancelled = false;
    setLoading(true);
    setError(null);
    // Drop the previous org's rows immediately: a switch must never leave the outgoing
    // org's cards on screen under the incoming org's label.
    setTemplates([]);
    setCardError(null);
    githubTemplatesApi
      .list(connectionId)
      .then((rows) => {
        if (cancelled) return;
        setTemplates(rows);
        setError(null);
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        setError(classifyTemplateError(err, 'Failed to load templates.'));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [connectionId, reloadNonce]);

  const refetch = useCallback(() => setReloadNonce((n) => n + 1), []);

  const selectedConnection = useMemo(
    () => connections.find((c) => c.id === connectionId) ?? null,
    [connections, connectionId],
  );

  // WHICH CONNECTION THE SURFACE IS OPEN FOR, derived. `null` from the override means nobody has
  // touched it, so the URL's `?connection=` decides; `''` means dismissed. The row must be resolved
  // out of `connections` rather than synthesised from the id, because the modal titles itself with
  // `provider`/`org` — a guessed pair would label an audit surface with the wrong org.
  const reconcileTarget = useMemo(() => {
    const wanted = surfaceOverride ?? searchParams.get('connection') ?? '';
    if (!wanted) return null;
    return connections.find((c) => c.id === wanted) ?? null;
  }, [connections, searchParams, surfaceOverride]);
  // The prompt belongs to the URL's arrival, not to a hand-opened surface: an operator who clicks
  // "Roll out templates" themselves is not being asked for consent to seed, they are inspecting.
  const seedPrompt = seedParam && surfaceOverride === null;
  const options = useMemo(() => orgOptions(connections), [connections]);

  const handleDelete = useCallback(
    (t: TemplateView) => {
      if (actionPendingName || !connectionId) return;
      // The sentence comes from the `.ts` — see `deleteTemplateConfirm` for why the copy that
      // used to be inline here was the epic's own defect class, and why it may not live in a
      // file no test can read.
      if (!window.confirm(deleteTemplateConfirm(t.name))) return;
      setActionPendingName(t.name);
      setCardError(null);
      githubTemplatesApi
        .remove(connectionId, t.name)
        .then(() => refetch())
        .catch((err: unknown) => {
          // MAPPED, never raw. T2's `store_error` → 503 arrives here as the fixed detail literal
          // (the interceptor has already discarded the status), and a bare render of it puts
          // "Template catalog is temporarily unavailable" on a card with no indication that
          // nothing was deleted and a retry is the remedy.
          setCardError({
            name: t.name,
            message: classifyTemplateError(err, 'The template could not be deregistered.').message,
          });
        })
        .finally(() => setActionPendingName(null));
    },
    [actionPendingName, connectionId, refetch],
  );

  const state = templatesBodyState({
    connectionsLoading: connLoading,
    // The failed connections read, carried into the state machine rather than only into a banner.
    // Without it an empty list after a FAILURE was indistinguishable from an empty list after a
    // SUCCESS, and the page told the operator they had no org connected.
    connectionsError: connError !== null,
    connectionCount: connections.length,
    connectionId,
    catalogLoading: loading,
    // The MESSAGE, because that is all the state machine's contract needs (`string | null` — a
    // failure or not). Widening it to carry the classification would put the retry verdict into a
    // module that has no use for it, and `templatesBodyState`'s totality test enumerates this
    // field as two values.
    catalogError: error?.message ?? null,
    templateCount: templates.length,
  });

  const hasConnection = connectionId.length > 0;
  // The filter strip is meaningless with nothing to filter, and an org <select> shown to someone who
  // has no org — or whose org list could not be read — is a dead control that competes with the copy
  // explaining why. Decided by an exhaustive table in the `.ts` rather than by a chain of `!==`
  // here: a new state added to the union must be a compile error, not a silent default.
  const showFilterStrip = SHOW_FILTER_STRIP[state];

  return (
    <OpsPage
      backTo="/ops"
      title="Templates"
      // FROM THE `.ts`, not a literal here — E28C/T6, design 4a. The sentence this replaces was
      // false in both of its claims and it rotted for a whole epic precisely because it lived in a
      // file no test can read (`TEMPLATES_SUBTITLE` documents which claims and why).
      subtitle={TEMPLATES_SUBTITLE}
      action={canMutate ? (
        // The page's primary action is an admin's, so for everyone else there is no action at
        // all. `OpsPage` renders the slot only when it is truthy, so the heading row closes up
        // rather than holding a gap or a greyed button. `disabled` below stays for the case it
        // belongs to: an admin with no org selected HAS the standing but has nothing to upload
        // INTO, which is temporary and fixable from the picker right below.
        <div className="flex items-center gap-2">
          {/* THE RECONCILE ENTRY. Secondary, not primary: it is the surface an operator visits when
              something DISAGREES, while upload is the routine act. Both are admin-only — reconcile's
              read route is `require_role(Role.ADMIN)`, so offering it below that rung would be a
              guaranteed 403, the over-promise this page already refuses for Upload. */}
          <button
            type="button"
            onClick={() => {
              setSurfaceOverride(connectionId);
            }}
            disabled={!hasConnection}
            title="Compare this org against AGP’s registry"
            className="px-3.5 py-1.5 rounded-lg bg-white border border-slate-300 text-slate-700 text-sm font-medium hover:bg-slate-50 transition-colors disabled:opacity-40"
          >
            {/* THE SAME WORDS AS EVERY OTHER WAY IN (E28D). This said "Reconcile org" while the
                connections row's menu item said "Roll out templates" and the modal titled itself a
                third way, so an operator clicked one word and landed on another. The `title`
                attribute still describes what the surface DOES first — it reads before it writes —
                which is the honest place for that, rather than in a label that has to match the
                other entry point. */}
            Roll out templates
          </button>
          <button
            type="button"
            onClick={() => setShowUpload(true)}
            disabled={!hasConnection}
            className={`${OPS_PRIMARY_BTN} disabled:opacity-40`}
          >
            Upload template
          </button>
        </div>
      ) : null}
    >
      {/* The connections fetch failing is a page-level error — the org picker below it cannot be
          populated, so it is stated once, at the top.

          ONCE being the operative word (E28 final review, FR-4). This banner and the body state
          below were unconditional siblings, so one failure was told TWICE: this, and then a
          full-page "no org connected yet" card underneath it — which is a different and false
          statement about the same failure. The banner now yields whenever the body owns the
          failure, and stays for the case it is genuinely additive: a failed REFETCH over a list the
          page still holds, where the body legitimately keeps rendering the orgs it has. */}
      {connError && state !== 'connections-unreadable' && (
        <div className="bg-white/80 backdrop-blur rounded-xl border border-rose-200/70 shadow-sm p-6 mb-4">
          <h2 className="text-sm font-semibold text-rose-700">Couldn’t load org connections</h2>
          <p className="text-sm text-slate-600 mt-1">{connError}</p>
        </div>
      )}

      {/* FILTER STRIP — org picker left, catalog count + Refresh right. Sits between the
          heading and the cards so re-picking reads as re-filtering the grid below. */}
      {showFilterStrip && (
        <div
          className={`${OPS_CARD} px-4 py-3 mb-4 flex flex-wrap items-center justify-between gap-x-6 gap-y-3`}
        >
          <div className="flex items-center gap-2.5">
            <svg
              className="w-4 h-4 text-emerald-600 shrink-0"
              fill="none"
              viewBox="0 0 24 24"
              strokeWidth={1.6}
              stroke="currentColor"
              aria-hidden="true"
            >
              <path strokeLinecap="round" strokeLinejoin="round" d={ICON_FUNNEL} />
            </svg>
            {/* A real, VISIBLY labelled control. Templates being org-scoped is the whole
                shape of this page, so the label is on screen rather than sr-only. */}
            <label
              htmlFor="templates-org"
              className="text-[11px] uppercase tracking-wide text-slate-400 font-medium"
            >
              Org
            </label>
            <select
              id="templates-org"
              value={connectionId}
              onChange={(e) => setConnectionId(e.target.value)}
              disabled={options.length === 0}
              className="px-3 py-1.5 text-sm rounded-lg border border-slate-300 bg-white text-slate-700 focus:outline-none focus:ring-2 focus:ring-emerald-500/40 disabled:opacity-40"
            >
              {options.length === 0 ? (
                <option value="">No orgs connected</option>
              ) : (
                options.map((o) => (
                  <option key={o.value} value={o.value}>
                    {o.label}
                  </option>
                ))
              )}
            </select>
          </div>

          <div className="flex items-center gap-3">
            {/* The count is the switch's receipt: it changes with the org, which is what
                makes a switch legible when two orgs' grids look alike. Suppressed while
                loading or errored — 0 would read as "this org is empty". */}
            <span className="text-xs text-slate-400 tabular-nums">
              {state === 'grid' || state === 'empty-catalog'
                ? `${templates.length} template${templates.length === 1 ? '' : 's'}`
                : '—'}
            </span>
            <button
              type="button"
              onClick={refetch}
              disabled={!hasConnection || loading}
              title="Refresh the template catalog"
              className="px-3 py-1.5 rounded-lg bg-white border border-slate-300 text-slate-700 text-sm font-medium hover:bg-slate-50 transition-colors disabled:opacity-40"
            >
              {loading ? 'Refreshing…' : 'Refresh'}
            </button>
          </div>
        </div>
      )}

      {/* BODY — one branch per state, chosen by the pure state machine. */}
      {state === 'loading-orgs' && (
        <div className={`${OPS_CARD} p-8 text-center`}>
          <p className="text-sm text-slate-400">Loading orgs…</p>
        </div>
      )}

      {/* THE READ FAILED — not an empty state at all, though it used to render as one. The card
          says only what the failure establishes (nothing about how many orgs exist) and its action
          is the retry, because the thing that went wrong is a read this page can re-run. The
          message from the failure itself is shown too: it is the only debuggable detail. */}
      {state === 'connections-unreadable' && (
        <StateCard
          iconPath={ICON_PLUG}
          headline={EMPTY_STATE_COPY['connections-unreadable'].headline}
          body={EMPTY_STATE_COPY['connections-unreadable'].body}
        >
          <div className="flex flex-col items-center gap-3">
            {connError && <p className="text-xs text-slate-400 max-w-md">{connError}</p>}
            <button
              type="button"
              onClick={() => setConnNonce((n) => n + 1)}
              className={OPS_PRIMARY_BTN}
            >
              {EMPTY_STATE_COPY['connections-unreadable'].cta.label}
            </button>
          </div>
        </StateCard>
      )}

      {/* EMPTY STATE 1 of 2 — no org connected. The fix is on another page, so the CTA
          goes there. Distinct from the empty catalog below in headline, body AND action. */}
      {state === 'no-connection' && (
        <StateCard
          iconPath={ICON_PLUG}
          headline={EMPTY_STATE_COPY['no-connection'].headline}
          body={EMPTY_STATE_COPY['no-connection'].body}
        >
          <Link to={EMPTY_STATE_COPY['no-connection'].cta.to} className={OPS_PRIMARY_BTN}>
            {EMPTY_STATE_COPY['no-connection'].cta.label} →
          </Link>
        </StateCard>
      )}

      {state === 'no-selection' && (
        <StateCard
          iconPath={ICON_FUNNEL}
          headline={EMPTY_STATE_COPY['no-selection'].headline}
          body={EMPTY_STATE_COPY['no-selection'].body}
        />
      )}

      {state === 'loading' && (
        <div className={`${OPS_CARD} p-8 text-center`}>
          <p className="text-sm text-slate-400">Loading templates…</p>
        </div>
      )}

      {/* THE CATALOG READ FAILED — and after E28B/T2 there are two kinds of failure here, which
          this card must not flatten into one (item 4). A `store_error` is a 503: the registry
          partition was unreadable, which is OURS, TRANSIENT, and has changed nothing. A 4xx is the
          caller's and is terminal.

          So the tone follows `retryable`. Amber says "known, accepted, wait" — the same weight the
          repo row's `attention` tone carries for a request rather than a fault — and rose stays for
          the failures that really are faults. Rendering a 503 in rose would tell an admin the
          catalog is broken; rendering it as a bare failure would invite them to conclude the
          templates are GONE and re-upload templates that already exist, which is the exact
          confusion `list_templates` refuses when it raises `store_error` instead of returning an
          empty list. The sentence itself comes from the classifier, so it is pinned by a test. */}
      {state === 'error' && error && (
        <div
          className={`bg-white/80 backdrop-blur rounded-xl border shadow-sm p-6 ${
            error.retryable ? 'border-amber-200/70' : 'border-rose-200/70'
          }`}
        >
          <h2
            className={`text-sm font-semibold ${
              error.retryable ? 'text-amber-800' : 'text-rose-700'
            }`}
          >
            {/* The HEADLINE distinguishes them too, not only the tint: "couldn't load" over a
                transient store fault reads as a verdict on the catalog. Colour alone would also
                be the only channel carrying the difference, which is not a channel every reader
                has. */}
            {error.retryable ? 'Template catalog temporarily unavailable' : 'Couldn’t load templates'}
          </h2>
          <p className="text-sm text-slate-600 mt-1">{error.message}</p>
          {/* THE RETRY IS GATED, and it was not — a review finding, and the same defect class this
              whole task exists to fix: two comments claimed `retryable` decided whether a retry was
              OFFERED while the button rendered unconditionally, so both comments were false and a
              terminal `not_found` / `invalid_zip` / `invalid_input` offered an action that cannot
              succeed. Re-reading the same missing template returns the same 404.

              Withholding it is the right half to fix rather than the comments: an affordance whose
              every click is refused is worse than an absent one — it is the over-promise this page
              already refuses for the Upload button an operator may not use, and the reason
              `promoteBlockedReason` renders NOTHING for a role refusal. The classifier is the single
              authority on which failures are waitable, so the tint, the headline and this button all
              read the one verdict. */}
          {error.retryable && (
            <button
              type="button"
              onClick={refetch}
              className="mt-3 px-3 py-1.5 rounded-lg bg-white border border-slate-300 text-slate-700 text-xs font-medium hover:bg-slate-50 transition-colors"
            >
              Retry
            </button>
          )}
        </div>
      )}

      {/* EMPTY STATE 2 of 2 — the org is connected and answered with zero rows. Nothing is
          broken; the catalog is new. The fix is HERE, so the CTA is the upload itself. */}
      {state === 'empty-catalog' && (
        <StateCard
          iconPath={ICON_UPLOAD}
          headline={
            selectedConnection
              ? `No templates in ${selectedConnection.org} yet`
              : EMPTY_STATE_COPY['empty-catalog'].headline
          }
          // The stock body is an IMPERATIVE — "upload a .zip scaffold" — and under a gate that
          // has just removed the CTA it becomes the same over-promise as the button, in prose.
          // So this is the one place the gate substitutes words rather than rendering nothing:
          // a non-admin gets a statement of fact and who fixes it, not an instruction they
          // cannot carry out.
          body={canMutate ? EMPTY_STATE_COPY['empty-catalog'].body : EMPTY_CATALOG_READ_ONLY_BODY}
        >
          {canMutate ? (
            // TWO ROUTES, because the body now names three and one of them was the only one this
            // page could reach. The cold start is the state the reconcile surface exists for — an
            // org with no templates is exactly where "seed the starters" and "adopt what is already
            // here" apply — so leaving upload as the sole affordance would make the new copy an
            // instruction the page cannot honour, which is the defect class 4a is fixing.
            <div className="flex flex-wrap items-center justify-center gap-2">
              <button
                type="button"
                onClick={() => setSurfaceOverride(connectionId)}
                className={OPS_PRIMARY_BTN}
              >
                Seed or adopt templates
              </button>
              <button
                type="button"
                onClick={() => setShowUpload(true)}
                className="px-3.5 py-1.5 rounded-lg bg-white border border-slate-300 text-slate-700 text-sm font-medium hover:bg-slate-50 transition-colors"
              >
                {EMPTY_STATE_COPY['empty-catalog'].cta.label}
              </button>
            </div>
          ) : undefined}
        </StateCard>
      )}

      {state === 'grid' && (
        <TemplateCardGrid
          templates={templates}
          actionPendingName={actionPendingName}
          cardError={cardError}
          canMutate={canMutate}
          onEdit={setEditTarget}
          onDelete={handleDelete}
        />
      )}

      {showUpload && hasConnection && (
        <UploadTemplateModal
          orgLabel={selectedConnection ? orgLabel(selectedConnection) : 'this org'}
          onClose={() => setShowUpload(false)}
          onSubmit={async (file, meta) => {
            await githubTemplatesApi.upload(connectionId, file, meta);
            setShowUpload(false);
            refetch();
          }}
        />
      )}

      {/* THE RECONCILE SURFACE. On close it refetches the catalog: a seed, a re-create or an adopt
          all write registry records, so the cards behind this modal are stale the moment it closes.
          It carries no `onSuccess` — closing IS the completion signal, because the surface is a
          triage table an operator may act on several times before they are done. */}
      {reconcileTarget && (
        <RolloutTemplatesModal
          connection={reconcileTarget}
          seedPrompt={seedPrompt}
          onClose={() => {
            // `''` is DISMISSED, and it has to be an explicit value rather than `null`: `null`
            // means "nobody has touched this", which would hand the decision straight back to the
            // still-present `?connection=` in the URL and re-open the surface the operator just
            // closed. Same reason `ConnectionCallback` clears its single-use CSRF state.
            setSurfaceOverride('');
            refetch();
          }}
        />
      )}

      {editTarget && hasConnection && (
        <EditTemplateModal
          template={editTarget}
          onClose={() => setEditTarget(null)}
          onSubmit={async (patch) => {
            await githubTemplatesApi.patch(connectionId, editTarget.name, patch);
            setEditTarget(null);
            refetch();
          }}
        />
      )}
    </OpsPage>
  );
}
