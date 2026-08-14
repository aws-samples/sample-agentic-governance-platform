// opsStatus.test.ts — the closed-union boundary for the TWO independent ops status
// machines (E28/T10, contract C3).
//
// WHY THIS FILE IS THE CONTRACT. `cicd_status` is a BARE STRING on the wire with five
// writers, one of them a best-effort shell helper in a CodeBuild buildspec
// (`_st() { _u cicd_status "$1"; }`). `projectRoles.ts` says it outright: "With no enum
// behind `cicd_status` no backend test can catch that." It was right — a forked copy of
// `cicdBadgeKey` went un-extended when `promoting`/`deployed` were added, both fell
// through to the amber `pending`, and A REPO LIVE IN PRODUCTION RENDERED IN THE SAME
// AMBER AS ONE STILL PROVISIONING.
//
// The remedy in that file was an honour-system comment ("do not re-fork it"). This file
// replaces it with a mechanism: a CLOSED UNION narrowed once at the boundary, and lookup
// tables typed `Record<CicdStatus, X>` with NO `default` branch — so ADDING A STATUS
// WITHOUT A TABLE ENTRY IS A TYPE ERROR. The compiler, not a reviewer's memory, is the
// exhaustiveness guard.
//
// Two consequences worth stating, because both are behaviour CHANGES the task wants:
//   • An unrecognized wire value maps to `unknown`, never to a plausible-looking
//     neighbour. It used to read "Provisioning" in amber, which is a claim about a repo
//     nobody had established anything about.
//   • The runtime machine is SEPARATE. It is written by a different producer (a boto3
//     describe, not the buildspec), so a single merged pill would be forced to lie: a
//     deployed-but-unreachable agent would read green. Both unions carry `unknown`, and
//     `unknown` is visually distinct from `failed` — an unreachable control plane is not
//     a broken runtime.
//
// Note the two `satisfies`-style sweeps below iterate the EXPORTED const arrays, so a
// new status is automatically in scope of every test here the moment it is added.

import { describe, expect, it } from 'vitest';

import { OPS_BADGE } from './opsUi';
import {
  CICD_STATUSES,
  RUNTIME_STATUSES,
  CICD_BADGE_KEY,
  CICD_LABEL,
  RUNTIME_BADGE_KEY,
  RUNTIME_LABEL,
  isCicdInFlight,
  toCicdStatus,
  toRuntimeStatus,
  type CicdStatus,
  type RuntimeStatusKey,
} from './opsStatus';

// ---------------------------------------------------------------------------
// The unions themselves. Pinned literally against contract C3 — T11 and T13 code
// against these names, so a rename here is a cross-task break, not a local edit.
// ---------------------------------------------------------------------------
describe('the closed unions', () => {
  it('CICD_STATUSES is exactly the AMENDED C3 pin, in order', () => {
    expect([...CICD_STATUSES]).toEqual([
      'provisioning',
      'ready',
      'promoting',
      'deployed',
      'failed',
      'unknown',
    ]);
  });

  it('contains a member for every REAL writer, and nothing else', () => {
    // The amendment's whole point. Each member below was traced to a writer:
    //   provisioning — the initial materialize row + `retry_repo`'s reset
    //   ready        — `_finalize_repo` (project_service.py:1670)
    //   promoting    — the promote route / a rollback
    //   deployed     — the buildspec's `_st deployed` (buildspec.yml:391)
    //   failed       — several backend paths + the buildspec's `_st failed`
    //   unknown      — this module's own fallback; no writer emits it
    // `building` and `pending` were in the first draft and are GONE: nothing writes either,
    // and a union member no writer produces makes the exhaustiveness guard vouch for fiction.
    expect(CICD_STATUSES).not.toContain('building');
    expect(CICD_STATUSES).not.toContain('pending');
    expect(CICD_STATUSES).toHaveLength(6);
  });

  it('RUNTIME_STATUSES is exactly the C3 pin, in order', () => {
    expect([...RUNTIME_STATUSES]).toEqual([
      'ready',
      'creating',
      'updating',
      'failed',
      'not_deployed',
      'unknown',
    ]);
  });

  it('both unions carry `unknown` — it is mandatory, not a convenience', () => {
    // C2/C3: "an unreachable control plane is not a broken runtime". A union without
    // `unknown` forces every absent answer into a state that makes a claim.
    expect(CICD_STATUSES).toContain('unknown');
    expect(RUNTIME_STATUSES).toContain('unknown');
  });
});

