/**
 * No component hands focus to an element on a zero timeout. A `setTimeout(() => ref.current?.focus(), 0)`
 * races the commit it waits for: when the close follows an awaited request, React renders it in a later
 * task, the timeout fires first, the trigger is still unmounted and the focus call is a no-op, so focus
 * falls to the body after the list re-renders. `useReturnFocus` focuses in an effect of the commit that
 * mounts the trigger again. The check reads every non-test `.ts`/`.tsx` under `src/components/`.
 */
import { describe, expect, it } from 'vitest';

const sources = import.meta.glob<string>('/src/components/**/*.{ts,tsx}', {
  query: '?raw',
  import: 'default',
  eager: true,
});

const FOCUS_ON_TIMEOUT =
  /(?:window\.)?setTimeout\(\s*\(\)\s*=>\s*(?:\{\s*)?[\w.]+\.current(?:\?|!)?\.focus\([^)]*\)\s*;?\s*\}?(?:\s*,\s*0)?\s*\)/;

function offenders(files: Record<string, string>): string[] {
  return Object.entries(files)
    .filter(([path]) => !/\.test\.tsx?$/.test(path))
    .filter(([, text]) => FOCUS_ON_TIMEOUT.test(text))
    .map(([path]) => path)
    .sort();
}

describe('focus return', () => {
  it('no component focuses an element on a zero timeout', () => {
    const problems = offenders(sources).map(
      (path) =>
        `${path} focuses an element on a zero timeout, which fires before an async close commits; use useReturnFocus from components/shared/useReturnFocus.ts instead.`,
    );
    expect(problems, problems.join('\n')).toEqual([]);
  }, 30_000);

  it('the pattern names both arrow body forms, with and without window', () => {
    expect(
      offenders({
        'expr.tsx': 'window.setTimeout(() => addTriggerRef.current?.focus(), 0);',
        'block.tsx': 'setTimeout(() => { promoteTriggerRef.current?.focus(); }, 0);',
        'later.tsx': 'window.setTimeout(() => addTriggerRef.current?.focus(), 200);',
        'hook.tsx': 'const returnFocus = useReturnFocus(addTriggerRef);',
        'flow.test.ts': 'setTimeout(() => ref.current?.focus(), 0);',
        'nodelay.tsx': 'window.setTimeout(() => addTriggerRef.current?.focus());',
        'bang.tsx': 'setTimeout(() => addTriggerRef.current!.focus(), 0);',
        'bare.tsx': 'setTimeout(() => addTriggerRef.current.focus(), 0);',
        'options.tsx': 'window.setTimeout(() => addTriggerRef.current?.focus({ preventScroll: true }), 0);',
      }),
    ).toEqual(['bang.tsx', 'bare.tsx', 'block.tsx', 'expr.tsx', 'nodelay.tsx', 'options.tsx']);
  });

  it('the scan saw the access tab', () => {
    expect(Object.keys(sources)).toContain('/src/components/governance/AccessTab.tsx');
  });
});
