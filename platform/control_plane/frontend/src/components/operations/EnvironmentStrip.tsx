// EnvironmentStrip — one row per stage the API returns (E28/T11, contract C5).
//
// ---------------------------------------------------------------------------
// WHAT THIS IS FOR
//
// The one place an operator can see EVERY environment at once: what each stage is running, when
// it landed, who put it there, and whether it is behind. A global environment SWITCHER is
// explicitly rejected by the design — hiding the comparison behind a dropdown is the incident
// class ("I thought I was in dev"), and the comparison is the entire value of this strip.
//
// ---------------------------------------------------------------------------
// NO HARDCODED STAGES, ANYWHERE
//
// Stages come from the tenant record, which is `Record<string, TenantStageConfig>`: the backend
// requires at least ONE stage, not a dev+prod pair, so a tenant may legitimately be `uat`-only.
// A stage the API does not return DOES NOT RENDER, and zero stages renders an honest empty
// state rather than a fabricated dev row. There is no stage literal in this file and a test
// asserts there is none.
//
// ---------------------------------------------------------------------------
// THE RUNTIME COLUMN NOW SHOWS A PILL — BUT ONLY WHERE THE READING IS ATTRIBUTABLE
//
// This is the column's second design, and the first one was right for the data it had. C5 gives
// the strip a per-stage `Runtime status` column; when T11 built it the platform had exactly ONE
// runtime answer per agent (a single `agent_arn` that whichever stage deployed last overwrote),
// so the probe could not attribute what it read to a stage. Rendering that one answer in every
// row would have claimed each stage was probed — three rows reading "Ready" look like three
// probes — so the column stated the limitation instead and the reading was rendered ONCE,
// agent-level, in the page header.
//
// Since E28A an agent owns one runtime PER STAGE and `GET /agents/{id}/runtime?stage=` probes a
// named one (a stage the agent owns no runtime for answers not-deployed WITHOUT an AWS call).
// E28C/T7 finally asks. So the column fills in where the evidence is real, and the old note
// remains for where it is not — `runtimeScope` was written forward-compatible for precisely this
// day and is UNCHANGED.
//
// THE PILL IS STILL NOT THIS FILE'S DECISION. `stageRuntimeCell` decides, per stage, between a
// pill, the note, and absence — including the guard that one stage's reading may never caption
// another's row. This file cannot construct a pill for a cell the model did not authorise,
// because the status it would need arrives only on the `pill` answer (`null` otherwise). The
// mistake stays unreachable rather than discouraged (finding M-d); what changed is that the
// honest answer is now sometimes a pill.
//
// This file DECIDES nothing: every judgement (which stages, what each is running, drift, actor
// currency, and now the runtime cell) is `repositoryDetailTabs.ts`, where a test can reach it.
// House style: emerald-on-glass Ops tokens, Tailwind v4 utility strings, 2-space indent.

import type { JSX } from 'react';

import type { RuntimeStatus } from '../../api/client';
import { NO_VALUE } from './opsLabels';
import { RUNTIME_BADGE_KEY, RUNTIME_LABEL } from './opsStatus';
import { OPS_BADGE, OPS_CARD, OPS_TABLE_DIVIDE, OPS_TABLE_HEAD } from './opsUi';
import {
  ACTOR_KIND_TITLE,
  ENVIRONMENT_EMPTY_COPY,
  environmentRows,
  environmentStripState,
  runtimeStatusTitle,
  stageRuntimeCell,
  type EnvironmentRow,
  type EnvironmentRowSource,
} from './repositoryDetailTabs';

export interface EnvironmentStripProps {
  /** The tenant's stage map + the agent's deployment history. */
  source: EnvironmentRowSource;
  /**
   * ONE RUNTIME READING PER STAGE, keyed by the tenant's own stage name (E28C/T7).
   *
   * Replaces the single agent-scoped `RuntimeScope` this took while the platform had one runtime
   * answer per agent. A stage absent from the map — or present and `undefined` — is a stage whose
   * probe failed or was never made, which `stageRuntimeCell` renders as absence rather than as a
   * status. The AGENT-level reading is deliberately NOT accepted here: it belongs to the page
   * header's pill, and passing it per-stage is the fabrication this strip exists to prevent.
   */
  runtimeByStage: Record<string, RuntimeStatus | undefined>;
  /** True while the deployment history is still loading — distinct from "no history". */
  loading?: boolean;
}

const COLUMNS = 6;

// Sentence-case labels arrive correctly cased from the status tables, so no `capitalize`. Declared
// here rather than imported because `opsUi.ts` exports no pill shape — `RepoRow.tsx` and
// `RepositoryDetail.tsx` each declare the same string for the same reason, which is the house rule
// that each surface owns its class strings while the TABLES (label, tint) are shared.
const PILL = 'inline-flex items-center gap-1.5 text-[11px] font-semibold px-2 py-0.5 rounded-full';

