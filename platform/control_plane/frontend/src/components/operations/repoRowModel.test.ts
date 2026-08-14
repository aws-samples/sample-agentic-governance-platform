// repoRowModel.test.ts — the pure derivations behind the shared repo row (E28/T10, C4).
//
// `RepoRow.tsx` is a `.tsx`, and vitest only collects `src/**/*.test.ts`, so a decision made
// inside the component is a decision no test can reach. Every judgement the row makes
// therefore lives here — the same split `projectRoles.ts` established, and the reason the
// status tables could be pinned at all.
//
// The two judgements are:
//   • repoAction — WHAT, if anything, a human must do about this repo. It is the row's first
//     column and its sort key, so getting it wrong either buries a real failure or cries
//     wolf on every row.
//   • prodVersion — what prod is running, and whether dev has moved past it.

import { describe, expect, it } from 'vitest';

import {
  PROMOTION_READY_LABEL,
  PROMOTION_TAG_ONLY_LABEL,
  PROMOTION_TAG_ONLY_NOTE,
  REPO_ACTIONS,
  REPO_ACTION_LABEL,
  REPO_ACTION_RANK,
  ownerLabel,
  prodVersion,
  promotionReadiness,
  repoAction,
  shortDigest,
  type RepoActionKey,
  type RepoRowSource,
} from './repoRowModel';
// The promote GATE, imported so the indicator is tested against the very function the detail
// page's button reads. A local re-implementation of "would promote be offered?" here would prove
// only that the copy agrees with itself — the drift this pairing exists to catch.
import { promoteBlockedReason } from './projectRoles';

/** A repo with nothing interesting about it: shipped, and prod matches dev. */
function repo(over: Partial<RepoRowSource> = {}): RepoRowSource {
  return {
    cicd_status: 'deployed',
    last_dev_image_tag: 'agent-7-3f9a1c2',
    last_promoted_image_tag: 'agent-7-3f9a1c2',
    prod_candidate_status: null,
    created_by: 'jane.doe',
    ...over,
  };
}

