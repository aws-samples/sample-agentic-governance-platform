import { describe, expect, it } from 'vitest';

import type { RepoDeleteItemResult, RepoDeleteResult, RepoDeleteSelection } from '../../api/client';
import { stripComments } from '../../rawSource';
import { deleteResultView, outcomeLabel, outcomeTone, selectionFromPreview } from './deleteRepoResult';
import modalRaw from './DeleteRepositoryModal.tsx?raw';
import tabRaw from './ProjectRepositoriesTab.tsx?raw';
import detailRaw from './RepositoryDetail.tsx?raw';
import overviewTabRaw from './repo-tabs/OverviewTab.tsx?raw';
import accessTabRaw from './repo-tabs/AccessTab.tsx?raw';
import repoSummaryCardRaw from './RepoSummaryCard.tsx?raw';
import repoFieldsRaw from './RepoFields.tsx?raw';
import useRepositoryDetailRaw from './useRepositoryDetail.ts?raw';

const modalSrc = stripComments(modalRaw);
const tabSrc = stripComments(tabRaw);
const detailSrc = [detailRaw, overviewTabRaw, accessTabRaw, repoSummaryCardRaw, repoFieldsRaw, useRepositoryDetailRaw].map(stripComments).join('\n');

function repoDeleteItemResult(overrides: Partial<RepoDeleteItemResult> = {}): RepoDeleteItemResult {
  return { item: 'record', outcome: 'deleted', reason: null, ...overrides };
}

function repoDeleteResult(overrides: Partial<RepoDeleteResult> = {}): RepoDeleteResult {
  return { items: [], record_removed: false, ...overrides };
}

const ok = (item: string) => repoDeleteItemResult({ item });
const ALL: RepoDeleteSelection = { record: true, github: true, image: true, runtime: true, identity: true };
const NO_RECORD = 'no registry record — nothing known to tear down';

describe('outcomeTone', () => {
  it('maps the three backend outcomes', () => {
    expect(outcomeTone('deleted')).toBe('done');
    expect(outcomeTone('skipped')).toBe('kept');
    expect(outcomeTone('failed')).toBe('attention');
  });
  it('treats an unknown outcome as needing attention, never as done', () => {
    expect(outcomeTone('error')).toBe('attention');
    expect(outcomeTone('')).toBe('attention');
  });
});

