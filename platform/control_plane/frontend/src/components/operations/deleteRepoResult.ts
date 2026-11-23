import type { RepoDeleteResult, RepoDeleteSelection } from '../../api/client';

export type DeleteTone = 'done' | 'kept' | 'maybeRemains' | 'attention';

const SELECTED_BY: Record<string, keyof RepoDeleteSelection> = {
  exec_role: 'runtime',
  langfuse: 'identity',
};

function wasSelected(item: string, selection: RepoDeleteSelection): boolean {
  const key = SELECTED_BY[item] ?? (item as keyof RepoDeleteSelection);
  return selection[key] === true;
}

export interface DeleteResultRow {
  item: string;
  outcome: string;
  tone: DeleteTone;
  reason: string | null;
}

export interface DeleteResultView {
  rows: DeleteResultRow[];
  summary: { tone: DeleteTone; text: string };
  canRetry: boolean;
  navigateAway: boolean;
}

export function outcomeTone(outcome: string): DeleteTone {
  const o = outcome.toLowerCase();
  if (o === 'deleted') return 'done';
  if (o === 'skipped' || o === 'absent') return 'kept';
  if (o === 'manual') return 'maybeRemains';
  return 'attention';
}

export function deleteResultView(
  result: RepoDeleteResult,
  selection: RepoDeleteSelection,
): DeleteResultView {
  const rows = result.items.map((it) => {
    let tone = outcomeTone(it.outcome);
    if (it.outcome.toLowerCase() === 'skipped' && wasSelected(it.item, selection)) tone = 'maybeRemains';
    return {
      item: it.item,
      outcome: it.outcome,
      tone,
      reason: it.reason?.trim() ? it.reason.trim() : null,
    };
  });
  const failed = rows.some((r) => r.tone === 'attention');
  const notTornDown = rows.some((r) => r.tone === 'maybeRemains');
  const removed = result.record_removed;

  let summary: DeleteResultView['summary'];
  if (removed && failed) {
    summary = {
      tone: 'attention',
      text: 'Repository removed from AGP, but some items failed. Clean those up by hand.',
    };
  } else if (removed && notTornDown) {
    summary = {
      tone: 'maybeRemains',
      text: 'Repository removed from AGP, but some items were not torn down and may still exist. Check the items marked below by hand.',
    };
  } else if (removed) {
    summary = { tone: 'done', text: 'Repository removed from AGP.' };
  } else if (failed) {
    summary = {
      tone: 'attention',
      text: 'Some steps failed, so the repository was kept. Fix the cause and retry.',
    };
  } else if (notTornDown) {
    summary = {
      tone: 'maybeRemains',
      text: 'Some items were not torn down and may still exist. Check the items marked below by hand. The repository was kept.',
    };
  } else {
    summary = { tone: 'kept', text: 'Selected items removed. The repository was kept.' };
  }

  return { rows, summary, canRetry: failed && !removed, navigateAway: removed };
}

export function outcomeLabel(outcome: string): string {
  if (outcome === 'absent') return 'Nothing to remove';
  if (outcome === 'manual') return 'Manual';
  return outcome.charAt(0).toUpperCase() + outcome.slice(1);
}

export function selectionFromPreview(
  states: Partial<Record<string, string>>,
  isMcp: boolean,
): Required<RepoDeleteSelection> {
  return {
    record: states.record !== 'gone',
    github: states.github !== 'gone',
    image: !isMcp && states.image !== 'gone',
    runtime: !isMcp && states.runtime !== 'gone',
    identity: states.identity !== 'gone',
  };
}
