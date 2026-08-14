import { useState } from 'react';
import type { PiiEntityConfig, GuardrailPiiAction } from '../../types';

const PII_GROUPS = {
  Financial: [
    { type: 'CREDIT_DEBIT_CARD_NUMBER', label: 'Credit/Debit Card Number' },
    { type: 'CREDIT_DEBIT_CARD_CVV', label: 'Card CVV' },
    { type: 'CREDIT_DEBIT_CARD_EXPIRY', label: 'Card Expiry' },
    { type: 'PIN', label: 'PIN' },
    { type: 'SWIFT_CODE', label: 'SWIFT Code' },
    { type: 'INTERNATIONAL_BANK_ACCOUNT_NUMBER', label: 'IBAN' },
  ],
  Personal: [
    { type: 'NAME', label: 'Name' },
    { type: 'EMAIL', label: 'Email' },
    { type: 'PHONE', label: 'Phone' },
    { type: 'ADDRESS', label: 'Address' },
    { type: 'AGE', label: 'Age' },
    { type: 'US_SOCIAL_SECURITY_NUMBER', label: 'SSN' },
    { type: 'US_PASSPORT_NUMBER', label: 'Passport' },
    { type: 'DRIVER_ID', label: 'Driver ID' },
  ],
  Technical: [
    { type: 'IP_ADDRESS', label: 'IP Address' },
    { type: 'MAC_ADDRESS', label: 'MAC Address' },
    { type: 'URL', label: 'URL' },
    { type: 'USERNAME', label: 'Username' },
    { type: 'PASSWORD', label: 'Password' },
    { type: 'AWS_ACCESS_KEY', label: 'AWS Access Key' },
    { type: 'AWS_SECRET_KEY', label: 'AWS Secret Key' },
  ],
};

interface Props {
  entities: PiiEntityConfig[];
  onChange: (entities: PiiEntityConfig[]) => void;
}

export default function PiiDetectionPanel({ entities, onChange }: Props) {
  const [expandedGroup, setExpandedGroup] = useState<string | null>('Financial');

  const isSelected = (type: string) => entities.some((e) => e.type === type);
  const getAction = (type: string): GuardrailPiiAction => entities.find((e) => e.type === type)?.action || 'ANONYMIZE';

  const toggleEntity = (type: string) => {
    if (isSelected(type)) {
      onChange(entities.filter((e) => e.type !== type));
    } else {
      onChange([...entities, { type, action: 'ANONYMIZE' }]);
    }
  };

  const setAction = (type: string, action: GuardrailPiiAction) => {
    onChange(entities.map((e) => (e.type === type ? { ...e, action } : e)));
  };

  const selectAllInGroup = (group: string) => {
    const types = PII_GROUPS[group as keyof typeof PII_GROUPS].map((e) => e.type);
    const allSelected = types.every((t) => isSelected(t));
    if (allSelected) {
      onChange(entities.filter((e) => !types.includes(e.type)));
    } else {
      const newEntities = [...entities.filter((e) => !types.includes(e.type))];
      types.forEach((t) => newEntities.push({ type: t, action: 'ANONYMIZE' }));
      onChange(newEntities);
    }
  };

  return (
    <div className="space-y-3">
      {Object.entries(PII_GROUPS).map(([group, items]) => {
        const allSelected = items.every((item) => isSelected(item.type));
        const someSelected = items.some((item) => isSelected(item.type));
        const isExpanded = expandedGroup === group;

        return (
          <div key={group} className="border border-slate-200 rounded-xl overflow-hidden">
            <button
              onClick={() => setExpandedGroup(isExpanded ? null : group)}
              className="w-full flex items-center justify-between px-4 py-3 bg-slate-50/50 hover:bg-slate-50 transition-colors"
            >
              <div className="flex items-center gap-3">
                <input
                  type="checkbox"
                  checked={allSelected}
                  ref={(el) => { if (el) el.indeterminate = someSelected && !allSelected; }}
                  onChange={() => selectAllInGroup(group)}
                  onClick={(e) => e.stopPropagation()}
                  className="w-4 h-4 rounded border-slate-300 text-blue-600 focus:ring-blue-500"
                />
                <span className="text-sm font-semibold text-slate-800">{group}</span>
                <span className="text-xs text-slate-400">
                  {items.filter((item) => isSelected(item.type)).length}/{items.length}
                </span>
              </div>
              <svg
                className={`w-4 h-4 text-slate-400 transition-transform ${isExpanded ? 'rotate-180' : ''}`}
                fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}
              >
                <path strokeLinecap="round" strokeLinejoin="round" d="M19 9l-7 7-7-7" />
              </svg>
            </button>
            {isExpanded && (
              <div className="px-4 py-2 space-y-1 border-t border-slate-100">
                {items.map(({ type, label }) => (
                  <div key={type} className="flex items-center justify-between py-1.5">
                    <label className="flex items-center gap-3 cursor-pointer flex-1">
                      <input
                        type="checkbox"
                        checked={isSelected(type)}
                        onChange={() => toggleEntity(type)}
                        className="w-3.5 h-3.5 rounded border-slate-300 text-blue-600 focus:ring-blue-500"
                      />
                      <span className="text-sm text-slate-700">{label}</span>
                    </label>
                    {isSelected(type) && (
                      <select
                        value={getAction(type)}
                        onChange={(e) => setAction(type, e.target.value as GuardrailPiiAction)}
                        className="text-xs px-2 py-1 rounded-lg border border-slate-200 bg-white text-slate-600"
                      >
                        <option value="ANONYMIZE">Anonymize</option>
                        <option value="BLOCK">Block</option>
                      </select>
                    )}
                  </div>
                ))}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}
