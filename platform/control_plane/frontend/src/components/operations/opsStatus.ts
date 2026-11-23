// Decision: docs/adr/frontend-two-status-machines.md

import type { Repository, RuntimeStatus } from '../../api/client';
import type { OPS_BADGE } from './opsUi';

type BadgeKey = keyof typeof OPS_BADGE;

export type CicdStatus = Repository['cicd_status'];

export type RuntimeStatusKey = RuntimeStatus['status'];

export const CICD_BADGE_KEY: Record<CicdStatus, BadgeKey> = {
  provisioning: 'provisioning',
  promoting: 'provisioning',
  ready: 'pending',
  failed: 'failed',
  deployed: 'ready',
  unknown: 'unknown',
};

export const CICD_LABEL: Record<CicdStatus, string> = {
  provisioning: 'Provisioning',
  ready: 'Not built yet',
  promoting: 'Deploying…',
  deployed: 'Deployed',
  failed: 'Delivery failed',
  unknown: 'No status reported',
};

export const RUNTIME_BADGE_KEY: Record<RuntimeStatusKey, BadgeKey> = {
  ready: 'ready',
  creating: 'provisioning',
  updating: 'provisioning',
  failed: 'failed',
  not_deployed: 'unknown',
  unknown: 'unknown',
};

export const RUNTIME_LABEL: Record<RuntimeStatusKey, string> = {
  ready: 'Ready',
  creating: 'Creating…',
  updating: 'Updating…',
  failed: 'Runtime failed',
  not_deployed: 'Not deployed',
  unknown: 'Runtime unknown',
};

export function isCicdInFlight(status: CicdStatus): boolean {
  return status === 'provisioning' || status === 'promoting';
}