// ---------------------------------------------------------------------------
// repoAction — the "Action required" column.
// ---------------------------------------------------------------------------
describe('repoAction', () => {
  it('answers `none` for a repo that is shipped and in step with dev', () => {
    expect(repoAction(repo(), 'ready')).toBe('none');
  });

  it('reports a FAILED delivery above everything else', () => {
    // A broken build outranks every other observation: nothing else about the row is
    // actionable until it is fixed.
    expect(repoAction(repo({ cicd_status: 'failed' }), 'ready')).toBe('delivery-failed');
    // Even when there is also a candidate waiting and prod is behind dev.
    expect(
      repoAction(
        repo({
          cicd_status: 'failed',
          prod_candidate_status: 'pending',
          last_dev_image_tag: 'agent-7-newer',
        }),
        'failed',
      ),
    ).toBe('delivery-failed');
  });

  it('reports a FAILED runtime — a deployed repo can still be down', () => {
    // The second machine earning its place: delivery says `deployed` and has nothing bad to
    // say, while the thing actually serving traffic is broken.
    expect(repoAction(repo(), 'failed')).toBe('runtime-failed');
  });

  it('reports a PENDING candidate as an owner action', () => {
    expect(repoAction(repo({ prod_candidate_status: 'pending' }), 'ready')).toBe('approval-pending');
  });

  it('compares the candidate against the LITERAL "pending", not truthiness', () => {
    // Same comparison `canPromote` and `prodCandidateView` make, so the row and the promote
    // surface can never disagree about whether something is waiting. A leftover or
    // unrecognized value is not a candidate.
    expect(repoAction(repo({ prod_candidate_status: 'promoted' }), 'ready')).toBe('none');
    expect(repoAction(repo({ prod_candidate_status: '' }), 'ready')).toBe('none');
  });

  it('reports DRIFT when dev has moved past prod', () => {
    expect(
      repoAction(repo({ last_dev_image_tag: 'agent-7-newer', last_promoted_image_tag: 'agent-7-older' }), 'ready'),
    ).toBe('drift');
  });

  it('does NOT report drift when prod and dev match', () => {
    expect(repoAction(repo(), 'ready')).toBe('none');
  });

  it('does NOT report drift when prod has never been promoted', () => {
    // "Never shipped to prod" is `never-deployed`, a different and more accurate statement
    // than "prod is behind" — there is no prod version for dev to be ahead OF.
    expect(repoAction(repo({ last_promoted_image_tag: null }), 'not_deployed')).toBe('never-deployed');
  });

  it('reports `never-deployed` for a freshly scaffolded repo', () => {
    // `ready` is exactly this state on the wire: `_finalize_repo` has run, so the repo and agent
    // exist, and no build has ever landed.
    expect(
      repoAction(repo({ cicd_status: 'ready', last_dev_image_tag: null, last_promoted_image_tag: null }), 'not_deployed'),
    ).toBe('never-deployed');
  });

  it('says NOTHING is required while materialize or a promotion is in flight', () => {
    // The row must not ask for an action whose answer is "wait". In-flight is transient and
    // self-resolving, and the pill already says so. (`building` was in this list until fix
    // round 1 removed it from the union — nothing writes it.)
    for (const status of ['provisioning', 'promoting']) {
      expect(repoAction(repo({ cicd_status: status }), 'ready')).toBe('none');
    }
    // Even with a candidate still recorded mid-promotion: the promotion IS the approval
    // being acted on, so asking for approval again would be wrong.
    expect(repoAction(repo({ cicd_status: 'promoting', prod_candidate_status: 'pending' }), 'updating')).toBe('none');
  });

  it('reports an UNESTABLISHED delivery status as needing a look', () => {
    expect(repoAction(repo({ cicd_status: 'nonsense' }), 'ready')).toBe('status-unknown');
    expect(repoAction(repo({ cicd_status: null }), 'ready')).toBe('status-unknown');
  });

  it('does NOT treat an ABSENT RUNTIME ANSWER as an action', () => {
    // Deliberate, and the most consequential rule in this file. `undefined` means the
    // runtime read failed or was never made — which is the NORMAL case wherever the runtime
    // route is not wired yet. Treating it as actionable would put "action required" on every
    // row in the fleet at once, and a column that always fires tells an operator nothing.
    // The runtime PILL still reports it honestly as unreachable; the ACTION column does not
    // manufacture work from a missing answer.
    expect(repoAction(repo(), undefined)).toBe('none');
    expect(repoAction(repo(), 'unknown')).toBe('none');
    expect(repoAction(repo(), 'not_deployed')).toBe('none');
  });

  it('still reports a real failure when the runtime answer is missing', () => {
    // The rule above must not become a way to lose a delivery failure.
    expect(repoAction(repo({ cicd_status: 'failed' }), undefined)).toBe('delivery-failed');
  });

  it('is TOTAL — every input yields a known action key', () => {
    const runtimes = [undefined, 'ready', 'creating', 'updating', 'failed', 'not_deployed', 'unknown'] as const;
    // Every union member plus values outside it — totality must hold for both.
    const statuses = ['deployed', 'failed', 'promoting', 'ready', 'provisioning', 'unknown', 'nonsense', null];
    for (const runtime of runtimes) {
      for (const status of statuses) {
        expect(REPO_ACTIONS).toContain(repoAction(repo({ cicd_status: status }), runtime));
      }
    }
  });
});

