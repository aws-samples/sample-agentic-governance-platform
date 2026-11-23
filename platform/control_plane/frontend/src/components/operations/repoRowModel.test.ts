import { describe, expect, it } from 'vitest';

import {
  PROMOTION_READY_LABEL,
  PROMOTION_TAG_ONLY_LABEL,
  PROMOTION_TAG_ONLY_NOTE,
  REPO_ACTIONS,
  REPO_ACTION_LABEL,
  REPO_ACTION_RANK,
  REPO_ACTION_TONE,
  isCommitShaTag,
  ownerLabel,
  prodVersion,
  promotionReadiness,
  repoActionBand,
  repoAction,
  shortCommitSha,
  shortDigest,
  type RepoActionKey,
  type RepoRowSource,
} from './repoRowModel';
import { promoteBlockedReason } from './projectRoles';
import type { CicdStatus } from './opsStatus';
import repoRowModelSrc from './repoRowModel.ts?raw';
import { stripComments } from '../../rawSource';

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

describe('repoAction', () => {
  it('answers `none` for a repo that is shipped and in step with dev', () => {
    expect(repoAction(repo(), 'ready')).toBe('none');
  });

  it('reports a FAILED delivery above everything else', () => {
    expect(repoAction(repo({ cicd_status: 'failed' }), 'ready')).toBe('delivery-failed');
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
    expect(repoAction(repo(), 'failed')).toBe('runtime-failed');
  });

  it('reports a PENDING candidate as an owner action', () => {
    expect(repoAction(repo({ prod_candidate_status: 'pending' }), 'ready')).toBe('approval-pending');
  });

  it('compares the candidate against the LITERAL "pending", not truthiness', () => {
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
    expect(repoAction(repo({ last_promoted_image_tag: null }), 'not_deployed')).toBe('never-deployed');
  });

  it('reports `never-deployed` for a freshly scaffolded repo', () => {
    expect(
      repoAction(repo({ cicd_status: 'ready', last_dev_image_tag: null, last_promoted_image_tag: null }), 'not_deployed'),
    ).toBe('never-deployed');
  });

  it('says NOTHING is required while materialize or a promotion is in flight', () => {
    const inFlight: CicdStatus[] = ['provisioning', 'promoting'];
    for (const status of inFlight) {
      expect(repoAction(repo({ cicd_status: status }), 'ready')).toBe('none');
    }
    expect(repoAction(repo({ cicd_status: 'promoting', prod_candidate_status: 'pending' }), 'updating')).toBe('none');
  });

  it('reports an UNESTABLISHED delivery status as needing a look', () => {
    expect(repoAction(repo({ cicd_status: 'unknown' }), 'ready')).toBe('status-unknown');
  });

  it('does NOT treat an UNKNOWN or NOT-DEPLOYED runtime answer as an action', () => {
    expect(repoAction(repo(), 'unknown')).toBe('none');
    expect(repoAction(repo(), 'not_deployed')).toBe('none');
  });

  it('still reports a real failure when the runtime answer is unknown', () => {
    expect(repoAction(repo({ cicd_status: 'failed' }), 'unknown')).toBe('delivery-failed');
  });

  it('is TOTAL — every input yields a known action key', () => {
    const runtimes = ['ready', 'creating', 'updating', 'failed', 'not_deployed', 'unknown'] as const;
    const statuses: CicdStatus[] = ['deployed', 'failed', 'promoting', 'ready', 'provisioning', 'unknown'];
    for (const runtime of runtimes) {
      for (const status of statuses) {
        expect(REPO_ACTIONS).toContain(repoAction(repo({ cicd_status: status }), runtime));
      }
    }
  });
});

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
    expect(REPO_ACTION_LABEL['approval-pending']).not.toMatch(/approve|promote/i);
  });
});

