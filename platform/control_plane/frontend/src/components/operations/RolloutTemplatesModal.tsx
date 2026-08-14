// RolloutTemplatesModal — THE RECONCILE SURFACE for one org connection (E28C/T6, design D-C3).
//
// Rebuilt, not adjusted. The predecessor read a single "is it already in the org?" BOOLEAN answered
// from AGP's own DDB catalog — evidence about AGP's store, never about the org — so it rendered two
// wrong answers in opposite directions: a registered template whose repository had been deleted
// appeared under "Already in org" with only an overwrite toggle, and a repository that already
// carried a seed's name appeared under "Available to roll out" as a plain checkbox. Ticking that
// checkbox is what ended the 2026-08-04 live test in a manual repo delete. (The field's name is
// deliberately not written here — `reconcileView.test.ts` greps `src/` for it, because a deleted
// field that survives in prose is how the next reader learns to look for it again.)
//
// WHAT IT IS NOW. One audit table: every name AGP or the org knows about, each row carrying the
// STATE of the comparison and the ONE action honest for that state. Four states, four verbal labels,
// and the mapping is a pure function in `reconcileView.ts` — because vitest here has no jsdom, so a
// decision written in this file is a decision no test can reach (E28B's I-1 lesson).
//
//   In sync                  → Re-push seed (SEED rows only — there is no seed to push otherwise)
//   Repository missing       → Re-create from seed / Deregister
//   Found in org, not ours   → ADOPT      (never "create": rollout refuses this row server-side)
//   Not in org yet           → Create
//
// THE PREVIEW IS THE CONSENT SCREEN (tenet 6). Nothing executes on open or on navigation: the
// surface reconciles (one read), states what it found, and waits. That is what makes the
// post-finalize prompt — "Nothing is created until you confirm below" — a true sentence, and it is
// why the footer's primary is disabled until the operator has selected something.
//
// THE COST MODEL IS RULED (D-C3). This is the ONLY surface in the console that pays for a provider
// call, and it pays on OPEN and on explicit Refresh only — one paginated `list_repos` plus a bounded
// set of `read_repo` probes. There is no polling, no cache and no per-row probe: an org-origin adopt
// row's `default_branch`/`head_sha` are null PRE-POST by that ruling, so its confirm says what WILL
// happen instead of showing provenance it deliberately did not buy.
//
// ONE VOCABULARY, AND IT IS "ROLL OUT" (E28D). The surface used to call itself three things: this
// file and the batch confirm said "roll out", the title and both aria labels said "reconcile", and
// the two entry points carried a different label each. "Roll out" won because it is the word on the
// wire and the word attached to the consent; "reconcile" survives for the READ, in the module names
// and the route path, which are not operator-facing. See the `title` computation for the full note.
//
// House style unchanged: emerald-on-glass Ops tokens, the shared `ModalShell`, `OPS_BADGE`'s
// semantic tints via `RECONCILE_STATE_TONE`, Tailwind v4 utility strings, 2-space indent. No new
// visual language — this is an audit register, so the rows read name · state · provenance · action.

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import {
  githubTemplatesApi,
  rolloutApi,
  type Connection,
  type Provider,
  type ReconcileView,
  type RolloutResult,
} from '../../api/client';
import { ModalShell } from './ConnectionsAdmin';
import {
  ADOPT_NAME_RULE_HINT,
  RECONCILE_ACTION_LABEL,
  SEED_CONSENT_PROMPT,
  adoptConfirm,
  batchConfirmLines,
  batchConfirmTitle,
  batchPlan,
  classifyRolloutError,
  deregisterConfirm,
  infraIntentFor,
  reconcileRows,
  rolloutActionLabel,
  toggleQueuedAction,
  type ClassifiedErrorView,
  type ReconcileAction,
  type ReconcileRowModel,
  // The RECONCILE read's own failure keeps the narrower type: that error can only ever come from
  // `classifyRolloutError`, so there is no reason to widen it and lose the `kind`.
  type RolloutErrorView,
  type SingleRepoAction,
} from './reconcileView.ts';
// DEREGISTER is `githubTemplatesApi.remove` — the CATALOG router, with its own `detail` literals —
// so its failures need that route's classifier, not the rollout one (F3). Routing them through
// `classifyRolloutError` matched none of them, so a retryable 503 catalog fault rendered raw, in
// rose, with no Retry: "the catalog is broken" for a condition whose remedy is to wait.
import { classifyTemplateError } from './templatesView.ts';

const PROVIDER_LABEL: Record<Provider, string> = {
  github: 'GitHub',
  gitlab: 'GitLab',
};

const SECTION_LABEL = 'text-[11px] uppercase tracking-wide text-slate-400 font-medium';
const PILL = 'inline-flex items-center px-2 py-0.5 rounded-full text-[11px] font-semibold';
const ROW_BTN =
  'px-2 py-0.5 rounded-md bg-white border border-slate-300 text-slate-700 text-[11px] font-medium hover:bg-slate-50 transition-colors disabled:opacity-40';
const SECONDARY_BTN =
  'px-3.5 py-1.5 rounded-lg bg-white border border-slate-300 text-slate-700 text-sm font-medium hover:bg-slate-50 transition-colors disabled:opacity-40';