export default function EnvironmentStrip({
  source,
  runtimeByStage,
  loading = false,
}: EnvironmentStripProps): JSX.Element {
  const rows = environmentRows(source);
  // WHY the strip is empty, when it is — decided in the `.ts`. Two distinct answers, because "this
  // tenant has no environments" is a claim about the tenant and "we could not resolve the tenant"
  // is not, and the strip used to render the second as the first.
  const state = environmentStripState(source);

  return (
    <div className={`${OPS_CARD} overflow-hidden mb-6`}>
      <table className="min-w-full text-sm">
        <thead className={OPS_TABLE_HEAD}>
          <tr>
            <th className="text-left px-4 py-2 font-medium">Stage</th>
            <th className="text-left px-4 py-2 font-medium">Deployed version</th>
            <th className="text-left px-4 py-2 font-medium">Deployed at</th>
            <th className="text-left px-4 py-2 font-medium">Deployed by</th>
            <th className="text-left px-4 py-2 font-medium">Runtime status</th>
            <th className="text-left px-4 py-2 font-medium">Drift</th>
          </tr>
        </thead>
        <tbody className={OPS_TABLE_DIVIDE}>
          {loading && (
            <tr>
              <td colSpan={COLUMNS} className="px-4 py-6 text-center text-slate-400 text-xs">
                Loading deployment history…
              </td>
            </tr>
          )}
          {/* Zero stages is an HONEST empty state — and there are TWO of them, because "the tenant
              carries none" and "the tenant did not resolve" are different facts. The copy for each
              comes from the table so neither can drift back into the other's wording; inventing a
              row here is the thing C5 forbids, and inventing a CONFIGURATION for an unresolved
              tenant is the same error one level up. */}
          {!loading && state !== 'rows' && (
            <tr>
              <td
                colSpan={COLUMNS}
                className={`px-4 py-6 text-center text-xs ${
                  state === 'stages-unknown' ? 'text-slate-500' : 'text-slate-400'
                }`}
              >
                {ENVIRONMENT_EMPTY_COPY[state]}
              </td>
            </tr>
          )}
          {!loading &&
            rows.map((row) => (
              <StageRow key={row.stage} row={row} runtime={runtimeByStage[row.stage]} />
            ))}
        </tbody>
      </table>
    </div>
  );
}

