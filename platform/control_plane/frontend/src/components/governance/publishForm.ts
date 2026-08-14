// publishForm — pure helpers for the "Publish to marketplace" panel (E33/T7), shared by BOTH
// product types: AgentDetail and McpServerDetail render the same panel over this module
// (E33 Amendment 1 / C11). Nothing here is product-specific — the panels supply the noun.
//
// Kept out of the components so the datasheet validation, the draft→wire shaping, and the
// publication-state derivation are unit-testable in vitest (the runner only picks up
// src/**/*.test.ts, node env — no DOM render harness). No React, no axios — pure TS.
//
// NOTE: this is the MARKETPLACE publication (E33), which is a different feature from the
// E24 tenant `published` flag in tenantUi.derivePublishState. Nothing here is shared with it.

import type {
  MarketplacePublicationBlock,
  MarketplacePublishRequest,
  PublishDatasheet,
} from '../../api/client';

// The form's own shape: every field is a string because every control is a text input.
// `compliance` and `guardrails` are comma-separated in the form and become arrays on the wire.
export interface DatasheetDraft {
  owner_team: string;
  support_contact: string;
  data_classification: string;
  sla_tier: string;
  compliance: string;
  support_hours: string;
  version: string;
  region: string;
  guardrails: string;
  pitch: string;
}

export const EMPTY_DATASHEET_DRAFT: DatasheetDraft = {
  owner_team: '',
  support_contact: '',
  data_classification: '',
  sla_tier: '',
  compliance: '',
  support_hours: '',
  version: '',
  region: '',
  guardrails: '',
  pitch: '',
};

// Client-side mirror of the backend's mandatory datasheet fields (owner_team,
// support_contact, data_classification). Returns [] when the draft is submittable;
// each message names the field it is about. A blank support contact yields only the
// "required" message — the email-shape complaint would be noise on an empty box.
export function validateDatasheet(d: DatasheetDraft): string[] {
  const errors: string[] = [];
  if (!d.owner_team.trim()) errors.push('Owner team is required.');
  const contact = d.support_contact.trim();
  if (!contact) {
    errors.push('Support contact is required.');
  } else if (!contact.includes('@')) {
    errors.push('Support contact must be an email address.');
  }
  if (!d.data_classification.trim()) errors.push('Data classification is required.');
  return errors;
}

// Blank optional scalar → null (the backend field is Optional[str] = None).
function orNull(value: string): string | null {
  const trimmed = value.trim();
  return trimmed ? trimmed : null;
}

// "GDPR, ,SOC2" → ['GDPR', 'SOC2']; blank → [].
function commaList(value: string): string[] {
  return value
    .split(',')
    .map((entry) => entry.trim())
    .filter((entry) => entry.length > 0);
}

// Draft → POST body datasheet. Call only after validateDatasheet returns [].
export function buildDatasheet(d: DatasheetDraft): PublishDatasheet {
  return {
    owner_team: d.owner_team.trim(),
    support_contact: d.support_contact.trim(),
    data_classification: d.data_classification.trim(),
    sla_tier: orNull(d.sla_tier),
    compliance: commaList(d.compliance),
    support_hours: orNull(d.support_hours),
    version: orNull(d.version),
    region: orNull(d.region),
    guardrails: commaList(d.guardrails),
    pitch: orNull(d.pitch),
  };
}

// The inverse of buildDatasheet: seed the form from the datasheet already on record so an
// "Update datasheet" re-publish edits it instead of retyping it. The backend keeps ONE
// publish record per (product_type, product_id) and a re-publish overwrites it wholesale, so submitting a
// half-filled draft would drop declared fields — prefilling is what prevents that.
export function draftFromDatasheet(ds: PublishDatasheet | null | undefined): DatasheetDraft {
  if (!ds) return EMPTY_DATASHEET_DRAFT;
  return {
    owner_team: ds.owner_team ?? '',
    support_contact: ds.support_contact ?? '',
    data_classification: ds.data_classification ?? '',
    sla_tier: ds.sla_tier ?? '',
    compliance: (ds.compliance ?? []).join(', '),
    support_hours: ds.support_hours ?? '',
    version: ds.version ?? '',
    region: ds.region ?? '',
    guardrails: (ds.guardrails ?? []).join(', '),
    pitch: ds.pitch ?? '',
  };
}

