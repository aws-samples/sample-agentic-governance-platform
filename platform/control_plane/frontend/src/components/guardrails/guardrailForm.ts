import type {
  ContentFilterConfigOutput,
  ContextualGroundingConfigOutput,
  DeniedTopicOutput,
  GuardrailTemplateCreate,
  PiiEntityConfigOutput,
  WordFilterConfigOutput,
} from '../../api/client';

export interface GuardrailFormValues {
  name: string;
  description: string;
  tenantId: string;
  contentFilters: ContentFilterConfigOutput[];
  piiEntities: PiiEntityConfigOutput[];
  deniedTopics: DeniedTopicOutput[];
  wordFilter: WordFilterConfigOutput;
  contextualGrounding: ContextualGroundingConfigOutput;
}

export function canCreateGuardrail(name: string, tenantId: string): boolean {
  return name.trim().length > 0 && tenantId.trim().length > 0;
}

export function guardrailCreatePayload(form: GuardrailFormValues): GuardrailTemplateCreate {
  return {
    name: form.name.trim(),
    tenant_id: form.tenantId.trim(),
    description: form.description.trim() || undefined,
    content_filters: form.contentFilters,
    denied_topics: form.deniedTopics,
    pii_entities: form.piiEntities,
    sensitive_regexes: [],
    word_filter: form.wordFilter,
    contextual_grounding: form.contextualGrounding,
  };
}