// ---------------------------------------------------------------------------
// The sort. C4: "`Action required` sorts first."
// ---------------------------------------------------------------------------
describe('REPO_ACTION_RANK', () => {
  it('ranks every action, with `none` last', () => {
    for (const action of REPO_ACTIONS) {
      expect(typeof REPO_ACTION_RANK[action]).toBe('number');
    }
    const others = REPO_ACTIONS.filter((a) => a !== 'none').map((a) => REPO_ACTION_RANK[a]);
    for (const rank of others) {
      expect(rank).toBeLessThan(REPO_ACTION_RANK.none);
    }
  });

  it('ranks failures above requests, and requests above observations', () => {
    // The order an operator scanning a fleet needs: something is broken, then something
    // needs your decision, then something is merely worth knowing.
    expect(REPO_ACTION_RANK['delivery-failed']).toBeLessThan(REPO_ACTION_RANK['approval-pending']);
    expect(REPO_ACTION_RANK['runtime-failed']).toBeLessThan(REPO_ACTION_RANK['approval-pending']);
    expect(REPO_ACTION_RANK['approval-pending']).toBeLessThan(REPO_ACTION_RANK.drift);
    expect(REPO_ACTION_RANK.drift).toBeLessThan(REPO_ACTION_RANK.none);
  });

  it('gives every action a distinct rank, so the sort is deterministic', () => {
    const ranks = REPO_ACTIONS.map((a) => REPO_ACTION_RANK[a]);
    expect(new Set(ranks).size).toBe(REPO_ACTIONS.length);
  });

  it('sorts a mixed fleet the way the column promises', () => {
    const rows: RepoActionKey[] = ['none', 'drift', 'delivery-failed', 'approval-pending'];
    const sorted = [...rows].sort((a, b) => REPO_ACTION_RANK[a] - REPO_ACTION_RANK[b]);
    expect(sorted).toEqual(['delivery-failed', 'approval-pending', 'drift', 'none']);
  });
});

describe('REPO_ACTION_LABEL', () => {
  it('labels every action exhaustively', () => {
    expect(Object.keys(REPO_ACTION_LABEL).sort()).toEqual([...REPO_ACTIONS].sort());
  });

  it('renders `none` as an em dash, not as a reassurance', () => {
    // "OK" / "Healthy" would be a claim about facts this column never checked — it only
    // knows that nothing it looks at is asking for a human.
    expect(REPO_ACTION_LABEL.none).toBe('—');
  });

  it('states an ACTION or an OBSERVATION for every other key, in sentence case', () => {
    for (const action of REPO_ACTIONS) {
      if (action === 'none') continue;
      expect(REPO_ACTION_LABEL[action]).toMatch(/^[A-Z]/);
      expect(REPO_ACTION_LABEL[action]).not.toBe(action);
    }
  });

  it('never phrases the approval as an affordance', () => {
    // The row STATES that an approval is outstanding; it does not offer one. Ops surfaces
    // must not grow approve/grant verbs (the shadow-governance failure mode), and C4's props
    // carry no promote callback — only `onNavigate`.
    expect(REPO_ACTION_LABEL['approval-pending']).not.toMatch(/approve|promote/i);
  });
});

