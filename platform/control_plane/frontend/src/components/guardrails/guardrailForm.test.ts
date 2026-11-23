import { describe, it, expect } from 'vitest';
import { canCreateGuardrail, guardrailCreatePayload, type GuardrailFormValues } from './guardrailForm';

const form = (over: Partial<GuardrailFormValues> = {}): GuardrailFormValues => ({
  name: '  FSI Production Guardrail  ',
  description: '',
  tenantId: 't-retail',
  contentFilters: [{ type: 'HATE', input_strength: 'HIGH', output_strength: 'HIGH' }],
  piiEntities: [{ type: 'EMAIL', action: 'ANONYMIZE' }],
  deniedTopics: [{ name: 'Insider trading', definition: 'Non-public material information', examples: [] }],
  wordFilter: { enable_profanity: true, blocked_words: ['foo'] },
  contextualGrounding: { enabled: false, grounding_threshold: 0.7, relevance_threshold: 0.7 },
  ...over,
});

describe('canCreateGuardrail', () => {
  it('needs a name and a tenant', () => {
    expect(canCreateGuardrail('', 't-retail')).toBe(false);
    expect(canCreateGuardrail('   ', 't-retail')).toBe(false);
    expect(canCreateGuardrail('FSI Guardrail', '')).toBe(false);
    expect(canCreateGuardrail('FSI Guardrail', '  ')).toBe(false);
    expect(canCreateGuardrail('FSI Guardrail', 't-retail')).toBe(true);
  });
});

describe('guardrailCreatePayload', () => {
  it('carries tenant_id and the trimmed name', () => {
    const payload = guardrailCreatePayload(form());
    expect(payload.tenant_id).toBe('t-retail');
    expect(payload.name).toBe('FSI Production Guardrail');
  });

  it('sends sensitive_regexes as an empty list', () => {
    expect(guardrailCreatePayload(form()).sensitive_regexes).toEqual([]);
  });

  it('drops a blank description and trims a written one', () => {
    expect(guardrailCreatePayload(form({ description: '   ' })).description).toBeUndefined();
    expect(guardrailCreatePayload(form({ description: ' Blocks PII ' })).description).toBe('Blocks PII');
  });

  it('forwards every configured layer unchanged', () => {
    const values = form();
    const payload = guardrailCreatePayload(values);
    expect(payload.content_filters).toBe(values.contentFilters);
    expect(payload.pii_entities).toBe(values.piiEntities);
    expect(payload.denied_topics).toBe(values.deniedTopics);
    expect(payload.word_filter).toBe(values.wordFilter);
    expect(payload.contextual_grounding).toBe(values.contextualGrounding);
  });
});
