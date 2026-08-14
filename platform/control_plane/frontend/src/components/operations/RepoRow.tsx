// RepoRow — THE repository row, for both call sites (E28/T10, contract C4).
//
// ---------------------------------------------------------------------------
// WHY THIS COMPONENT EXISTS
//
// Two pages render a repository: the fleet-wide list at `/ops/repositories` and a project's
// own repositories tab. They were separate implementations, and the divergence shipped: a
// module-private copy of the status→tint table was extended on one page and not the other,
// so on the fleet list a REPO LIVE IN PRODUCTION RENDERED IN THE SAME AMBER AS ONE STILL
// PROVISIONING. The tables were consolidated afterwards with a comment asking the next author
// not to re-fork them, which is an honour system — and this epic gives both pages new columns,
// i.e. exactly the pressure that comment cannot survive.
//
// So the row itself is now ONE component. `opsStatus.ts` makes the status tables
// `tsc`-exhaustive, `repoRowModel.ts` holds every judgement (vitest collects only `.ts`, so a
// decision made in this file is a decision no test can reach), and this file is wiring:
// props in, markup out, no branching that a test cannot see.
//
// ---------------------------------------------------------------------------
// TWO PILLS, NOT ONE
//
// The row shows delivery and runtime SEPARATELY, because they are separate facts from
// separate producers: `cicd_status` is written by the CodeBuild buildspec through a targeted
// DynamoDB update, while runtime health comes from a boto3 describe behind
// `GET /agents/{id}/runtime`. A single merged pill would be forced to lie, and the reachable
// direction is the harmful one — an agent whose last build deployed fine but whose runtime is
// now unreachable would read green.
//
// `runtime` is therefore OPTIONAL, and `undefined` renders as UNREACHABLE, never as ready:
// absent data is not good news. (`repoRowModel.repoAction` deliberately does not turn that
// same absence into an "action required" — see its comment; the pill makes the claim, the
// action column does not manufacture work from a missing answer.)
//
// ---------------------------------------------------------------------------
// NO GOVERNANCE VERBS
//
// The only callback is `onNavigate`. The row STATES that an approval is outstanding and never
// offers one: an approve/promote affordance on an Ops surface is the shadow-governance failure
// mode this epic forbids. Promotion stays on the governed surface that gates it.
//
// House style: emerald-on-glass Ops tokens (opsUi.ts), Tailwind v4 utility strings, 2-space
// indent — mirroring Repositories.tsx / ProjectRepositoriesTab.tsx.

import type { JSX } from 'react';

import type { Repository } from '../../api/client';
import {
  CICD_BADGE_KEY,
  CICD_LABEL,
  RUNTIME_BADGE_KEY,
  RUNTIME_LABEL,
  isCicdInFlight,
  toCicdStatus,
  toRuntimeStatus,
  type RuntimeStatus,
} from './opsStatus';
import {
  REPO_ACTION_LABEL,
  REPO_ACTION_TONE,
  ownerLabel,
  prodVersion,
  repoAction,
} from './repoRowModel';
// The runtime pill's tooltip composition (E29/T11, OB-15). Imported from the detail page's pure
// companion rather than duplicated: it is the same two-claim rule, and a second copy of "how do the
// scope note and the probe hint combine" is the fork this project has already paid for once.
import { runtimeStatusTitle } from './repositoryDetailTabs';
import { OPS_BADGE } from './opsUi';

// Sentence-case labels arrive correctly cased from the status tables, so the pill must NOT
// apply `capitalize` — that would render "Promoting To Prod…".
const PILL =
  'inline-flex items-center gap-1.5 text-[11px] font-semibold px-2 py-0.5 rounded-full';

// The Action-required cell's tint, by tone. `quiet` is deliberately unstyled text rather than
// a pill: a row with nothing to do should be visually silent, so the eye lands only on rows
// that are asking for something.
const ACTION_TONE_CLS: Record<'fault' | 'attention' | 'quiet', string> = {
  fault: `${PILL} ${OPS_BADGE.failed}`,
  attention: `${PILL} ${OPS_BADGE.warn}`,
  quiet: 'text-[11px] text-slate-400',
};

export interface RepoRowProps {
  repo: Repository;
  /** undefined ⇒ render "unknown", never "ready". */
  runtime?: RuntimeStatus;
  projectName?: string;
  /** true in the fleet table, false inside a project. */
  showProject: boolean;
  onNavigate: (repoId: string) => void;
}

/**
 * Column count, exported so both call sites' `<thead>` and their loading/empty `colSpan`
 * cannot drift from the row's actual width. C4 caps this at SIX status slots — a seventh is a
 * design change, not a row change.
 */
export const REPO_ROW_COLUMNS = 6;

