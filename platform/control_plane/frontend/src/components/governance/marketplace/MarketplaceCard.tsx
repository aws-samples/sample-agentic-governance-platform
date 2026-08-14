// MarketplaceCard — a single product tile in the Marketplace grids (Epic 9, T8/F3).
//
// One card shape for both consumer pages (published agents + gateway MCPs). The
// chrome is the governance glass CARD used everywhere (AgentsList filter band,
// CedarPoliciesTab, AgentDetail) so the grid sits in the same surface family as
// the rest of the governance UI. Avatars/aliases/kind badges are the SHARED
// agentUi helpers — no reinvented visuals.
//
// The CTA is state-aware via ctaFor(card): a never-subscribed product shows a
// Subscribe button; a pending request shows a Pending pill; an approved MCP shows
// a "Subscribed" pill.
//
// E31F/T6 — an approved agent subscription shows a DISABLED "Deploy Agent — coming
// soon" button. It used to be an active primary affordance that opened a deploy
// form whose own Deploy action was a no-op: a button that looked like it worked,
// led to a form that looked like it worked, and provisioned nothing. A disabled
// control with the reason in its tooltip is the honest shape. E33/T6 finished the
// job: the parked form and the `onDeploy` prop that used to open it are deleted, so
// there is no dead plumbing left behind the disabled button.
//
// F3 — MCP cards are enriched: a compact governance metadata block (data
// classification, line of business, region, version, "Updated <relative>") and a
// whole-card click-through to the registry detail page (/mcp-servers/:id) when an
// `onView` handler is supplied. The card root then behaves like a button
// (role/tabIndex/Enter+Space) and the Subscribe button stops propagation so it
// never doubles as a navigation.
//
// Cards are "mesh product" tiles published by other business units: under the name an
// owner-team label (+ the registry kind badge when the record carries one); then a trust strip
// (N teams · data-classification chip · tenant) and a declared-datasheet chip row (SLA tier ·
// compliance frameworks). The pitch stays. Agent cards click through to a detail DRAWER (not a
// registry page) via `onView`, with a violet "View details" cue mirroring the MCP card.
//
// Amendment 1 (E33) — the datasheet chrome is keyed on FIELD PRESENCE, not on the product type.
// It used to be guarded by `!isMcp`, from when MCP servers were auto-listed by registry kind and
// had no declaration behind them. Publication is now the only door for BOTH types: an MCP card
// exists because a publisher declared a datasheet and an admin approved it, and the service
// projects those fields onto it identically — so gating them off threw an approved declaration
// away. The one field the strips still split by type is classification/tenant, which the MCP
// governance strip already prints (strip ownership, not a datasheet gate — see the component).
//
// E31F/T6 stripped the SLA tier, 30-day uptime, ★ rating and status off these
// strips because none of them were measured or declared — they were literals in a
// hardcoded blueprint list, i.e. the card inventing service-level promises and peer
// reviews. E33 removed that list: an agent product now exists only because its
// publisher submitted a datasheet and an admin APPROVED it, so `owner_team`,
// `sla_tier` and `compliance[]` are attested declarations with a named owner and a
// declaration timestamp. All three come back — `owner_team` as the header sub-label
// it already was, `sla_tier` + `compliance[]` as chips under a visible "Declared"
// row label (the disclosure is on-screen, never hover-only) — with the long-form
// qualifier restated in each tooltip. Uptime / latency / rating / status do NOT:
// nothing measures them and the fields no longer exist on ProductCard. The `Preview`
// badge is gone with them: it disclosed a hardcoded preview catalog that no longer
// exists, and the "Declared" row label carries the provenance it used to imply.

import type { KeyboardEvent } from 'react';
import type { ProductCard } from '../../../api/client';
import { AgentAvatar, kindBadge } from '../agentUi';
import { ctaFor, datasheetChips, relativeTime } from './marketplaceForm';

// Deploy-affordance copy (E31F/T6). Pinned and exported so the card and the detail
// drawer cannot drift into two different explanations of the same missing
// capability. (The third consumer, the parked deploy form, was deleted in E33/T6.)
export const DEPLOY_SOON_LABEL = 'Deploy Agent — coming soon';
export const DEPLOY_SOON_TITLE =
  'Agent deployment from the marketplace is coming in a future release';

