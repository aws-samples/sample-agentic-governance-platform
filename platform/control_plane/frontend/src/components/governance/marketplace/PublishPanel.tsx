import { useState } from 'react';
import { marketplaceApi } from '../../../api/client';
import { isApiFailure } from '../../../api/failure';
import type { LifecycleState, MarketplacePublication, ProductType, PublishRequest } from '../../../api/client';
import { useLoad } from '../../../api/useLoad';
import { formatDate } from '../../operations/opsUi';
import ListingStrip from '../ListingStrip';
import { listingSteps } from '../listingSteps';
import {
  EMPTY_DATASHEET_DRAFT,
  MARKETPLACE_STATE_UI,
  buildDatasheet,
  draftFromDatasheet,
  isMissingPublishRequest,
  listingStaysLive,
  publishStateLabel,
  validateDatasheet,
} from '../publishForm';
import type { DatasheetDraft } from '../publishForm';

export interface PublishPanelCopy {
  intro: string;
  never: string;
  pitchPlaceholder: string;
}

const CARD = 'bg-white/70 backdrop-blur rounded-xl border border-slate-200/60 shadow-sm';

function DraftField({
  label,
  field,
  draft,
  onChange,
  placeholder,
  required,
}: {
  label: string;
  field: keyof DatasheetDraft;
  draft: DatasheetDraft;
  onChange: (field: keyof DatasheetDraft, value: string) => void;
  placeholder: string;
  required?: boolean;
}) {
  const id = `datasheet-${field}`;
  return (
    <div>
      <label htmlFor={id} className="text-xs font-medium text-slate-600">
        {label}{required ? ' *' : ''}
      </label>
      <input
        id={id}
        type="text"
        value={draft[field]}
        onChange={(e) => onChange(field, e.target.value)}
        placeholder={placeholder}
        className="mt-1 w-full px-3 py-2 text-sm rounded-lg border border-slate-300 focus:outline-none focus:ring-2 focus:ring-blue-500/40"
      />
    </div>
  );
}

