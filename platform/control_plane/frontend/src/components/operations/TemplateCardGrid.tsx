// TemplateCardGrid — the template catalog's card grid (E28/T9), extracted verbatim from
// the former `TemplatesAdmin.tsx`. It was extracted so the standalone `/ops/templates` page
// and the Ops Admin "Templates" tab could render the SAME cards; T8 then deleted the admin
// console and T9b deleted the tab body, so `Templates.tsx` is now the sole call site. Kept
// as its own module regardless: it is the catalog's visual contract, and separating it is
// what let the page's markup stay reviewable.
//
// Presentation only: it decides nothing, owns no state and fetches nothing — every
// judgement it displays is handed to it.
//
// Cards rather than a table, deliberately. Templates are BROWSED FOR SELECTION — you are
// choosing a scaffold by what it is made of (framework, AWS services, tags), which is a
// comparison of unlike attributes across a handful of items. Repositories are MONITORED,
// which is one scanned column of status across many rows, and that is a table. Same reason
// the description, tag chips and service chips get real vertical room here.
//
// House style: emerald-on-glass Ops tokens (opsUi.ts), Tailwind v4 utility strings,
// 2-space indent, inline SVG only — matching the other Ops surfaces.

import { type JSX } from 'react';

import type { TemplateView } from '../../api/client';
// Explicit extension: with `allowImportingTsExtensions` on a case-insensitive filesystem an
// extensionless specifier can land on a sibling differing only in casing and import as
// `undefined` with no error (see `githubLinkApi.test.ts:18-20`).
import { OPS_CARD } from './opsUi.ts';

// Framework pill keeps the emerald content identity (not a status pill — the
// framework is descriptive, so it borrows the healthy-emerald tint).
export const FRAMEWORK_PILL = 'bg-emerald-50 text-emerald-700';
// Small descriptive tag pill.
export const TAG_PILL = 'bg-emerald-50/70 text-emerald-700';
// Muted AWS-service chip — brand-neutral slate so it reads as metadata, not a tag.
export const SERVICE_CHIP = 'bg-slate-100 text-slate-500';

// Readable date for updated_at; falls back to the raw string if unparseable.
export function formatTemplateDate(iso: string): string {
  if (!iso) return '—';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' });
}

export interface TemplateCardGridProps {
  templates: readonly TemplateView[];
  /** The template with a mutation in flight (single-flight across the whole grid). */
  actionPendingName: string | null;
  /** A mutation error pinned to one card rather than raised as a page banner. */
  cardError: { name: string; message: string } | null;
  /**
   * May the caller change the catalog (finding #14)? `false` ⇒ no Edit, no Delete — ABSENT,
   * not disabled.
   *
   * The two are different axes and conflating them is the bug. `disabled` here means "a
   * mutation is in flight" (`anyPending`, single-flight across the grid) — a temporary state
   * that clears. Lacking the standing is permanent, and the backend already enforces it:
   * `PATCH`/`DELETE` on `/github-templates` are `require_role(Role.ADMIN)` while the `GET`
   * that fills this grid needs only OPERATOR. So an operator saw both buttons on every card
   * and got a 403 from each. A greyed button would still advertise the dead end; the caller
   * is simply not offered a verb they do not have.
   *
   * Required rather than defaulted: a component that shrugged and assumed `true` would gate
   * nothing at the one call site that forgot to pass it.
   */
  canMutate: boolean;
  onEdit: (template: TemplateView) => void;
  onDelete: (template: TemplateView) => void;
}

export default function TemplateCardGrid(props: TemplateCardGridProps): JSX.Element {
  const { templates, actionPendingName, cardError, canMutate, onEdit, onDelete } = props;
  const anyPending = actionPendingName !== null;

  return (
    <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
      {templates.map((t) => {
        const rowPending = actionPendingName === t.name;
        const err = cardError && cardError.name === t.name ? cardError.message : null;
        return (
          <div key={t.name} className={`${OPS_CARD} p-5 flex flex-col`}>
            {/* Name + framework pill. */}
            <div className="flex items-start justify-between gap-3">
              <h2 className="text-base font-semibold text-slate-900 truncate" title={t.name}>
                {t.name}
              </h2>
              {t.framework && (
                <span
                  className={`text-[11px] font-medium px-2 py-0.5 rounded-full shrink-0 ${FRAMEWORK_PILL}`}
                >
                  {t.framework}
                </span>
              )}
            </div>

            {/* Description. */}
            <p className="text-sm text-slate-500 mt-2">{t.description || 'No description.'}</p>

            {/* Tags. */}
            {t.tags.length > 0 && (
              <div className="mt-3 flex flex-wrap gap-1.5">
                {t.tags.map((tag) => (
                  <span
                    key={tag}
                    className={`text-[11px] font-medium px-2 py-0.5 rounded-full ${TAG_PILL}`}
                  >
                    {tag}
                  </span>
                ))}
              </div>
            )}

            {/* AWS services. */}
            {t.aws_services.length > 0 && (
              <div className="mt-2 flex flex-wrap gap-1.5">
                {t.aws_services.map((svc) => (
                  <span
                    key={svc}
                    className={`text-[10px] font-medium px-1.5 py-0.5 rounded ${SERVICE_CHIP}`}
                  >
                    {svc}
                  </span>
                ))}
              </div>
            )}

            {/* Metadata footer: last updated + repo link. */}
            <div className="mt-4 pt-3 border-t border-emerald-100/70 flex items-center justify-between gap-2 text-[11px] text-slate-400">
              <span title="Last updated">{formatTemplateDate(t.updated_at)}</span>
              {t.html_url && (
                <a
                  href={t.html_url}
                  target="_blank"
                  rel="noreferrer"
                  className="text-emerald-700 hover:underline font-medium"
                >
                  View repo ↗
                </a>
              )}
            </div>

            {/* Per-card mutation error (delete). */}
            {err && (
              <p className="mt-2 text-xs text-rose-600" role="alert">
                {err}
              </p>
            )}

            {/* Actions — ADMIN only, and ABSENT rather than greyed for everyone else.
                Nothing takes their place: no "admins only" line, no tooltip on a missing
                button. A card is one of many, so a per-card explanation would repeat itself
                down the whole grid to say something the reader cannot act on. Rendering
                nothing is this app's idiom for absent standing (Settings' tab strip omits the
                Admin tab outright; GitHubLink shows no button with no action). The one place
                the gate DOES change words is the empty-catalog card, whose body would
                otherwise instruct a non-admin to upload — see EMPTY_CATALOG_READ_ONLY_BODY.

                The card keeps its full metadata footer either way, so a read-only card is
                still a complete card rather than a visibly truncated one. */}
            {canMutate && (
              <div className="mt-3 flex items-center gap-2">
                <button
                  type="button"
                  onClick={() => onEdit(t)}
                  disabled={anyPending}
                  className="px-2.5 py-1 rounded-md bg-white border border-slate-300 text-slate-700 text-xs font-medium hover:bg-slate-50 transition-colors disabled:opacity-40"
                >
                  Edit
                </button>
                <button
                  type="button"
                  onClick={() => onDelete(t)}
                  disabled={anyPending}
                  className="px-2.5 py-1 rounded-md bg-white border border-slate-300 text-rose-600 text-xs font-medium hover:bg-rose-50 transition-colors disabled:opacity-40"
                >
                  {rowPending ? '…' : 'Delete'}
                </button>
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}