export default function RepoRow({
  repo,
  runtime,
  projectName,
  showProject,
  onNavigate,
}: RepoRowProps): JSX.Element {
  // Every judgement is made in the pinned pure module; nothing below decides anything.
  const cicd = toCicdStatus(repo.cicd_status);
  const runtimeKey = toRuntimeStatus(runtime?.status);
  const action = repoAction(repo, runtimeKey);
  const prod = prodVersion(repo);
  const owner = ownerLabel(repo.created_by);
  const inFlight = isCicdInFlight(cicd);

  return (
    <tr
      onClick={() => onNavigate(repo.id)}
      // KEYBOARD-REACHABLE (M-e, E28/T13). A clickable `<tr>` carrying none of the three
      // attributes below is a control only a mouse can operate — and since T13 the row IS the
      // navigation to the detail page, so without them the whole per-repo surface is unreachable
      // by keyboard. The role names what the row now is (it does one thing when activated), the
      // tab-stop index puts it in the tab sequence, and Enter/Space are handled because a native
      // button responds to both and an element merely CLAIMING that role must honour the contract
      // it advertises — a role without the keys is the same failure, relabelled.
      //
      // A guard asserts all three are PRESENT, and it reads raw source without skipping comments,
      // so this comment must not spell any of them out: a mutation test proved that quoting one
      // here let the guard pass with the real attribute deleted. Naming them in prose is what
      // makes the guard able to fail.
      //
      // `aria-label` names the destination per row: a table of rows announcing only "button"
      // is indistinguishable out of context, exactly as the promote triggers were.
      //
      // The pattern is INHERITED from `Projects.tsx:147`, which has the same defect. That file is
      // deliberately NOT refactored here (out of scope) — this fixes the rows this task wires.
      // `TabStrip.tsx` / `ProjectDetail.tsx` are the house a11y precedents this follows.
      role="button"
      tabIndex={0}
      aria-label={`Open ${repo.name}`}
      onKeyDown={(e) => {
        // Space must be `preventDefault`ed or it scrolls the page as well as activating; Enter
        // needs it so a row inside a form cannot submit one. Any other key is left alone.
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault();
          onNavigate(repo.id);
        }
      }}
      className="hover:bg-emerald-50/40 transition-colors cursor-pointer focus:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-emerald-500/60"
    >
      {/* 1 — Repository (+ project). The repo link is NOT nested here as an <a> to the
          provider: the whole row navigates to the detail page now, and a link inside a
          clickable row gives one target two destinations. The provider link lives on the
          detail page. */}
      <td className="px-4 py-3 font-medium text-slate-900">
        {repo.name}
        {showProject && (
          <span className="block text-[11px] font-normal text-slate-400">
            {projectName ?? repo.project_id}
          </span>
        )}
      </td>

      {/* 2 — Action required. First column after the name and the table's sort key (C4), so
          a fleet scan reads failures and pending approvals before anything else. */}
      <td className="px-4 py-3">
        <span className={ACTION_TONE_CLS[REPO_ACTION_TONE[action]]}>{REPO_ACTION_LABEL[action]}</span>
      </td>

      {/* 3 — Runtime. The independent second machine. `undefined` reads "Unreachable" in
          slate — never emerald, and never rose either: an unreachable control plane is not a
          broken runtime.

          DELIBERATELY UNCAPTIONED BY STAGE (T5). The pill carries NO stage qualifier and must not
          gain one: runtime status is per-AGENT, not per-stage. The agent envelope holds a single
          `agent_arn` overwritten by whichever stage deployed last, so the probe cannot attribute
          what it read to a stage and the route reports the stage as unknown. Naming a stage here
          would attach that name to a reading not known to have come from it — see the `stage`
          field's note in `opsStatus.ts`.

          A C5 guard asserts no stage name appears anywhere in this file, which is why this comment
          does not spell the two conventional ones out: it reads raw source and does not skip
          comments, deliberately. */}
      {/* THE PROBE'S HINT ON HOVER (E29/T11, OB-15) — and NO scope note, which is the difference
          between this pill and the detail page's. This row is a fleet listing: it makes no
          per-stage claim to qualify, so there is nothing for a scope sentence to say. The `detail`
          alone is what separates "the platform answered with a state this build does not
          recognize" from "the probe never completed", two situations that print the identical
          word here. Null ⇒ no attribute, so a row with nothing to add gains no dead hover
          target. */}
      <td className="px-4 py-3">
        <span
          className={`${PILL} ${OPS_BADGE[RUNTIME_BADGE_KEY[runtimeKey]]}`}
          title={runtimeStatusTitle(runtime) ?? undefined}
        >
          <span aria-hidden="true">●</span>
          {RUNTIME_LABEL[runtimeKey]}
        </span>
      </td>

      {/* 4 — Delivery. `cicd_status`, through the one exhaustive table. */}
      <td className="px-4 py-3">
        <span className={`${PILL} ${OPS_BADGE[CICD_BADGE_KEY[cicd]]}`}>
          {inFlight ? (
            <svg className="w-2.5 h-2.5 animate-spin" viewBox="0 0 24 24" fill="none" aria-hidden="true">
              <circle cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" strokeDasharray="40 60" />
            </svg>
          ) : (
            <span aria-hidden="true">●</span>
          )}
          {CICD_LABEL[cicd]}
        </span>
      </td>

      {/* 5 — Prod version (+ drift). Names what prod is RUNNING, and when dev has moved past
          it, what it is behind — "prod is behind" without the newer tag is not actionable. */}
      <td className="px-4 py-3">
        {prod.tag ? (
          <>
            <span className="font-mono text-xs text-slate-600">{prod.tag}</span>
            {prod.drifted && prod.devTag && (
              <span className="block text-[11px] text-amber-700">dev at {prod.devTag}</span>
            )}
          </>
        ) : (
          <span className="text-slate-400">—</span>
        )}
      </td>

      {/* 6 — Owner. Initials come from the shared derivation that splits on dots, so
          `jane.doe` and `jane.smith` do not collapse into the same avatar. */}
      <td className="px-4 py-3">
        <span className="inline-flex items-center gap-2 min-w-0">
          <span
            className="shrink-0 h-6 w-6 rounded-md bg-slate-100 text-slate-500 text-[10px] font-semibold inline-flex items-center justify-center"
            aria-hidden="true"
          >
            {owner.initials}
          </span>
          <span className="text-slate-600 text-xs truncate max-w-[12rem]" title={owner.name}>
            {owner.name}
          </span>
        </span>
      </td>
    </tr>
  );
}