// ---------------------------------------------------------------------------
// toCicdStatus — the ONE narrowing point for the bare wire string.
//
// The five writers, enumerated from source rather than from memory:
//   provisioning — project_service `create_repo` (the initial row) + `retry_repo`'s reset
//   ready        — materialize's `finalize` step
//   promoting    — the promote route + the cross-account promote
//   failed       — a failed materialize step, `_mark_failed`, or a promotion that
//                  could not start; ALSO the buildspec's `_st failed`
//   deployed     — the buildspec's terminal `_st deployed`
// ---------------------------------------------------------------------------
describe('toCicdStatus', () => {
  it('maps every value a backend writer actually produces', () => {
    expect(toCicdStatus('provisioning')).toBe('provisioning');
    expect(toCicdStatus('ready')).toBe('ready');
    expect(toCicdStatus('promoting')).toBe('promoting');
    expect(toCicdStatus('deployed')).toBe('deployed');
    expect(toCicdStatus('failed')).toBe('failed');
  });

  it('TRANSLATES NOTHING — every writer value passes through as itself', () => {
    // An earlier draft aliased `ready → pending`, working around a pinned union that had no
    // `ready` member even though `_finalize_repo` writes one. With the pin corrected the alias
    // would report a state the backend never wrote — inventing a status is the same defect as
    // mislabelling one. So the narrowing is now membership only.
    for (const written of ['provisioning', 'ready', 'promoting', 'deployed', 'failed']) {
      expect(toCicdStatus(written)).toBe(written);
    }
  });

  it('no longer recognizes the deleted `building` / `pending` members', () => {
    // They were never written by anything, so they must now read as unestablished rather than
    // as a state — the same treatment any other value no writer produces gets.
    expect(toCicdStatus('building')).toBe('unknown');
    expect(toCicdStatus('pending')).toBe('unknown');
  });

  it('is TOTAL — null, undefined and blank narrow to `unknown`', () => {
    expect(toCicdStatus(null)).toBe('unknown');
    expect(toCicdStatus(undefined)).toBe('unknown');
    expect(toCicdStatus('')).toBe('unknown');
    expect(toCicdStatus('   ')).toBe('unknown');
  });

  it('tolerates case and surrounding whitespace', () => {
    // One writer is a shell helper in a buildspec; the value is compared, not trusted.
    expect(toCicdStatus('DEPLOYED')).toBe('deployed');
    expect(toCicdStatus('  Promoting  ')).toBe('promoting');
  });

  it('maps an UNRECOGNIZED value to `unknown`, never to a plausible neighbour', () => {
    // The heart of the defect this module replaces. Every one of these previously landed
    // on the amber `pending` tint with the label "Provisioning" — a confident sentence
    // about a repo whose state nobody had established. Note `ok`/`success`/`passing`/
    // `error`/`running` are included ON PURPOSE: the old `cicdBadgeKey` carried defensive
    // aliases for them, but NO writer in this codebase emits them, so under a closed
    // union guessing at their meaning is exactly the failure mode.
    for (const raw of ['weird', 'ok', 'success', 'passing', 'error', 'running', 'Deployed!', 'deploy']) {
      expect(toCicdStatus(raw)).toBe('unknown');
    }
  });

  it('always returns a member of the union', () => {
    for (const raw of ['provisioning', 'ready', 'nonsense', '', 'FAILED']) {
      expect(CICD_STATUSES).toContain(toCicdStatus(raw));
    }
  });
});