describe('deleteResultView — the record outcomes: record_removed is the only signal the repository is gone', () => {
  it('full delete: done, navigates away, no retry', () => {
    const v = deleteResultView(repoDeleteResult({
      items: [ok('github'), ok('image'), ok('runtime'), ok('identity'), ok('record')],
      record_removed: true,
    }), ALL);
    expect(v.summary).toEqual({ tone: 'done', text: 'Repository removed from AGP.' });
    expect(v.navigateAway).toBe(true);
    expect(v.canRetry).toBe(false);
  });

  it('record not found: failed with its reason, repo kept, retry offered, stays on page', () => {
    const v = deleteResultView(repoDeleteResult({
      items: [
        ok('github'),
        repoDeleteItemResult({ item: 'record', outcome: 'failed', reason: 'record not found at delete time — retry to remove the row' }),
      ],
      record_removed: false,
    }), ALL);
    expect(v.rows[1]).toEqual({
      item: 'record',
      outcome: 'failed',
      tone: 'attention',
      reason: 'record not found at delete time — retry to remove the row',
    });
    expect(v.summary.tone).toBe('attention');
    expect(v.summary.text).toMatch(/kept/);
    expect(v.canRetry).toBe(true);
    expect(v.navigateAway).toBe(false);
  });

  it('the no-registry-record shape with everything selected: nothing reads as deleted, the banner is not a clean success and names no systems, it points at the rows', () => {
    const v = deleteResultView(repoDeleteResult({
      items: [
        ok('github'),
        ok('image'),
        repoDeleteItemResult({ item: 'runtime', outcome: 'skipped', reason: NO_RECORD }),
        repoDeleteItemResult({ item: 'exec_role', outcome: 'skipped', reason: 'no agent record to derive the role name' }),
        repoDeleteItemResult({ item: 'identity', outcome: 'skipped', reason: NO_RECORD }),
        repoDeleteItemResult({ item: 'langfuse', outcome: 'skipped', reason: NO_RECORD }),
        repoDeleteItemResult({ item: 'record', outcome: 'skipped', reason: 'not found in the registry' }),
      ],
      record_removed: true,
    }), ALL);
    const byItem = Object.fromEntries(v.rows.map((r) => [r.item, r]));
    for (const k of ['runtime', 'exec_role', 'identity', 'langfuse', 'record']) {
      expect(byItem[k].tone).toBe('maybeRemains');
    }
    expect(byItem.runtime.reason).toBe(NO_RECORD);
    expect(byItem.record.reason).toBe('not found in the registry');
    expect(v.summary.tone).toBe('maybeRemains');
    expect(v.summary.text).not.toBe('Repository removed from AGP.');
    expect(v.summary.text).toMatch(/not torn down/);
    expect(v.summary.text).toMatch(/Check the items marked below by hand\.$/);
    expect(v.summary.text).not.toMatch(/AWS and GitHub/);
    expect(v.canRetry).toBe(false);
    expect(v.navigateAway).toBe(true);
  });

  it('the no-registry-record shape with the record unchecked: selected-but-skipped warns, repo kept, no retry', () => {
    const v = deleteResultView(repoDeleteResult({
      items: [
        repoDeleteItemResult({ item: 'runtime', outcome: 'skipped', reason: NO_RECORD }),
        repoDeleteItemResult({ item: 'exec_role', outcome: 'skipped', reason: 'no agent record to derive the role name' }),
        repoDeleteItemResult({ item: 'record', outcome: 'skipped', reason: 'not selected' }),
      ],
      record_removed: false,
    }), { ...ALL, record: false, github: false, image: false, identity: false });
    expect(v.rows.map((r) => r.tone)).toEqual(['maybeRemains', 'maybeRemains', 'kept']);
    expect(v.summary.tone).toBe('maybeRemains');
    expect(v.summary.text).toMatch(/not torn down.*Check the items marked below by hand.*kept/);
    expect(v.canRetry).toBe(false);
    expect(v.navigateAway).toBe(false);
  });

  it('GitHub unreachable with the record removed: warns, never a green "Repository removed from AGP."', () => {
    const v = deleteResultView(repoDeleteResult({
      items: [
        repoDeleteItemResult({ item: 'github', outcome: 'skipped', reason: 'connection removed — GitHub repo unreachable' }),
        ok('image'),
        ok('runtime'),
        ok('exec_role'),
        ok('identity'),
        ok('langfuse'),
        ok('record'),
      ],
      record_removed: true,
    }), ALL);
    expect(v.rows[0].tone).toBe('maybeRemains');
    expect(v.summary.tone).toBe('maybeRemains');
    expect(v.navigateAway).toBe(true);
  });

  it('an unselected item that reports skipped stays quietly kept', () => {
    const v = deleteResultView(repoDeleteResult({
      items: [repoDeleteItemResult({ item: 'github', outcome: 'skipped', reason: 'not selected' }), ok('record')],
      record_removed: true,
    }), { ...ALL, github: false });
    expect(v.rows[0].tone).toBe('kept');
    expect(v.summary).toEqual({ tone: 'done', text: 'Repository removed from AGP.' });
  });

  it('an earlier step failed: record kept for retry', () => {
    const v = deleteResultView(repoDeleteResult({
      items: [
        repoDeleteItemResult({ item: 'runtime', outcome: 'failed', reason: 'AccessDenied' }),
        repoDeleteItemResult({ item: 'record', outcome: 'skipped', reason: 'kept for retry — an earlier step failed' }),
      ],
      record_removed: false,
    }), ALL);
    expect(v.summary.tone).toBe('attention');
    expect(v.canRetry).toBe(true);
    expect(v.navigateAway).toBe(false);
  });

  it('a non-blocking failure (exec_role, langfuse) after the record was removed: no retry, since no repo is left to re-run against, still navigates', () => {
    const v = deleteResultView(repoDeleteResult({
      items: [ok('record'), repoDeleteItemResult({ item: 'exec_role', outcome: 'failed', reason: 'not reclaimed: role-a (AccessDenied)' })],
      record_removed: true,
    }), ALL);
    expect(v.summary.tone).toBe('attention');
    expect(v.summary.text).toMatch(/^Repository removed from AGP, but some items failed/);
    expect(v.canRetry).toBe(false);
    expect(v.navigateAway).toBe(true);
  });

  it('partial teardown with the record unchecked: nothing failed, but the repo stays — no navigation', () => {
    const v = deleteResultView(repoDeleteResult({
      items: [ok('image'), repoDeleteItemResult({ item: 'record', outcome: 'skipped', reason: 'not selected' })],
      record_removed: false,
    }), { ...ALL, record: false });
    expect(v.summary.tone).toBe('kept');
    expect(v.summary.text).toMatch(/kept/);
    expect(v.canRetry).toBe(false);
    expect(v.navigateAway).toBe(false);
  });

  it('a blank reason renders as no reason', () => {
    const r = repoDeleteResult({ items: [repoDeleteItemResult({ item: 'github', outcome: 'skipped', reason: '  ' })], record_removed: false });
    expect(deleteResultView(r, ALL).rows[0].reason).toBeNull();
  });
});

