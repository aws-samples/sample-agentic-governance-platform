/**
 * Honesty copy — the platform's not-yet-real vocabulary (Epic 31F, Task 1).
 *
 * A pure constants module with NO react import, deliberately: the strings are a
 * cross-task contract (T2–T6 and T8 render them on real pages) and freezing them
 * needs a test, but the frontend's vitest runs node-env with an
 * `src/**\/*.test.ts`-only include pattern — nothing that imports react can be
 * covered there. Splitting the strings out of `comingSoon.tsx` is what makes the
 * contract testable at all; see `comingSoonCopy.test.ts`.
 *
 * These four strings are PINNED. Consumers import them instead of retyping the
 * wording, so one page cannot quietly disagree with another about how honest the
 * platform is being. Changing a value here changes every surface at once — which
 * is the point, and why the test asserts exact equality.
 */

/** Banner title — bold lead line of `ComingSoonBanner`. */
export const COMING_SOON_TITLE = 'Coming soon';

/** Banner body — short and plain: what you see is an example design, not the feature. */
export const COMING_SOON_BODY = 'Example design — not functional yet.';

/** Inline pill label marking one illustrative widget inside an otherwise-live page. */
export const SAMPLE_BADGE_LABEL = 'Sample data';

/** Nav-row tag label — lowercase source string; the tag renders it uppercase via CSS. */
export const SOON_TAG_LABEL = 'soon';