// ---------------------------------------------------------------------------
// toRuntimeStatus — the SECOND, independent machine (C2's `RuntimeStatus.status`).
// ---------------------------------------------------------------------------
describe('toRuntimeStatus', () => {
  it('maps every value the route can return', () => {
    expect(toRuntimeStatus('ready')).toBe('ready');
    expect(toRuntimeStatus('creating')).toBe('creating');
    expect(toRuntimeStatus('updating')).toBe('updating');
    expect(toRuntimeStatus('failed')).toBe('failed');
    expect(toRuntimeStatus('not_deployed')).toBe('not_deployed');
    expect(toRuntimeStatus('unknown')).toBe('unknown');
  });

  it('is TOTAL, and ABSENT DATA IS NOT GOOD NEWS', () => {
    // The rule the whole second machine exists for: `undefined` is "we did not get an
    // answer", which must never render as `ready`. A green pill over an agent nobody
    // could reach is the single most damaging thing this row could say.
    expect(toRuntimeStatus(undefined)).toBe('unknown');
    expect(toRuntimeStatus(null)).toBe('unknown');
    expect(toRuntimeStatus('')).toBe('unknown');
    expect(toRuntimeStatus(undefined)).not.toBe('ready');
  });

  it('tolerates case and whitespace, and accepts the boto3 CamelCase shapes', () => {
    expect(toRuntimeStatus('READY')).toBe('ready');
    expect(toRuntimeStatus(' not_deployed ')).toBe('not_deployed');
    // T5 maps boto3 → the C2 enum server-side, but a status that reaches the browser
    // in its raw provider shape must still not be guessed at as `ready`.
    expect(toRuntimeStatus('NOT_DEPLOYED')).toBe('not_deployed');
  });

  it('maps an UNRECOGNIZED value to `unknown`, never to a plausible neighbour', () => {
    for (const raw of ['CREATE_FAILED', 'deleting', 'starting', 'healthy', 'nonsense']) {
      expect(toRuntimeStatus(raw)).toBe('unknown');
    }
  });

  it('always returns a member of the union', () => {
    for (const raw of ['ready', 'nonsense', '', 'FAILED']) {
      expect(RUNTIME_STATUSES).toContain(toRuntimeStatus(raw));
    }
  });
});

