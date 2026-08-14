// RepositoryDetail — `/ops/repositories/:id` (E28/T11, contract C5 + design D11).
//
// ---------------------------------------------------------------------------
// WHY THIS PAGE EXISTS
//
// The repository row had become the whole product: fleet status, promotion state, drift, the
// materialize timeline and the delete cascade all competed for one table row, and most of the
// record was simply never rendered anywhere. `created_by`, `created_at`, `status`,
// `prod_candidate_at` and the agent record behind `agent_id` existed on the wire and had no
// surface at all; the materialize timeline was visible ONLY inside the creation modal,
// so the moment the modal closed the operator lost the one view that explained how the repo
// came to be. This is the destination that row now navigates to (T13 wires `onNavigate`).
//
// ---------------------------------------------------------------------------
// TWO INDEPENDENT PILLS, AND WHY THEY ARE NOT ONE
//
// The header shows DELIVERY (`cicd_status`) and RUNTIME (the boto3 probe) side by side, never
// merged. They are separate facts from separate producers, and merging them would be forced to
// lie in the harmful direction: an agent whose last build deployed fine but whose runtime is
// now unreachable would read GREEN, because the delivery machine has nothing bad to say.
//
// The pills render UNCAPTIONED (no column header to disambiguate them), which is exactly why
// T10 gave the two machines DISTINCT `failed` labels — "Delivery failed" and "Runtime failed".
// Both come from `CICD_LABEL` / `RUNTIME_LABEL`; this file writes no status string of its own,
// and every wire value passes through `toCicdStatus` / `toRuntimeStatus` rather than being
// narrowed by assertion. An absent runtime answer is `unknown`, never `ready` — absent data is
// not good news.
//
// THE HEADER'S RUNTIME PILL IS NOT CAPTIONED BY STAGE and must not gain a caption. It renders
// the UN-PARAMETERISED probe — the call with no `?stage=`, which reads the runtime the
// `agent_arn` scalar names — so it is an AGENT-level answer and lives HERE, in the header, where
// that scope is truthfully what it claims. `runtimeScope` supplies the note that says so.
//
// SEPARATELY, and since E28C/T7, the environment strip makes ONE PROBE PER STAGE (the backend
// has taken `?stage=` since E28A) and its Runtime column shows a real pill for each reading it
// can attribute, keeping the not-attributable note only where it cannot. The two are not
// substitutes and the page holds both: this pill must keep answering when every per-stage probe
// fails, and no stage row may ever fall back to this reading — three rows echoing one
// agent-level answer is the "three rows look like three probes" fabrication. See
// `stageRuntimeCell`.
//
// ---------------------------------------------------------------------------
// READ-ONLY GOVERNANCE (D11)
//
// The lifecycle pill STATES the agent's governance standing and offers NO approve button. An
// approve/grant/classify/deprecate affordance on an Operations surface is the shadow-governance
// failure mode this epic forbids — the approval lives on the governance surface, which this
// page LINKS to and never edits.
//
// THE THREE ACTIONS. Promote to prod (primary), Retry from failed step, and Delete — each the
// EXISTING route or modal, never a second path, and each gated by `headerActions` in the `.ts`
// so a test reaches the decision. The gates are deliberately NOT interchangeable: promote is on
// the STRICT owner gate, while retry's route carries the design-§3 ungoverned fallback, so
// collapsing them into one predicate would either offer a promote that 403s on every
// pre-migration project or hide the only recovery path on one. Retry and Delete are here
// because an operator lands on this page precisely WHEN a repo is broken — a detail page that
// reports a failure it cannot act on is the wrong shape.
//
// The UNGOVERNED-IN-PROD banner fires only when production is ESTABLISHED AS SERVING and the
// agent is still `proposed`: an agent serving production that governance never approved. Either
// alone is an ordinary state.
//
// "Established as serving" is a JOIN, not a status read (E28A/T4, finding #5). The banner used to
// gate on the delivery status, which the buildspec writes for EVERY stage with no branch — so a
// successful non-production build made the page claim "Serving production" with nothing promoted
// at all. The production verdict now comes from the promotion build id matched against the
// delivery history, whose terminal row is the only party that knows the outcome, and its
// unestablished third state renders the slate banner rather than the amber one.
//
// ---------------------------------------------------------------------------
// WHERE THE DECISIONS LIVE
//
// `repositoryDetailTabs.ts`. vitest collects only `src/**/*.test.ts`, so anything decided in
// this file is unpinnable — this is wiring: props in, markup out. Same split as T10's
// `RepoRow.tsx` / `repoRowModel.ts`.
//
// House style: emerald-on-glass Ops tokens (`opsUi.ts`), the `OpsPage` frame, Tailwind v4
// utility strings, 2-space indent.

import { useCallback, useEffect, useMemo, useRef, useState, type JSX } from 'react';
import { Link, useNavigate, useParams } from 'react-router-dom';

import {
  agentMcpGrantsApi,
  agentOpsApi,
  agentsApi,
  connectionsApi,
  grantsApi,
  projectsApi,
  pullRequestsApi,
  repositoriesApi,
  type Agent,
  type Connection,
  type Deployment,
  type PullRequestView,
  type Repository,
  type RuntimeStatus,
} from '../../api/client';
import { useUser } from '../../contexts/UserContext';
import { lifecycleBadge } from '../governance/agentUi';
import { findTenantAccount } from '../governance/tenantUi';
import { useTenantDirectory } from '../governance/useTenantDirectory';
// The step-timeline predicates are re-exported by `ProjectDetail` (their pinned test imports
// them from there) but they LIVE in `ProjectRepositoriesTab`. Imported from the home module so
// this page does not depend on a re-export that exists for a test's sake. `stepStatusText` is
// the per-step WORDING: rendering `s.status` raw here made one repo's step read "pending" on
// this page and "Waiting" on the project page — two spellings of one state.
//
// `nextBadgeFromSteps` is the three-way did-it-SUCCEED answer (failed / all-done / in flight) that
// the materialize modal and both repository lists already read. This page used to ask the
// HAS-IT-STOPPED predicate beside it instead, and a halted run is stopped — so a FAILED materialize
// rendered as complete. `materializeSummary` turns this function's key into the header word.
//
// A guard asserts the stopped-predicate is not called from this file, and it reads raw source
// without skipping comments — so this comment describes that function rather than naming it, which
// is what leaves the guard able to fail.
import { nextBadgeFromSteps, stepStatusText } from './ProjectRepositoriesTab';
import DeleteRepositoryModal from './DeleteRepositoryModal';
import EnvironmentStrip from './EnvironmentStrip.tsx';
import OpsPage from './OpsPage';
// The product's ONE promote dialog (E28C/T7, D-C4d), extracted from the project tab's row so the
// consent screen exists once — and this page is now its only caller, which is the ruling.
//
// TWO SPECIFIERS, ONE PAIR, and the casing is load-bearing exactly as it is on the imports above:
// `./PromoteConfirm.tsx` is the component (the extension is required, or the specifier resolves to
// the companion, which has no default export) and `./promoteConfirm` is that companion — the class
// tables, in a `.ts` so a test can index the tone table instead of regexing a source slice.
// `ARTIFACT_TONE_CLS` is read here for the Overview tab's "Approvable artifact" field, which shows
// the same marker outside the moment of approval.
import PromoteConfirm from './PromoteConfirm.tsx';
import { ARTIFACT_TONE_CLS } from './promoteConfirm';
import TabStrip from './TabStrip.tsx';
// The three tab bodies E28/T12 added, each in its own module under `repo-tabs/` with a `.ts`
// companion holding its judgements. They are IMPORTED rather than declared here for the same reason
// Overview and Access are not: this file is already the page, and a body declared inline is a body
// whose decisions no test can reach.
//
// THE EXPLICIT `.tsx` IS LOAD-BEARING, exactly as it is on the two imports above. Each component has
// a `.ts` companion whose name differs from it only in casing, and on a case-insensitive filesystem
// an extensionless specifier probes `<Name>.ts` FIRST — which resolves to the companion, so the
// import silently binds to a module with no default export. Verified: without the extensions `tsc`
// fails with TS1149 + "has no default export". Do not remove them.
import DeploymentsTab from './repo-tabs/DeploymentsTab.tsx';
import ObservabilityTab from './repo-tabs/ObservabilityTab.tsx';
import PullRequestsTab from './repo-tabs/PullRequestsTab.tsx';
import ResourcesTab from './repo-tabs/ResourcesTab.tsx';
// The Pull requests tab's RUNTIME visibility (E28/T14, A3). Its own judgements live in the
// `.ts` companion beside it; only the visibility resolution is needed HERE, because the answer
// decides whether the tab appears in the strip — and a tab body cannot fetch the answer to
// whether it should exist (nothing mounts to ask until the tab is already selectable).
import { prTabVisibility } from './repo-tabs/pullRequestsTab.ts';
import { NO_VALUE, orgLabel } from './opsLabels';
// The delivery pill no longer reads `CICD_LABEL` or the in-flight predicate DIRECTLY: since E28D
// both come out of `deliveryHeaderState`, which reads that same shared table and adds the one
// derived word the wire cannot carry. The tint key is still indexed here, because the tint is a
// pure function of the narrowed status and no derivation changes it.
import {
  CICD_BADGE_KEY,
  RUNTIME_BADGE_KEY,
  RUNTIME_LABEL,
  toCicdStatus,
  toRuntimeStatus,
} from './opsStatus';
import { OPS_BADGE, OPS_CARD } from './opsUi';
import {
  effectiveRole,
  maintainerActionMessage,
  prodCandidateView,
  promotionActionMessage,
} from './projectRoles';
import {
  REPOSITORY_DETAIL_TABS,
  deliveryHeaderState,
  environmentRows,
  headerActions,
  isTabSelectable,
  materializeSummary,
  prodGovernanceState,
  prodServingState,
  promotionArtifact,
  recordStatusLabel,
  repoBackLink,
  runtimeScope,
  runtimeStatusTitle,
  selectableTabKeys,
  shouldPollRepo,
  sortedStageNames,
  tabId,
  tabPanelId,
} from './repositoryDetailTabs';