// ---------------------------------------------------------------------------
// prodVersion — the "Prod version (+drift)" column.
// ---------------------------------------------------------------------------
describe('prodVersion', () => {
  it('names the promoted image and reports no drift when dev matches', () => {
    expect(prodVersion(repo())).toEqual({ tag: 'agent-7-3f9a1c2', drifted: false, devTag: null });
  });

  it('reports drift AND names the dev tag prod is behind', () => {
    // Both halves matter: "behind" without saying what it is behind is not actionable.
    expect(prodVersion(repo({ last_dev_image_tag: 'agent-7-newer', last_promoted_image_tag: 'agent-7-older' }))).toEqual(
      { tag: 'agent-7-older', drifted: true, devTag: 'agent-7-newer' },
    );
  });

  it('reports no tag when the repo has never been promoted', () => {
    expect(prodVersion(repo({ last_promoted_image_tag: null }))).toEqual({ tag: null, drifted: false, devTag: null });
  });

  it('never reports drift without a prod tag to drift FROM', () => {
    // A repo with a dev image and no prod one has not "drifted"; it has never shipped.
    // Calling that drift would put a warning on every newly created repo.
    expect(prodVersion(repo({ last_promoted_image_tag: null, last_dev_image_tag: 'agent-7-newer' })).drifted).toBe(false);
  });

  it('treats a blank tag as absent, not as a value', () => {
    expect(prodVersion(repo({ last_promoted_image_tag: '   ' })).tag).toBeNull();
    expect(prodVersion(repo({ last_dev_image_tag: '  ', last_promoted_image_tag: 'agent-7-older' })).drifted).toBe(false);
  });

  it('agrees with repoAction about whether this repo has drifted', () => {
    // Two columns, one fact. They must not disagree on the same row.
    const drifting = repo({ last_dev_image_tag: 'agent-7-newer', last_promoted_image_tag: 'agent-7-older' });
    expect(prodVersion(drifting).drifted).toBe(true);
    expect(repoAction(drifting, 'ready')).toBe('drift');
  });
});

// ---------------------------------------------------------------------------
// ownerLabel — the "Owner" column.
// ---------------------------------------------------------------------------
describe('ownerLabel', () => {
  it('names the creator and derives initials that distinguish dotted logins', () => {
    expect(ownerLabel('jane.doe')).toEqual({ name: 'jane.doe', initials: 'JD' });
    expect(ownerLabel('jane.smith').initials).toBe('JS');
  });

  it('answers an unknown owner honestly', () => {
    // `created_by` is always written by the backend, but a pre-migration or partial record
    // can lack it. "Unknown" is the honest label; blank would look like a rendering fault.
    expect(ownerLabel(null)).toEqual({ name: 'Unknown', initials: '?' });
    expect(ownerLabel('')).toEqual({ name: 'Unknown', initials: '?' });
  });
});

// ---------------------------------------------------------------------------
// promotionReadiness — the row's PASSIVE indicator, where its Promote button used to be
// (E28C/T7, D-C4d).
//
// 4d INVERTED the obvious fix. The repositories row and the repository detail page both offered
// a Promote button, and only one of them had the reveal-then-confirm dialog — so the instinct
// was to give the row a dialog too. The ruling went the other way: the row's BUTTON is removed
// entirely, because the row cannot show what promoting would actually do. Promotion is an
// approval of specific bytes, and the operator can only see what dev is running, what the
// candidate is, and whether it is digest-pinned on the DETAIL page. One entry point, at the one
// place with the evidence.
//
// What the row keeps is the INFORMATION, which was never the problem: "this repository is
// waiting for an owner". Hence a passive indicator — a statement, not an affordance.
//
// DRIVEN BY THE SAME PREDICATE THAT GATED THE BUTTON, deliberately. If the indicator asked a
// question of its own, a repo could show "Ready for promotion" while the detail page offered
// nothing (or the reverse), and the two surfaces would disagree about whether an approval is
// outstanding — the drift class this module exists to remove. So readiness is `'pending'` on the
// candidate, the same LITERAL comparison `canPromote` / `promoteBlockedReason` /
// `prodCandidateView` / `repoAction` all make.
//
// AND IT IS NOT ROLE-GATED, which is the one deliberate difference from the button. The button
// was gated on the caller's role (a role refusal renders NOTHING — a hint reading "you need
// Owner" on every row is noise and an invitation to go asking for privilege). This is a FACT
// about the repository, not an affordance offered to the reader, so it renders for everyone —
// the same rule `isCurrentAttempt` states for "this is what is live". Withholding "an approval
// is outstanding" from a viewer would hide the state the fleet list exists to surface.
// ---------------------------------------------------------------------------