export default function PublishPanel({
  productType,
  productId,
  lifecycle,
  marketplace,
  blocker,
  copy,
  roleLevel,
}: {
  productType: ProductType;
  productId: string;
  lifecycle: LifecycleState;
  marketplace: MarketplacePublication | null;
  blocker: string | null;
  copy: PublishPanelCopy;
  roleLevel: number;
}) {
  const eligible = roleLevel >= 1 && lifecycle === 'approved';

  const requestLoad = useLoad(
    () =>
      eligible
        ? marketplaceApi.publishRequestForProduct(productType, productId).catch((err: unknown) => {
            if (isApiFailure(err) && isMissingPublishRequest(err)) return null;
            throw err;
          })
        : Promise.resolve<PublishRequest | null>(null),
    [productType, productId, eligible],
    'Failed to load the marketplace publication.',
  );
  const request = requestLoad.state.data;
  const loading = requestLoad.state.kind === 'loading';
  const loadError = requestLoad.state.kind === 'error' ? requestLoad.state.message : null;

  const [formOpen, setFormOpen] = useState(false);
  const [draft, setDraft] = useState<DatasheetDraft>(EMPTY_DATASHEET_DRAFT);
  const [formErrors, setFormErrors] = useState<string[]>([]);
  const [submitPending, setSubmitPending] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);

  if (roleLevel < 1) return null;

  const state = publishStateLabel(request, marketplace);
  const ui = MARKETPLACE_STATE_UI[state];
  const staysLive = listingStaysLive(state, marketplace);
  const guide = listingSteps({
    lifecycle,
    publishState: !eligible || loading || loadError ? null : state,
    staysLive,
    blocker,
  });

  const openForm = () => {
    setDraft(draftFromDatasheet(request?.datasheet ?? marketplace?.datasheet));
    setFormErrors([]);
    setSubmitError(null);
    setFormOpen(true);
  };

  const cancelForm = () => {
    setFormOpen(false);
    setDraft(EMPTY_DATASHEET_DRAFT);
    setFormErrors([]);
    setSubmitError(null);
  };

  const onField = (field: keyof DatasheetDraft, value: string) =>
    setDraft((d) => ({ ...d, [field]: value }));

  const submitDatasheet = async () => {
    const errors = validateDatasheet(draft);
    setFormErrors(errors);
    if (errors.length > 0) return;
    setSubmitPending(true);
    setSubmitError(null);
    try {
      await marketplaceApi.createPublishRequest({
        product_type: productType,
        product_id: productId,
        datasheet: buildDatasheet(draft),
      });
      setFormOpen(false);
      setDraft(EMPTY_DATASHEET_DRAFT);
      requestLoad.reload();
    } catch (err: unknown) {
      setSubmitError(err instanceof Error ? err.message : 'Publish request failed.');
    } finally {
      setSubmitPending(false);
    }
  };

  const intro = (
    <div className="min-w-0">
      <h2 className="text-sm font-semibold text-slate-800">Marketplace</h2>
      <p className="text-xs text-slate-500 mt-0.5">
        {copy.intro}
      </p>
    </div>
  );
  const strip = (
    <ListingStrip steps={guide.steps} next={guide.next} onCta={openForm} isAdmin={roleLevel >= 2} />
  );

  if (!eligible || blocker) {
    return (
      <div className={`${CARD} p-5`}>
        {intro}
        {strip}
      </div>
    );
  }

  return (
    <div className={`${CARD} p-5`}>
      <div className="flex items-start justify-between gap-3 flex-wrap">
        {intro}
        <span className={`inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium ${ui.cls}`}>
          {ui.label}
        </span>
      </div>
      {!formOpen && strip}

      <div className="mt-3 text-sm text-slate-700">
        {loading ? (
          <span className="text-slate-400">Loading marketplace publication…</span>
        ) : loadError ? (
          <span className="text-red-600" role="alert">{loadError}</span>
        ) : state === 'pending' ? (
          <span>
            {staysLive
              ? 'Datasheet update in review — awaiting admin review. The current listing remains live.'
              : 'Datasheet submitted — awaiting admin review.'}
          </span>
        ) : state === 'rejected' ? (
          <span>
            {staysLive
              ? 'An admin declined this datasheet update. The current listing remains live.'
              : 'An admin declined this datasheet.'}{' '}
            {request?.decision_reason ? (
              <span className="text-slate-600">Reason: {request.decision_reason}</span>
            ) : (
              <span className="text-slate-400">No reason was recorded.</span>
            )}
          </span>
        ) : state === 'published' ? (
          <span>Live in the marketplace since {formatDate(marketplace?.declared_at)}.</span>
        ) : state === 'unpublished' ? (
          <span>An admin removed this card from the marketplace. The declared datasheet is kept.</span>
        ) : (
          <span className="text-slate-500">{copy.never}</span>
        )}
      </div>

      {!loading && !loadError && ui.cta && !formOpen && guide.next.kind !== 'cta' && (
        <button
          type="button"
          onClick={openForm}
          className="mt-3 px-3.5 py-1.5 rounded-lg bg-white border border-slate-300 text-slate-700 text-sm font-medium hover:bg-slate-50 transition-colors"
        >
          {ui.cta}
        </button>
      )}

      {formOpen && (
        <div className={`${CARD} mt-3 p-4`}>
          <p className="text-xs font-medium text-slate-600">
            Declared datasheet — publisher-asserted, shown on the marketplace card.
          </p>
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-x-6 gap-y-3 mt-3">
            <DraftField label="Owner team" field="owner_team" draft={draft} onChange={onField} placeholder="Claims Platform" required />
            <DraftField label="Support contact" field="support_contact" draft={draft} onChange={onField} placeholder="claims-support@example.com" required />
            <DraftField label="Data classification" field="data_classification" draft={draft} onChange={onField} placeholder="internal" required />
            <DraftField label="SLA tier" field="sla_tier" draft={draft} onChange={onField} placeholder="gold" />
            <DraftField label="Support hours" field="support_hours" draft={draft} onChange={onField} placeholder="09:00–17:00 CET" />
            <DraftField label="Version" field="version" draft={draft} onChange={onField} placeholder="1.2.0" />
            <DraftField label="Region" field="region" draft={draft} onChange={onField} placeholder="eu-central-1" />
            <DraftField label="Compliance (comma-separated)" field="compliance" draft={draft} onChange={onField} placeholder="GDPR, SOC2" />
            <DraftField label="Guardrails (comma-separated)" field="guardrails" draft={draft} onChange={onField} placeholder="pii-redaction, no-pricing" />
          </div>

          <div className="mt-3">
            <label htmlFor="datasheet-pitch" className="text-xs font-medium text-slate-600">Pitch</label>
            <textarea
              id="datasheet-pitch"
              value={draft.pitch}
              onChange={(e) => onField('pitch', e.target.value)}
              rows={2}
              className="mt-1 w-full px-3 py-2 text-sm rounded-lg border border-slate-300 focus:outline-none focus:ring-2 focus:ring-blue-500/40"
              placeholder={copy.pitchPlaceholder}
            />
          </div>

          {formErrors.length > 0 && (
            <ul className="mt-3 text-sm text-red-600 list-disc list-inside" role="alert">
              {formErrors.map((msg) => <li key={msg}>{msg}</li>)}
            </ul>
          )}

          <div className="flex items-center gap-2 mt-3">
            <button
              type="button"
              onClick={submitDatasheet}
              disabled={submitPending}
              className="px-3.5 py-1.5 rounded-lg bg-blue-600 text-white text-sm font-medium hover:bg-blue-700 transition-colors disabled:opacity-50"
            >
              {submitPending ? 'Working…' : 'Send for admin review'}
            </button>
            <button
              type="button"
              onClick={cancelForm}
              disabled={submitPending}
              className="px-3.5 py-1.5 rounded-lg bg-white border border-slate-300 text-slate-700 text-sm font-medium hover:bg-slate-50 transition-colors disabled:opacity-50"
            >
              Cancel
            </button>
          </div>

          {submitError && <p className="text-sm text-red-600 mt-3" role="alert">{submitError}</p>}
        </div>
      )}
    </div>
  );
}
