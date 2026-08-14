// AgentDetailDrawer — the full "mesh product datasheet" for a published agent
// ProductCard, opened from the whole-card click-through on MarketplaceAgents.
//
// The overlay/panel chrome is cloned from SubscribeModal: the same backdrop
// (click-to-close on the backdrop only), Escape-to-close, an AgentAvatar + title +
// close-X header, a scrollable body, and a Close + primary-action footer. Visuals are
// the shared agentUi/MarketplaceCard helpers (avatar, lifecycle/classification
// tints) — no reinvented chrome and no icon library (↗ / a tinted chip).
//
// Content = three clearly-titled datasheet sections (Adoption, Governance, Support)
// plus a header and the pitch + the FULL capability list. Every field/section is
// OMITTED when empty (no "—" placeholders), exactly like the F3 governance chips on
// the card.
//
// E31F/T6 stripped this drawer back to almost nothing: it used to lead with a
// "Service & reliability" section quoting an SLA tier, 30-day uptime, p95 latency and
// support hours, rate the product out of five, and list compliance attestations —
// every value a literal in a hardcoded blueprint list, i.e. invented. E33 replaced
// that list with a real publication workflow: a product exists only because its
// publisher submitted a datasheet and an admin APPROVED it. So the DECLARED fields
// return here — sla_tier, compliance[], support_contact, support_hours, version,
// region, guardrails[] and the declared_at attestation timestamp — each labelled as a
// publisher declaration. What does NOT return: uptime, p95 latency, ★ rating and
// service status. Nothing measures those; the fields no longer exist on ProductCard.
// The `Preview` badge is gone too: it disclosed a preview catalog that no longer
// exists, and a datasheet with a named owner and a declaration timestamp is exactly
// what it was standing in for.
//
// The footer's primary CTA is Subscribe only. The Deploy button stays parked with the
// card's (see MarketplaceCard's DEPLOY_SOON_*); E33/T6 deleted the unreachable deploy
// form and the `onDeploy` plumbing that used to open it.

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
  lifecycleBadgeCls,
} from './MarketplaceCard';

// Section heading — the small uppercase label idiom used by the subscribe modal's
// field labels, promoted to a section title.
const SECTION_TITLE =
  'text-[11px] uppercase tracking-wide text-slate-400 font-semibold mb-2';
// A datasheet key/value row inside a section.
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
  // Focus the close button on open (a safe focus target — the drawer is read-only).
  const closeRef = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    closeRef.current?.focus();
  }, []);

  // Close on Escape (mirrors SubscribeModal).
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
  // WHEN the datasheet was attested (E33) — the agent-product projection carries
  // `declared_at` and no `updated_at`, so this replaces the old "Updated" row rather
  // than sitting beside it. Same relative-time idiom as the MCP card; the tooltip
  // keeps the exact timestamp.
  const declared = relativeTime(card.declared_at);
  const compliance = declaredList(card.compliance);
  const guardrails = declaredList(card.guardrails);

  // Which datasheet sections have any content (omit an empty section entirely).
  const hasAdoption = !!(card.consumers != null || card.lifecycle || card.version || declared);
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
        // Click on the backdrop (not the panel) closes.
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div className="w-full max-w-lg max-h-[85vh] flex flex-col bg-white rounded-2xl border border-slate-200 shadow-xl">
        {/* Header — avatar + name + owner team + lifecycle, close-X. */}
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
                {(card.owner_team || card.category) && (
                  <span
                    className="text-xs text-slate-500 truncate"
                    title={`${card.owner_team ? 'Owner team (declared)' : 'Category'}: ${card.owner_team ?? card.category}`}
                  >
                    {card.owner_team ?? card.category}
                  </span>
                )}
                {card.lifecycle && (
                  <span
                    className={`inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium shrink-0 ${lifecycleBadgeCls(card.lifecycle)}`}
                    title="Lifecycle stage"
                  >
                    {card.lifecycle}
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

        {/* Body — scrolls; the header + footer stay pinned. */}
        <div className="px-5 py-4 space-y-5 overflow-y-auto">
          {/* Pitch. */}
          {card.pitch && (
            <p className="text-sm text-slate-600 leading-relaxed">{card.pitch}</p>
          )}

          {/* Adoption — the live subscriber count (counted server-side from real
              subscriptions), the lifecycle/version on the record, and WHEN the datasheet
              was declared. No service status: nothing measures one, and a declaration
              cannot assert it (the field is gone from ProductCard in E33). */}
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
                {card.lifecycle && (
                  <div className={ROW}>
                    <span className={ROW_KEY}>Lifecycle</span>
                    <span className={ROW_VAL}>{card.lifecycle}</span>
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
                    <span className={ROW_VAL} title={card.declared_at}>
                      {declared}
                    </span>
                  </div>
                )}
              </div>
            </section>
          )}

          {/* Governance — classification, SLA tier, region, compliance and guardrails.
              E33: the compliance chips are back, but they are the publisher's DECLARED
              frameworks (approved by an admin), not certifications the platform verified
              — the section note below says so on the page, not just in this comment. */}
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

          {/* Support — who owns the product and how a consumer reaches them. All three
              are datasheet declarations; `support_contact` is a mandatory one, so this
              section is present for every published product. The contact is a mailto so
              the drawer is actionable, not just informative. */}
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

          {/* Capabilities — the FULL list (the card line-clamps; the drawer doesn't). */}
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

          {/* Provenance — the disclosure that used to be a "Preview" badge in the
              header. It is no longer a caveat about invented data (there is none left);
              it says exactly where these values come from, which is what an operator
              reading a datasheet needs in order to weigh them. */}
          <p className="text-xs text-slate-400">
            Datasheet values are declared by the publishing team and approved by a platform
            administrator. The platform does not verify or measure them.
          </p>
        </div>

        {/* Footer — Close + the state-aware primary CTA, repeated from the card. A
            never-subscribed (or re-requestable) product offers Subscribe; an approved
            agent subscription offers the parked Deploy affordance. Pending/Subscribed
            states have no primary action. */}
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
            // E31F/T6 — parked, matching the card. E33/T6 deleted the form it used to
            // open, so there is no handler to call and nothing left to reach.
            <button type="button" disabled title={DEPLOY_SOON_TITLE} className={DISABLED_CTA}>
              {DEPLOY_SOON_LABEL}
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