describe('prodVersion', () => {
  it('names the promoted image and reports no drift when dev matches', () => {
    expect(prodVersion(repo())).toEqual({ tag: 'agent-7-3f9a1c2', drifted: false, devTag: null });
  });

  it('reports drift AND names the dev tag prod is behind', () => {
    expect(prodVersion(repo({ last_dev_image_tag: 'agent-7-newer', last_promoted_image_tag: 'agent-7-older' }))).toEqual(
      { tag: 'agent-7-older', drifted: true, devTag: 'agent-7-newer' },
    );
  });

  it('reports no tag when the repo has never been promoted', () => {
    expect(prodVersion(repo({ last_promoted_image_tag: null }))).toEqual({ tag: null, drifted: false, devTag: null });
  });

  it('never reports drift without a prod tag to drift FROM', () => {
    expect(prodVersion(repo({ last_promoted_image_tag: null, last_dev_image_tag: 'agent-7-newer' })).drifted).toBe(false);
  });

  it('treats a blank tag as absent, not as a value', () => {
    expect(prodVersion(repo({ last_promoted_image_tag: '   ' })).tag).toBeNull();
    expect(prodVersion(repo({ last_dev_image_tag: '  ', last_promoted_image_tag: 'agent-7-older' })).drifted).toBe(false);
  });

  it('agrees with repoAction about whether this repo has drifted', () => {
    const drifting = repo({ last_dev_image_tag: 'agent-7-newer', last_promoted_image_tag: 'agent-7-older' });
    expect(prodVersion(drifting).drifted).toBe(true);
    expect(repoAction(drifting, 'ready')).toBe('drift');
  });
});

describe('ownerLabel', () => {
  it('names the creator and derives initials that distinguish dotted logins', () => {
    expect(ownerLabel('jane.doe')).toEqual({ name: 'jane.doe', initials: 'JD' });
    expect(ownerLabel('jane.smith').initials).toBe('JS');
  });

  it('answers an unknown owner honestly', () => {
    expect(ownerLabel(null)).toEqual({ name: 'Unknown', initials: '?' });
    expect(ownerLabel('')).toEqual({ name: 'Unknown', initials: '?' });
  });
});

describe('promotionReadiness — a passive statement, not an affordance', () => {
  it('is ready exactly when a candidate is pending', () => {
    expect(promotionReadiness(repo({ prod_candidate_status: 'pending' }))).toBe(true);
    expect(promotionReadiness(repo({ prod_candidate_status: null }))).toBe(false);
  });

  it('compares the LITERAL pending, so it cannot disagree with the promote gate', () => {
    for (const status of ['Pending', 'PENDING', 'pending ', 'consumed', 'approved', '', null]) {
      expect(promotionReadiness(repo({ prod_candidate_status: status })), String(status)).toBe(
        false,
      );
    }
  });

  it('agrees with promoteBlockedReason for an owner, on every candidate status', () => {
    for (const status of ['pending', 'consumed', null, 'unrecognized']) {
      const offered = promoteBlockedReason('owner', 0, status) === 'ok';
      expect(promotionReadiness(repo({ prod_candidate_status: status })), String(status)).toBe(
        offered,
      );
    }
  });

  it('is NOT role-gated — it states a fact about the repo, not an offer to the reader', () => {
    const waiting = repo({ prod_candidate_status: 'pending' });
    expect(promotionReadiness(waiting)).toBe(true);
    expect(promotionReadiness.length).toBe(1);
  });

  it('reads as a state, and names no verb the row cannot perform', () => {
    expect(PROMOTION_READY_LABEL).toBe('Ready for promotion');
    for (const verb of ['click', 'approve', 'promote to', 'deploy now']) {
      expect(PROMOTION_READY_LABEL.toLowerCase(), verb).not.toContain(verb);
    }
  });

  it('stays ready whatever cicd_status says, since every stage shares that field — pending is the only input', () => {
    const members: CicdStatus[] = ['deployed', 'failed', 'provisioning', 'promoting', 'unknown'];
    for (const cicd of members) {
      const r = repo({ prod_candidate_status: 'pending', cicd_status: cicd });
      expect(promotionReadiness(r), cicd).toBe(true);
    }
  });

  it('still agrees with promoteBlockedReason, which also takes no cicd_status', () => {
    for (const status of ['pending', 'consumed', null, 'unrecognized']) {
      const r = repo({ prod_candidate_status: status });
      const offered = promoteBlockedReason('owner', 0, r.prod_candidate_status) === 'ok';
      expect(promotionReadiness(r), String(status)).toBe(offered);
    }
  });

  it('is NOT ready when the candidate itself failed its dev deploy', () => {
    const r = repo({ prod_candidate_status: 'pending', dev_outcome_for_candidate: 'failed' });
    expect(promotionReadiness(r)).toBe(false);
  });

  it('stays ready when the dev outcome is succeeded, started, or unknown', () => {
    for (const outcome of ['succeeded', 'started', null, undefined]) {
      const r = repo({ prod_candidate_status: 'pending', dev_outcome_for_candidate: outcome });
      expect(promotionReadiness(r), String(outcome)).toBe(true);
    }
  });

  it('agrees with promoteBlockedReason, now including the dev-outcome fact', () => {
    for (const status of ['pending', 'consumed', null]) {
      for (const outcome of ['succeeded', 'failed', null]) {
        const r = repo({ prod_candidate_status: status, dev_outcome_for_candidate: outcome });
        const offered =
          promoteBlockedReason('owner', 0, r.prod_candidate_status, r.dev_outcome_for_candidate) ===
          'ok';
        expect(promotionReadiness(r), `${status}/${outcome}`).toBe(offered);
      }
    }
  });
});

