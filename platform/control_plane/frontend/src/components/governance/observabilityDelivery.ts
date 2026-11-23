import type { Platform } from '../../api/client';
import {
  platformSupportsAutoTraceDelivery,
  type TraceDelivery,
} from './agentRegistrationWizardModel';

export const OBSERVABILITY_HEADING = 'Trace delivery';

export const AUTO_DELIVERY_NOTE =
  'Traces reach this agent’s Langfuse project automatically — AGP injects the OpenTelemetry ' +
  'endpoint on the AgentCore runtime it manages. Nothing to wire.';

export const MANUAL_DELIVERY_INTRO =
  'AGP provisioned this agent’s Langfuse project, but it does not manage the runtime, so it ' +
  'cannot deliver traces for you. Point the agent’s OpenTelemetry exporter at the values below; ' +
  'the keys live in the named secret (read its value from Secrets Manager — AGP never shows it).';

export const MANUAL_NO_PROJECT_NOTE =
  'No Langfuse project is provisioned for this agent yet, so there is nothing to wire to. It is ' +
  'created best-effort at registration and on the next reprovision.';

export const AUTO_NO_PROJECT_NOTE =
  'No Langfuse project is provisioned for this agent yet, so there is nothing to deliver traces ' +
  'to. It is created best-effort at registration and on the next reprovision.';

export const FIELD_LABELS = {
  host: 'Langfuse host',
  project: 'Project',
  secret: 'Key secret (read the value from Secrets Manager)',
} as const;

export function autoDeliveryHasProject(langfuseProjectId: string | null | undefined): boolean {
  return !!langfuseProjectId;
}

export function observabilityDelivery(
  platform: Platform | undefined | null,
  traceDelivery: TraceDelivery | null | undefined,
): TraceDelivery {
  if (!platform || !platformSupportsAutoTraceDelivery(platform)) return 'manual';
  return traceDelivery === 'manual' ? 'manual' : 'auto';
}