const FIELD_INPUT =
  'w-full px-3 py-2 text-sm rounded-lg border border-slate-300 focus:outline-none focus:ring-2 focus:ring-emerald-500/40 disabled:opacity-40';

/**
 * The row awaiting a confirm: which verb, on which row. `null` = no confirm open.
 *
 * Only the two SINGLE-REPO verbs land here (`adopt`, `deregister`). The three rollout-batch verbs
 * are queued and consented to once, in the footer's batch confirm.
 */
type PendingConfirm = { action: SingleRepoAction; row: ReconcileRowModel } | null;

export default function RolloutTemplatesModal({
  connection,
  onClose,
  // The post-finalize consent prompt (design D-C3). Rendered ONLY when the operator arrived here
  // from a just-finished connection — the same surface reached from the Templates page shows no
  // prompt, because there is nothing to consent to that they did not ask for by opening it.
  seedPrompt = false,
}: {
  connection: Connection;
  onClose: () => void;
  seedPrompt?: boolean;
}) {
  const [view, setView] = useState<ReconcileView | null>(null);
  const [loading, setLoading] = useState(true);
  // CLASSIFIED, never raw (the `githubTemplatesApi` pattern — `rolloutApi` had no classification at
  // all). The axios interceptor discards the status, so the fixed `detail` literal is the only thing
  // left to tell "ours, transient, wait" from "that name is already accounted for".
  const [error, setError] = useState<RolloutErrorView | null>(null);
  const [reloadNonce, setReloadNonce] = useState(0);

  // THE ROLLOUT BATCH — ONE map of queued names → THE VERB EACH WAS TICKED AGAINST (F1, then E28D).
  //
  // It was two sets, and the second one had no writer: nothing could ever add a re-push, so the
  // wire's `overwrite` was permanently false and the "Queued" pill was unreachable markup. The fix
  // is not a third setter but a simpler shape — the rollout route is a BATCH endpoint
  // (`template_names` + one `overwrite`), so the three actions that call it are SELECTIONS, and
  // which action a queued name contributes is a property of its row, answered by
  // `ReconcileRowModel.queueAction`. `batchPlan` turns this into the body, so the overwrite decision
  // is testable instead of living in this file.
  //
  // A `Map` AND NOT A `Set`, since E28D: a tick has to carry the verb that was VISIBLE when it was
  // placed. As a set of bare names it could not, which is the T6-L3 live-test finding — a box ticked
  // on "Create" survived a Refresh that returned the row as in-sync, and was then submitted as a
  // re-push with `overwrite: true`. The operator consented to a create and the wire carried the
  // epic's most dangerous flag.
  //
  // No stale-prune effect any more either: `batchPlan` self-prunes against the CURRENT rows, so a
  // name queued before a Refresh that came back offering a different verb simply is not carried —
  // and it says so, from `plan.dropped`, instead of vanishing.
  const [queued, setQueued] = useState<ReadonlyMap<string, ReconcileAction>>(new Map());
  // THE FORCED INFRA REPO'S OWN CONSENT (E28D), defaulted OFF — the safe answer and the whole point:
  // `overwrite` used to serve both consumers server-side, so a template re-push pushed AGP's
  // Terraform module over the org's existing `agp-runtime-infra` without anyone asking. This is the
  // ask. It is only reachable when that repo is already PRESENT; creating an absent one is
  // unconditional, so there is no box for it (see the infra section below).
  const [overwriteInfra, setOverwriteInfra] = useState(false);

  // The open confirm, and the adopt confirm's optional description field.
  const [pending, setPending] = useState<PendingConfirm>(null);
  // THE BATCH's consent gate. Execution moved from per-row buttons to the footer, so the sentences
  // that used to sit in each per-row confirm travel with it — `batchConfirmLines` states every write
  // the click performs, including the forced infra repo the selection cannot opt out of.
  const [confirmingBatch, setConfirmingBatch] = useState(false);
  const [adoptDescription, setAdoptDescription] = useState('');

  const [actionPending, setActionPending] = useState(false);
  // `ClassifiedErrorView`, not `RolloutErrorView`: this surface has TWO classifiers because it talks
  // to two routers (rollout/adopt, and the catalog route deregister uses — F3), and their `kind`
  // unions are disjoint by design. The state holds the two fields every render site actually reads
  // (`message`, `retryable`); each classifier still returns its own full view with its own `kind`,
  // which is what its tests pin. Casting one view to the other would have claimed a catalog kind is
  // a rollout kind — a lie that only bites the next person to branch on `.kind`.
  const [actionError, setActionError] = useState<ClassifiedErrorView | null>(null);
  const [result, setResult] = useState<RolloutResult | null>(null);
  // Single-row outcomes (adopt / deregister / a one-row rollout) — reported inline rather than by
  // switching the whole modal to the batch result view, because the operator is mid-triage on a
  // table and replacing it would lose the rest of their read.
  const [rowNotice, setRowNotice] = useState<{ name: string; text: string } | null>(null);

  // Unmount guard — reconcile / rollout / adopt can land after the modal closes.
  const mountedRef = useRef(true);
  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  // THE ONE PROVIDER-BACKED READ, on open and on explicit Refresh (`reloadNonce`) only. Stale
  // selections are pruned against the fresh view: a name that changed state between reads must not
  // stay selected under a row that no longer offers that action.
  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    rolloutApi
      .reconcile(connection.id)
      .then((data) => {
        if (cancelled) return;
        setView(data);
        // NO stale-prune here. `batchPlan` prunes against the current rows at submit time, which is
        // strictly better: pruning on arrival silently dropped the operator's selection, while
        // pruning at the plan keeps the tick visible only while its row still offers that action.
        //
        // THE INFRA TICK IS THE ONE EXCEPTION, and it is not an exception to the reasoning — it is
        // the same rule reaching a control the plan cannot speak for (review Critical 1). A template
        // tick survives a Refresh because it stays VISIBLE and self-describing: its row is on screen,
        // the plan re-checks its verb, and a drop is reported. The infra checkbox has neither
        // property — it UNMOUNTS when the repo comes back absent, so a surviving tick would be a
        // consent the operator can no longer see, untick, or be told about. Dropping it on every
        // re-read is the drop-on-world-change rule the verb-carry gives every other tick.
        setOverwriteInfra(false);
        setError(null);
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        setError(classifyRolloutError(err, 'Could not compare this org against AGP’s templates.'));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [connection.id, reloadNonce]);

  const refetch = useCallback(() => setReloadNonce((n) => n + 1), []);

  const rows = useMemo(() => (view ? reconcileRows(view.templates) : []), [view]);
  const infraRow = useMemo(
    () => (view ? reconcileRows([view.infra_repo])[0] : null),
    [view],
  );
  const summary = useMemo(() => reconcileRows.summarize(rows), [rows]);

  /**
   * Queue or un-queue a row for the batch. The ONE writer of `queued` (F1's missing half).
   *
   * It stores the row's CURRENT `queueAction` alongside the name, which is what makes a tick
   * self-describing: `batchPlan` can then tell "still a create" from "was a create, is now a
   * re-push" and refuse the second. Takes the row rather than the name for exactly that reason — a
   * name alone is the shape that could not express the verb.
   */
  // THE REDUCER ITSELF LIVES IN `reconcileView.ts` (review Critical 2). It was inline here and keyed
  // on bare presence, which gave a DEAD FIRST CLICK on the very path the dropped-tick notice sends
  // operators down — and being inline is why no test caught it. It is a decision, so it moved to
  // where decisions are testable; this stays a one-line setter.
  const toggleQueued = useCallback((row: ReconcileRowModel) => {
    setQueued((prev) => toggleQueuedAction(prev, row));
  }, []);

  // WHAT THIS CLICK WILL DO TO THE FORCED REPO, as one value (E28D). It makes a rollout meaningful
  // even with no template queued: the repo is ensured server-side on every call, so a run that only
  // creates it — or only re-pushes its module, now that that is its own consent — is a legitimate
  // run. The predicate lives in the selector (F4), where its reasoning is pinned by a test.
  //
  // COMPUTED BEFORE THE PLAN, because the plan now takes it: the raw checkbox is not what reaches the
  // wire. `infraIntentFor` answers 'create' for an absent repo whatever the tick says, so a consent
  // left over from a view where the repo still existed cannot be sent (review Critical 1).
  const infraIntent = infraIntentFor(view?.infra_repo, overwriteInfra);
  // The batch, DECIDED IN THE SELECTOR. `plan.overwrite` is true exactly when a queued row's action
  // is `repush` — the flag the old two-set shape could never set — and `plan.overwriteInfra` is the
  // infra repo's SEPARATE consent, reconciled against what is actually there rather than read off
  // the checkbox, because that repo is its own field on the view and not one of these rows.
  const plan = useMemo(() => batchPlan(rows, queued, infraIntent), [rows, queued, infraIntent]);
  // The infra tick is the THIRD disjunct: ticking only the infra re-push must be submittable.
  const canSubmit = !plan.empty || infraIntent !== 'none';

  // THE ONE EXECUTION POINT for the rollout verb. Every queued row goes through this single call
  // with the selector's body, so "what the footer sends" has exactly one definition.
  const runRollout = useCallback(async () => {
    // An empty plan is still submittable: the forced infra repo is ensured server-side on every
    // rollout, so a run with nothing queued is a legitimate run. The footer's `canSubmit` is the
    // gate on whether even THAT is meaningful.
    if (actionPending) return;
    setActionPending(true);
    setActionError(null);
    setRowNotice(null);
    try {
      const res = await rolloutApi.rollout(connection.id, {
        template_names: plan.names,
        overwrite: plan.overwrite,
        // The infra repo's OWN consent (E28D). Read off the PLAN, never off the checkbox state: the
        // plan reconciles the tick against what the last read actually found, so the flag on the wire
        // is the one the confirm the operator just read described. Reading `overwriteInfra` here
        // instead is review Critical 1 — it sent a re-push consent for a repo that is not there.
        overwrite_infra: plan.overwriteInfra,
      });
      if (!mountedRef.current) return;
      setResult(res);
      setQueued(new Map());
      // The infra consent is per-run too: it is a one-time authorisation for a specific write, so
      // leaving it ticked would silently re-authorise the next run from this same open modal.
      setOverwriteInfra(false);
      setPending(null);
    } catch (err: unknown) {
      if (mountedRef.current) {
        setActionError(classifyRolloutError(err, 'The rollout could not be completed.'));
        setPending(null);
      }
    } finally {
      if (mountedRef.current) setActionPending(false);
    }
  }, [actionPending, connection.id, plan]);

  const runAdopt = useCallback(
    async (row: ReconcileRowModel, description: string) => {
      if (actionPending) return;
      setActionPending(true);
      setActionError(null);
      setRowNotice(null);
      try {
        await rolloutApi.adopt(connection.id, {
          repo_name: row.item.name,
          ...(description.trim() ? { description: description.trim() } : {}),
        });
        if (!mountedRef.current) return;
        setRowNotice({
          name: row.item.name,
          text: `${rolloutActionLabel('adopted').label} — registered as a template, as-is.`,
        });
        setPending(null);
        setAdoptDescription('');
        refetch();
      } catch (err: unknown) {
        if (mountedRef.current) {
          setActionError(classifyRolloutError(err, 'That repository could not be adopted.'));
          setPending(null);
        }
      } finally {
        if (mountedRef.current) setActionPending(false);
      }
    },
    [actionPending, connection.id, refetch],
  );

  // DEREGISTER goes through the catalog route, not the rollout one: it deletes AGP's RECORD and
  // leaves the repository completely alone (E28B/T2 — a 204 there never means "the repository was
  // removed"). Its failures are the catalog's, so they are reported through the same classifier the
  // rest of this surface uses only for its generic sentence.
  const runDeregister = useCallback(
    async (row: ReconcileRowModel) => {
      if (actionPending) return;
      setActionPending(true);
      setActionError(null);
      setRowNotice(null);
      try {
        await githubTemplatesApi.remove(connection.id, row.item.name);
        if (!mountedRef.current) return;
        setRowNotice({ name: row.item.name, text: 'Deregistered — the record is gone; no repository was touched.' });
        setPending(null);
        refetch();
      } catch (err: unknown) {
        if (mountedRef.current) {
          // THE CATALOG route's classifier (F3), because this is the catalog route. Its `detail`
          // literals are `github_templates.py`'s, none of which `classifyRolloutError` knows — so
          // that classifier returned `kind: null`, `retryable: false` and the RAW server sentence,
          // rendering a transient 503 as a terminal fault with no Retry offered.
          setActionError(classifyTemplateError(err, 'That template could not be deregistered.'));
          setPending(null);
        }
      } finally {
        if (mountedRef.current) setActionPending(false);
      }
    },
    [actionPending, connection.id, refetch],
  );

  /**
   * Open a confirm for one of the two SINGLE-REPO verbs. Every write on this surface passes through
   * a confirm — the other three are the rollout batch, consented to once in the footer.
   *
   * Typed `SingleRepoAction` rather than `ReconcileAction`: it only ever receives those two (the row
   * renders buttons for exactly them), and claiming the wider type made this function's signature
   * contradict the prop that feeds it — which is the type error `tsc -b` reported.
   */
  const askConfirm = useCallback((action: SingleRepoAction, row: ReconcileRowModel) => {
    setActionError(null);
    setRowNotice(null);
    setAdoptDescription('');
    setPending({ action, row });
  }, []);

  const orgName = connection.org;
  // ONE VOCABULARY: "ROLL OUT" (E28D). This surface had three names for itself — the file and the
  // batch confirm said "roll out", this title and both aria labels said "reconcile", and the two
  // entry points were labelled differently again ("Roll out templates" on the connections row,
  // "Reconcile org" on the Templates page). An operator clicked one word and landed on another.
  //
  // "Roll out" wins over the more precise "reconcile" for two reasons that outrank precision: it is
  // the word on the WIRE (`POST /{id}/rollout`) and the word on the surviving entry point (the
  // connections row's kebab), and it is the word attached to the CONSENT — the footer, the batch
  // confirm and its primary button all say it, which is the moment the vocabulary matters most.
  // Internal identifiers (`reconcileView`, `reconcileRowModel`, the `/rollout/reconcile` route) keep
  // their names deliberately: "reconcile" is the accurate name for the READ, and the file names are
  // not operator-facing.
  const title = `Roll out templates — ${PROVIDER_LABEL[connection.provider]} · ${orgName}`;

  // --- Batch result view ----------------------------------------------------
  if (result) {
    return (
      <ModalShell
        title={title}
        description="Every repository’s outcome, in the platform’s own words."
        ariaLabel="Roll out templates — result"
        actionPending={false}
        onClose={onClose}
        footer={
          <button type="button" onClick={onClose} className="px-3.5 py-1.5 rounded-lg bg-emerald-600 text-white text-sm font-medium hover:bg-emerald-700 transition-colors">
            Done
          </button>
        }
      >
        {result.items.length === 0 ? (
          <p className="text-sm text-slate-400">Nothing was rolled out.</p>
        ) : (
          <ul className="space-y-1">
            {result.items.map((item) => {
              // A TOTAL lookup (F2). `action` is `str` on the wire, so indexing a `Record` with it
              // meant a sixth backend word threw on `.cls` and white-screened this whole list — the
              // operator's receipt for writes that already happened.
              const style = rolloutActionLabel(item.action);
              return (
                <li key={item.name} className="flex items-start justify-between gap-3 px-2 py-1.5 rounded-lg bg-emerald-50/30">
                  <div className="min-w-0">
                    <span className="text-sm text-slate-700 font-medium truncate block" title={item.name}>
                      {item.name}
                    </span>
                    {/* The `reason` is operator-facing prose from the server, and for a repo that
                        exists but is not a registered template it POINTS AT ADOPT — the only honest
                        action for that state. Rendering it is how the refusal becomes actionable
                        instead of a silent skip. */}
                    {item.reason && <span className="text-[11px] text-slate-500">{item.reason}</span>}
                  </div>
                  <span className={`shrink-0 ${PILL} ${style.cls}`}>{style.label}</span>
                </li>
              );
            })}
          </ul>
        )}
      </ModalShell>
    );
  }

  // --- Confirm view ---------------------------------------------------------
  // A REPLACEMENT rather than a nested dialog: a second overlay over a table the operator cannot
  // read anyway competes for the same focus trap, and this surface's confirms are the moment that
  // matters — they get the whole panel and say exactly what will be written.
  if (pending) {
    const { action, row } = pending;
    // Only the two SINGLE-REPO verbs reach a per-row confirm now. `create`/`repush`/`recreate` are
    // the batch endpoint, so they are queued from the row and consented to ONCE in the footer's
    // confirm below — which is also what stops a five-row triage becoming five separate dialogs.
    const text = action === 'adopt' ? adoptConfirm(row.item) : deregisterConfirm(row.item.name);
    const confirmLabel = RECONCILE_ACTION_LABEL[action];
    const onConfirm = () => {
      if (action === 'adopt') return void runAdopt(row, adoptDescription);
      return void runDeregister(row);
    };

    return (
      <ModalShell
        title={`${confirmLabel} — ${row.item.name}`}
        description={`${PROVIDER_LABEL[connection.provider]} · ${orgName}`}
        ariaLabel={`${confirmLabel} ${row.item.name}`}
        actionPending={actionPending}
        onClose={() => setPending(null)}
        footer={
          <>
            <button type="button" onClick={() => setPending(null)} disabled={actionPending} className={SECONDARY_BTN}>
              Cancel
            </button>
            <button
              type="button"
              onClick={onConfirm}
              disabled={actionPending}
              className={
                action === 'deregister'
                  ? 'px-3.5 py-1.5 rounded-lg bg-rose-600 text-white text-sm font-medium hover:bg-rose-700 transition-colors disabled:opacity-40'
                  : 'px-3.5 py-1.5 rounded-lg bg-emerald-600 text-white text-sm font-medium hover:bg-emerald-700 transition-colors disabled:opacity-40'
              }
            >
              {actionPending ? 'Working…' : confirmLabel}
            </button>
          </>
        }
      >
        <p className="text-sm text-slate-600">{text}</p>

        {/* ADOPT's one optional field. Everything else on the record is editable afterwards via
            PATCH, which is why the confirm collects exactly this and nothing more. */}
        {action === 'adopt' && (
          <div className="space-y-1.5">
            <label htmlFor="adopt-description" className={`block ${SECTION_LABEL}`}>
              Description (optional)
            </label>
            <input
              id="adopt-description"
              type="text"
              value={adoptDescription}
              onChange={(e) => setAdoptDescription(e.target.value)}
              disabled={actionPending}
              placeholder="What this template is for"
              className={FIELD_INPUT}
              autoComplete="off"
            />
            <p className="text-[11px] text-slate-400">
              Framework, AWS services and tags are editable on the template afterwards.
            </p>
          </div>
        )}

        {actionError && (
          <p className={`text-sm ${actionError.retryable ? 'text-amber-800' : 'text-rose-600'}`} role="alert">
            {actionError.message}
          </p>
        )}
      </ModalShell>
    );
  }

  // --- The BATCH confirm ----------------------------------------------------
  // ONE gate for every queued write, listing them line by line. This is where the per-row confirms'
  // sentences went when execution moved to the footer: a batch that re-pushed onto live repositories
  // while saying only "Roll out (3)" would reopen the consent gap those confirms closed.
  if (confirmingBatch) {
    const lines = batchConfirmLines(rows, queued, infraIntent);
    // FROM THE SELECTOR, not a literal (T6-L2). The literal that used to sit here rendered "Roll out
    // 0 templates" over a body whose one bullet was the infra line — a title contradicting its own
    // content, in a file no test can read, at the exact moment the footer was getting the same count
    // right. It takes the infra INTENT because the zero-template case is not one case: creating that
    // repo and re-pushing its module are different writes and the title has to name which.
    const confirmTitle = batchConfirmTitle(plan, infraIntent, orgName);
    return (
      <ModalShell
        title={confirmTitle}
        description="Exactly what this writes. Nothing has happened yet."
        // The aria label MOVES WITH THE TITLE. It said "Confirm roll out" while the title said "0
        // templates", so a screen-reader user and a sighted one were told different things about the
        // same dialog — and the generic version could not name the infra-only run at all.
        ariaLabel={`Confirm: ${confirmTitle}`}
        actionPending={actionPending}
        onClose={() => setConfirmingBatch(false)}
        footer={
          <>
            <button
              type="button"
              onClick={() => setConfirmingBatch(false)}
              disabled={actionPending}
              className={SECONDARY_BTN}
            >
              Back
            </button>
            <button
              type="button"
              onClick={() => void runRollout()}
              disabled={actionPending}
              className="px-3.5 py-1.5 rounded-lg bg-emerald-600 text-white text-sm font-medium hover:bg-emerald-700 transition-colors disabled:opacity-40"
            >
              {actionPending ? 'Rolling out…' : 'Roll out'}
            </button>
          </>
        }
      >
        <ul className="space-y-2">
          {lines.map((line) => (
            <li key={line} className="text-sm text-slate-600 flex gap-2">
              <span className="text-slate-300 shrink-0" aria-hidden="true">
                •
              </span>
              <span>{line}</span>
            </li>
          ))}
        </ul>

        {/* The wire flags, stated. An operator authorising a re-push should be able to see that this
            is the request that carries it — a flag is scoped consent, not a mood, and the whole
            reason `batchPlan` refuses to set one for a re-create.
            TWO flags since E28D, and the sentence can finally be true: "applies only to the
            templates listed above" was the claim, while the single flag it described was ALSO what
            authorised the infra push server-side. Each is now named with its own scope. */}
        {plan.overwrite && (
          <p className="text-[11px] text-amber-800">
            This request is sent with overwrite enabled for the registered templates listed above.
            It does not authorise anything in the runtime-infra repo.
          </p>
        )}
        {plan.overwriteInfra && (
          <p className="text-[11px] text-amber-800">
            This request is sent with the runtime-infra repo’s own overwrite enabled, which applies
            only to AGP’s Terraform module in that one repository.
          </p>
        )}

        {actionError && (
          <p className={`text-sm ${actionError.retryable ? 'text-amber-800' : 'text-rose-600'}`} role="alert">
            {actionError.message}
          </p>
        )}
      </ModalShell>
    );
  }

  // --- The reconcile table --------------------------------------------------
  return (
    <ModalShell
      title={title}
      description="What AGP has registered, compared against what is actually in the org. Nothing is written until you confirm an action."
      ariaLabel="Roll out templates"
      actionPending={actionPending}
      onClose={onClose}
      footer={
        <>
          <button type="button" onClick={onClose} disabled={actionPending} className={SECONDARY_BTN}>
            Close
          </button>
          <button
            type="button"
            onClick={() => setConfirmingBatch(true)}
            disabled={actionPending || loading || !!error || !canSubmit}
            className="px-3.5 py-1.5 rounded-lg bg-emerald-600 text-white text-sm font-medium hover:bg-emerald-700 transition-colors disabled:opacity-40"
          >
            {/* Opens the confirm; it does NOT execute. The count comes from the selector's plan, so
                the number on the button is the number of names the request will carry. */}
            {`Review roll-out${plan.names.length ? ` (${plan.names.length})` : ''}`}
          </button>
        </>
      }
    >
      {/* THE CONSENT PROMPT — post-finalize only. Its second sentence is a promise about this
          surface's behaviour, and it is true because reconcile is a READ: arriving here creates
          nothing. Emerald rather than amber: this is an invitation, not a warning. */}
      {seedPrompt && (
        <div className="rounded-lg border border-emerald-200/70 bg-emerald-50/50 p-3">
          <p className="text-sm text-slate-700 font-medium">{SEED_CONSENT_PROMPT(orgName)}</p>
          <p className="text-[11px] text-slate-500 mt-1">
            Everything below is a comparison, not a queue. Select what to create, or adopt a
            repository AGP found.
          </p>
        </div>
      )}

      {loading ? (
        <p className="text-sm text-slate-400">Comparing this org against AGP’s registry…</p>
      ) : error ? (
        <div
          className={`rounded-lg border p-3 ${error.retryable ? 'border-amber-200/70 bg-amber-50/40' : 'border-rose-200/70 bg-rose-50/40'}`}
        >
          <h3 className={`text-sm font-semibold ${error.retryable ? 'text-amber-800' : 'text-rose-700'}`}>
            {error.retryable ? 'Couldn’t reach the provider' : 'Couldn’t reconcile this org'}
          </h3>
          <p className="text-sm text-slate-600 mt-1">{error.message}</p>
          {/* GATED on the verdict, like the Templates page's: re-reading a malformed connection id
              returns the same 422, so a Retry there is an affordance whose every click is refused. */}
          {error.retryable && (
            <button type="button" onClick={refetch} className={`mt-2 ${ROW_BTN}`}>
              Retry
            </button>
          )}
        </div>
      ) : view ? (
        <>
          {/* THE PREVIEW LINE — what this surface found, in numbers, above the table it describes.
              This is the consent screen's summary: an operator who reads nothing else must still
              learn that N will be created and M are not AGP's. */}
          <div className="flex items-center justify-between gap-3">
            <span className="text-[11px] text-slate-500 tabular-nums">
              {summary.createCount} to create · {summary.adoptCount} found unregistered ·{' '}
              {summary.inSyncCount} in sync
              {summary.missingCount > 0 && ` · ${summary.missingCount} missing`}
            </span>
            <button
              type="button"
              onClick={refetch}
              disabled={actionPending}
              title="Re-read the org from the provider"
              className={ROW_BTN}
            >
              Refresh
            </button>
          </div>

          {rowNotice && (
            <p className="text-[11px] text-emerald-700" role="status">
              <span className="font-medium">{rowNotice.name}</span> — {rowNotice.text}
            </p>
          )}

          {/* THE DROPPED TICKS, SAID OUT LOUD (E28D). `batchPlan` refuses a tick whose row now offers
              a different verb — that is the T6-L3 fix — but a refusal the operator cannot see is the
              stale-prune effect T6 deleted wearing a different hat: the box clears, the run does less
              than they authorised, and nothing says so.
              DERIVED, deliberately: it reads `plan.dropped` on every render rather than writing state
              from an effect. An effect that fired on `view` would BE the deleted prune, and it would
              also have to decide when to clear itself. Amber because the selection changed under the
              operator — not rose: nothing failed, and the drop is the safe outcome. */}
          {plan.dropped.length > 0 && (
            <p className="text-[11px] text-amber-800" role="status">
              {plan.dropped.length === 1
                ? '1 selection was cleared'
                : `${plan.dropped.length} selections were cleared`}{' '}
              — the org changed under {plan.dropped.length === 1 ? 'it' : 'them'}:{' '}
              <span className="font-medium">{plan.dropped.join(', ')}</span>. Re-select what you still
              want, against the action each row offers now.
            </p>
          )}

          {/* THE REGISTER. One table, every row, sorted by the server. Deliberately NOT grouped
              into "available" / "already there" sections like its predecessor: those groups WERE
              the boolean, and grouping by state again would hide that a row's group can be wrong
              while its state is right. Reading down one column of state labels is also how an
              auditor reads — the question is "what disagrees?", not "what can I tick?". */}
          <div className="space-y-2">
            <span className={SECTION_LABEL}>Templates</span>
            {rows.length === 0 ? (
              <p className="text-sm text-slate-400">
                AGP ships no starter templates and this org has no unregistered repositories.
              </p>
            ) : (
              <ul className="divide-y divide-emerald-100/70">
                {rows.map((row) => (
                  <ReconcileRow
                    key={row.item.name}
                    row={row}
                    // CARRIED, not merely ticked. Read off the plan so the visible tick and the wire
                    // body have ONE authority: a tick whose verb changed under a Refresh is dropped
                    // by `batchPlan`, and a box that stayed checked while the request omitted the
                    // name would be the same lie in the other direction.
                    queued={plan.names.includes(row.item.name)}
                    disabled={actionPending}
                    onToggleQueued={() => toggleQueued(row)}
                    onAction={(action) => askConfirm(action, row)}
                  />
                ))}
              </ul>
            )}
          </div>

          {/* THE FORCED INFRA REPO — its own section because it is its own FIELD on the wire, not a
              flagged row. That is how "always ensured, never a choice" is expressed now that
              `selectable` is gone: a structural separation cannot be flipped, so its INCLUSION in a
              roll-out has no checkbox and no way to opt out.
              WHAT E28D ADDED IS NOT AN OPT-OUT. The structural separation held for inclusion and
              NOT for overwrite: `overwrite` was one wire flag with two consumers, so ticking a
              template re-push above pushed AGP's Terraform module over the org's existing repo and
              reported "overwritten" — a write to a repository the operator never named. The box
              below is that write's own consent, and it renders in the PRESENT branch only. */}
          {infraRow && (
            <div className="space-y-2">
              <span className={SECTION_LABEL}>Runtime infra repo</span>
              <div className="rounded-lg border border-emerald-200/60 bg-emerald-50/40 px-3 py-2">
                <div className="flex items-center gap-3">
                  <span className="text-sm text-slate-700 font-medium truncate" title={infraRow.item.name}>
                    {infraRow.item.name}
                  </span>
                  <span className={`shrink-0 ml-auto ${PILL} bg-emerald-100 text-emerald-700`}>Required</span>
                </div>
                {/* WORDED TO `_ensure_infra`, not to the section's "Required" pill (E28C final
                    review, the same string defect as the consent line's). That method SKIPS an
                    existing repo when its consent is false — no push, action="skipped" — so the
                    present branch may not promise a re-push on every roll-out. The absent branch
                    does create and seed, so it says so.
                    E28D re-worded the present branch again: it said the module is re-pushed "only
                    when a re-push above authorises it", which described the coupling accurately and
                    is now false, because that coupling is deleted. It points at the box instead. */}
                <p className="text-[11px] text-slate-500 mt-1">
                  {infraRow.provenance
                    ? `Present at ${infraRow.provenance}. A roll-out leaves it in place — AGP’s Terraform module is pushed into it only if you ask below, and never because of a re-push you selected above.`
                    : 'Not in the org yet. Every roll-out creates it and pushes AGP’s Terraform module — it is not optional, because a tenant runtime cannot deploy without it.'}
                </p>

                {/* THE PRESENT BRANCH ONLY, and that is the honest half of this control. When the
                    repo is ABSENT, creating and seeding it is genuinely unconditional — a tenant
                    runtime cannot deploy without it — so a checkbox there would be a control whose
                    off position is a lie. A choice is only offered where it exists. */}
                {infraRow.provenance && (
                  <label className="flex items-start gap-2 mt-2 cursor-pointer">
                    <input
                      type="checkbox"
                      checked={overwriteInfra}
                      onChange={(e) => setOverwriteInfra(e.target.checked)}
                      disabled={actionPending}
                      className="h-4 w-4 mt-0.5 shrink-0 rounded border-slate-300 text-emerald-600 focus:ring-emerald-500/40 disabled:opacity-40"
                    />
                    <span className="text-[11px] text-slate-600">
                      Also re-push AGP’s Terraform module into this existing repository. One commit on
                      its default branch; its history stays, and an unchanged module writes no commit
                      at all.
                    </span>
                  </label>
                )}
              </div>
            </div>
          )}

          {actionError && (
            <p className={`text-sm ${actionError.retryable ? 'text-amber-800' : 'text-rose-600'}`} role="alert">
              {actionError.message}
            </p>
          )}
        </>
      ) : null}
    </ModalShell>
  );
}

// --- One reconcile row ------------------------------------------------------
// Name · state label · provenance · the one action. All four channels, because colour alone is
// not a channel every reader has and because provenance is the evidence behind the state.

function ReconcileRow({
  row,
  queued,
  disabled,
  onToggleQueued,
  onAction,
}: {
  row: ReconcileRowModel;
  queued: boolean;
  disabled: boolean;
  onToggleQueued: () => void;
  onAction: (action: SingleRepoAction) => void;
}) {
  const { item, label, tone, actions, queueAction, provenance, actionDisabled, note } = row;

  return (
    <li className="py-1.5">
      <div className="flex items-center gap-3">
        {queueAction ? (
          // ONE control for all three rollout-batch actions (F1). The checkbox used to be
          // create-only, which is why re-push had no way into the batch and its wire flag was
          // permanently false. The `aria-label` names the ACTUAL verb, so a screen reader hears
          // "Re-push seed x" rather than a generic "select" — and since E28D that verb is also what
          // the tick is RECORDED against, so what the label says is what the plan later checks.
          <input
            type="checkbox"
            checked={queued}
            onChange={onToggleQueued}
            disabled={disabled}
            aria-label={`${RECONCILE_ACTION_LABEL[queueAction]} ${item.name}`}
            className="h-4 w-4 rounded border-slate-300 text-emerald-600 focus:ring-emerald-500/40 disabled:opacity-40"
          />
        ) : (
          // A fixed-width spacer so every row's name starts on the same x — a table that jitters by
          // state is harder to scan down, and scanning down is what this surface is for.
          <span className="w-4 shrink-0" aria-hidden="true" />
        )}

        <div className="min-w-0 flex-1">
          <span className="text-sm text-slate-700 font-medium truncate block" title={item.name}>
            {item.name}
          </span>
          {provenance && (
            <span className="text-[11px] text-slate-400 tabular-nums" title="Default branch and head commit AGP read">
              {provenance}
            </span>
          )}
        </div>

        <span className={`shrink-0 ${PILL} ${tone}`}>{label}</span>

        <div className="shrink-0 flex items-center gap-1.5">
          {/* WHAT THE CHECKBOX WOULD DO, in words. The three batch verbs differ in what they write —
              creating a name nothing occupies is not the same act as pushing a commit onto a live
              repository — so the row has to say which one it is offering rather than leaving the
              operator to infer it from the state pill. Amber once queued: the footer's count is then
              traceable to specific rows, which is what makes the batch confirm auditable. */}
          {queueAction && (
            <span className={`${PILL} ${queued ? 'bg-amber-50 text-amber-700' : 'bg-slate-100 text-slate-500'}`}>
              {queued ? `Queued · ${RECONCILE_ACTION_LABEL[queueAction]}` : RECONCILE_ACTION_LABEL[queueAction]}
            </span>
          )}
          {/* The two SINGLE-REPO verbs keep immediate buttons: they are different routes, and adopt
              is a governance statement about one specific repository rather than a batchable push. */}
          {actions
            .filter((a): a is SingleRepoAction => a === 'adopt' || a === 'deregister')
            .map((action) => (
              <button
                key={action}
                type="button"
                onClick={() => onAction(action)}
                disabled={disabled || actionDisabled}
                title={actionDisabled ? ADOPT_NAME_RULE_HINT : undefined}
                className={
                  action === 'deregister'
                    ? `${ROW_BTN} text-rose-700 hover:bg-rose-50`
                    : ROW_BTN
                }
              >
                {RECONCILE_ACTION_LABEL[action]}
              </button>
            ))}
        </div>
      </div>

      {/* The name-rule annotation. Rendered as text and not only as a tooltip: a disabled button
          with a hover-only explanation is unreachable by keyboard and by touch, and this is the one
          case on this surface where an action is withheld for a reason the operator can FIX. */}
      {note && (
        <p className="ml-7 mt-0.5 text-[11px] text-slate-500">{note}</p>
      )}
    </li>
  );
}