describe('isCommitShaTag', () => {
  it('is true for a bare 40-lowercase-hex sha, and only that shape', () => {
    expect(isCommitShaTag('0123456789abcdef0123456789abcdef01234567')).toBe(true);
    expect(isCommitShaTag('agent-7-3f9a1c2')).toBe(false);
    expect(isCommitShaTag('0123456789abcdef0123456789abcdef0123456')).toBe(false);
    expect(isCommitShaTag('0123456789ABCDEF0123456789abcdef01234567')).toBe(false);
    expect(isCommitShaTag(null)).toBe(false);
    expect(isCommitShaTag(undefined)).toBe(false);
    expect(isCommitShaTag('')).toBe(false);
  });
});

describe('shortCommitSha', () => {
  it('shows a git-short, labelled sha — never bare, never the whole 40', () => {
    expect(shortCommitSha('0123456789abcdef0123456789abcdef01234567')).toBe('commit 0123456');
  });
});

const DIGEST = `sha256:${'abc1234'}${'d'.repeat(57)}`;

describe('shortDigest', () => {
  it('abbreviates a digest to its algorithm prefix plus seven hex chars', () => {
    expect(shortDigest(DIGEST)).toBe('sha256:abc1234…');
  });

  it('KEEPS the sha256: prefix and marks the truncation', () => {
    const short = shortDigest(DIGEST) as string;
    expect(short.startsWith('sha256:')).toBe(true);
    expect(short.endsWith('…')).toBe(true);
    expect(short.length).toBeLessThan(DIGEST.length);
  });

  it('treats blank and absent alike as no digest', () => {
    expect(shortDigest(null)).toBeNull();
    expect(shortDigest(undefined)).toBeNull();
    expect(shortDigest('')).toBeNull();
    expect(shortDigest('   ')).toBeNull();
  });

  it('echoes an unrecognized value VERBATIM rather than truncating it', () => {
    for (const bad of [
      'sha512:abc1234',
      `sha256:${'A'.repeat(64)}`,
      `sha256:${'a'.repeat(63)}`,
      `sha256:${'a'.repeat(65)}`,
      'abc1234',
      `${DIGEST} `.trim() + 'x',
    ]) {
      expect(shortDigest(bad), bad).toBe(bad);
    }
  });

  it('mirrors the backend digest grammar exactly, in both directions', () => {
    expect(shortDigest(`sha256:${'0123456789abcdef'.repeat(4)}`)).toContain('…');
    expect(shortDigest(`sha256:${'0123456789abcdeg'.repeat(4)}`)).not.toContain('…');
  });
});

