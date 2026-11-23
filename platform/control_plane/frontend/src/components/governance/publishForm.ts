import type {
  MarketplacePublication,
  PublishRequest,
  DatasheetInput,
} from '../../api/client';
import type {
  ApiFailure,
} from '../../api/failure';

export type DatasheetDraft = Record<keyof DatasheetInput, string>;

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

function orNull(value: string): string | null {
  const trimmed = value.trim();
  return trimmed ? trimmed : null;
}

function commaList(value: string): string[] {
  return value
    .split(',')
    .map((entry) => entry.trim())
    .filter((entry) => entry.length > 0);
}

export function buildDatasheet(d: DatasheetDraft): DatasheetInput {
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

export function draftFromDatasheet(ds: DatasheetInput | null | undefined): DatasheetDraft {
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

export function publishStateLabel(
  req: PublishRequest | null,
  block: MarketplacePublication | null | undefined,
): PublishState {
  if (req?.status === 'pending') return 'pending';
  if (req?.status === 'rejected') return 'rejected';
  if (block) return block.published ? 'published' : 'unpublished';
  return 'never';
}

export function listingStaysLive(
  state: PublishState,
  block: MarketplacePublication | null | undefined,
): boolean {
  if (state !== 'pending' && state !== 'rejected') return false;
  return block?.published === true;
}

export const MARKETPLACE_STATE_UI: Record<PublishState, { label: string; cls: string; cta: string | null }> = {
  never: { label: 'Not listed', cls: 'bg-slate-100 text-slate-500', cta: 'Publish to marketplace' },
  pending: { label: 'In review', cls: 'bg-amber-50 text-amber-700', cta: null },
  rejected: { label: 'Declined', cls: 'bg-red-50 text-red-700', cta: 'Revise and resubmit' },
  published: { label: 'Listed', cls: 'bg-emerald-50 text-emerald-700', cta: 'Update datasheet' },
  unpublished: { label: 'Delisted', cls: 'bg-slate-100 text-slate-500', cta: 'Publish to marketplace' },
};

export function isMissingPublishRequest(failure: ApiFailure): boolean {
  return failure.code === 'marketplace.not_found';
}