describe('promotionReadiness — a passive statement, not an affordance', () => {
  it('is ready exactly when a candidate is pending', () => {
    expect(promotionReadiness(repo({ prod_candidate_status: 'pending' }))).toBe(true);
    expect(promotionReadiness(repo({ prod_candidate_status: null }))).toBe(false);
  });

  it('compares the LITERAL pending, so it cannot disagree with the promote gate', () => {
    // The same literal `canPromote` / `promoteBlockedReason` / `prodCandidateView` compare. A
    // looser test here (truthiness, or a `startsWith`) is how the indicator and the detail page's
    // button would come to disagree about whether an approval is outstanding.
    for (const status of ['Pending', 'PENDING', 'pending ', 'consumed', 'approved', '', null]) {
      expect(promotionReadiness(repo({ prod_candidate_status: status })), String(status)).toBe(
        false,
      );
    }
  });

  it('agrees with promoteBlockedReason for an owner, on every candidate status', () => {
    // The property that makes "one entry point" true rather than asserted: for a caller who MAY
    // promote, the indicator is shown exactly when the detail page would offer the action. Any
    // second notion of readiness breaks this.
    for (const status of ['pending', 'consumed', null, 'unrecognized']) {
      const offered = promoteBlockedReason('owner', 0, status) === 'ok';
      expect(promotionReadiness(repo({ prod_candidate_status: status })), String(status)).toBe(
        offered,
      );
    }
  });

  it('is NOT role-gated — it states a fact about the repo, not an offer to the reader', () => {
    // The deliberate divergence from the button it replaces. A viewer sees that an approval is
    // outstanding; they are simply not the one who can grant it.
    const waiting = repo({ prod_candidate_status: 'pending' });
    expect(promotionReadiness(waiting)).toBe(true);
    // …and the predicate takes no role at all, so it CANNOT be gated by a later edit without
    // changing its signature. Pinned as arity, which a source grep cannot fake.
    expect(promotionReadiness.length).toBe(1);
  });

  it('reads as a state, and names no verb the row cannot perform', () => {
    // The copy is pinned because the row must not grow an imperative. "Promote to prod" here
    // would read as a button; this reads as a queue position.
    expect(PROMOTION_READY_LABEL).toBe('Ready for promotion');
    // No call to action, and nothing suggesting the reader acts HERE.
    for (const verb of ['click', 'approve', 'promote to', 'deploy now']) {
      expect(PROMOTION_READY_LABEL.toLowerCase(), verb).not.toContain(verb);
    }
  });
});

// ---------------------------------------------------------------------------
// shortDigest — the digest, readable, and never mistakable for the sha beside it (E28B/T6, D-B3).
// ---------------------------------------------------------------------------

/** A real-shaped digest: `sha256:` + exactly 64 lowercase hex. */
const DIGEST = `sha256:${'abc1234'}${'d'.repeat(57)}`;