export type PublishState = 'published' | 'pending' | 'rejected' | 'unpublished' | 'never';

// The publish request is the latest *action* on this product's publication; the product's
// marketplace block is the last *committed* state. Truth table, in order:
//
//   request pending                  → 'pending'      (wins even over a live listing: a second
//                                                      create would 409 publish_conflict, so the
//                                                      re-publish CTA must be hidden)
//   request rejected                 → 'rejected'     (a declined update must surface its reason)
//   block.published === true         → 'published'
//   block present, published false    → 'unpublished'  (admin delisted; datasheet history retained)
//   otherwise                        → 'never'
//
// `req` is null when publishRequestForProduct 404d — which means BOTH "never requested" and
// "not your tenant", byte-identically on purpose; the block then answers on its own.
//
// The block param is the SHARED `MarketplacePublicationBlock` (C11), not `Agent['marketplace']`:
// `Agent.marketplace` and `McpServer.marketplace` are the same declared shape, so one signature
// serves both panels. `null | undefined` is spelled out because both fields are optional.
export function publishStateLabel(
  req: MarketplacePublishRequest | null,
  block: MarketplacePublicationBlock | null | undefined,
): PublishState {
  if (req?.status === 'pending') return 'pending';
  if (req?.status === 'rejected') return 'rejected';
  if (block) return block.published ? 'published' : 'unpublished';
  return 'never';
}

// True when 'pending'/'rejected' is masking a still-live marketplace card: the product was already
// published, then an "Update datasheet" re-publish went into review (or was declined). Because
// `pending`/`rejected` outrank the block in publishStateLabel, the state alone would read as "not
// listed" while consumers still see the card — so the copy MUST be qualified in this case.
// False for every other state (published/unpublished/never already state the listing truth).
export function listingStaysLive(
  state: PublishState,
  block: MarketplacePublicationBlock | null | undefined,
): boolean {
  if (state !== 'pending' && state !== 'rejected') return false;
  return block?.published === true;
}

// Badge + CTA copy per state, shared by both panels so the two cannot drift (C11). The copy is
// deliberately DISTINCT from the E24 tenant `published`/`shared` controls that also live on these
// pages ("Listed"/"Delisted" here vs "Published"/"Private"/"Shared" there) — they are different
// features and must never read as one. `cta: null` hides the button (a second create while a
// request is pending is a 409 publish_conflict).
export const MARKETPLACE_STATE_UI: Record<PublishState, { label: string; cls: string; cta: string | null }> = {
  never: { label: 'Not listed', cls: 'bg-slate-100 text-slate-500', cta: 'Publish to marketplace' },
  pending: { label: 'In review', cls: 'bg-amber-50 text-amber-700', cta: null },
  rejected: { label: 'Declined', cls: 'bg-red-50 text-red-700', cta: 'Revise and resubmit' },
  published: { label: 'Listed', cls: 'bg-emerald-50 text-emerald-700', cta: 'Update datasheet' },
  unpublished: { label: 'Delisted', cls: 'bg-slate-100 text-slate-500', cta: 'Publish to marketplace' },
};

// GET /publish-requests/product/{type}/{id} answers 404 for BOTH "never requested" and "not your
// tenant" with one byte-identical literal, so a 404 is never an error to show — it is the 'never'
// state. Anything else is a real load failure. Takes the already-extracted Error.message (the
// axios interceptor puts the backend `detail` there) so both panels test it identically.
export function isMissingPublishRequest(errorMessage: string): boolean {
  return /not found/i.test(errorMessage) || /404/.test(errorMessage);
}