// ---------------------------------------------------------------------------
// EXHAUSTIVENESS. This is the guard the epic's anti-drift task exists to install, so
// it is worth being explicit about what enforces what:
//
//   • `tsc` is the PRIMARY guard. Each table is declared `Record<CicdStatus, X>`, so
//     adding a member to `CICD_STATUSES` without adding a table entry is a compile
//     error in `opsStatus.ts` itself — it cannot reach a reviewer, let alone prod.
//   • The tests below are the SECONDARY guard, and they are not redundant with `tsc`:
//     they catch the case a `Record` cannot see, namely a table that is complete but
//     WRONG (a status silently pointing at the fall-through entry, which is exactly the
//     shape of the original bug — `promoting`/`deployed` both landed on `pending`).
//
// The sweeps iterate the const array, so a new status is covered the moment it is added
// rather than when someone remembers to extend a hand-written list.
// ---------------------------------------------------------------------------
describe('lookup-table exhaustiveness', () => {
  it('every cicd status has a badge key AND a label', () => {
    for (const status of CICD_STATUSES) {
      expect(Object.keys(CICD_BADGE_KEY)).toContain(status);
      expect(Object.keys(CICD_LABEL)).toContain(status);
      expect(CICD_LABEL[status].length).toBeGreaterThan(0);
    }
    // No EXTRA keys either: a leftover entry for a status that no longer exists is dead
    // code that reads as coverage.
    expect(Object.keys(CICD_BADGE_KEY).sort()).toEqual([...CICD_STATUSES].sort());
    expect(Object.keys(CICD_LABEL).sort()).toEqual([...CICD_STATUSES].sort());
  });

  it('every runtime status has a badge key AND a label', () => {
    for (const status of RUNTIME_STATUSES) {
      expect(Object.keys(RUNTIME_BADGE_KEY)).toContain(status);
      expect(Object.keys(RUNTIME_LABEL)).toContain(status);
      expect(RUNTIME_LABEL[status].length).toBeGreaterThan(0);
    }
    expect(Object.keys(RUNTIME_BADGE_KEY).sort()).toEqual([...RUNTIME_STATUSES].sort());
    expect(Object.keys(RUNTIME_LABEL).sort()).toEqual([...RUNTIME_STATUSES].sort());
  });

  it('every badge key names a REAL OPS_BADGE tint', () => {
    // A table entry pointing at a key the palette does not have renders an unstyled
    // pill — visually "no status at all".
    for (const status of CICD_STATUSES) {
      expect(OPS_BADGE[CICD_BADGE_KEY[status]]).toBeTruthy();
    }
    for (const status of RUNTIME_STATUSES) {
      expect(OPS_BADGE[RUNTIME_BADGE_KEY[status]]).toBeTruthy();
    }
  });

  it('no REAL cicd status silently wears the unknown tint', () => {
    // The original bug, generalized: a delivery status that IS known must never render as
    // one nobody could establish. On this machine `unknown` is the only member allowed to
    // look unknown — every other member describes an observed pipeline state.
    for (const status of CICD_STATUSES) {
      if (status === 'unknown') continue;
      expect(CICD_BADGE_KEY[status]).not.toBe('unknown');
      expect(CICD_LABEL[status]).not.toBe(CICD_LABEL.unknown);
    }
  });

  it('every runtime status is DISTINGUISHABLE, even where two share the neutral tint', () => {
    // The runtime machine is deliberately NOT held to "only `unknown` may look neutral":
    // `not_deployed` shares the slate tint on purpose, because both members mean "nothing
    // is running here" and neither is a failure or a success. Collapsing them in COLOR is
    // honest; collapsing them in WORDS would not be — `not_deployed` is a known absence and
    // `unknown` is an unanswered question, and only the label can carry that difference. So
    // the guard here is on the labels, which must all be distinct.
    const labels = RUNTIME_STATUSES.map((s) => RUNTIME_LABEL[s]);
    expect(new Set(labels).size).toBe(RUNTIME_STATUSES.length);
    // And the two neutral members must still never be confused with a verdict.
    for (const status of ['not_deployed', 'unknown'] as RuntimeStatusKey[]) {
      expect(RUNTIME_BADGE_KEY[status]).toBe('unknown');
      expect(OPS_BADGE[RUNTIME_BADGE_KEY[status]]).not.toBe(OPS_BADGE.ready);
      expect(OPS_BADGE[RUNTIME_BADGE_KEY[status]]).not.toBe(OPS_BADGE.failed);
    }
    // Every member that DOES make a claim keeps its own tint.
    for (const status of ['ready', 'creating', 'updating', 'failed'] as RuntimeStatusKey[]) {
      expect(RUNTIME_BADGE_KEY[status]).not.toBe('unknown');
    }
  });

  it('the cicd labels are all distinct too', () => {
    const labels = CICD_STATUSES.map((s) => CICD_LABEL[s]);
    expect(new Set(labels).size).toBe(CICD_STATUSES.length);
  });

  it('no label leaks the raw lowercase wire value', () => {
    // The pill used to render the bare wire string, so `promoting` sat in sentence-case
    // copy as `promoting`. Every label is sentence-case and none equals its own key.
    for (const status of CICD_STATUSES) {
      expect(CICD_LABEL[status]).not.toBe(status);
      expect(CICD_LABEL[status]).toMatch(/^[A-Z]/);
    }
    for (const status of RUNTIME_STATUSES) {
      expect(RUNTIME_LABEL[status]).not.toBe(status);
      expect(RUNTIME_LABEL[status]).toMatch(/^[A-Z]/);
    }
  });

  it('`unknown` describes OUR READ, never an event that did not happen', () => {
    // E28 final review, FR-3. The label was "Unreachable", which ASSERTS a probe was made and did
    // not come back. Three routes reach `unknown` and only one of them involves a probe: the probe
    // returned an AMBIGUOUS error (AccessDenied, which a LIVE runtime behind a misconfig also
    // returns — the producer maps it here precisely because it cannot conclude anything); the
    // request failed or was never sent; or no runtime was passed AT ALL, which is what BOTH
    // repository lists do — permanently, because the route is per-agent and a fleet list would need
    // one request per row. So every row of both lists claimed a failed probe nobody attempted.
    //
    // Asserted as a VOCABULARY rule rather than an exact string, so the property survives a future
    // rewording: the label must not name an outcome of an attempt.
    for (const word of [/unreachable/i, /timed? ?out/i, /no response/i, /could not (be )?reach/i]) {
      expect(RUNTIME_LABEL.unknown, String(word)).not.toMatch(word);
    }
    // …and it must still say WHICH machine has nothing to report, because this pill renders
    // UNCAPTIONED beside the delivery one on the repository detail header.
    expect(RUNTIME_LABEL.unknown).toMatch(/runtime/i);
    expect(RUNTIME_LABEL.unknown).toMatch(/unknown|not reported|no status/i);
  });

  it('`unknown` is not confusable with a real verdict or a known absence', () => {
    // The two neighbours it must stay apart from, named explicitly. `failed` is a conclusion and
    // `not_deployed` is an established absence; `unknown` is neither, and it shares a TINT with
    // `not_deployed`, so the label is the only thing keeping them apart.
    expect(RUNTIME_LABEL.unknown).not.toBe(RUNTIME_LABEL.failed);
    expect(RUNTIME_LABEL.unknown).not.toBe(RUNTIME_LABEL.not_deployed);
    // It must not read as a failure by wording either, having already been cleared by tint.
    expect(RUNTIME_LABEL.unknown).not.toMatch(/\bfail(ed|ure)?\b/i);
    // …nor claim the deployment state, which is `not_deployed`'s to state.
    expect(RUNTIME_LABEL.unknown).not.toMatch(/deployed/i);
  });

  it('keeps the commonest runtime pill SHORT (the M-f rule, applied to this machine)', () => {
    // This is the DEFAULT pill on both repository lists — today it is every row — so its width sets
    // the column's. M-f shortened delivery's `ready` for exactly this reason. The bound is the
    // widest label the column must already fit, so the rendered width cannot grow.
    const widest = Math.max(
      ...RUNTIME_STATUSES.filter((s) => s !== 'unknown').map((s) => RUNTIME_LABEL[s].length),
    );
    expect(RUNTIME_LABEL.unknown.length).toBeLessThanOrEqual(widest + 1);
  });
});

