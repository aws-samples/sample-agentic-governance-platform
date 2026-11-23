import { useEffect, useRef } from 'react';
import type { ProductCard } from '../../../api/client';
import { AgentAvatar } from '../agentUi';
import { ctaFor, declaredList, relativeTime } from './marketplaceForm';
import {
  DEPLOY_SOON_LABEL,
  DEPLOY_SOON_TITLE,
  DISABLED_CTA,
  META_CHIP,
  classificationChipCls,
} from './marketplaceCardStyles';

const SECTION_TITLE =
  'text-[11px] uppercase tracking-wide text-slate-400 font-semibold mb-2';
const ROW = 'flex items-baseline justify-between gap-3 text-sm';
const ROW_KEY = 'text-slate-500 shrink-0';
const ROW_VAL = 'text-slate-800 font-medium text-right';

export default function AgentDetailDrawer({
  card,
  onClose,
  onSubscribe,
}: {
  card: ProductCard;
  onClose: () => void;
  onSubscribe?: () => void;
}) {
  const closeRef = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    closeRef.current?.focus();
  }, []);

  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.preventDefault();
        onClose();
      }
    };
    document.addEventListener('keydown', onKeyDown);
    return () => document.removeEventListener('keydown', onKeyDown);
  }, [onClose]);

  const cta = ctaFor(card);
  const declared = relativeTime(card.declared_at);
  const compliance = declaredList(card.compliance);
  const guardrails = declaredList(card.guardrails);

  const hasAdoption = !!(card.consumers != null || card.version || declared);
  const hasGovernance = !!(
    card.data_classification ||
    card.sla_tier ||
    compliance.length ||
    guardrails.length ||
    card.region
  );
  const hasSupport = !!(card.owner_team || card.support_contact || card.support_hours);

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/30 backdrop-blur-sm p-4"
      role="dialog"
      aria-modal="true"
      aria-label={`Details for ${card.name}`}
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div className="w-full max-w-lg max-h-[85vh] flex flex-col bg-white rounded-2xl border border-slate-200 shadow-xl">
        <div className="flex items-start justify-between gap-3 px-5 py-4 border-b border-slate-200/60">
          <div className="flex items-start gap-3 min-w-0">
            <AgentAvatar name={card.name} size="md" />
            <div className="min-w-0">
              <h2
                className="min-w-0 text-sm font-semibold text-slate-900 leading-tight truncate"
                title={card.name}
              >
                {card.name}
              </h2>
              <div className="mt-1 flex items-center gap-1.5 min-w-0">
                {card.owner_team && (
                  <span
                    className="text-xs text-slate-500 truncate"
                    title={`Owner team (declared): ${card.owner_team}`}
                  >
                    {card.owner_team}
                  </span>
                )}
              </div>
            </div>
          </div>
          <button
            ref={closeRef}
            type="button"
            onClick={onClose}
            aria-label="Close"
            className="shrink-0 inline-flex items-center justify-center h-7 w-7 rounded-md text-slate-400 hover:text-slate-700 hover:bg-slate-100 transition-colors"
          >
            <span aria-hidden="true" className="text-base leading-none">×</span>
          </button>
        </div>

        <div className="px-5 py-4 space-y-5 overflow-y-auto">
          {card.pitch && (
            <p className="text-sm text-slate-600 leading-relaxed">{card.pitch}</p>
          )}

          {hasAdoption && (
            <section>
              <h3 className={SECTION_TITLE}>Adoption</h3>
              <div className="space-y-1.5">
                {card.consumers != null && (
                  <div className={ROW}>
                    <span className={ROW_KEY}>Consumers</span>
                    <span className={ROW_VAL}>{card.consumers} teams</span>
                  </div>
                )}
                {card.version && (
                  <div className={ROW}>
                    <span className={ROW_KEY}>Version</span>
                    <span className={ROW_VAL}>v{card.version}</span>
                  </div>
                )}
                {declared && (
                  <div className={ROW}>
                    <span className={ROW_KEY}>Declared</span>
                    <span className={ROW_VAL} title={card.declared_at ?? undefined}>
                      {declared}
                    </span>
                  </div>
                )}
              </div>
            </section>
          )}

          {hasGovernance && (
            <section>
              <h3 className={SECTION_TITLE}>Governance</h3>
              <div className="space-y-2.5">
                {card.data_classification && (
                  <div className="flex items-center gap-2">
                    <span className={ROW_KEY}>Classification</span>
                    <span
                      className={`inline-flex items-center px-2 py-0.5 rounded-md text-xs font-medium ${classificationChipCls(card.data_classification)}`}
                    >
                      {card.data_classification}
                    </span>
                  </div>
                )}
                {card.sla_tier && (
                  <div className={ROW}>
                    <span className={ROW_KEY}>SLA tier</span>
                    <span className={ROW_VAL}>{card.sla_tier}</span>
                  </div>
                )}
                {card.region && (
                  <div className={ROW}>
                    <span className={ROW_KEY}>Region</span>
                    <span className={ROW_VAL}>{card.region}</span>
                  </div>
                )}
                {compliance.length > 0 && (
                  <div>
                    <p className="text-xs text-slate-500 mb-1.5">Compliance (declared)</p>
                    <div className="flex flex-wrap gap-1.5">
                      {compliance.map((c) => (
                        <span key={c} className={META_CHIP}>{c}</span>
                      ))}
                    </div>
                  </div>
                )}
                {guardrails.length > 0 && (
                  <div>
                    <p className="text-xs text-slate-500 mb-1.5">Guardrails (declared)</p>
                    <div className="flex flex-wrap gap-1.5">
                      {guardrails.map((g) => (
                        <span key={g} className={META_CHIP}>{g}</span>
                      ))}
                    </div>
                  </div>
                )}
              </div>
            </section>
          )}

          {hasSupport && (
            <section>
              <h3 className={SECTION_TITLE}>Support</h3>
              <div className="space-y-1.5">
                {card.owner_team && (
                  <div className={ROW}>
                    <span className={ROW_KEY}>Owner team</span>
                    <span className={ROW_VAL}>{card.owner_team}</span>
                  </div>
                )}
                {card.support_contact && (
                  <div className={ROW}>
                    <span className={ROW_KEY}>Contact</span>
                    <a
                      href={`mailto:${card.support_contact}`}
                      className="text-sm font-medium text-right text-blue-700 hover:underline break-all"
                    >
                      {card.support_contact}
                    </a>
                  </div>
                )}
                {card.support_hours && (
                  <div className={ROW}>
                    <span className={ROW_KEY}>Support hours</span>
                    <span className={ROW_VAL}>{card.support_hours}</span>
                  </div>
                )}
              </div>
            </section>
          )}

          {card.capabilities.length > 0 && (
            <section>
              <h3 className={SECTION_TITLE}>Capabilities</h3>
              <div className="flex flex-wrap gap-1.5">
                {card.capabilities.map((cap) => (
                  <span key={cap} className={META_CHIP}>{cap}</span>
                ))}
              </div>
            </section>
          )}

          <p className="text-xs text-slate-400">
            Datasheet values are declared by the publishing team and approved by a platform
            administrator. The platform does not verify or measure them.
          </p>
        </div>

        <div className="flex items-center justify-end gap-2 px-5 py-4 border-t border-slate-200/60">
          <button
            type="button"
            onClick={onClose}
            className="px-3.5 py-1.5 rounded-lg bg-white border border-slate-300 text-slate-700 text-sm font-medium hover:bg-slate-50 transition-colors"
          >
            Close
          </button>
          {cta === 'subscribe' && onSubscribe && (
            <button
              type="button"
              onClick={onSubscribe}
              className="px-3.5 py-1.5 rounded-lg bg-blue-600 text-white text-sm font-medium hover:bg-blue-700 transition-colors"
            >
              Subscribe
            </button>
          )}
          {cta === 'deploy' && (
            <button type="button" disabled title={DEPLOY_SOON_TITLE} className={DISABLED_CTA}>
              {DEPLOY_SOON_LABEL}
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