// A disabled primary button: the Subscribe geometry with the fill drained, so it
// still reads as "the primary action here" while being visibly unavailable.
export const DISABLED_CTA =
  'px-3.5 py-1.5 rounded-lg bg-slate-100 text-slate-400 text-sm font-medium ' +
  'border border-slate-200 cursor-not-allowed';

// Card chrome — identical to the governance glass surface (AgentsList / CedarPoliciesTab).
const CARD = 'bg-white/70 backdrop-blur rounded-xl border border-slate-200/60 shadow-sm';

// A clickable card lifts on hover/focus to advertise the click-through; the focus
// ring matches the blue used by the search inputs + Subscribe button.
const CLICKABLE =
  'cursor-pointer transition-shadow transition-colors hover:border-slate-300 hover:shadow-md ' +
  'focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-500/40 focus-visible:border-slate-300';

// Neutral slate chip — matches the existing capability chips. Exported so the agent
// detail drawer reuses the exact same chip tint.
export const META_CHIP =
  'inline-flex items-center gap-1 px-2 py-0.5 rounded-md text-xs font-medium bg-slate-100 text-slate-600';

// Data-classification chip — tinted by sensitivity so the governance signal reads
// at a glance (Public/Internal stay neutral-cool; Confidential amber; Restricted
// rose). Falls back to the neutral META_CHIP tint for unknown values. Exported so
// the detail drawer tints its classification chip identically.
export function classificationChipCls(value: string): string {
  switch (value.toLowerCase()) {
    case 'restricted':
      return 'bg-rose-50 text-rose-700';
    case 'confidential':
      return 'bg-amber-50 text-amber-700';
    case 'internal':
      return 'bg-sky-50 text-sky-700';
    case 'public':
      return 'bg-emerald-50 text-emerald-700';
    default:
      return 'bg-slate-100 text-slate-600';
  }
}

// Lifecycle badge — GA emerald (production-ready), Beta amber, Experimental slate,
// Deprecated rose. Mirrors the agentUi lifecycle palette so the catalogs read alike.
//
// NO LONGER USED BY THIS CARD (fix round 2): nothing populates `ProductCard.lifecycle`, so the
// badge it tinted was unreachable and was deleted from the header. Kept exported ONLY because
// AgentDetailDrawer.tsx still imports it (a file outside this task's manifest) — its own
// `card.lifecycle` blocks are dead for exactly the same reason and are on the cleanup ledger.
// Delete this helper together with them; do not add new consumers.
export function lifecycleBadgeCls(value: string): string {
  switch (value.toLowerCase()) {
    case 'ga':
      return 'bg-emerald-50 text-emerald-700';
    case 'beta':
      return 'bg-amber-50 text-amber-700';
    case 'experimental':
      return 'bg-slate-100 text-slate-600';
    case 'deprecated':
      return 'bg-rose-50 text-rose-700';
    default:
      return 'bg-slate-100 text-slate-600';
  }
}