// ---------------------------------------------------------------------------
// The tint SEMANTICS. Exhaustiveness proves every status has an answer; these pin that
// the answers are the right ones. Each assertion below corresponds to a misreading that
// would be actively harmful on a row whose question is "what is in production?".
// ---------------------------------------------------------------------------
describe('tint semantics', () => {
  it('`deployed` is the ONLY terminal-success cicd tint', () => {
    expect(CICD_BADGE_KEY.deployed).toBe('ready');
    for (const status of CICD_STATUSES) {
      if (status === 'deployed') continue;
      expect(OPS_BADGE[CICD_BADGE_KEY[status]]).not.toBe(OPS_BADGE.ready);
    }
  });

  it('`promoting` is NOT emerald — in-flight must never read as "already in prod"', () => {
    // THE regression. These two rendered identically once; they may never again.
    expect(CICD_BADGE_KEY.promoting).toBe('provisioning');
    expect(OPS_BADGE[CICD_BADGE_KEY.promoting]).not.toBe(OPS_BADGE[CICD_BADGE_KEY.deployed]);
  });

  it('delivery `ready` is NOT emerald — built-but-never-shipped is not a success', () => {
    // `ready` is `_finalize_repo`'s terminal materialize state: the repo and agent exist and
    // nothing has ever been built or promoted. The old table gave it emerald, which claimed a
    // terminal success over a repo that had shipped nothing.
    expect(CICD_BADGE_KEY.ready).toBe('pending');
    expect(OPS_BADGE[CICD_BADGE_KEY.ready]).not.toBe(OPS_BADGE.ready);
    expect(OPS_BADGE[CICD_BADGE_KEY.ready]).not.toBe(OPS_BADGE[CICD_BADGE_KEY.deployed]);
  });

  it('`deployed` and `provisioning` are visually DISTINCT', () => {
    // Stated as its own assertion because this exact pair is the shipped bug: a repo
    // live in production wore provisioning's amber.
    expect(OPS_BADGE[CICD_BADGE_KEY.deployed]).not.toBe(OPS_BADGE[CICD_BADGE_KEY.provisioning]);
  });

  it('`failed` is the only failure tint, on BOTH machines', () => {
    expect(CICD_BADGE_KEY.failed).toBe('failed');
    expect(RUNTIME_BADGE_KEY.failed).toBe('failed');
  });

  it('`unknown` is visually distinct from `failed` — on BOTH machines', () => {
    // C2, verbatim: "an unreachable control plane is not a broken runtime". Rose would
    // report a failure nobody observed.
    expect(OPS_BADGE[CICD_BADGE_KEY.unknown]).not.toBe(OPS_BADGE[CICD_BADGE_KEY.failed]);
    expect(OPS_BADGE[RUNTIME_BADGE_KEY.unknown]).not.toBe(OPS_BADGE[RUNTIME_BADGE_KEY.failed]);
  });

  it('`unknown` is also distinct from in-flight — it is not "provisioning"', () => {
    // Amber would make "no answer" indistinguishable from "working on it", which is the
    // confusion the whole task removes.
    expect(OPS_BADGE[CICD_BADGE_KEY.unknown]).not.toBe(OPS_BADGE[CICD_BADGE_KEY.provisioning]);
    expect(OPS_BADGE[RUNTIME_BADGE_KEY.unknown]).not.toBe(OPS_BADGE[RUNTIME_BADGE_KEY.creating]);
  });

  it('runtime `not_deployed` is NEITHER a failure NOR a success', () => {
    // "Never deployed" is a normal state for a freshly scaffolded repo. Rose would
    // accuse it; emerald would claim it is serving traffic.
    expect(OPS_BADGE[RUNTIME_BADGE_KEY.not_deployed]).not.toBe(OPS_BADGE.failed);
    expect(OPS_BADGE[RUNTIME_BADGE_KEY.not_deployed]).not.toBe(OPS_BADGE.ready);
  });

  it('runtime `ready` is the only emerald runtime tint', () => {
    expect(RUNTIME_BADGE_KEY.ready).toBe('ready');
    for (const status of RUNTIME_STATUSES) {
      if (status === 'ready') continue;
      expect(OPS_BADGE[RUNTIME_BADGE_KEY[status]]).not.toBe(OPS_BADGE.ready);
    }
  });

  it('the two machines share NO LABEL — the row shows them side by side', () => {
    // Both unions have a `ready` MEMBER, and they mean unrelated things: delivery's is
    // "materialized, never built", runtime's is "the agent is up and serving". `RepoRow` renders
    // Runtime and Delivery as adjacent columns, so two pills both reading "Ready" would be
    // actively misleading — and delivery's is the WEAKER state, so the stronger-sounding word
    // would sit on the column with less to say. Hence delivery's label says it has not built
    // yet. This assertion is what stops the collision coming back.
    const cicdLabels = new Set(CICD_STATUSES.map((s) => CICD_LABEL[s]));
    for (const runtime of RUNTIME_STATUSES) {
      expect(cicdLabels).not.toContain(RUNTIME_LABEL[runtime]);
    }
    // Named explicitly, because `ready` is the pair that actually collided. SHORTENED in T13
    // (M-f) from "Awaiting first build": it was the longest pill in the palette AND the default
    // state of every fresh repo, so a new project's list was a column of the widest label.
    expect(CICD_LABEL.ready).not.toBe(RUNTIME_LABEL.ready);
    expect(CICD_LABEL.ready).toBe('Not built yet');
    // The shortening had to keep the label DISTINCT from the terminal good state — the whole
    // point of the wording is that this repo has shipped nothing.
    expect(CICD_LABEL.ready).not.toBe(CICD_LABEL.deployed);
    // …and it must still say so. A label that dropped the "not" would invert the meaning while
    // staying short, which is the one way this change could go wrong.
    expect(CICD_LABEL.ready).toMatch(/\bnot\b/i);
    // And the two `failed` members must not read identically either: "which one failed?" is
    // the first question an operator asks, and the column alone should not have to answer it.
    expect(CICD_LABEL.failed).not.toBe(RUNTIME_LABEL.failed);
  });

  it('the two machines are INDEPENDENT — a deployed repo with no runtime answer is not green', () => {
    // The reason there are two pills and not one. `cicd_status: deployed` +
    // `runtime: undefined` is a real, expected combination (the buildspec wrote the
    // status; the runtime describe failed or was never asked). A single merged pill
    // would be forced to pick one, and picking emerald is the harmful direction.
    const cicd = toCicdStatus('deployed');
    const runtime = toRuntimeStatus(undefined);
    expect(CICD_BADGE_KEY[cicd]).toBe('ready');
    expect(RUNTIME_BADGE_KEY[runtime]).toBe('unknown');
    expect(OPS_BADGE[RUNTIME_BADGE_KEY[runtime]]).not.toBe(OPS_BADGE.ready);
  });
});

