// ResourcesTab — the five artifacts this repository owns, as a first-class READ-ONLY panel
// (E28/T12).
//
// ---------------------------------------------------------------------------
// WHY THIS TAB EXISTS
//
// The platform materializes five artifacts per repository, and until now the only place that
// inventory was ever ENUMERATED was inside the delete modal — as a list of things about to be
// destroyed. So the one view that answered "what does this repository actually own, and does it all
// still exist?" was reachable only by starting a teardown, which is a poor way to ask a question and
// a worse way to answer it during an incident.
//
// It reads the SAME read-only reachability pre-check the modal seeds its checklist from
// (`GET …/delete-preview` — probes only, deletes nothing), so there is one probe implementation and
// the two surfaces cannot disagree about what exists.
//
// ---------------------------------------------------------------------------
// IT OFFERS NO DELETE, DELIBERATELY
//
// The E23 five-item cascade lives in the existing teardown modal, which owns the per-item opt-in, the
// per-item outcome and the retry semantics; the page header carries the OWNER-gated Delete that opens
// it. A delete from here would be a second teardown path to keep in step with that cascade, and an
// inventory panel is exactly the wrong place to grow one — the point of this tab is to answer a
// question, not to act. Guards assert that neither that modal nor the delete call appears in this
// file at all, which is why this comment names neither: they read raw source and do not skip
// comments, because a guard that has to decide what is "only a comment" is a guard a comment-shaped
// hit can defeat.
//
// ---------------------------------------------------------------------------
// UNREPORTED ≠ GONE, AND A 403 SAYS NOTHING ABOUT THE REPOSITORY
//
// Both judgements live in `resourcesTab.ts` where tests reach them. An artifact the probe did not
// report renders as not-established, never as already-deleted — the reassuring direction is the wrong
// one on a panel an operator reads to find out whether something has vanished. And because the
// preview route is OWNER-gated (it is the delete modal's own surface), a lesser role gets a 403,
// which is a fact about the CALLER: rendering it as an empty inventory would state something about
// the repository that nothing established.
//
// House style: emerald-on-glass Ops tokens, Tailwind v4 utility strings, 2-space indent.

import { useEffect, useState, type JSX } from 'react';

import { projectsApi, type RepoDeletePreview, type Repository } from '../../../api/client';
import { OPS_BADGE, OPS_CARD, OPS_TABLE_DIVIDE, OPS_TABLE_HEAD } from '../opsUi';
import {
  INVENTORY_STATE_COPY,
  RESOURCE_STATE_BADGE_KEY,
  RESOURCE_STATE_LABEL,
  inventoryState,
  resourceInventory,
} from './resourcesTab';

const PILL =
  'inline-flex items-center gap-1.5 text-[11px] font-semibold px-2 py-0.5 rounded-full';

export default function ResourcesTab({ repo }: { repo: Repository }): JSX.Element {
  const [preview, setPreview] = useState<RepoDeletePreview | null>(null);
  const [loading, setLoading] = useState(true);
  // The RAW `err.message`, kept raw on purpose: `inventoryState` matches the backend's pinned 403
  // literal on it, and pre-mapping it here would put that decision in a `.tsx` no test can reach.
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    // Written as ONE expression rather than a `projectsApi` / `.deletePreview` chain across lines,
    // so the guard asserting this panel reads the PREVIEW route (and never the delete) can see it.
    projectsApi.deletePreview(repo.project_id, repo.id)
      .then((p) => {
        if (!cancelled) setPreview(p);
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        setPreview(null);
        setError(err instanceof Error ? err.message : 'unknown');
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [repo.project_id, repo.id]);

  const state = inventoryState({ loading, error, preview });
  const copy = INVENTORY_STATE_COPY[state];
  const rows = resourceInventory(preview);

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

      {/* Loading, refused, or unreadable — each STATED, and none of them rendered as an inventory of
          absences. `copy` is null only for the state that has rows to show. */}
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
                  {/* The one state that needs its meaning spelled out at the point of use: "not
                      established" is easy to read as "not there", which is the conflation this
                      panel exists to avoid. */}
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