function StageRow({
  row,
  runtime,
}: {
  row: EnvironmentRow;
  /** THIS stage's reading, or `undefined` when its probe failed / was never made. */
  runtime: RuntimeStatus | undefined;
}): JSX.Element {
  // The cell's whole decision, made where a test can reach it. The stage name is passed so the
  // model can refuse a reading that belongs to a DIFFERENT stage — this file never compares them.
  const cell = stageRuntimeCell(row.stage, runtime);
  return (
    <tr>
      {/* 1 — the stage's own name, verbatim. Free-form (D8): it may be `uat`, a region, or
          anything else the tenant configured. */}
      <td className="px-4 py-3">
        <span className="font-medium text-slate-900">{row.stage}</span>
        {row.region && (
          <span className="block text-[11px] font-normal text-slate-400">{row.region}</span>
        )}
      </td>

      {/* 2 — the image this stage is RUNNING (its newest SUCCEEDED attempt — a failed or
          still-running one is not what is serving), with the short source sha beside it. The sha
          comes off the attempt's `started` row, because the succeeded row carries none (C1).

          A FAILED HISTORY READ IS NOT "Never deployed". "This stage has never shipped" is a
          definite claim about production and "we could not ask" is not, so an unreadable history
          says so — the same rule as an absent runtime reading `unknown` rather than `ready`. */}
      <td className="px-4 py-3">
        {row.historyUnknown ? (
          <span
            className="text-slate-400 text-xs"
            title="The deployment history could not be read, so what this stage is running is unknown — it is not known to have never deployed."
          >
            {NO_VALUE}
          </span>
        ) : row.imageTag ? (
          <>
            <span className="font-mono text-xs text-slate-600">{row.imageTag}</span>
            {row.shortSha && (
              <span className="block text-[11px] font-mono text-slate-400">{row.shortSha}</span>
            )}
          </>
        ) : (
          <span className="text-slate-400 text-xs">Never deployed</span>
        )}
        {/* An attempt is running RIGHT NOW. Reported separately from the deployed version,
            because the version above is still what is serving until it finishes. */}
        {row.inFlight && (
          <span className="block text-[11px] text-amber-700">Deployment in progress</span>
        )}
      </td>

      {/* 3 — when. Date only; the full timestamp is in the title. */}
      <td className="px-4 py-3 text-slate-600 text-xs tabular-nums">
        {row.deployedAt ? (
          <span title={row.deployedAt}>{row.deployedAt.slice(0, 10)}</span>
        ) : (
          <span className="text-slate-400">{NO_VALUE}</span>
        )}
      </td>

      {/* 4 — who, IN ITS OWN CURRENCY. A provider login (`@handle`) and a platform object id are
          never rendered as one thing (E27A §6). The actor comes off the attempt's `started` row
          — the succeeded row carries none by design (C1) — and a build with no started partner
          has no actor at all, which reads as absent rather than as "unknown user". The wording is
          "deployed by", matching the relabel T13 applies elsewhere: "promoted by" becomes false
          after a rollback.

          Both the currency LABELS come from `ACTOR_KIND_TITLE`, a `Record` over ALL THREE kinds,
          and neither is written here. `unknown` exists precisely so an unlabelled actor is not
          guessed into a currency, and a two-way ternary in this file put it under the provider's
          label — asserting an identity that was never established. The guard asserts that
          neither label string appears in this file at all, so a re-derived ternary cannot come
          back; it reads raw source and does not skip comments, which is why this one describes
          the labels rather than quoting them. */}
      <td className="px-4 py-3 text-xs">
        {row.actor ? (
          <span
            className={row.actor.kind === 'entra' ? 'font-mono text-slate-500' : 'text-slate-600'}
            title={ACTOR_KIND_TITLE[row.actor.kind]}
          >
            {row.actor.display}
          </span>
        ) : (
          <span className="text-slate-400">{NO_VALUE}</span>
        )}
      </td>

      {/* 5 — Runtime status. THIS stage's own reading, when the platform can attribute one to
          it (E28C/T7); the not-attributable note when it cannot; an em dash when there is no
          reading at all. The three-way choice was made by `stageRuntimeCell` — the previous
          version of this comment predicted the pill would belong here "the day the envelope
          becomes per-stage", and that day is E28A.

          THE THREE ARMS ARE THE MODEL'S, NOT THIS FILE'S. The status a pill needs arrives only
          on the `pill` answer and is `null` on the other two, so a pill cannot be rendered for a
          cell the model did not authorise — including, specifically, one whose reading describes
          a different stage. Label and tint come from the shared `RUNTIME_LABEL` /
          `RUNTIME_BADGE_KEY` tables, so this column and the header pill cannot disagree about
          what a status is called or coloured; there is no table and no default in this file.

          The note keeps its quiet 11px slate treatment: it is a statement about what the
          platform can know, not a status, and dressing it as one would be its own small claim. */}
      <td className="px-4 py-3">
        {cell.kind === 'pill' && cell.status !== null ? (
          <span
            className={`${PILL} ${OPS_BADGE[RUNTIME_BADGE_KEY[cell.status]]}`}
            // THE PROBE'S OWN HINT RIDES ALONG (E29/T11, OB-15). `RuntimeStatus.detail` was
            // returned by the backend and rendered by nobody — which stopped being harmless at
            // T10, when `unknown` came to mean either "the platform answered with a state this
            // build does not recognize" or "the probe never completed". Identical pill, opposite
            // instruction to an operator; the hint is what separates them. Composed by
            // `runtimeStatusTitle`, which returns null when there is nothing to add, so this
            // stage's own attribution sentence is not padded with an empty clause.
            title={
              runtimeStatusTitle(runtime, `Probed for this stage — ${RUNTIME_LABEL[cell.status]}`) ??
              undefined
            }
          >
            <span aria-hidden="true">●</span>
            {RUNTIME_LABEL[cell.status]}
          </span>
        ) : (
          // AND THE HINT IS DELIBERATELY *NOT* ADDED TO THIS ARM. On the `note` arm the reading
          // exists but describes a DIFFERENT stage, so its detail explains that other stage's
          // probe — attaching it here would caption this row with another stage's evidence, which
          // is the whole error `stageRuntimeCell` exists to make unreachable. On the `absent` arm
          // there is no reading and so no detail. Only a cell authorised to make a claim about
          // this stage gets the hint about it.
          <span className="text-[11px] text-slate-400">{cell.note ?? NO_VALUE}</span>
        )}
      </td>

      {/* 6 — Drift. Naming what this stage is behind, because "behind" without the newer tag is
          not actionable. A never-deployed stage is not drifted — it has never shipped. */}
      <td className="px-4 py-3">
        {row.drift ? (
          <span className="text-[11px] text-amber-700">
            behind <span className="font-mono">{row.drift.behindTag}</span>
          </span>
        ) : (
          <span className="text-slate-400 text-xs">{NO_VALUE}</span>
        )}
      </td>
    </tr>
  );
}
