import { type JSX } from 'react';

import type { TemplateView } from '../../api/client';
import { OPS_CARD } from './opsUi.ts';
import {
  BUILD_KIND_CHIP,
  INFRA_CHIP,
  PLATFORM_CHIP,
  PREVIEW_CHIP,
  buildKindLabel,
  templateKindLabel,
  templateKindOf,
} from './templateChain.ts';
import { FRAMEWORK_PILL, KIND_CHIP, SERVICE_CHIP, TAG_PILL, formatTemplateDate } from './templateCardStyles.ts';

export interface TemplateCardGridProps {
  templates: readonly TemplateView[];
  actionPendingName: string | null;
  cardError: { name: string; message: string } | null;
  /** `false` leaves Edit and Delete absent, not disabled. */
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
            <div className="flex items-start justify-between gap-3">
              <h2 className="min-w-0 text-base font-semibold text-slate-900 break-words line-clamp-2" title={t.name}>
                {t.name}
              </h2>
              <div className="shrink-0 flex items-center gap-1.5">
                {t.preview && (
                  <span className={`text-[11px] font-medium px-2 py-0.5 rounded-full ${PREVIEW_CHIP}`}>
                    Preview
                  </span>
                )}
                {t.framework && (
                  <span
                    className={`text-[11px] font-medium px-2 py-0.5 rounded-full ${FRAMEWORK_PILL}`}
                  >
                    {t.framework}
                  </span>
                )}
              </div>
            </div>

            <p className="text-sm text-slate-500 mt-2">{t.description || 'No description.'}</p>

            <div className="mt-2.5 flex flex-wrap items-center gap-1.5">
              <span className={`text-[10px] font-medium px-1.5 py-0.5 rounded ${KIND_CHIP[templateKindOf(t)]}`}>
                {templateKindLabel(t)}
              </span>
              <span className={`text-[10px] font-medium px-1.5 py-0.5 rounded ${PLATFORM_CHIP}`}>
                {t.platform}
              </span>
              <span className={`text-[10px] font-medium px-1.5 py-0.5 rounded ${BUILD_KIND_CHIP}`}>
                {buildKindLabel(t.build_kind)}
              </span>
              <span
                className={`text-[11px] font-medium px-2 py-0.5 rounded-full ${INFRA_CHIP}`}
                title="The infra template this one deploys through"
              >
                {t.infra_template_id}
              </span>
            </div>

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

            {err && (
              <p className="mt-2 text-xs text-rose-600" role="alert">
                {err}
              </p>
            )}

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