describe('shortDigest', () => {
  it('abbreviates a digest to its algorithm prefix plus seven hex chars', () => {
    expect(shortDigest(DIGEST)).toBe('sha256:abc1234…');
  });

  it('KEEPS the sha256: prefix and marks the truncation', () => {
    // Both are load-bearing on a surface that also shows a 7-char git sha and an image tag ending
    // in one. A bare `abc1234` is indistinguishable from the commit, and a truncation with nothing
    // marking it reads as a complete, copy-pasteable value.
    const short = shortDigest(DIGEST) as string;
    expect(short.startsWith('sha256:')).toBe(true);
    expect(short.endsWith('…')).toBe(true);
    // …and it is genuinely SHORTER than what it abbreviates, or the function does nothing.
    expect(short.length).toBeLessThan(DIGEST.length);
  });

  it('treats blank and absent alike as no digest', () => {
    // The caller renders absence, and on the promote surface absence MEANS something (tag-only) —
    // so a blank must not arrive there as a present-but-empty value.
    expect(shortDigest(null)).toBeNull();
    expect(shortDigest(undefined)).toBeNull();
    expect(shortDigest('')).toBeNull();
    expect(shortDigest('   ')).toBeNull();
  });

  it('echoes an unrecognized value VERBATIM rather than truncating it', () => {
    // The `toCicdStatus` / `recordStatusLabel` rule. Truncating something we did not parse would
    // present its first few characters as though we had. The backend validates the shape at ingest,
    // so these should be unreachable — which is exactly why they must not be silently mangled.
    for (const bad of [
      'sha512:abc1234',                              // another algorithm
      `sha256:${'A'.repeat(64)}`,                    // uppercase — would not resolve at a registry
      `sha256:${'a'.repeat(63)}`,                    // one hex short
      `sha256:${'a'.repeat(65)}`,                    // one hex long
      'abc1234',                                     // a bare git-short sha
      `${DIGEST} `.trim() + 'x',                     // trailing junk past a valid digest
    ]) {
      expect(shortDigest(bad), bad).toBe(bad);
    }
  });

  it('mirrors the backend digest grammar exactly, in both directions', () => {
    // `builds.py`'s `_IMAGE_DIGEST_RE` is `\Asha256:[0-9a-f]{64}\Z`. A looser mirror here would
    // abbreviate a value the API refuses; a tighter one would echo a value it accepts. Only a
    // canonical digest may take the abbreviating branch, and taking it is observable as the
    // ellipsis.
    expect(shortDigest(`sha256:${'0123456789abcdef'.repeat(4)}`)).toContain('…');
    expect(shortDigest(`sha256:${'0123456789abcdeg'.repeat(4)}`)).not.toContain('…');
  });
});

// ---------------------------------------------------------------------------
// The tag-only caution's copy (E28B/T6, item 2).
//
// `prod_candidate_digest` is optional by design, so a repo whose committed `build.yml` predates this
// epic still promotes a MUTABLE TAG. That is accepted staging — and it makes the epic's headline
// guarantee conditional on the deployed template, which nothing surfaced before this marker existed.
// ---------------------------------------------------------------------------

describe('the tag-only marker reads as a CAUTION, never as a fault', () => {
  it('never calls the repository broken, failed or invalid', () => {
    // The whole design constraint in one assertion. This is a known, accepted, self-healing state:
    // dressing it as an error would tell an operator to go fix a repository that is working.
    const said = `${PROMOTION_TAG_ONLY_LABEL} ${PROMOTION_TAG_ONLY_NOTE}`.toLowerCase();
    for (const alarm of ['error', 'broken', 'invalid', 'failed', 'unsafe', 'must not']) {
      expect(said, alarm).not.toContain(alarm);
    }
  });

  it('says what is weaker about it — a mutable pointer, not exact bytes', () => {
    // The note has to convey the actual consequence, or the caution is decoration: what the tag
    // points at can change between the approval and the deployment.
    expect(PROMOTION_TAG_ONLY_NOTE).toMatch(/mutable tag/i);
    expect(PROMOTION_TAG_ONLY_NOTE).toMatch(/can change/i);
    // …and the label carries it too, because the label may be all a scanning reader sees.
    expect(PROMOTION_TAG_ONLY_LABEL).toMatch(/mutable/i);
  });

  it('names the REMEDY, because a caution with no remedy is just an alarm', () => {
    expect(PROMOTION_TAG_ONLY_NOTE).toMatch(/workflow/i);
    expect(PROMOTION_TAG_ONLY_NOTE).toMatch(/updat/i);
  });

  it('is short enough to be a pill, and the note long enough to explain', () => {
    // The label sits in a badge; a sentence there would wrap the field grid. The note is a `title`
    // and is the only place the explanation exists.
    expect(PROMOTION_TAG_ONLY_LABEL.length).toBeLessThan(32);
    expect(PROMOTION_TAG_ONLY_NOTE.length).toBeGreaterThan(120);
    expect(PROMOTION_TAG_ONLY_LABEL).not.toBe(PROMOTION_TAG_ONLY_NOTE);
  });
});
