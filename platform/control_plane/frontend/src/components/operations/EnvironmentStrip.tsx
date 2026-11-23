import type { JSX } from 'react';

import type { RuntimeStatus } from '../../api/client';
import { NO_VALUE } from './opsLabels';
import { shortBuildTag } from './repo-tabs/evaluationsLogic';
import { RUNTIME_BADGE_KEY, RUNTIME_LABEL } from './opsStatus';
import { OPS_BADGE, OPS_CARD, OPS_TABLE_DIVIDE, OPS_TABLE_HEAD } from './opsUi';
import { useUser } from '../../contexts/useUser';
import {
  actorText,
  actorTitle,
  ENVIRONMENT_EMPTY_COPY,
  environmentRows,
  environmentStripState,
  runtimeStatusTitle,
  stageRuntimeDisplay,
  type EnvironmentRow,
  type EnvironmentRowSource,
} from './repositoryDetailTabs';

export interface EnvironmentStripProps {
  source: EnvironmentRowSource;
  /** One reading per stage, keyed by the tenant's stage name; absent or undefined when that stage's probe failed or was never made. */
  runtimeByStage: Record<string, RuntimeStatus | undefined>;
  loading?: boolean;
}

const COLUMNS = 6;

const PILL = 'inline-flex items-center gap-1.5 text-[11px] font-semibold px-2 py-0.5 rounded-full';

export default function EnvironmentStrip({
  source,
  runtimeByStage,
  loading = false,
}: EnvironmentStripProps): JSX.Element {
  const rows = environmentRows(source);
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
  runtime: RuntimeStatus | undefined;
}): JSX.Element {
  const { user } = useUser();
  const cell = stageRuntimeDisplay(row.stage, runtime, row.imageTag);
  return (
    <tr>
      <td className="px-4 py-3">
        <span className="font-medium text-slate-900">{row.stage}</span>
        {row.region && (
          <span className="block text-[11px] font-normal text-slate-400">{row.region}</span>
        )}
      </td>

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
            <span className="font-mono text-xs text-slate-600" title={row.imageTag}>{shortBuildTag(row.imageTag)}</span>
            {row.shortSha && row.shortSha !== shortBuildTag(row.imageTag) && (
              <span className="block text-[11px] font-mono text-slate-400">{row.shortSha}</span>
            )}
          </>
        ) : (
          <span className="text-slate-400 text-xs">Never deployed</span>
        )}
        {row.inFlight && (
          <span className="block text-[11px] text-amber-700">Deployment in progress</span>
        )}
      </td>

      <td className="px-4 py-3 text-slate-600 text-xs tabular-nums">
        {row.deployedAt ? (
          <span title={row.deployedAt}>{row.deployedAt.slice(0, 10)}</span>
        ) : (
          <span className="text-slate-400">{NO_VALUE}</span>
        )}
      </td>

      <td className="px-4 py-3 text-xs">
        {row.actor ? (
          <span className="text-slate-600" title={actorTitle(row.actor)}>
            {actorText(row.actor, user)}
          </span>
        ) : (
          <span className="text-slate-400">{NO_VALUE}</span>
        )}
      </td>

      <td className="px-4 py-3">
        {cell.kind === 'pill' && cell.status !== null ? (
          <span
            className={`${PILL} ${OPS_BADGE[RUNTIME_BADGE_KEY[cell.status]]}`}
            title={
              runtimeStatusTitle(runtime, `Probed for this stage — ${RUNTIME_LABEL[cell.status]}`) ??
              undefined
            }
          >
            <span aria-hidden="true">●</span>
            {RUNTIME_LABEL[cell.status]}
          </span>
        ) : (
          <span className="text-[11px] text-slate-400">{cell.note ?? NO_VALUE}</span>
        )}
      </td>

      <td className="px-4 py-3">
        {row.drift ? (
          <span className="text-[11px] text-amber-700">
            behind <span className="font-mono" title={row.drift.behindTag}>{shortBuildTag(row.drift.behindTag)}</span>
          </span>
        ) : (
          <span className="text-slate-400 text-xs">{NO_VALUE}</span>
        )}
      </td>
    </tr>
  );
}