describe('the tag-only marker reads as a CAUTION, never as a fault', () => {
  it('never calls the repository broken, failed or invalid', () => {
    const said = `${PROMOTION_TAG_ONLY_LABEL} ${PROMOTION_TAG_ONLY_NOTE}`.toLowerCase();
    for (const alarm of ['error', 'broken', 'invalid', 'failed', 'unsafe', 'must not']) {
      expect(said, alarm).not.toContain(alarm);
    }
  });

  it('says what is weaker about it — a mutable pointer, not exact bytes', () => {
    expect(PROMOTION_TAG_ONLY_NOTE).toMatch(/mutable tag/i);
    expect(PROMOTION_TAG_ONLY_NOTE).toMatch(/can change/i);
    expect(PROMOTION_TAG_ONLY_LABEL).toMatch(/mutable/i);
  });

  it('names the REMEDY, because a caution with no remedy is just an alarm', () => {
    expect(PROMOTION_TAG_ONLY_NOTE).toMatch(/workflow/i);
    expect(PROMOTION_TAG_ONLY_NOTE).toMatch(/updat/i);
  });

  it('is short enough to be a pill, and the note long enough to explain', () => {
    expect(PROMOTION_TAG_ONLY_LABEL.length).toBeLessThan(32);
    expect(PROMOTION_TAG_ONLY_NOTE.length).toBeGreaterThan(120);
    expect(PROMOTION_TAG_ONLY_LABEL).not.toBe(PROMOTION_TAG_ONLY_NOTE);
  });
});

describe('repoActionBand — no band unless there is something to act on', () => {
  const pending = repo({ prod_candidate_status: 'pending' });

  it('shows the awaiting-approval band for a pending candidate', () => {
    expect(repoActionBand(pending, 'ok', false)).toBe('awaiting-approval');
    expect(repoActionBand(pending, 'not-owner', false)).toBe('awaiting-approval');
  });

  it('shows the failure band when the candidate dev deploy failed (owner-addressed)', () => {
    const failed = { ...pending, dev_outcome_for_candidate: 'failed' };
    expect(repoActionBand(failed, 'dev-deploy-failed', false)).toBe('dev-deploy-failed');
    expect(repoActionBand(failed, 'not-owner', false)).toBeNull();
  });

  it('renders nothing for the no-candidate state (the old "Nothing on main" noise)', () => {
    expect(repoActionBand(repo(), 'no-candidate', false)).toBeNull();
  });

  it('renders nothing while a delivery is in flight', () => {
    expect(repoActionBand(pending, 'ok', true)).toBeNull();
  });

  it('never renders a promotion band for an MCP server repo, which cannot be promoted', () => {
    expect(repoActionBand({ ...pending, artifact_kind: 'mcp_server' }, 'ok', false)).toBeNull();
  });
});

describe('REPO_ACTION_TONE', () => {
  it('tints only the two failures as faults', () => {
    expect(REPO_ACTIONS.filter((a) => REPO_ACTION_TONE[a] === 'fault')).toEqual(['delivery-failed', 'runtime-failed']);
  });

  it('tints the request and the drift observation as attention', () => {
    expect(REPO_ACTION_TONE['approval-pending']).toBe('attention');
    expect(REPO_ACTION_TONE.drift).toBe('attention');
  });

  it('tints never-deployed quietly, so a new repo does not start life looking broken', () => {
    expect(REPO_ACTION_TONE['never-deployed']).toBe('quiet');
    expect(REPO_ACTION_TONE['status-unknown']).toBe('quiet');
    expect(REPO_ACTION_TONE.none).toBe('quiet');
  });
});

describe('repoRowModel.ts stays a plain unit', () => {
  it('imports nothing from api/, so no row decision pulls axios in', () => {
    const src = stripComments(repoRowModelSrc);
    expect(src).toContain('export function');
    expect(src).not.toMatch(/['"]\.\.\/\.\.\/api\//);
  });
});
