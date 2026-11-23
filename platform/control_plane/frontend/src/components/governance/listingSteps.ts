import type { PublishState } from './publishForm';

export type StepStatus = 'done' | 'current' | 'blocked' | 'upcoming';

export interface ListingStep {
  label: string;
  status: StepStatus;
}

export type NextAction =
  | { kind: 'cta'; text: string }
  | { kind: 'review'; text: string }
  | { kind: 'note'; text: string }
  | { kind: 'none'; text: string };

export interface ListingStepsInput {
  lifecycle: string;
  publishState: PublishState | null;
  staysLive: boolean;
  blocker: string | null;
}

const LABELS = ['Approved', 'Datasheet submitted', 'Listed in marketplace'] as const;

function steps(a: StepStatus, b: StepStatus, c: StepStatus): ListingStep[] {
  return [a, b, c].map((status, i) => ({ label: LABELS[i], status }));
}

export function listingSteps(input: ListingStepsInput): { steps: ListingStep[]; next: NextAction } {
  const { lifecycle, publishState, staysLive, blocker } = input;

  if (lifecycle !== 'approved') {
    if (lifecycle === 'pending_approval') {
      return { steps: steps('current', 'upcoming', 'upcoming'), next: { kind: 'note', text: 'Waiting for an admin to approve the record.' } };
    }
    if (lifecycle === 'rejected') {
      return { steps: steps('blocked', 'upcoming', 'upcoming'), next: { kind: 'note', text: 'The record was rejected. Revise it and submit it for approval again.' } };
    }
    if (lifecycle === 'deprecated') {
      return { steps: steps('blocked', 'upcoming', 'upcoming'), next: { kind: 'note', text: 'Deprecated records cannot be listed.' } };
    }
    return { steps: steps('current', 'upcoming', 'upcoming'), next: { kind: 'note', text: 'Submit it for approval (top of the page).' } };
  }

  if (blocker) return { steps: steps('done', 'blocked', 'upcoming'), next: { kind: 'note', text: blocker } };

  switch (publishState) {
    case null:
      return { steps: steps('done', 'current', 'upcoming'), next: { kind: 'none', text: '' } };
    case 'pending':
      return {
        steps: steps('done', 'done', staysLive ? 'done' : 'current'),
        next: { kind: 'review', text: 'Waiting for an admin to review the datasheet.' },
      };
    case 'rejected':
      return { steps: steps('done', 'blocked', staysLive ? 'done' : 'upcoming'), next: { kind: 'cta', text: 'Revise and resubmit' } };
    case 'published':
      return { steps: steps('done', 'done', 'done'), next: { kind: 'none', text: '' } };
    case 'unpublished':
    case 'never':
      return { steps: steps('done', 'current', 'upcoming'), next: { kind: 'cta', text: 'Publish to marketplace' } };
  }
}
