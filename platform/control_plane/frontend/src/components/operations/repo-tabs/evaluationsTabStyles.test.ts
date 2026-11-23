import { afterEach, describe, expect, it, vi } from 'vitest';

import { OPS_BADGE } from '../opsUi';
import { runStatusPill } from './evaluationsTabStyles';

afterEach(() => vi.restoreAllMocks());

describe('runStatusPill', () => {
  it('labels every declared run status', () => {
    expect(runStatusPill('passed')).toEqual({ cls: OPS_BADGE.ok, label: 'Passed' });
    expect(runStatusPill('failed')).toEqual({ cls: OPS_BADGE.failed, label: 'Failed' });
    expect(runStatusPill('error')).toEqual({ cls: OPS_BADGE.failed, label: 'Error' });
    expect(runStatusPill('running')).toEqual({ cls: OPS_BADGE.pending, label: 'Running' });
  });

  it('renders the raw word for a run status outside the union, without throwing', () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => undefined);
    const pill = runStatusPill('cancelled' as never);
    expect(pill.label).toBe('cancelled');
    expect(pill.cls).toBe(OPS_BADGE.unknown);
    runStatusPill('cancelled' as never);
    expect(warn).toHaveBeenCalledTimes(1);
  });
});