// `ARTIFACT_TONE_CLS` used to be declared HERE as well, identically in shape and differing only in
// chip weight from the promote dialog's copy (E28C/T7 collapsed them). Two tables mapping one
// judgement — "does this approval name exact bytes?" — to a tint is the same fork that once shipped
// a live production repo in provisioning's amber, so the single table now lives beside the dialog
// that is the judgement's primary surface: `PromoteConfirm.tsx`.

// Sentence-case labels arrive correctly cased from the status tables, so no `capitalize` —
// that would render "Promoting To Prod…". Same shape as `RepoRow.tsx`'s pill.
const PILL =
  'inline-flex items-center gap-1.5 text-[11px] font-semibold px-2 py-0.5 rounded-full';
const FIELD_LABEL = 'block text-[11px] uppercase tracking-wide text-slate-400 font-medium mb-1';

/**
 * A count that may not have been READ. `null` is "we did not get an answer" and renders as an
 * em dash — never as `0`, because absent data is not good news and "not instrumented ≠ zero"
 * (D13) is the same rule one level down. A zero that is really a failed read would tell an
 * operator this agent has no inbound grants, which is the reassuring direction and the wrong
 * one on a governance surface.
 */
type MaybeCount = number | null;

export default function RepositoryDetail(): JSX.Element {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const { user } = useUser();
  const tenantDirectory = useTenantDirectory((user?.role_level ?? 0) >= 2);

  const [repo, setRepo] = useState<Repository | null>(null);
  const [agent, setAgent] = useState<Agent | null>(null);
  // The agent read FAILED — distinct from "not yet read", which `agent === null` also means. The
  // ungoverned-in-prod banner needs the difference: `lifecycle_state` lives on that record, so an
  // unread record silently produced NO warning, and an absent alarm reads as "checked, nothing
  // wrong". Same rule as the runtime probe (absent ⇒ unknown, never ready) applied to the page's
  // highest-consequence statement.
  const [agentError, setAgentError] = useState(false);
  const [connection, setConnection] = useState<Connection | undefined>(undefined);
  // The project record's own `connection_id`, kept because it is `orgLabel`'s documented fallback
  // when the connection does not RESOLVE — a deleted connection, or a 403 for a caller who may not
  // list them. Passing `null` there discarded the fallback and blanked the cell to an em dash over a
  // project that does have an org; the id is what the record actually holds, so it is both honest
  // and the thing an operator can search for.
  const [connectionId, setConnectionId] = useState<string | null>(null);
  const [tenantId, setTenantId] = useState<string | null>(null);
  const [projectName, setProjectName] = useState<string | null>(null);
  const [heldRole, setHeldRole] = useState<ReturnType<typeof effectiveRole>>(null);
  // The server's `ungoverned` bit. The RETRY gate needs it (its route carries the design-§3
  // fallback), and `headerActions` keeps it away from `promote`, which is on the strict gate.
  const [ungoverned, setUngoverned] = useState<boolean | undefined>(undefined);
  const [runtime, setRuntime] = useState<RuntimeStatus | undefined>(undefined);
  // ONE READING PER STAGE (E28C/T7, D-C4c) — keyed by the tenant's own stage name.
  //
  // Separate state from `runtime` above, and separate from it on purpose. `runtime` is the
  // AGENT-level probe: the un-parameterised call, whose answer the page header's pill renders
  // with no stage caption. This map is the per-stage probe the environment strip needs, and the
  // two are not interchangeable — the header must keep asking the question it can honestly
  // answer even when a per-stage read fails, and a stage row must not fall back to the
  // agent-level answer, which is exactly the "three rows look like three probes" fabrication.
  //
  // A stage ABSENT from this map (or holding `undefined`) is a stage whose probe failed or was
  // never made; `stageRuntimeCell` renders that as absence rather than as a status.
  const [runtimeByStage, setRuntimeByStage] = useState<Record<string, RuntimeStatus | undefined>>(
    {},
  );
  const [deployments, setDeployments] = useState<Deployment[]>([]);
  // The history read FAILED — distinct from an empty history, which is a claim ("never
  // deployed") the failed read has not earned.
  const [historyError, setHistoryError] = useState(false);
  const [historyLoading, setHistoryLoading] = useState(true);
  const [inboundGrants, setInboundGrants] = useState<MaybeCount>(null);
  const [outboundMcps, setOutboundMcps] = useState<MaybeCount>(null);
  // The pull requests, and whether the Pull requests tab may render AT ALL (E28/T14, A3).
  //
  // The org's `pull_requests` grant is MANUAL per org and GitHub does not retro-apply a manifest
  // change to an already-created App, so an org onboarded before this feature refuses this read
  // forever. `prTabVisibility` turns EVERY failure — that refusal, an outage, an unrecognized
  // message — into `hidden`, because a tab that renders and then cannot answer is worse than an
  // absent one: the tablist announces it and hands the keyboard user somewhere broken.
  //
  // The read lives here rather than in the tab because its answer decides whether the tab is in
  // the strip, and a body only mounts once its tab is selectable — a tab that fetched its own
  // visibility could never become visible.
  const [pullRequests, setPullRequests] = useState<PullRequestView[]>([]);
  const [prVisibility, setPrVisibility] = useState<ReturnType<typeof prTabVisibility>>(
    prTabVisibility({ status: 'loading' }),
  );

  const [loading, setLoading] = useState(true);
  const [notFound, setNotFound] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [reloadNonce, setReloadNonce] = useState(0);

  const [activeKey, setActiveKey] = useState<string>(selectableTabKeys()[0]);
  const [promoting, setPromoting] = useState(false);
  const [promoteError, setPromoteError] = useState<string | null>(null);
  const [promoteNotice, setPromoteNotice] = useState<string | null>(null);
  // Is the promote CONFIRM revealed? (E28C/T7, D-C4d.)
  //
  // This page's Promote button used to fire `handlePromote` off a bare `onClick` — no dialog, no
  // provenance, nothing naming the artifact. The project tab had the reveal-then-confirm, so the
  // surface with LESS evidence available had MORE consent around it. 4d removed the tab's entry
  // point entirely and moved the dialog here, which is the only surface that can show what an
  // approval approves. Reveal-then-confirm, following the AgentDetail lifecycle idiom — never
  // `window.confirm`, which cannot render a digest, a commit or a caution.
  const [promoteConfirm, setPromoteConfirm] = useState(false);
  // Focus goes to the confirm on reveal and RETURNS to the trigger on cancel; without the return,
  // dismissing would drop focus to document start, from a control in the page header.
  const promoteTriggerRef = useRef<HTMLButtonElement>(null);
  const promoteConfirmRef = useRef<HTMLButtonElement>(null);
  const [retrying, setRetrying] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [deleteOpen, setDeleteOpen] = useState(false);

  const refetch = useCallback(() => setReloadNonce((n) => n + 1), []);

  // ---------------------------------------------------------------------------
  // Load. There is no `GET /repositories/{id}`, so the repo is found in the fleet list — which
  // is already the tenant- and project-role-filtered read, so a repo this caller may not see is
  // simply absent from it and lands on the not-found state. That is the same 404-shaped absence
  // the detail routes give, reached a different way.
  //
  // Everything after the repo is BEST-EFFORT and must not blank the page: the agent record, the
  // project (for `effective_role` and the org label), the runtime probe, the deployment history
  // and the two Access counts each degrade on their own. A failed count stays `null`, which
  // renders as unknown rather than as zero.
  // ---------------------------------------------------------------------------
  useEffect(() => {
    if (!id) {
      setLoading(false);
      setNotFound(true);
      return;
    }
    let cancelled = false;
    setLoading(true);
    setHistoryLoading(true);
    setHistoryError(false);
    // Reset per load, or a refetch after a transient failure would keep warning about a record it
    // has since read successfully.
    setAgentError(false);

    repositoriesApi
      .list()
      .then(async (rows) => {
        if (cancelled) return;
        const found = rows.find((r) => r.id === id);
        if (!found) {
          setNotFound(true);
          return;
        }
        setRepo(found);
        setNotFound(false);
        setError(null);

        // The project: its `effective_role` is the ONLY signal the browser cannot compute for
        // itself (a role may be granted to an Entra GROUP, and nothing client-side evaluates
        // group membership), and it carries the tenant id the environment strip needs.
        try {
          const detail = await projectsApi.get(found.project_id);
          if (cancelled) return;
          setHeldRole(effectiveRole(detail));
          setUngoverned(detail.ungoverned);
          setTenantId(detail.project.tenant_id ?? null);
          // The project's NAME, so the header cell reads it rather than the raw UUID. The
          // design dropped opaque ids from Ops surfaces, and this read was already happening
          // for `effective_role` — only the name was being thrown away.
          setProjectName(detail.project.name || null);
          // Held whether or not the connections read below succeeds — it IS the fallback for that
          // read failing.
          setConnectionId(detail.project.connection_id || null);
          try {
            const conns = await connectionsApi.list();
            if (!cancelled) {
              setConnection(conns.find((c) => c.id === detail.project.connection_id));
            }
          } catch {
            if (!cancelled) setConnection(undefined);
          }
        } catch {
          /* best-effort: no role hint, no tenant, no org label */
        }

        // The agent record — `lifecycle_state` (the read-only governance pill and half the
        // ungoverned-in-prod condition) plus the model/region/endpoint the Overview shows.
        try {
          const rec = await agentsApi.get(found.agent_id);
          if (!cancelled) {
            setAgent(rec);
            setAgentError(false);
          }
        } catch {
          // Best-effort for the descriptive fields — the lifecycle pill renders as not-reported.
          // But the FAILURE is recorded, because `lifecycle_state` is half the ungoverned-in-prod
          // condition and a swallowed read would silently withhold that warning.
          if (!cancelled) setAgentError(true);
        }

        // The runtime probe. LEFT `undefined` on failure, deliberately: `toRuntimeStatus`
        // narrows that to `unknown`, so the pill says "Unreachable" rather than claiming ready.
        try {
          const rt = await agentOpsApi.runtime(found.agent_id);
          if (!cancelled) setRuntime(rt);
        } catch {
          if (!cancelled) setRuntime(undefined);
        }

        // Deployment history — the environment strip's data. A FAILED read is reported as a
        // failed read, not as an empty history: an empty array is indistinguishable from "this
        // agent has never deployed", and the strip would render the failure as the positive
        // claim "Never deployed" for every stage. Same rule as the runtime probe above (absent
        // ⇒ unknown, never ready) and the counts below (failed ⇒ em-dash, never 0).
        try {
          const rows2 = await agentOpsApi.deployments(found.agent_id);
          if (!cancelled) {
            setDeployments(rows2);
            setHistoryError(false);
          }
        } catch {
          if (!cancelled) {
            setDeployments([]);
            setHistoryError(true);
          }
        } finally {
          if (!cancelled) setHistoryLoading(false);
        }

        // The two Access counts. Each failure stays `null` ⇒ rendered unknown, never 0.
        try {
          const grants = await grantsApi.list(found.agent_id);
          if (!cancelled) setInboundGrants(grants.length);
        } catch {
          if (!cancelled) setInboundGrants(null);
        }
        try {
          const mcps = await agentMcpGrantsApi.list(found.agent_id);
          if (!cancelled) setOutboundMcps(mcps.length);
        } catch {
          if (!cancelled) setOutboundMcps(null);
        }

        // The pull requests — and THIS READ IS ALSO THE CAPABILITY PROBE (E28/T14, A3).
        //
        // Reusing the list read is deliberate: a dedicated capability endpoint would be a second
        // thing that can disagree with the read the tab actually depends on, and this read already
        // answers the question definitively — it is the call a missing `pull_requests` grant
        // refuses.
        //
        // EVERY failure resolves to `hidden`, not just the recognized refusal (`prTabVisibility`
        // owns that, and its test pins the unrecognized case). Note which direction this fails in:
        // hiding on an unexpected error costs an operator a tab they expected and a reload fixes
        // it, whereas showing on an unexpected error costs them a tab whose every action fails.
        // Only the first is recoverable.
        try {
          const prs = await pullRequestsApi.list(found.id);
          if (!cancelled) {
            setPullRequests(prs);
            setPrVisibility(prTabVisibility({ status: 'ok' }));
          }
        } catch (err: unknown) {
          if (!cancelled) {
            setPullRequests([]);
            setPrVisibility(
              prTabVisibility({
                status: 'error',
                message: err instanceof Error ? err.message : null,
              }),
            );
          }
        }
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        const message = err instanceof Error ? err.message : 'Failed to load the repository.';
        if (/not found/i.test(message) || /404/.test(message)) setNotFound(true);
        else setError(message);
      })
      .finally(() => {
        if (!cancelled) {
          setLoading(false);
          setHistoryLoading(false);
        }
      });

    return () => {
      cancelled = true;
    };
  }, [id, reloadNonce]);

  const tenantAccount = findTenantAccount(tenantId, user?.tenants, tenantDirectory);
  // The tenant is KNOWN and did not RESOLVE — so which stages exist is unknown, and the strip must
  // say that instead of stating how the tenant is configured. Three live paths land here: the
  // project read threw (its catch leaves the tenant id null, which is why this is guarded on the id
  // rather than only on the record), the admin tenant directory read failed (that hook is
  // fail-silent by design), or the repository's tenant is not among a non-admin caller's own
  // memberships. `null` tenant id means there is nothing to have failed to resolve.
  const stagesUnknown = tenantId !== null && tenantAccount === null;

  // The tenant's stage names, from the SAME derivation the strip's rows come from
  // (`sortedStageNames`) so the probe set and the rendered set cannot diverge — a stage probed
  // but not rendered is wasted work, and a stage rendered but not probed silently shows absence.
  // SERIALIZED for the effect's dependency list, because the array identity changes on every render
  // and depending on it would re-probe forever (the same reason `promotingKey` exists in the project
  // tab). `JSON.stringify` rather than a join on a separator: stages are FREE-FORM (D8) — a tenant's
  // stage set is open, so a name may contain any character — and a round-trip that parses back without
  // guessing is what keeps the probe set EQUAL to the rendered set. Under a join, one stage whose name
  // contained the separator would split into probes for stages that do not exist, and the row that
  // stage actually renders would show absence forever.
  const stageNames = sortedStageNames(tenantAccount?.stages);
  const stageKey = JSON.stringify(stageNames);
  const agentId = repo?.agent_id ?? null;

  // ---------------------------------------------------------------------------
  // THE PER-STAGE RUNTIME PROBES (E28C/T7, D-C4c).
  //
  // Its own effect, and not part of the main load, because the stage set is not known when that
  // effect runs: the stages come from the tenant record, which is resolved from the project read
  // and the tenant directory in RENDER (above). Folding these calls into the loader would mean
  // guessing the stage set or serialising the whole page behind the directory.
  //
  // ONE REQUEST PER STAGE, in parallel, and each one's failure is ITS OWN. A rejected probe
  // leaves that stage `undefined` — rendered as absence by `stageRuntimeCell` — and says nothing
  // about the other stages: `Promise.all` would have let one unreachable stage erase every
  // reading on the page, which is the failure direction this whole surface is built to avoid.
  // The cost is bounded by the tenant's stage count (typically two or three), and the fleet list
  // still makes no runtime read at all for the reason `Repositories.tsx:106` gives.
  //
  // The agent-level probe in the loader is NOT replaced. It answers the header pill, whose scope
  // is truthfully the agent, and it must keep answering even when every per-stage probe fails.
  // ---------------------------------------------------------------------------
  useEffect(() => {
    // Nothing to key readings by, or no agent to ask about — hold an empty map rather than a
    // stale one from a previously-viewed repository.
    // Parsed back from the serialized dependency — the exact array `stageNames` held.
    const stages = JSON.parse(stageKey) as string[];
    if (agentId === null || stages.length === 0) {
      setRuntimeByStage({});
      return;
    }
    let cancelled = false;
    void Promise.all(
      stages.map(async (stage): Promise<[string, RuntimeStatus | undefined]> => {
        try {
          return [stage, await agentOpsApi.runtime(agentId, { stage })];
        } catch {
          // This stage only. `undefined` ⇒ the cell renders absence, never a status.
          return [stage, undefined];
        }
      }),
    ).then((entries) => {
      if (!cancelled) setRuntimeByStage(Object.fromEntries(entries));
    });
    return () => {
      cancelled = true;
    };
  }, [agentId, stageKey, reloadNonce]);

  // ---------------------------------------------------------------------------
  // KEEP THE RECORD LIVE while something is running (E28D).
  //
  // The page used to take ONE snapshot per load, and `handleRetry` took a second one at the instant
  // the retry was accepted — the moment the record is at its LEAST final (steps reset to pending,
  // delivery back to provisioning). Nothing asked again, so the surface whose whole point is that
  // the materialize timeline is permanently viewable here showed a FROZEN one for the entire run,
  // while the create modal and the project tab — both polling this same record on this same
  // endpoint — showed it advancing.
  //
  // WHETHER to poll is `shouldPollRepo`'s answer, not this file's: no jsdom here means a mounted
  // effect is untestable, so the condition lives where a test reaches it and this holds only the
  // interval plumbing. A settled repo polls ZERO times.
  //
  // ONE RECORD PER TICK, not the page. `getRepoStatus` is what the two existing pollers use;
  // `refetch()` re-runs the whole best-effort load — the fleet list, the project, the agent, every
  // per-stage runtime probe and the history — which is far too much to repeat every 3 seconds to
  // learn one record's steps.
  //
  // ...BUT EXACTLY ONCE ON THE SETTLE EDGE, because the record is not the only thing this page
  // derives from. `agentOpsApi.deployments` is called from ONE place — the load effect — so the
  // deployment history is a page-load snapshot, and the delivery header's derived word reads that
  // snapshot. Ticking the record alone left two live holes (E28D fix round 1, review I-A):
  //
  //   1. RETRY: materialize completes ⇒ the record goes `ready` with terminal steps ⇒ this poller
  //      stops. The delivery build then runs, and the buildspec writes only TERMINAL outcomes, so
  //      the record stays `ready` for its whole duration while the load-time history holds no
  //      launch row for it. The header would read "Not built yet" over a running build — precisely
  //      the contradiction this task exists to fix, surviving on the Retry path.
  //   2. PROMOTE: the promote settles ⇒ the record reads `deployed` ⇒ this poller stops, while the
  //      stale history still reports that promote's launch row as in flight. `deployed` IS
  //      overridable, so the header would claim a live build off a record read 3s earlier as done.
  //      (The derived word is deliberately not quoted anywhere in this file — it is a pinned
  //      constant in the `.ts` and a guard asserts this file never writes it.)
  //
  // So the moment the fresh record stops being pollable, the page reloads ONCE — per settle, never
  // per tick — and the history the header reads catches up with the record it is read beside. The
  // interval is stopped from inside the tick before that (AddRepoModal's own idiom), so no second
  // tick can fire between the settle and the deps flip and the reload cannot be requested twice.
  //
  // `handleRetry` needs no special-casing: its `setRepo(resumed)` makes the predicate true and this
  // effect arms itself. Deliberately NOT keyed on the whole record (a new object every tick would
  // rebuild the interval continuously and no 3s tick would ever fire) — only on the identity and
  // the boolean.
  // ---------------------------------------------------------------------------
  const pollRef = useRef<number | null>(null);
  const repoId = repo?.id ?? null;
  const repoProjectId = repo?.project_id ?? null;
  const polling = repo !== null && shouldPollRepo(repo);
  useEffect(() => {
    if (repoId === null || repoProjectId === null || !polling) return;
    let cancelled = false;
    const stop = () => {
      if (pollRef.current !== null) {
        window.clearInterval(pollRef.current);
        pollRef.current = null;
      }
    };
    pollRef.current = window.setInterval(async () => {
      // A transient failure is not a terminal state, so it yields `null` and the interval keeps
      // going. State is set from the TIMER, never synchronously in the effect body.
      const fresh = await projectsApi.getRepoStatus(repoProjectId, repoId).catch(() => null);
      // `clearInterval` does not cancel a request already in flight, so the tick that was awaiting
      // when this effect was torn down still resumes here — and must then touch nothing.
      if (cancelled || fresh === null) return;
      setRepo(fresh);
      // THE SETTLE EDGE, asked with the same pinned predicate that armed this interval — there is no
      // second definition of "still running" to drift from.
      if (!shouldPollRepo(fresh)) {
        stop();
        refetch();
      }
    }, 3000);
    return () => {
      cancelled = true;
      stop();
    };
  }, [repoId, repoProjectId, polling, refetch]);

  // Promote — the ONE action on this page and, since E28C/T7, THE ONE ENTRY POINT IN THE PRODUCT.
  // The EXISTING governed route: no body and no tag, so the backend resolves the image from
  // `prod_candidate_image_tag` and takes the actor from the validated principal, and there is no
  // input through which an arbitrary image could reach production. Errors are MAPPED, never raw —
  // the route pins five literals.
  //
  // IT IS NO LONGER REACHED FROM A BARE `onClick`. The trigger reveals `PromoteConfirm`, and only
  // that dialog's own button calls this — so the commit, the pusher, the image, whether prod will
  // match dev, and whether the approval names exact bytes or a mutable tag are all on screen
  // before a production deployment is authorised.
  const handlePromote = useCallback(async () => {
    if (!repo || promoting) return;
    setPromoting(true);
    setPromoteError(null);
    // Cleared too: without this a second promote that FAILS renders the rose error beside the
    // stale emerald success notice — two contradictory statements about one action.
    setPromoteNotice(null);
    setActionError(null);
    try {
      const updated = await projectsApi.promoteRepo(repo.project_id, repo.id);
      setRepo(updated);
      // The dialog closes only on SUCCESS. A failure leaves it open beside the mapped error, so the
      // operator can read what went wrong with the artifact still named above it — closing it would
      // take the object of the failed act off screen.
      setPromoteConfirm(false);
      setPromoteNotice(`Promoting ${repo.name} to prod. Refreshing this page now.`);
      refetch();
    } catch (err: unknown) {
      setPromoteError(
        promotionActionMessage(
          err instanceof Error ? err.message : '',
          'The promotion could not be started.',
        ),
      );
    } finally {
      setPromoting(false);
    }
  }, [repo, promoting, refetch]);

  // Close the confirm and hand focus BACK to the trigger. Deferred so it lands after the dialog
  // unmounts; without the return, focus falls to document start from a header control.
  const closePromoteConfirm = useCallback(() => {
    setPromoteConfirm(false);
    setPromoteError(null);
    window.setTimeout(() => promoteTriggerRef.current?.focus(), 0);
  }, []);

  // Move focus onto the confirm once revealed, so the action is reachable by keyboard immediately.
  useEffect(() => {
    if (promoteConfirm) promoteConfirmRef.current?.focus();
  }, [promoteConfirm]);

  // Escape dismisses it (the ProjectAccessTab picker idiom) — but never mid-request, when there is
  // nothing left to cancel and the promotion is already with the backend.
  useEffect(() => {
    if (!promoteConfirm) return;
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && !promoting) {
        e.preventDefault();
        closePromoteConfirm();
      }
    };
    document.addEventListener('keydown', onKeyDown);
    return () => document.removeEventListener('keydown', onKeyDown);
  }, [promoteConfirm, promoting, closePromoteConfirm]);

  // Retry from the first FAILED materialize step — the existing MAINTAINER-gated route, not a
  // second recovery path. 409 "Nothing to retry" is not an error: it means the run has nothing
  // retryable left, i.e. it already completed, so it is reported as such and the page refetches
  // to show the terminal timeline.
  const handleRetry = useCallback(async () => {
    if (!repo || retrying) return;
    setRetrying(true);
    setActionError(null);
    setPromoteError(null);
    setPromoteNotice(null);
    try {
      const resumed = await projectsApi.retryRepo(repo.project_id, repo.id);
      setRepo(resumed);
      setPromoteNotice(`Resuming ${resumed.name} from its failed step.`);
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : '';
      if (/nothing to retry/i.test(message)) {
        setPromoteNotice('There is nothing left to retry — this repository already completed.');
        refetch();
      } else {
        setActionError(maintainerActionMessage(message, 'The retry could not be started.'));
      }
    } finally {
      setRetrying(false);
    }
  }, [repo, retrying, refetch]);

  if (loading && repo === null) {
    return (
      <OpsPage backTo="/ops/repositories" title="Repository">
        <div className={`${OPS_CARD} p-8 text-center text-slate-400 text-sm`}>
          Loading repository…
        </div>
      </OpsPage>
    );
  }

  if (notFound) {
    return (
      <OpsPage backTo="/ops/repositories" title="Repository not found">
        <div className={`${OPS_CARD} p-8`}>
          <h3 className="text-sm font-semibold text-slate-800">Repository not found</h3>
          <p className="text-sm text-slate-500 mt-1">
            This repository doesn’t exist or is no longer available to you.
          </p>
        </div>
      </OpsPage>
    );
  }

  if (error || !repo) {
    return (
      <OpsPage backTo="/ops/repositories" title="Repository">
        <div className="bg-white/80 backdrop-blur rounded-xl border border-rose-200/70 shadow-sm p-6">
          <h3 className="text-sm font-semibold text-rose-700">Couldn’t load repository</h3>
          <p className="text-sm text-slate-600 mt-1">{error ?? 'Unknown error.'}</p>
          <button
            type="button"
            onClick={refetch}
            className="mt-3 px-3 py-1.5 rounded-lg bg-white border border-slate-300 text-slate-700 text-xs font-medium hover:bg-slate-50 transition-colors"
          >
            Retry
          </button>
        </div>
      </OpsPage>
    );
  }

  // Every status narrowed at the boundary — never asserted.
  const cicd = toCicdStatus(repo.cicd_status);
  const runtimeKey = toRuntimeStatus(runtime?.status);
  const scope = runtimeScope(runtime);
  // The per-stage rows, derived HERE as well as inside the strip — from the SAME source object, so
  // the header and the strip cannot answer differently about whether a build is running. That
  // disagreement is the finding this fixes: the pill read the record's single scalar (whose `ready`
  // honestly means "no build has landed yet") while the strip read the history, which knows a build
  // is running because its launch row has no terminal partner. The pill's word is now derived from
  // both, and it FAILS SAFE — an unreadable history never produces a claim about a live build.
  const rows = environmentRows({
    stages: tenantAccount?.stages,
    stagesUnknown,
    deployments,
    historyError,
  });
  const delivery = deliveryHeaderState({
    cicdStatus: repo.cicd_status,
    environmentRows: rows,
    historyError,
  });
  // IS PRODUCTION ACTUALLY SERVING? Derived from the promotion build id JOINED to the delivery
  // history, never from `cicd_status` — which the buildspec writes for EVERY stage with no branch,
  // so it is a delivery fact with no stage in it and was being read as a production claim (#5). The
  // delivery pill above still reads it, correctly, because that is all the pill claims.
  const serving = prodServingState({
    lastPromotionBuildId: repo.last_promotion_build_id,
    lastPromotedAt: repo.last_promoted_at,
    lastPromotedImageTag: repo.last_promoted_image_tag,
    deployments,
    historyError,
    historyLoading,
  });
  // THREE answers, not two. The yes-or-no predicate answered `false` when the agent read failed,
  // so an unreadable governance record silently produced no banner at all — and an absent alarm
  // reads as "checked, nothing wrong". Decided in the `.ts` so a test reaches all three.
  const prodGovernance = prodGovernanceState({
    serving,
    lifecycleState: agent?.lifecycle_state,
    agentRead: !agentError,
  });
  // All three header actions, on their EXISTING gates — decided in the `.ts` so a test reaches
  // them, and so `promote`'s strict gate cannot accidentally inherit `retry`'s §3 fallback.
  const may = headerActions({
    held: heldRole,
    roleLevel: user?.role_level ?? 0,
    ungoverned,
    cicdStatus: repo.cicd_status,
    prodCandidateStatus: repo.prod_candidate_status,
    steps: repo.steps,
  });
  // TWO filters, and they answer two different questions (E28/T14, A3).
  //
  //   • `ready` is a BUILD-TIME fact: does this tab have a body in the shipped app? After T14 all
  //     six do, permanently.
  //   • `prVisibility` is a RUNTIME, PER-ORG fact: can THIS org serve pull requests at all? The
  //     App's `pull_requests` permission is a manual per-org grant that GitHub does not
  //     retro-apply, so an org onboarded earlier cannot — and the tab must then be ABSENT rather
  //     than present-and-failing.
  //
  // Overloading `ready` with the second would make the registry lie about what the epic built, and
  // lie per-org on a value that is global to the build. Hence the separate filter here, with the
  // decision itself in `pullRequestsTab.ts` where a test reaches it.
  //
  // `pending` also hides, so the tab does not FLICKER into the strip and out again while the probe
  // is in flight.
  const tabs = REPOSITORY_DETAIL_TABS.filter(
    (t) => t.ready && (t.key !== 'pull-requests' || prVisibility === 'visible'),
  );
  const tabKeys = tabs.map((t) => t.key);
  // Selectability is now the RENDERED set, not the registry's — a key `isTabSelectable` allows but
  // this org cannot serve must not be openable, or a stale deep link would land on a hidden tab's
  // panel. The registry check stays as the first gate (it rejects an unknown key outright).
  const selectedKey =
    isTabSelectable(activeKey) && tabKeys.includes(activeKey) ? activeKey : tabKeys[0];
  // TabStrip renders NOTHING below two tabs, and its contract says a caller must then not point
  // a panel's `aria-labelledby` at a `role="tab"` that does not exist (the Settings near-miss).
  // Two tabs are ready today, so this is dormant — but the commit that extracts the contract
  // must not be the one that violates it.
  //
  // The WHOLE set is conditional, not just the label: a panel role with no owning tablist and no
  // accessible name is broken ARIA that reads worse than no ARIA, and a focusable tabindex only
  // earns its place beside a roving-tabindex bar that needs somewhere to hand focus. Same idiom
  // as `Settings.tsx:147-157`, which reached this conclusion in its own fix round. Spread as one
  // object so no attribute of the set can be made unconditional on its own — the guard in
  // `repositoryDetailTabs.test.ts` reads this file for the unconditional forms, which is why this
  // comment describes them instead of writing them out.
  // Where "back" goes, and what it says (D-C4b). Decided in the `.ts` — see the call site below.
  const backLink = repoBackLink({ projectId: repo.project_id, projectName });

  const tabStripRendered = tabs.length >= 2;
  const panelProps = (key: string) =>
    tabStripRendered
      ? { role: 'tabpanel', id: tabPanelId(key), 'aria-labelledby': tabId(key), tabIndex: 0 }
      : {};

  return (
    <OpsPage
      // BACK GOES TO THE PARENT PROJECT, not to the fleet list (E28C/T7, D-C4b) — and BOTH halves
      // are `repoBackLink`'s, not this file's.
      //
      // The destination and the label were inline template strings here until review re-pointed
      // `backTo` at the fleet list and the whole suite stayed green: the only assertions covering 4b
      // were about the three early returns below and the untouched `OpsPage` call sites, so the one
      // line this spec item is about had no guard at all. The selector gives it one that fails on
      // BEHAVIOUR — see `repoBackLink` for why the parent project is the right destination and why the
      // label falls back to the raw project id rather than to a vague word.
      //
      // Browser history is deliberately NOT touched: this page pushes none, and the reported Back
      // misbehaviour was never reproduced (design D-C4b — we do not fix blind).
      backTo={backLink.to}
      backLabel={backLink.label}
      title={repo.name}
      subtitle={agent?.purpose || undefined}
      action={
        // Role-gated by RENDERING, not by `disabled`: a caller without the standing is not
        // offered a button whose every click 403s (`disabled` is reserved for in-flight).
        // Promote is primary; Retry and Delete are the two things an operator needs when they
        // land here BECAUSE the repo is broken — a detail page that reports a failure it cannot
        // act on is the wrong shape. Each calls the EXISTING route/modal, never a second path.
        may.promote || may.retry || may.destroy ? (
          <div className="flex items-center gap-2">
            {may.retry && (
              <button
                type="button"
                onClick={() => void handleRetry()}
                disabled={retrying}
                className="px-3.5 py-1.5 rounded-lg bg-white border border-slate-300 text-slate-700 text-sm font-medium hover:bg-slate-50 transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
              >
                {retrying ? 'Retrying…' : 'Retry from failed step'}
              </button>
            )}
            {may.destroy && (
              // The E23 five-item cascade, through the EXISTING modal — which owns the
              // reachability pre-check, the checklist and the per-item outcome. A second
              // confirm here would be a second thing to keep in step with that cascade.
              <button
                type="button"
                onClick={() => setDeleteOpen(true)}
                className="px-3.5 py-1.5 rounded-lg bg-white border border-rose-300 text-rose-700 text-sm font-medium hover:bg-rose-50 transition-colors"
              >
                Delete
              </button>
            )}
            {/* REVEALS the confirm; it does not promote (E28C/T7). It used to call `handlePromote`
                straight from `onClick`, which authorised a production deployment on one click with
                nothing on screen naming what would ship. Hidden while the confirm is open — the
                dialog replaces its trigger rather than sitting under it, the same idiom the project
                tab's row used. */}
            {may.promote && !promoteConfirm && (
              <button
                ref={promoteTriggerRef}
                type="button"
                onClick={() => {
                  setPromoteConfirm(true);
                  setPromoteError(null);
                  setPromoteNotice(null);
                  setActionError(null);
                }}
                disabled={promoting}
                className="px-3.5 py-1.5 rounded-lg bg-emerald-600 text-white text-sm font-medium hover:bg-emerald-700 transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
              >
                {promoting ? 'Promoting…' : 'Promote to prod'}
              </button>
            )}
          </div>
        ) : undefined
      }
    >
      {/* Header meta — the two INDEPENDENT pills, the READ-ONLY lifecycle pill, and the
          identifying facts. */}
      <div className={`${OPS_CARD} px-4 py-3 mb-6 flex flex-wrap items-center gap-x-8 gap-y-3`}>
        <div>
          <span className={FIELD_LABEL}>Agent</span>
          {/* A link OUT to the governance surface. Reading and linking to it is expected;
              editing it is not this epic's to do. */}
          <Link
            to={`/agents/${repo.agent_id}`}
            className="text-sm text-emerald-700 hover:text-emerald-800 font-medium transition-colors"
          >
            {agent?.name ?? repo.agent_id}
          </Link>
        </div>
        <div>
          <span className={FIELD_LABEL}>Project</span>
          <Link
            to={`/ops/projects/${repo.project_id}`}
            className="text-sm text-emerald-700 hover:text-emerald-800 font-medium transition-colors"
          >
            {/* The NAME, not the raw UUID — the design dropped opaque ids from Ops surfaces,
                and the project read that supplies `effective_role` already carries it. */}
            {projectName ?? repo.project_id}
          </Link>
        </div>
        <div>
          <span className={FIELD_LABEL}>Org</span>
          {/* The id is the SECOND argument for a reason — `orgLabel` falls back to it when the
              connection did not resolve, which is a best-effort read on this page. Passing `null`
              threw that away and blanked the cell. */}
          <span className="text-sm text-slate-700">{orgLabel(connection, connectionId)}</span>
        </div>
        <div>
          <span className={FIELD_LABEL}>Owner</span>
          <span className="text-sm text-slate-700">{repo.created_by || NO_VALUE}</span>
        </div>

        {/* Delivery and runtime, side by side and NEVER merged. Uncaptioned, which is why the
            two machines' `failed` labels differ ("Delivery failed" / "Runtime failed"). */}
        <div>
          <span className={FIELD_LABEL}>Status</span>
          <span className="inline-flex items-center gap-2">
            {/* THE LIMIT THAT REMAINS: the derived word reads the deployment history, and the
                history is refreshed on page load and on each SETTLE (see the poller) — not
                continuously. So a build that starts while this page sits idle between settles is
                seen at the next load. The header and the strip read the same snapshot, so they can
                never disagree with each other; both are as fresh as that snapshot. */}
            {/* The TINT stays a pure function of the narrowed record status: it is the RECORD's
                claim, and the derived word does not change what the record says. In the state the
                contradiction was observed in (`ready`) that lands on the resting amber, which reads
                correctly under the spinner. On a REDEPLOY (`deployed`) it lands on emerald while the
                pill spins — honest about the image that is still serving, but the tint is the one
                part of this pill the derivation does not reach. Moving it needs the badge key to
                come out of the same decision rather than a literal chosen here, which is the shape
                a `building` WRITER would give it — noted, not smuggled in as a render branch. */}
            <span className={`${PILL} ${OPS_BADGE[CICD_BADGE_KEY[cicd]]}`}>
              {delivery.inFlight ? (
                <svg
                  className="w-2.5 h-2.5 animate-spin"
                  viewBox="0 0 24 24"
                  fill="none"
                  aria-hidden="true"
                >
                  <circle
                    cx="12"
                    cy="12"
                    r="10"
                    stroke="currentColor"
                    strokeWidth="4"
                    strokeDasharray="40 60"
                  />
                </svg>
              ) : (
                <span aria-hidden="true">●</span>
              )}
              {delivery.label}
            </span>
            {/* The runtime pill lives HERE, agent-level, and carries NO stage caption — the
                probe cannot attribute its reading to a stage. `title` says so out loud so the
                scope is available to a reader hovering the pill, not only to whoever reads the
                source. */}
            {/* AND SINCE E29/T11 THE `title` ALSO CARRIES THE PROBE'S OWN HINT (OB-15). The
                backend has always returned `RuntimeStatus.detail` and nothing rendered it, which
                went from harmless to costly at T10: `unknown` now means either "the platform
                answered with a state this build does not recognize" (the probe SUCCEEDED) or "the
                probe could not be made — bad credentials, a throttle, or no Databricks reader
                configured" (nothing established at all). Same pill, opposite instructions to an
                operator. `runtimeStatusTitle` composes the two claims and returns null when there
                is nothing to say, so the attribute disappears rather than becoming an empty
                tooltip. */}
            <span
              className={`${PILL} ${OPS_BADGE[RUNTIME_BADGE_KEY[runtimeKey]]}`}
              title={runtimeStatusTitle(runtime, scope.note) ?? undefined}
            >
              <span aria-hidden="true">●</span>
              {RUNTIME_LABEL[runtimeKey]}
            </span>
          </span>
        </div>

        {/* The lifecycle pill: READ-ONLY (D11). It states that an approval is outstanding and
            offers none — the approve verb lives on the governance surface this links to. */}
        <div>
          <span className={FIELD_LABEL}>Governance</span>
          {agent ? (
            <span className={`${PILL} ${lifecycleBadge(agent.lifecycle_state).cls}`}>
              {lifecycleBadge(agent.lifecycle_state).label}
            </span>
          ) : (
            <span className={`${PILL} ${OPS_BADGE.unknown}`}>Not reported</span>
          )}
        </div>
      </div>

      {/* Ungoverned in prod (D11): production ESTABLISHED as serving AND the agent still
          `proposed` — an agent serving production that governance never approved. STATED, with a
          link to where the approval happens; there is deliberately no approve button here. */}
      {prodGovernance === 'ungoverned' && (
        <div
          role="alert"
          className="mb-6 rounded-xl border border-amber-200/70 bg-amber-50/70 px-4 py-3"
        >
          <h3 className="text-sm font-semibold text-amber-800">
            Serving production without governance approval
          </h3>
          <p className="text-sm text-amber-800/90 mt-1">
            This repository has deployed to production while its agent is still recorded as
            proposed. Approval is requested and granted on the{' '}
            <Link to={`/agents/${repo.agent_id}`} className="underline font-medium">
              agent’s governance record
            </Link>
            .
          </p>
        </div>
      )}

      {/* A governance question we could not ANSWER. Not the warning above — it accuses nobody —
          but not silence either: silence here is the absent alarm that reads as "checked, nothing
          wrong". Slate rather than amber, because this is a limit of our read and not a finding
          about the repository.

          TWO independent uncertainties reach it (E28A/T4), and the copy must not assert either
          away: the governance record could not be read, OR whether production is serving is
          unestablished. The old body claimed "This repository has deployed to production" — a
          definite statement the second case has not earned, on the banner whose whole purpose is
          to stop making unearned statements. */}
      {prodGovernance === 'unknown' && (
        <div
          role="status"
          className="mb-6 rounded-xl border border-slate-200/70 bg-slate-50/70 px-4 py-3"
        >
          <h3 className="text-sm font-semibold text-slate-700">
            Production governance could not be confirmed
          </h3>
          <p className="text-sm text-slate-600 mt-1">
            Either this repository’s governance record could not be read, or what production is
            serving is not established from the records available here — so this is neither a report
            that something unapproved is running nor confirmation that nothing is. Check the{' '}
            <Link to={`/agents/${repo.agent_id}`} className="underline font-medium">
              agent’s governance record
            </Link>{' '}
            and the Deployments tab directly.
          </p>
        </div>
      )}

      {/* THE CONSENT MOMENT (E28C/T7, D-C4d) — the product's one promote dialog, at the product's
          one promote entry point. Rendered ABOVE the mapped error so a 409/502 reads beneath the
          artifact it refers to, and above the environment strip because what is about to change is
          more urgent than what is currently running.

          Every input is a resolved value: `prodCandidateView` shapes the provenance and
          `promotionArtifact` resolves the marker's text, tooltip AND tone. This page picks none of
          them — see `PromoteConfirm.tsx` for why the marker must arrive with no branch left in it. */}
      {/* GATED ON `may.promote` TOO, not only on the reveal state. The reveal is a user action and
          the gate is a fact about the record, and the two can come apart while the dialog is open:
          a refetch — or the same candidate being promoted from another session — clears
          `prod_candidate_status`, at which point this dialog would still be offering an approval for
          a candidate that no longer exists, and its button would 409. Conditional render rather than
          `disabled`, which is this epic's frontend constraint and the right shape here: a vanished
          candidate is not a temporarily-unavailable action. */}
      {may.promote && promoteConfirm && (
        <div className="mb-4">
          <PromoteConfirm
            repoName={repo.name}
            candidate={prodCandidateView(repo)}
            artifact={promotionArtifact(repo)}
            devImageTag={repo.last_dev_image_tag ?? null}
            pending={promoting}
            onConfirm={() => void handlePromote()}
            onCancel={closePromoteConfirm}
            confirmRef={promoteConfirmRef}
          />
        </div>
      )}

      {promoteError && (
        <p className="text-sm text-red-600 mb-4" role="alert">
          {promoteError}
        </p>
      )}
      {actionError && (
        <p className="text-sm text-red-600 mb-4" role="alert">
          {actionError}
        </p>
      )}
      {promoteNotice && (
        <p className="text-sm text-emerald-700 mb-4" role="status">
          {promoteNotice}
        </p>
      )}

      {/* The environment strip — one row per stage the API returns (C5). Above the tabs
          because "what is running where" is the question this page exists to answer, and a
          global environment SWITCHER is explicitly rejected: every stage shows at once. */}
      <EnvironmentStrip
        source={{ stages: tenantAccount?.stages, stagesUnknown, deployments, historyError }}
        // PER-STAGE readings, not the one agent-scoped scope object (E28C/T7). The strip decides
        // nothing about them: `stageRuntimeCell` resolves each cell, and a stage with no entry
        // renders as absence. The agent-level `scope` is still what the HEADER pill above uses —
        // that reading's scope is truthfully the agent, and this map's is not a substitute for it.
        runtimeByStage={runtimeByStage}
        loading={historyLoading}
      />

      {/* The tab strip — the extracted accessible bar, not a fourth hand-rolled copy. Only the
          tabs with a BODY are passed, and Pull requests only when this ORG can serve them (A3) —
          see the two filters behind `tabs`. */}
      <TabStrip
        tabs={tabs}
        activeKey={selectedKey}
        onSelect={setActiveKey}
        ariaLabel="Repository sections"
        tabId={tabId}
        tabPanelId={tabPanelId}
      />

      {selectedKey === 'overview' && (
        <div {...panelProps('overview')}>
          <OverviewTab repo={repo} agent={agent} />
        </div>
      )}
      {/* The per-stage delivery history and the OWNER-gated rollback. It receives the SAME
          `deployments` / `historyError` pair the environment strip reads, so the two surfaces can
          never disagree about what shipped — and it consumes `collapseByBuild` through its own
          companion rather than re-deriving the started-row rule. */}
      {selectedKey === 'deployments' && (
        <div {...panelProps('deployments')}>
          <DeploymentsTab
            repo={repo}
            deployments={deployments}
            historyError={historyError}
            loading={historyLoading}
            heldRole={heldRole}
            roleLevel={user?.role_level ?? 0}
            onChanged={(updated: Repository) => {
              setRepo(updated);
              refetch();
            }}
          />
        </div>
      )}
      {/* Pull requests, acted on as the caller's OWN linked GitHub account — never as AGP's App,
          which is what keeps the self-approval refusal meaningful (D15). The rows come from the
          page's own read because that read is also the capability probe deciding whether this tab
          exists at all; the tab reports a completed write back so the page re-reads, rather than
          keeping a second copy of the list that could disagree with the strip.
          `onChanged` and `onRefresh` are the SAME `refetch` under two names on purpose (T9): one
          means "this tab wrote something", the other "the operator asked for a fresh read". A
          refresh button wired to `onChanged` would work and would tell a future reader that
          refreshing mutates. */}
      {selectedKey === 'pull-requests' && (
        <div {...panelProps('pull-requests')}>
          <PullRequestsTab
            repoId={repo.id}
            pullRequests={pullRequests}
            heldRole={heldRole}
            roleLevel={user?.role_level ?? 0}
            ungoverned={ungoverned}
            loading={loading}
            onChanged={refetch}
            onRefresh={refetch}
          />
        </div>
      )}
      {selectedKey === 'access' && (
        <div {...panelProps('access')}>
          <AccessTab
            agentId={repo.agent_id}
            inbound={inboundGrants}
            outbound={outboundMcps}
            onNavigateAway={() => navigate(`/agents/${repo.agent_id}`)}
          />
        </div>
      )}
      {selectedKey === 'observability' && (
        <div {...panelProps('observability')}>
          <ObservabilityTab repo={repo} />
        </div>
      )}
      {selectedKey === 'resources' && (
        <div {...panelProps('resources')}>
          <ResourcesTab repo={repo} />
        </div>
      )}

      {/* The EXISTING teardown modal (E23) — same component the project's repo table opens, so
          the cascade, its pre-check and its per-item outcomes have ONE implementation. On
          success the repo no longer exists, so this NAVIGATES AWAY rather than refetching: a
          refetch would land on this page's not-found state, which is a worse way to say
          "deleted" than the list the operator came from. */}
      {deleteOpen && (
        <DeleteRepositoryModal
          open
          repo={repo}
          projectId={repo.project_id}
          onClose={() => setDeleteOpen(false)}
          onDeleted={() => {
            setDeleteOpen(false);
            navigate('/ops/repositories');
          }}
        />
      )}
    </OpsPage>
  );
}

