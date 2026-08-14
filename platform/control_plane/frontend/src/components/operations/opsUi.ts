/**
 * Operations content-identity tokens (Epic 18).
 *
 * The emerald-tinted counterpart to the governance pages' local `CARD`/badge
 * literals. The Operations section runs a dark-slate sidebar with emerald
 * accents over an emerald/teal-tinted page background (see operationsTheme.ts
 * SECTION_THEME.operations); these tokens give every Ops page a matching
 * warmer/greener content identity so the body feels of-a-piece with the shell.
 *
 * Every later Operations page imports these verbatim instead of redeclaring a
 * local card/badge/table literal — keep the names and shapes stable.
 */

/** Emerald-tinted glass card primitive — the Ops replacement for the governance white `CARD`. */
export const OPS_CARD =
  'bg-white/80 backdrop-blur rounded-xl border border-emerald-200/50 shadow-sm';

/** Page `<h1>` classes — large readable heading; emerald identity comes from cards/badges/accents, not the H1 text color. */
export const OPS_HEADING = 'text-3xl font-semibold tracking-tight text-slate-900';

/** Subtitle `<p>` classes sitting under the heading. */
export const OPS_SUBTITLE = 'text-slate-500 mt-1 max-w-2xl';

/**
 * Status pill classes keyed by the statuses used across the epic. Shares the
 * governance pill shape (inline-flex items-center gap-1.5 text-[11px]
 * font-semibold px-2 py-0.5 rounded-full) — that shape is applied by the caller;
 * each value here is the color pair only.
 *  - emerald: healthy / done (ready, ok, running)
 *  - amber:   in-flight / attention (provisioning, warn, pending)
 *  - rose:    failure (failed)
 *  - slate:   NOT KNOWN (unknown) — see below
 *
 * `unknown` (E28/T10) is the palette's only NEUTRAL tint, and it exists because the
 * other three all make a claim. Both status machines the repo row renders
 * (`cicd_status` and the runtime's health) carry a mandatory `unknown` member, for the
 * same reason: an unreachable control plane, or a wire value no writer in this codebase
 * produces, is NOT a broken runtime and NOT a healthy one. Rendering that in rose would
 * report a failure nobody observed; rendering it in amber would make it
 * indistinguishable from `provisioning`, which is precisely the confusion this task
 * exists to remove (a repo live in prod once wore provisioning's amber). Slate says
 * "no answer" — the only honest thing to say.
 */
export const OPS_BADGE: Record<
  'ready' | 'provisioning' | 'ok' | 'warn' | 'failed' | 'running' | 'pending' | 'unknown',
  string
> = {
  ready: 'bg-emerald-50 text-emerald-700',
  provisioning: 'bg-amber-50 text-amber-700',
  ok: 'bg-emerald-50 text-emerald-700',
  warn: 'bg-amber-50 text-amber-700',
  failed: 'bg-rose-50 text-rose-700',
  running: 'bg-emerald-50 text-emerald-700',
  pending: 'bg-amber-50 text-amber-700',
  unknown: 'bg-slate-100 text-slate-500',
};

/** Emerald primary button classes — matches the existing Ops "New deployment" button. */
export const OPS_PRIMARY_BTN =
  'px-3.5 py-1.5 rounded-lg bg-emerald-600 text-white text-sm font-medium hover:bg-emerald-700 transition-colors';

/** Table `<thead>` classes — emerald-tinted header band. */
export const OPS_TABLE_HEAD = 'bg-emerald-50/60 text-slate-500 text-xs uppercase tracking-wide';

/** Table `<tbody>` row-divider classes — emerald-tinted dividers. */
export const OPS_TABLE_DIVIDE = 'divide-y divide-emerald-100/70';
