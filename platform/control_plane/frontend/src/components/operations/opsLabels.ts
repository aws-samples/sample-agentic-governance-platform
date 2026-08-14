// opsLabels.ts — the pure display-string derivations the Operations surface renders in more
// than one place (E28/T10). Pure and framework-free so vitest collects the tests that pin
// them (`src/**/*.test.ts` only), which is the whole mechanism: these three had each been
// FORKED, silently diverged, and had no test that could notice.
//
// Deliberately SEPARATE from `opsStatus.ts`. That module is a pinned cross-task contract
// (C3) about two status state machines; a provider name and a set of avatar initials are
// not status, and folding them into that file would make its name a lie — the same drift
// this task is here to stop, one level up.
//
// This module owns NO styling: like `opsStatus`, it returns strings and semantic keys, and
// each surface applies its own classes.

import type { Provider } from '../../api/client';

// ---------------------------------------------------------------------------
// providerLabel — the wire provider enum → its BRAND casing.
//
// The `{ github: 'GitHub', gitlab: 'GitLab' }` table was forked THREE times across the Ops
// surface (`TemplatesAdmin.tsx:31`, `ConnectionsAdmin.tsx:44`,
// `RolloutTemplatesModal.tsx:32`), and that is precisely why two OTHER files rendered the
// raw lowercase enum instead: neither had the table in scope, so each wrote its own line
// without it. One exported table removes both the duplication and the incentive.
//
// Takes a WIDE `string`, not `Provider`, on purpose. The connection `provider` field is a
// closed union in the client types, but the value reaching a label is often a fallback
// derived from a URL host, and a build can meet a connection created by a newer backend.
// An unknown provider therefore passes THROUGH unchanged rather than being mapped or
// blanked: echoing the raw value is honest and debuggable, whereas defaulting it to
// "GitHub" would state a provider nobody established — the same failure mode `opsStatus`
// refuses for a status.
// ---------------------------------------------------------------------------
const PROVIDER_LABEL: Record<Provider, string> = {
  github: 'GitHub',
  gitlab: 'GitLab',
};

/** The em dash the Ops tables use for "no value" — never an empty cell or pill. */
export const NO_VALUE = '—';

export function providerLabel(raw: string | null | undefined): string {
  const trimmed = (raw ?? '').trim();
  if (!trimmed) return NO_VALUE;
  const known = PROVIDER_LABEL[trimmed.toLowerCase() as Provider];
  // `?? trimmed` keeps an unrecognized provider legible instead of guessing at it. Note the
  // ORIGINAL casing is echoed, not the lowercased probe — the raw value is the evidence.
  return known ?? trimmed;
}

// ---------------------------------------------------------------------------
// orgLabel — a connection → the `Provider · org` line the Ops tables show.
//
// THE divergence this task was asked to fix: `Projects.tsx:43` and `ProjectDetail.tsx:59`
// rendered `github · acme` (the raw enum) while `TemplatesAdmin.tsx:57` rendered
// `GitHub · acme`. One fact, one surface, two spellings — visible side by side when an
// operator moves from the projects list into a project.
//
// The source is a STRUCTURAL shape rather than the `Connection` type, so both call sites
// pass what they already hold (one resolves through a `Map`, the other holds a single
// connection) without this module importing a page's data type. Same idiom as
// `projectRoles.ts`'s `ProdCandidateSource`.
//
// Both fallbacks matter and neither is cosmetic:
//   • An UNRESOLVED connection falls back to the raw `connectionId`. The Ops tables resolve
//     connections best-effort — a deleted connection, or a 403 for a caller who cannot list
//     them, must not blank the row — and the id is what the project record actually holds,
//     so it is both honest and the thing an operator can search for.
//   • A connection that resolved with a BLANK org falls back the same way, because
//     `GitHub · ` is a trailing separator over nothing: it reads as a rendering bug rather
//     than as missing data.
// ---------------------------------------------------------------------------
export interface OrgLabelSource {
  provider: string;
  org: string;
}

export function orgLabel(
  connection: OrgLabelSource | null | undefined,
  connectionId?: string | null,
): string {
  const org = (connection?.org ?? '').trim();
  if (connection && org) return `${providerLabel(connection.provider)} · ${org}`;
  const id = (connectionId ?? '').trim();
  return id || NO_VALUE;
}

// ---------------------------------------------------------------------------
// initialsFor — a display name, login or UPN → up to two letters for an avatar.
//
// Splits on whitespace AND dots, underscores and hyphens, taking the first letter of the
// first two segments. The governance fork at `PrincipalPicker.tsx:36` splits on WHITESPACE
// ONLY — its own comment claims it "mirrors agentUi's initialsFor", which splits on all
// four — so `jane.doe` yields `JA` there and `JD` in `agentUi.tsx:105`.
//
// `JA` is not merely inconsistent, it is LOSSY on exactly the names this surface renders:
// `created_by` is commonly a dotted login or a UPN, and taking the first two letters of the
// first name gives `jane.doe` and `jane.smith` the SAME avatar. An avatar that merges two
// people is worse than no avatar.
//
// FIRST TWO segments, not first-and-last. This matches `agentUi.initialsFor` (the correct
// copy, which the Sidebar already reuses) and reads better for the kebab agent names on
// this surface: `claims-triage-de` gives `CT`, whereas first-and-last would give `CD` —
// two letters that appear in no readable prefix of the name. The Ops mock page
// `Experiments.tsx:64` uses first-and-last, but it is one of the six frozen mock components
// and is not edited here.
//
// Both governance copies are reported to their owner rather than changed: this epic may not
// touch `components/governance/**`.
// ---------------------------------------------------------------------------
export function initialsFor(name: string | null | undefined): string {
  const trimmed = (name ?? '').trim();
  if (!trimmed) return '?';
  const segments = trimmed.split(/[\s._@-]+/).filter(Boolean);
  // A name made only of separators ('...') has no segments at all. Answering '?' beats
  // echoing punctuation into an avatar.
  if (segments.length === 0) return '?';
  if (segments.length >= 2) return (segments[0][0] + segments[1][0]).toUpperCase();
  return segments[0].slice(0, 2).toUpperCase();
}