// ---------------------------------------------------------------------------
// Overview — the record, finally rendered, plus the FREED materialize timeline.
//
// The timeline was visible only inside the creation modal, so it disappeared the moment the
// operator closed it — taking with it the only explanation of how a half-materialized repo got that
// way. It is permanently viewable here.
//
// `s.label` IS RENDERED VERBATIM, AND THAT IS NOW LOAD-BEARING RATHER THAN MERELY TIDY. This comment
// used to declare the step count a frozen backend↔frontend contract; E28B/T3 unfroze it. Materialize
// is five steps now, and the ones that went away were DELETED rather than renamed — AGP pushes the
// whole template in one commit instead of making six writes into a repository GitHub was
// concurrently writing to, so a branch cut, two GitHub Environments and their variables stopped
// existing. `models/repository.py`'s `MATERIALIZE_STEPS` is the authority; no count is restated here.
//
// Historical and in-flight records still carry the SUPERSEDED keys, and they must keep rendering
// their own stored labels. So: render `label`, use `key` only as a React list key, and DO NOT add a
// key→label map anywhere — a key this build has never heard of has to degrade to the label the
// record stored, not to a blank row or a crash. (The original reason for verbatim labels still holds
// too: re-deriving them is how the old "Create dev environment" came to lie for a tenant that had no
// dev stage.) Nothing here counts the steps either; the header's verdict takes `repo.steps.length`.
// ---------------------------------------------------------------------------
function OverviewTab({ repo, agent }: { repo: Repository; agent: Agent | null }): JSX.Element {
  // The header word, decided in the `.ts`. Three-way, because "the run stopped" and "the run
  // succeeded" are different facts and this header used to render the first as the second — a
  // FAILED materialize read "Complete", directly above its own rose failed step.
  const materialize = materializeSummary(nextBadgeFromSteps(repo.steps), repo.steps.length);
  // WHAT WOULD AN APPROVAL APPROVE — the exact bytes, or a mutable pointer? Three-way and decided
  // in the `.ts`; see `promotionArtifact` for why the third answer has to exist.
  const artifact = promotionArtifact(repo);

  return (
    <div className="space-y-6">
      <div className={`${OPS_CARD} p-5`}>
        <h3 className="text-sm font-semibold text-slate-800 mb-4">Repository</h3>
        <dl className="grid grid-cols-2 md:grid-cols-3 gap-x-8 gap-y-4">
          <Field label="Template">{repo.template_name}</Field>
          {/* The MATERIALIZE run's own status, labelled in the `.ts` rather than rendered raw
              (E28A, #12's frontend half). Raw was a bare lowercase wire value beside two
              sentence-case pills — and it shares the word `ready` with both of them while meaning
              something else by it, which is the collision `opsStatus.ts` already refused once when
              it labelled delivery's `ready` "Not built yet". NOTHING WRITES `ready` TO THIS FIELD
              YET — the record's only writers put `provisioning` and `failed` in it — so that row of
              the table is deliberately pre-placed rather than currently reachable, and the mapping
              is ready for the writer a later task adds. An unrecognized value renders verbatim, so
              a writer that lands a different word shows it rather than being relabelled. */}
          <Field label="Record status">{recordStatusLabel(repo.status) ?? NO_VALUE}</Field>
          <Field label="Created by">{repo.created_by || NO_VALUE}</Field>
          <Field label="Created">{repo.created_at.slice(0, 10)}</Field>
          <Field label="Purpose">{agent?.purpose || NO_VALUE}</Field>
          {/* FRAMEWORK, not model. `framework` is the scaffold the repo was generated from
              (fixed at creation), and the governance agent page labels the same field
              "Framework". The real `model_id` exists on the backend record but not on the TS
              `Agent` interface, which this task may not edit — recorded as a seam. */}
          <Field label="Framework">{agent?.framework || NO_VALUE}</Field>
          <Field label="Region">{agent?.region || NO_VALUE}</Field>
          <Field label="Business unit">{agent?.business_unit || NO_VALUE}</Field>
          <Field label="Data classification">{agent?.data_classification || NO_VALUE}</Field>
          <Field label="Repository">
            {repo.repo_url ? (
              <a
                href={repo.repo_url}
                target="_blank"
                rel="noreferrer"
                className="text-emerald-700 hover:text-emerald-800 transition-colors break-all"
              >
                {repo.repo_url}
              </a>
            ) : (
              NO_VALUE
            )}
          </Field>
          <Field label="Endpoint">
            <span className="font-mono text-xs break-all">{agent?.endpoint_url || NO_VALUE}</span>
          </Field>
          {/* The pending prod candidate's timestamp — a field that existed on the wire and had
              no surface anywhere. Stated, never acted on: approving it is the Promote action in
              the header, which is role-gated. */}
          <Field label="Prod candidate">
            {repo.prod_candidate_at ? repo.prod_candidate_at.slice(0, 10) : NO_VALUE}
          </Field>
          {/* WHAT AN APPROVAL WOULD APPROVE — beside the candidate's date, because they describe one
              thing and an owner reading either needs the other. Rendered only when a candidate is
              really pending (`kind !== 'none'`), so it does not sit as an em dash on every repo
              between merges.

              The DIGEST is the whole point of the field: it names the bytes, so it is the primary
              line, in the same `font-mono` weight the page gives an endpoint and the promote confirm
              gives a commit. The tag stays beneath it because it is the label every other surface
              renders and a rollback still validates against it — the digest replaces neither. */}
          {/* `marker !== null` rather than `kind !== 'none'`: they are the same condition by
              construction (a test pins that), and narrowing on the thing actually rendered is what
              lets `tsc` prove the access below is safe without an assertion. */}
          {artifact.marker !== null && (
            <Field label="Approvable artifact">
              <span className="flex flex-col gap-1">
                {/* ONE VALUE, NO BRANCH — and that is the fix, not a style preference. This was
                    `artifact.digest !== null ? <digest/> : <caution/>`, and a reviewer made the
                    caution arm unreachable with the whole suite still green: there is no jsdom
                    here, so every guard over a `.tsx` is a guard over TEXT, and text cannot tell a
                    live branch from a dead one. The marker now arrives already resolved from
                    `promotionArtifact`, so a dead render branch is unrepresentable rather than
                    merely detectable. Do not reintroduce a condition here.

                    `title` carries the caution's explanation and remedy rather than a fourth
                    paragraph on a dense field grid — the idiom the runtime pill already uses for
                    its scope note. `role="note"` is deliberately absent: this is a fact in a
                    definition list, not an alert, and it must not interrupt. */}
                <span
                  className={ARTIFACT_TONE_CLS[artifact.marker.tone]}
                  title={artifact.marker.note ?? undefined}
                >
                  {artifact.marker.text}
                </span>
                {artifact.imageTag !== null && (
                  <span className="font-mono text-[11px] text-slate-400 break-all">
                    {artifact.imageTag}
                  </span>
                )}
              </span>
            </Field>
          )}
        </dl>
      </div>

      <div className={`${OPS_CARD} p-5`}>
        <div className="flex items-center justify-between gap-3 mb-4">
          {/* SCOPED, and the scope is the point (E28A/T11, finding #13). The verdict beside this
              heading read "Complete" on a page whose delivery pill read failed. Both were true —
              materialize genuinely completed and the production deploy is a different machine — but
              a bare noun beside a bare verdict reads as a claim about the whole repository. Naming
              the RUN means the verdict cannot be read past its own subject. Copy only: the badge
              logic and its three-way state are untouched. */}
          <h3 className="text-sm font-semibold text-slate-800">Materialization run</h3>
          {/* Only the ONE state that earned it is tinted as success; everything else stays the
              neutral slate this card already used. A failure is not tinted rose here because the
              failed STEP below already carries that tint with the error beside it — a second rose
              node in the header would read as a second failure. */}
          <span
            className={`text-[11px] ${
              materialize.succeeded ? 'text-emerald-700 font-medium' : 'text-slate-400'
            }`}
          >
            {materialize.label}
          </span>
        </div>
        <ol className="relative">
          {repo.steps.map((s) => (
            <li key={s.key} className="relative flex gap-3 pb-3 last:pb-0">
              <span
                aria-hidden="true"
                className={`mt-1.5 shrink-0 h-2 w-2 rounded-full ${
                  s.status === 'done'
                    ? 'bg-emerald-500'
                    : s.status === 'failed'
                      ? 'bg-rose-500'
                      : s.status === 'running'
                        ? 'bg-amber-500'
                        : 'bg-slate-300'
                }`}
              />
              <div className="min-w-0 flex-1">
                <div className="flex items-center justify-between gap-2">
                  {/* Rendered VERBATIM — see the comment above this component. */}
                  <span
                    className={`text-sm truncate ${
                      s.status === 'failed'
                        ? 'text-rose-700 font-medium'
                        : s.status === 'done'
                          ? 'text-slate-700'
                          : 'text-slate-400'
                    }`}
                  >
                    {s.label}
                  </span>
                  {/* The WORDING is `stepStatusText`, shared with the project page's timeline.
                      Rendering `s.status` raw made `pending` read "pending" here and "Waiting"
                      there — one repo's step, two spellings. Re-deriving the visual density of
                      a timeline is a design choice; re-deriving its vocabulary is drift. */}
                  <span className="shrink-0 text-[11px] capitalize text-slate-400">
                    {stepStatusText(s.status)}
                  </span>
                </div>
                {s.status === 'failed' && s.error && (
                  <p className="mt-1 rounded-md bg-rose-50 border border-rose-200/70 px-2 py-1 text-[11px] text-rose-700 break-words">
                    {s.error}
                  </p>
                )}
              </div>
            </li>
          ))}
        </ol>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Access — COUNTS and a deep link out. No roster, no verbs.
//
// Inbound invoke grants and outbound MCP dependencies are governance facts, and this is an
// Operations surface: it reports the SHAPE of the agent's access (how many principals can call
// it, how many tools it reaches) so an operator can see whether an agent is wired at all, and
// sends anyone who needs the detail — or needs to CHANGE it — to the governance record. Adding
// a principal list here would put a grant/revoke affordance one step away, which is the
// shadow-governance failure mode.
//
// A FAILED READ IS `null` AND RENDERS AS AN EM DASH, never as `0`. "No inbound grants" and "we
// could not ask" are different facts and only one of them is reassuring — the same rule as
// D13's "not instrumented ≠ zero".
// ---------------------------------------------------------------------------
function AccessTab({
  agentId,
  inbound,
  outbound,
  onNavigateAway,
}: {
  agentId: string;
  inbound: MaybeCount;
  outbound: MaybeCount;
  onNavigateAway: () => void;
}): JSX.Element {
  const rows = useMemo(
    () => [
      {
        label: 'Inbound invoke grants',
        hint: 'Principals that may invoke this agent, as Entra app-role assignments.',
        count: inbound,
      },
      {
        label: 'Outbound MCP dependencies',
        hint: 'MCP servers this agent is granted to reach.',
        count: outbound,
      },
    ],
    [inbound, outbound],
  );

  return (
    <div className={`${OPS_CARD} p-5`}>
      <div className="flex items-start justify-between gap-4 mb-4">
        <div>
          <h3 className="text-sm font-semibold text-slate-800">Access</h3>
          <p className="text-sm text-slate-500 mt-1 max-w-2xl">
            A read-only mirror of the agent’s access shape. Who exactly holds a grant, and every
            change to one, lives on the governance record.
          </p>
        </div>
        {/* A button rather than a bare span, so it is keyboard-reachable — this page does not
            inherit the clickable-non-interactive-element defect (finding M-e). */}
        <button
          type="button"
          onClick={onNavigateAway}
          className="shrink-0 px-3 py-1.5 rounded-lg bg-white border border-slate-300 text-slate-700 text-xs font-medium hover:bg-slate-50 transition-colors"
        >
          Open governance record
        </button>
      </div>
      <dl className="grid grid-cols-1 sm:grid-cols-2 gap-4">
        {rows.map((row) => (
          <div key={row.label} className="rounded-lg border border-emerald-100/70 px-4 py-3">
            <dt className={FIELD_LABEL}>{row.label}</dt>
            <dd className="text-2xl font-semibold text-slate-900 tabular-nums">
              {row.count === null ? (
                <span
                  className="text-slate-400 text-base font-normal"
                  title="This count could not be read — it is not known to be zero."
                >
                  {NO_VALUE}
                </span>
              ) : (
                row.count
              )}
            </dd>
            <p className="text-[11px] text-slate-400 mt-1">{row.hint}</p>
          </div>
        ))}
      </dl>
      <p className="text-[11px] text-slate-400 mt-4">
        Agent <span className="font-mono">{agentId}</span>
      </p>
    </div>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }): JSX.Element {
  return (
    <div>
      <dt className={FIELD_LABEL}>{label}</dt>
      <dd className="text-sm text-slate-700">{children}</dd>
    </div>
  );
}