// ---------------------------------------------------------------------------
// isCicdInFlight — the `promoting` predicate, now on the union rather than on a raw
// string. Kept because two behaviours hang off it (suppressing a second Promote, which
// the route 409s, and the row's in-flight treatment).
// ---------------------------------------------------------------------------
describe('isCicdInFlight', () => {
  it('is true for exactly the in-flight members', () => {
    // `building` left the union in fix round 1, so the in-flight set is the two states a
    // writer parks a repo in WHILE work is actually running.
    const inFlight = CICD_STATUSES.filter((s) => isCicdInFlight(s));
    expect([...inFlight]).toEqual(['provisioning', 'promoting']);
  });

  it('is false for every terminal, resting or unestablished member', () => {
    for (const status of ['failed', 'deployed', 'ready', 'unknown'] as CicdStatus[]) {
      expect(isCicdInFlight(status)).toBe(false);
    }
  });

  it('does NOT treat `ready` as in flight — it is resting, not working', () => {
    // `ready` persists from the end of materialize until the first build writes `deployed` or
    // `failed`; nothing is running during that span. Calling it in-flight would give the row a
    // spinner that never resolves, and would suppress affordances that gate on in-flight.
    expect(isCicdInFlight('ready')).toBe(false);
    expect(CICD_BADGE_KEY.ready).not.toBe('provisioning');
  });

  it('agrees with the in-flight TINT', () => {
    // The predicate and the color must not disagree about the same row.
    for (const status of CICD_STATUSES) {
      if (isCicdInFlight(status)) expect(CICD_BADGE_KEY[status]).toBe('provisioning');
    }
  });
});

// ---------------------------------------------------------------------------
// A compile-time assertion, kept in the test file so it is read as a guard rather than
// as production code. If `CicdStatus` ever stops being a subtype of the const array's
// element type (e.g. someone widens it to `string`, which would silently re-open the
// union and disable every `Record` check above), this stops compiling.
// ---------------------------------------------------------------------------
describe('the unions stay CLOSED', () => {
  it('narrows to a literal union, not to string', () => {
    type AssertClosed<T extends CicdStatus> = T;
    type _Cicd = AssertClosed<(typeof CICD_STATUSES)[number]>;
    type AssertRuntimeClosed<T extends RuntimeStatusKey> = T;
    type _Runtime = AssertRuntimeClosed<(typeof RUNTIME_STATUSES)[number]>;
    // A widened `CicdStatus = string` would make the two aliases above error, and would
    // ALSO make this runtime check pass vacuously — so assert the closure at runtime too.
    const notAStatus = 'definitely-not-a-status';
    expect(CICD_STATUSES).not.toContain(notAStatus);
    expect(toCicdStatus(notAStatus)).toBe('unknown');
    const _typecheck: [_Cicd, _Runtime] = ['deployed', 'ready'];
    expect(_typecheck).toHaveLength(2);
  });
});