describe('DeleteRepositoryModal wiring (source guards, comments stripped)', () => {
  it('derives the result view from the helper, over the selection that was SENT, captured at submit time', () => {
    expect(modalSrc).toContain("from './deleteRepoResult'");
    expect(modalSrc).toMatch(/deleteResultView\(result, submitted\)/);
    expect(modalSrc).toMatch(/setSubmitted\(selection\);\s*setResult\(res\);/);
  });

  it('never leaves the page straight from the delete call — only from Done, and only when the record is gone', () => {
    const submit = modalSrc.slice(modalSrc.indexOf('const handleSubmit'), modalSrc.indexOf('const handleDone'));
    expect(submit.length).toBeGreaterThan(100);
    expect(submit).not.toContain('onDeleted');
    expect(modalSrc).toMatch(/if \(view\?\.navigateAway\) onDeleted\(\);\s*else onClose\(\);/);
  });

  it('routes ×/Esc/backdrop to Done once results show, so the parent always learns of a removal', () => {
    expect(modalSrc).toMatch(/onClose=\{result\s*\?\s*handleDone\s*:\s*onClose\}/);
    expect(modalSrc).toMatch(/actionPending=\{actionPending\}/);
  });

  it('the detail page, the only caller, reloads on close, so a partial teardown never leaves stale rows', () => {
    expect(tabSrc).not.toMatch(/DeleteRepositoryModal/);
    expect(detailSrc).toMatch(/onClose=\{\(\) => \{[^}]*setDeleteOpen\(false\);\s*refetch\(\);/);
  });
});

describe('deleteResultView — harness repo', () => {
  const HARNESS = "harness teardown isn't automated yet — delete the harness in the AgentCore console (its Terraform state and managed memory are left too)";
  const HARNESS_ROLE = 'not automated for harnesses yet — delete the my-agent-dev-harness-exec role in IAM';
  it('a selected harness runtime/exec-role reported skipped reads amber, never a clean delete', () => {
    const v = deleteResultView(repoDeleteResult({
      items: [
        ok('github'),
        ok('image'),
        repoDeleteItemResult({ item: 'runtime', outcome: 'skipped', reason: HARNESS }),
        repoDeleteItemResult({ item: 'exec_role', outcome: 'skipped', reason: HARNESS_ROLE }),
        ok('identity'),
        ok('langfuse'),
        ok('record'),
      ],
      record_removed: true,
    }), ALL);
    expect(v.rows.find((r) => r.item === 'runtime')).toEqual({
      item: 'runtime', outcome: 'skipped', tone: 'maybeRemains', reason: HARNESS,
    });
    expect(v.rows.find((r) => r.item === 'exec_role')?.tone).toBe('maybeRemains');
    expect(v.summary.tone).toBe('maybeRemains');
    expect(v.summary.text).toMatch(/may still exist/);
    expect(v.summary.text).not.toBe('Repository removed from AGP.');
  });
});

describe('the MCP gateway is manual, never-built is absent', () => {
  it('absent reads as a quiet no-op and manual always reads amber, whatever was selected', () => {
    expect(outcomeTone('absent')).toBe('kept');
    expect(outcomeTone('manual')).toBe('maybeRemains');
    expect(outcomeLabel('absent')).toBe('Nothing to remove');
    expect(outcomeLabel('manual')).toBe('Manual');
    expect(outcomeLabel('deleted')).toBe('Deleted');
  });

  it('a never-built gateway and an absent Cedar engine leave no amber banner', () => {
    const v = deleteResultView(repoDeleteResult({
      items: [
        ok('github'),
        repoDeleteItemResult({ item: 'image', outcome: 'skipped', reason: 'not used by MCP servers' }),
        repoDeleteItemResult({ item: 'runtime', outcome: 'absent', reason: 'never built — nothing to remove' }),
        ok('identity'),
        repoDeleteItemResult({ item: 'policy_engine', outcome: 'absent', reason: 'no policy engine on record — nothing to remove' }),
        ok('record'),
      ],
      record_removed: true,
    }), { record: true, github: true, image: false, runtime: false, identity: true });
    expect(v.summary).toEqual({ tone: 'done', text: 'Repository removed from AGP.' });
    expect(v.rows.find((r) => r.item === 'runtime')?.tone).toBe('kept');
  });

  it('a ticked item reported absent stays quiet — only a ticked skipped may still exist', () => {
    const v = deleteResultView(repoDeleteResult({
      items: [
        ok('github'),
        repoDeleteItemResult({ item: 'identity', outcome: 'absent', reason: 'no Entra app on record — nothing to remove' }),
        ok('record'),
      ],
      record_removed: true,
    }), { record: true, github: true, image: false, runtime: false, identity: true });
    expect(v.rows.find((r) => r.item === 'identity')?.tone).toBe('kept');
    expect(v.summary).toEqual({ tone: 'done', text: 'Repository removed from AGP.' });
  });

  it('a built gateway is flagged for manual cleanup even though it was never selectable', () => {
    const v = deleteResultView(repoDeleteResult({
      items: [ok('github'), repoDeleteItemResult({ item: 'runtime', outcome: 'manual', reason: 'Not removed by AGP — …' }), ok('record')],
      record_removed: true,
    }), { record: true, github: true, image: false, runtime: false, identity: true });
    expect(v.summary.tone).toBe('maybeRemains');
    expect(v.rows[1].tone).toBe('maybeRemains');
  });

  it('seeds the selection from the preview; an MCP gateway is never sent as selected', () => {
    expect(selectionFromPreview({ runtime: 'manual', github: 'gone' }, true)).toEqual({
      record: true, github: false, image: false, runtime: false, identity: true,
    });
    expect(selectionFromPreview({ image: 'gone' }, false)).toEqual({
      record: true, github: true, image: false, runtime: true, identity: true,
    });
  });
});
