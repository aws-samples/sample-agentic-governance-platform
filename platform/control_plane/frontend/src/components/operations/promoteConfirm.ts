// promoteConfirm.ts — the promote dialog's class tables (E28C/T7, D-C4d).
//
// The `.ts` companion to `PromoteConfirm.tsx`, and it exists for two reasons that happen to point
// the same way.
//
// 1. THE TONE TABLE IS A JUDGEMENT WEARING CLASSES, so a test must be able to hold it directly.
//    `ARTIFACT_TONE_CLS` answers "how does this surface show that an approval names exact BYTES
//    rather than a mutable pointer?" — the epic's headline distinction, at the moment production is
//    authorised. While it lived in the `.tsx`, the only way to check its entries was to slice raw
//    source and regex the slice, which is precisely the guard shape this project has now found
//    vacuous nine times. Here it is IMPORTED and indexed, so a wrong tint is a failing assertion on
//    a real value rather than a pattern that may or may not have matched the right window.
//
//    `opsUi.ts` is the precedent: a pure module MAY hold class strings when the mapping itself is
//    the thing that must not drift (`OPS_BADGE` is exactly this). The rule it must not break is the
//    other one — a pure module never picks COPY, and no copy appears here. Text, tooltip and tone
//    KEY all come from `promotionArtifact`.
//
// 2. It keeps the component file exporting only its component, which is what
//    `react-refresh/only-export-components` asks for.
//
// THERE WAS ONE COPY OF THIS TABLE PER SURFACE BEFORE THIS TASK — one in the project tab's inline
// dialog, one in the detail page's field grid — each defensible under "a surface owns its class
// strings", and together the same two-tables-for-one-decision shape that once shipped a live
// production repository wearing provisioning's amber. Now there is one.

import type { ArtifactMarker } from './repositoryDetailTabs';

/**
 * The approvable artifact's tone → its classes. `Record<ArtifactMarker['tone'], string>` with NO
 * default branch, so a third tone later is a `tsc` error naming this table rather than a marker
 * silently inheriting whichever side of a ternary it happens to fall on (how `SHOW_FILTER_STRIP`
 * broke once).
 *
 * `caution` is AMBER — the `attention` weight `REPO_ACTION_TONE` gives a request rather than a
 * fault. A tag-only candidate is a known, accepted, self-healing state, and rose here would tell an
 * operator their repository is broken when what they need to know is that this ONE APPROVAL is
 * weaker than the surface otherwise promises. It must never read as an error and never block.
 *
 * `pinned` is a NEUTRAL MONO CHIP: a digest is the normal, correct case and must not look like a
 * status at all. The chip idiom (bordered, slate) is the one that survived the collapse of the two
 * copies, because it is legible in both places the marker renders — beside a commit sha in the
 * dialog, and inside the detail page's definition list.
 */
export const ARTIFACT_TONE_CLS: Record<ArtifactMarker['tone'], string> = {
  pinned:
    'inline-flex w-fit items-center px-2 py-0.5 rounded-md bg-slate-100 border border-slate-200 font-mono text-xs text-slate-700 break-all',
  caution:
    'inline-flex w-fit items-center gap-1.5 px-2 py-0.5 rounded-md bg-amber-50 border border-amber-200 text-[11px] font-semibold text-amber-800',
};

/**
 * The confirm's primary button — emerald, for the product's one production-affecting verb.
 *
 * Weighty but not loud. This is the single most consequential control in the product and it should
 * read as deliberate; it should NOT read as urgent, because nothing about a normal promotion is an
 * emergency and an alarming button teaches an operator to brace rather than to read.
 */
export const PROMOTE_CONFIRM_BTN =
  'px-2.5 py-1 rounded-md bg-emerald-600 text-white text-xs font-medium hover:bg-emerald-700 transition-colors disabled:opacity-40';

/** Cancel — plainly secondary, so the pair does not present two equal-weight choices. */
export const PROMOTE_CANCEL_BTN =
  'px-2.5 py-1 rounded-md bg-white border border-slate-300 text-slate-700 text-xs font-medium hover:bg-slate-50 transition-colors disabled:opacity-40';
