import { describe, expect, it } from 'vitest';
import { autoDeliveryHasProject, observabilityDelivery } from './observabilityDelivery';
import type { Platform } from '../../api/client';

describe('observabilityDelivery', () => {
  it('is "auto" on aws_bedrock at the default or absent choice: AGP wires delivery itself on an auto-capable platform', () => {
    expect(observabilityDelivery('aws_bedrock', 'auto')).toBe('auto');
    expect(observabilityDelivery('aws_bedrock', null)).toBe('auto');
    expect(observabilityDelivery('aws_bedrock', undefined)).toBe('auto');
  });

  it('is "manual" on aws_bedrock when the operator opted out, so the panel shows the wiring instructions', () => {
    expect(observabilityDelivery('aws_bedrock', 'manual')).toBe('manual');
  });

  it('is always "manual" on databricks, whatever the choice says, since it cannot auto-deliver', () => {
    expect(observabilityDelivery('databricks', 'auto')).toBe('manual');
    expect(observabilityDelivery('databricks', null)).toBe('manual');
    expect(observabilityDelivery('databricks', 'manual')).toBe('manual');
  });

  it('is "manual" for every inventory-only platform and for an absent platform: nothing governable to wire', () => {
    for (const p of ['azure', 'salesforce', 'sap', 'google', 'on_prem', 'other'] as Platform[]) {
      expect(observabilityDelivery(p, 'auto')).toBe('manual');
    }
    expect(observabilityDelivery(null, 'auto')).toBe('manual');
    expect(observabilityDelivery(undefined, 'auto')).toBe('manual');
  });
});

describe('autoDeliveryHasProject: independent of whether the platform can auto-deliver', () => {
  it('is false for a missing or empty project id: best-effort provisioning can miss, and "delivered automatically" would then be false', () => {
    expect(autoDeliveryHasProject(null)).toBe(false);
    expect(autoDeliveryHasProject(undefined)).toBe(false);
    expect(autoDeliveryHasProject('')).toBe(false);
  });

  it('is true once a project id is provisioned', () => {
    expect(autoDeliveryHasProject('clx1')).toBe(true);
  });
});
