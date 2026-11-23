import type { KeyboardEvent } from 'react';
import type { Kind, ProductCard } from '../../../api/client';
import { AgentAvatar } from '../agentUi';
import { kindBadge } from '../agentBadges';
import { ctaFor, datasheetChips, relativeTime } from './marketplaceForm';
import {
  DEPLOY_SOON_LABEL,
  DEPLOY_SOON_TITLE,
  DISABLED_CTA,
  META_CHIP,
  classificationChipCls,
} from './marketplaceCardStyles';

const CARD = 'bg-white/70 backdrop-blur rounded-xl border border-slate-200/60 shadow-sm';

const CLICKABLE =
  'cursor-pointer transition-shadow transition-colors hover:border-slate-300 hover:shadow-md ' +
  'focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-500/40 focus-visible:border-slate-300';

export default function MarketplaceCard({
  card,
  onSubscribe,
  onView,
}: {
  card: ProductCard;
  onSubscribe: () => void;
  onView?: () => void;
}) {
  const cta = ctaFor(card);
  const isMcp = card.product_type === 'mcp';
  const kind = card.kind ? kindBadge(card.kind as Kind) : null;

  const clickable = !!onView;
  const updated = isMcp ? relativeTime(card.updated_at) : '';
  const hasGovernance =
    isMcp &&
    !!(
      card.data_classification ||
      card.business_unit ||
      card.tenant_name ||
      card.region ||
      card.version ||
      updated
    );

  const showTrustClassification = !isMcp && !!card.data_classification;
  const showTrustTenant = !isMcp && !!card.tenant_name;
  const hasTrustStrip = card.consumers != null || showTrustClassification || showTrustTenant;
  const declaredChips = datasheetChips(card);

  const handleKeyDown = (e: KeyboardEvent<HTMLDivElement>) => {
    if (!clickable || !onView) return;
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      onView();
    }
  };

  return (
    <div
      className={`${CARD} ${clickable ? CLICKABLE : ''} p-4 flex flex-col h-full`}
      {...(clickable
        ? {
            onClick: onView,
            onKeyDown: handleKeyDown,
            role: 'button',
            tabIndex: 0,
            'aria-label': `View details for ${card.name}`,
          }
        : {})}
    >
      <div className="flex items-start gap-3">
        <AgentAvatar name={card.name} size="md" />
        <div className="min-w-0 flex-1">
          <h3
            className="min-w-0 text-sm font-semibold text-slate-900 leading-tight truncate"
            title={card.name}
          >
            {card.name}
          </h3>
          {(kind || card.owner_team) && (
            <div className="mt-1 flex items-center gap-1.5 min-w-0">
              {kind && (
                <span
                  className={`inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium shrink-0 ${kind.cls}`}
                >
                  {kind.label}
                </span>
              )}
              {card.owner_team && (
                <span
                  className="text-xs text-slate-500 truncate"
                  title={`Owner team (declared): ${card.owner_team}`}
                >
                  {card.owner_team}
                </span>
              )}
            </div>
          )}
        </div>
      </div>

      {hasTrustStrip && (
        <div className="mt-3 flex flex-wrap items-center gap-x-3 gap-y-1.5 text-xs text-slate-500">
          {card.consumers != null && (
            <span title="Subscribing teams">
              <span className="text-slate-600 font-medium">{card.consumers}</span> teams
            </span>
          )}
          {showTrustClassification && card.data_classification && (
            <span
              className={`inline-flex items-center px-2 py-0.5 rounded-md text-xs font-medium ${classificationChipCls(card.data_classification)}`}
              title="Data classification"
            >
              {card.data_classification}
            </span>
          )}
          {showTrustTenant && card.tenant_name && (
            <span className={META_CHIP} title="Tenant">
              {card.tenant_name}
            </span>
          )}
        </div>
      )}

      {declaredChips.length > 0 && (
        <div className="mt-2 flex flex-wrap items-center gap-1.5">
          <span className="text-[11px] uppercase tracking-wide text-slate-500 font-semibold">
            Declared
          </span>
          {declaredChips.map((chip) => (
            <span key={chip.key} className={META_CHIP} title={chip.title}>
              {chip.label}
            </span>
          ))}
        </div>
      )}

      {card.pitch && (
        <p className="mt-3 text-sm text-slate-600 leading-relaxed line-clamp-3">{card.pitch}</p>
      )}

      {card.capabilities.length > 0 && (
        <div className="mt-3 flex flex-wrap gap-1.5">
          {card.capabilities.map((cap) => (
            <span
              key={cap}
              className="inline-flex items-center px-2 py-0.5 rounded-md text-xs font-medium bg-slate-100 text-slate-600"
            >
              {cap}
            </span>
          ))}
        </div>
      )}

      {hasGovernance && (
        <div className="mt-3 space-y-2">
          <div className="flex flex-wrap gap-1.5">
            {card.data_classification && (
              <span
                className={`inline-flex items-center gap-1 px-2 py-0.5 rounded-md text-xs font-medium ${classificationChipCls(card.data_classification)}`}
                title="Data classification"
              >
                {card.data_classification}
              </span>
            )}
            {card.business_unit && (
              <span className={META_CHIP} title="Line of business">
                {card.business_unit}
              </span>
            )}
            {card.tenant_name && (
              <span className={META_CHIP} title="Tenant">
                {card.tenant_name}
              </span>
            )}
            {card.region && (
              <span className={META_CHIP} title="Region">
                {card.region}
              </span>
            )}
            {card.version && (
              <span className={META_CHIP} title="Version">
                v{card.version}
              </span>
            )}
          </div>
          {updated && <p className="text-xs text-slate-400">Updated {updated}</p>}
        </div>
      )}

      <div className="mt-auto pt-4 flex items-center justify-between gap-2">
        {clickable ? (
          <span className="inline-flex items-center gap-0.5 text-xs font-medium text-violet-700">
            View details
            <span aria-hidden="true">↗</span>
          </span>
        ) : (
          <span aria-hidden="true" />
        )}

        <div className="flex items-center">
          {cta === 'subscribe' && (
            <button
              type="button"
              onClick={(e) => {
                e.stopPropagation();
                onSubscribe();
              }}
              className="px-3.5 py-1.5 rounded-lg bg-blue-600 text-white text-sm font-medium hover:bg-blue-700 transition-colors"
            >
              Subscribe
            </button>
          )}

          {cta === 'pending' && (
            <span className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-xs font-medium bg-amber-50 text-amber-700">
              <span aria-hidden="true" className="h-1.5 w-1.5 rounded-full bg-amber-500" />
              Pending approval
            </span>
          )}

          {cta === 'deploy' && (
            <button
              type="button"
              disabled
              title={DEPLOY_SOON_TITLE}
              className={DISABLED_CTA}
            >
              {DEPLOY_SOON_LABEL}
            </button>
          )}

          {cta === 'subscribed' && (
            <span className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-xs font-medium bg-emerald-50 text-emerald-700">
              <span aria-hidden="true" className="h-1.5 w-1.5 rounded-full bg-emerald-500" />
              Subscribed
            </span>
          )}
        </div>
      </div>
    </div>
  );
}
