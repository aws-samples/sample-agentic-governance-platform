import { describe, expect, it } from 'vitest';
import { PROMOTE_CANCEL_BTN, PROMOTE_CONFIRM_BTN } from './promoteConfirmModel';

describe('promoteConfirmModel buttons', () => {
  it("PROMOTE_CONFIRM_BTN is emerald, the product's one production-affecting verb, and never an alarm tint", () => {
    expect(PROMOTE_CONFIRM_BTN).toMatch(/bg-emerald-\d+/);
    expect(PROMOTE_CONFIRM_BTN).not.toMatch(/rose|red|amber/);
  });
  it('PROMOTE_CANCEL_BTN is plainly secondary: white, bordered, slate text, so the pair never reads as two equal choices', () => {
    expect(PROMOTE_CANCEL_BTN).toMatch(/bg-white/);
    expect(PROMOTE_CANCEL_BTN).toMatch(/border-slate-\d+/);
    expect(PROMOTE_CANCEL_BTN).toMatch(/text-slate-\d+/);
    expect(PROMOTE_CANCEL_BTN).not.toMatch(/emerald|rose|red|amber/);
  });
});
