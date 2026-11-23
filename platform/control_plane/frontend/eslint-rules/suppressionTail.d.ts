import type { Rule } from 'eslint';

export declare function checkTail(
  justification: string,
  today: Date,
): 'sayWhy' | 'bandAid' | 'expired' | 'tooFar' | null;

declare const suppressionTail: Rule.RuleModule;
export default suppressionTail;
