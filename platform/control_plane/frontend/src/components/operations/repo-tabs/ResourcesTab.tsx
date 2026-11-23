import { type JSX } from 'react';

import { projectsApi, type Repository } from '../../../api/client';
import { useLoad } from '../../../api/useLoad';
import { OPS_BADGE, OPS_CARD, OPS_TABLE_DIVIDE, OPS_TABLE_HEAD } from '../opsUi';
import {
  INVENTORY_STATE_COPY,
  RESOURCE_STATE_BADGE_KEY,
  RESOURCE_STATE_LABEL,
  inventoryState,
  resourceInventory,
} from './resourcesTabModel';

const PILL =
  'inline-flex items-center gap-1.5 text-[11px] font-semibold px-2 py-0.5 rounded-full';

export default function ResourcesTab({ repo }: { repo: Repository }): JSX.Element {
  const { state } = useLoad(
    () => projectsApi.deletePreview(repo.project_id, repo.id),
    [repo.project_id, repo.id],
    'unknown',
  );
  const preview = state.kind === 'ready' ? state.data : null;
  const inventory = inventoryState({
    loading: state.kind === 'loading',
    failure: state.kind === 'error' ? state.failure : null,
    preview,
  });
  const copy = INVENTORY_STATE_COPY[inventory];
  const rows = resourceInventory(preview, repo.artifact_kind === 'mcp_server' ? 'mcp_server' : 'agent');

  return (
    <div className={`${OPS_CARD} overflow-hidden`}>
      <div className="px-4 py-3">
        <h3 className="text-sm font-semibold text-slate-800">Provisioned resources</h3>
        <p className="text-sm text-slate-500 mt-1 max-w-2xl">
          What this repository owns, and whether each piece still exists. This is a read-only check —
          nothing here changes anything. Tearing any of it down is the Delete action in the header,
          which runs the full cascade with its own confirmation.
        </p>
      </div>

      {copy !== null && (
        <div className="px-4 pb-5">
          <div className="rounded-lg border border-slate-200/70 bg-slate-50/60 px-4 py-3">
            <h4 className="text-sm font-semibold text-slate-800">{copy.headline}</h4>
            <p className="text-sm text-slate-500 mt-1 max-w-2xl">{copy.detail}</p>
          </div>
        </div>
      )}

      {copy === null && (
        <table className="min-w-full text-sm">
          <thead className={OPS_TABLE_HEAD}>
            <tr>
              <th className="text-left px-4 py-2 font-medium">Resource</th>
              <th className="text-left px-4 py-2 font-medium">State</th>
            </tr>
          </thead>
          <tbody className={OPS_TABLE_DIVIDE}>
            {rows.map((row) => (
              <tr key={row.key}>
                <td className="px-4 py-3">
                  <span className="font-medium text-slate-900">{row.label}</span>
                  <span className="block text-[11px] font-normal text-slate-400">{row.hint}</span>
                </td>
                <td className="px-4 py-3 align-top">
                  <span className={`${PILL} ${OPS_BADGE[RESOURCE_STATE_BADGE_KEY[row.state]]}`}>
                    <span aria-hidden="true">●</span>
                    {RESOURCE_STATE_LABEL[row.state]}
                  </span>
                  {row.state === 'unknown' && (
                    <span className="block text-[11px] text-slate-400 mt-1">
                      The probe couldn’t tell — not known to be missing.
                    </span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