// (E33/T6 — `statusDotCls` was deleted with `ProductCard.status`: a declared
// datasheet cannot assert a live service state, and nothing measures one.)

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
  // The registry kind badge is keyed on the FIELD, not on the product type. It used to default
  // a null kind to "gateway" because `list_mcp_products` only surfaced gateways — Amendment 1
  // retired that filter (publication is the only door, any kind can be published), so inventing
  // "Gateway" would now mislabel a standard server. Absent kind → no badge (agent cards never
  // carry one).
  const kind = card.kind ? kindBadge(card.kind as 'gateway' | 'standard' | 'runtime') : null;

  // The whole card is a click-through when an onView handler is supplied: MCP cards
  // go to the registry detail page; agent cards open the detail drawer.
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

  // Datasheet signals, keyed on FIELD PRESENCE for both product types: since Amendment 1 an MCP
  // card also exists only because a publisher declared a datasheet and an admin approved it, and
  // the service projects those fields onto MCP cards exactly as onto agent cards. Gating them on
  // `!isMcp` discarded a real, approved declaration.
  //
  // The trust strip is what the platform itself knows (live consumers, declared classification,
  // owning tenant). Classification + tenant stay `!isMcp` here NOT as a datasheet gate but as
  // strip OWNERSHIP: an MCP card also renders the registry governance strip below, whose OR-list
  // contains both fields, so a present value is always printed there — printing it here too would
  // duplicate the same chip on one card. `consumers` is owned by this strip alone, for both types
  // (the MCP projection does not tally it today, so the token simply stays absent until it does).
  const showTrustClassification = !isMcp && !!card.data_classification;
  const showTrustTenant = !isMcp && !!card.tenant_name;
  const hasTrustStrip = card.consumers != null || showTrustClassification || showTrustTenant;
  // The declared chip row (SLA tier + compliance) comes from the tested pure helper, which omits
  // blanks and de-duplicates — so an empty result renders NO row and, with it, no dangling
  // "Declared" label.
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
      {/* Header: avatar + name + (agent) category / (mcp) owner + kind badge. */}
      <div className="flex items-start gap-3">
        <AgentAvatar name={card.name} size="md" />
        <div className="min-w-0 flex-1">
          {/* min-w-0 on the heading itself: `truncate` only takes effect on a flex
              item that is allowed to shrink below its content width. */}
          <h3
            className="min-w-0 text-sm font-semibold text-slate-900 leading-tight truncate"
            title={card.name}
          >
            {card.name}
          </h3>
          {/* Header sub-row — ONE branch for both product types, each token keyed on its own
              field: the registry kind badge (MCP records only carry one) and the DECLARED owning
              team. Omitted entirely when neither exists.

              The MCP-only `emailAlias(owner_email)` sub-label that used to sit here is GONE: a
              published card is marketplace-wide, so the service deliberately stops projecting
              the registry owner's address onto it (cross-tenant contact PII), which left this
              label permanently empty. `owner_team` replaces it in the same slot and is the
              better signal — declared, admin-approved and a team rather than a person. The
              declared `support_contact` is not duplicated here: it belongs with the rest of the
              datasheet in the detail views, and a mailbox in the header would crowd out the
              owner it identifies.

              The tinted `lifecycle` badge and the `category` fallback are gone for the SAME
              reason (fix round 2): NEITHER projection in marketplace_service.py ever assigns
              `lifecycle=` or `category=`, and `Datasheet` has no lifecycle field — so both were
              permanently None, i.e. an unreachable branch dressed as a live governance signal.
              A lifecycle badge here would also have to be a DECLARATION to be honest, and the
              datasheet deliberately declares no lifecycle. `ProductCard.lifecycle`/`.category`
              stay typed in client.ts (out of manifest, backend model keeps them too); nothing
              reads them on this card any more. */}
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

      {/* Datasheet strips (both product types): a trust row (teams · classification · tenant),
          then the declared chip row (SLA · compliance). Each token is omitted when its field is
          empty — no "—" placeholders. Classification/tenant are printed by the MCP governance
          strip further down instead (strip ownership, see above), so they never appear twice. */}
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
          {/* Owning tenant (E24) — same neutral META_CHIP as the MCP chip row. */}
          {showTrustTenant && card.tenant_name && (
            <span className={META_CHIP} title="Tenant">
              {card.tenant_name}
            </span>
          )}
        </div>
      )}

      {/* Declared datasheet chips (E33) — the SLA tier and compliance frameworks the
          publisher asserted and an admin approved. Neutral META_CHIP tint: these are
          claims, not platform-verified status.

          The row is prefixed by a VISIBLE "Declared" label, not just per-chip tooltips.
          With the Preview badge gone this is the card's only provenance signal, and a
          `title` on a non-interactive span is invisible on touch and unreliably exposed
          to assistive tech — which would leave these chips reading as platform facts,
          the softer version of exactly what E31F removed. The label also separates this
          row from the trust strip above it, which IS platform-known. */}
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

      {/* Pitch. */}
      {card.pitch && (
        <p className="mt-3 text-sm text-slate-600 leading-relaxed line-clamp-3">{card.pitch}</p>
      )}

      {/* Capability chips. Empty for agent products (E33: a datasheet declares no
          capability list, and inventing one is what E31F removed), so this block is
          effectively MCP-only until something derives them. */}
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

      {/* Governance metadata (MCP only, F3). Each chip is omitted when its field is
          empty — no "—" placeholders. The "Updated" line sits on its own row. */}
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
            {/* Owning tenant (E24) — a sibling META_CHIP to the LoB chip. */}
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

      {/* Footer — CTA pinned to the bottom so cards line up across the grid. For a
          clickable MCP card we also render a "View details" cue for discoverability
          (the whole card is the affordance, this is the visible signpost). */}
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
                // On a clickable card, keep the Subscribe action from also
                // triggering the card's navigation.
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
            // E31F/T6 — parked, not wired. `disabled` also stops the click from
            // reaching the card's own onView, so no handler runs at all here.
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
