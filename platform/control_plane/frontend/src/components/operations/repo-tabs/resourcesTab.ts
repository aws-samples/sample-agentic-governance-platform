// resourcesTab.ts — the pure companion behind the repository's Resources tab (E28/T12).
//
// Pure and framework-free so vitest reaches it; `ResourcesTab.tsx` is wiring. Same split as
// `repositoryDetailTabs.ts` / `repoRowModel.ts`.
//
// ---------------------------------------------------------------------------
// WHAT THIS PANEL IS FOR, AND WHY IT IS READ-ONLY
//
// The platform materializes FIVE artifacts per repository — a tracking record plus the governed
// agent, a provider repository, container images, an AgentCore runtime with its Terraform state, and
// an Entra identity — and until now the only place that inventory was ever ENUMERATED was inside the
// delete modal, as a list of things about to be destroyed. So the one view that answered "what does
// this repository actually own?" was reachable only by starting a teardown, which is a poor way to
// ask a question.
//
// This surfaces the same inventory as a first-class panel, read through the same READ-ONLY
// reachability pre-check (`GET …/delete-preview`, which probes and deletes nothing). It offers NO
// delete of its own: the E23 cascade lives in `DeleteRepositoryModal`, which owns the checklist, the
// per-item opt-in and the per-item outcome, and a second teardown path here would be a second thing
// to keep in step with it. The page header already carries the gated Delete.
//
// ---------------------------------------------------------------------------
// AN ARTIFACT THE PROBE DID NOT REPORT IS UNKNOWN, NEVER GONE
//
// The rule this file exists to hold. Absence of evidence is not evidence of absence, and the two
// possible errors here are not symmetric: rendering an unreported artifact as `gone` tells an
// operator it was already deleted — which on a partially-torn-down repository is exactly the wrong
// conclusion, and it is the reassuring direction. `unknown` claims nothing, which is all an
// unreported or unrecognised state supports. The delete modal reaches the same conclusion from the
// other side (it treats `unknown` as "assume present" and still attempts teardown).

import type { RepoDeletePreview } from '../../../api/client';
import type { OPS_BADGE } from '../opsUi';

/** An `OPS_BADGE` key — a SEMANTIC tint name, never a class string (the C3 idiom). */
type BadgeKey = keyof typeof OPS_BADGE;

// ---------------------------------------------------------------------------
// The five artifacts, in a FIXED order.
//
// The keys are the backend `RepoDeleteSelection` fields verbatim, which is what lets the preview's
// `item` values join to these rows. The ORDER matches `DeleteRepositoryModal`'s checklist so one
// operator never meets two orderings of one list — record first, because it is the platform's own
// row and the thing the other four hang off.
// ---------------------------------------------------------------------------
export const RESOURCE_ITEMS = ['record', 'github', 'image', 'runtime', 'identity'] as const;
export type ResourceKey = typeof RESOURCE_ITEMS[number];

/**
 * What each artifact IS. `Record<ResourceKey, string>` with no `default`, so a sixth artifact is a
 * `tsc` error naming this table.
 *
 * Worded as an INVENTORY, not as a teardown checklist: the modal's labels describe what will be
 * destroyed ("Internal record (platform tracking + governed agent)"), which is the right voice under
 * a Delete button and the wrong one on a panel answering "what does this repository own?". Same five
 * facts, stated as possessions.
 */
export const RESOURCE_LABEL: Record<ResourceKey, string> = {
  record: 'Platform record + governed agent',
  github: 'Source repository',
  image: 'Container image(s)',
  runtime: 'AgentCore runtime + Terraform state',
  identity: 'Entra identity',
};

/** A one-line note on where each artifact lives, so the panel is legible without the design doc. */
export const RESOURCE_HINT: Record<ResourceKey, string> = {
  record: 'This repository’s tracking row and its entry in the agent registry.',
  github: 'The repository on the connected provider, created from the template.',
  image: 'The images built for this agent in the tenant’s container registry.',
  runtime: 'The deployed runtime and the Terraform state that describes it.',
  identity: 'The app registration this agent authenticates as.',
};

// ---------------------------------------------------------------------------
// The three reachability states, narrowed ONCE at the boundary.
//
// The wire field is a bare string (`RepoDeletePreviewItem.state`), so it goes through
// `toResourceState` and every lookup below is a `Record` with no `default` — the compiler is the
// exhaustiveness test, same mechanism as `opsStatus.ts`.
// ---------------------------------------------------------------------------
export const RESOURCE_STATES = ['present', 'gone', 'unknown'] as const;
export type ResourceState = typeof RESOURCE_STATES[number];

/**
 * Total by construction: every input, including `null` / blank / garbage, returns a member — and the
 * fallback is `unknown`, never `gone` and never `present`. See the header note: those two each make a
 * claim, and an unrecognised value supports neither.
 *
 * Case- and whitespace-tolerant for the same reason `toCicdStatus` is: the value crosses a wire and
 * is compared, not trusted.
 */
export function toResourceState(raw: string | null | undefined): ResourceState {
  const s = (raw ?? '').trim().toLowerCase();
  return (RESOURCE_STATES as readonly string[]).includes(s) ? (s as ResourceState) : 'unknown';
}

/**
 * What each state READS. Distinct sentences, because the tint alone cannot carry the difference
 * between "we looked and it is not there" and "we could not tell".
 */
export const RESOURCE_STATE_LABEL: Record<ResourceState, string> = {
  present: 'Provisioned',
  gone: 'Not found',
  unknown: 'Not established',
};

/**
 * The tint each state wears.
 *
 *   present → emerald. The artifact exists; that is the healthy, expected state.
 *   gone    → the neutral `pending` amber, NOT the failure rose. A missing artifact is worth
 *             noticing — a repository whose runtime has vanished is a real anomaly — but it is also
 *             the ordinary aftermath of a partial teardown, and rose would accuse the platform of a
 *             fault it has not established.
 *   unknown → slate, the palette's only NEUTRAL tint. The probe could not tell, so the row makes no
 *             claim in either direction.
 */
export const RESOURCE_STATE_BADGE_KEY: Record<ResourceState, BadgeKey> = {
  present: 'ready',
  gone: 'pending',
  unknown: 'unknown',
};

// ---------------------------------------------------------------------------
// resourceInventory — the five rows, always five, joined to whatever the probe reported.
//
// ALWAYS FIVE ROWS, whatever came back. The five artifacts are a fixed contract, so a panel that
// rendered only the reported ones would silently shrink on a partial response and the operator would
// have no way to tell a missing PROBE from a missing ARTIFACT.
//
// An artifact the contract does not name is IGNORED rather than appended: a newer backend reporting a
// sixth item must not add an unlabelled row to a panel whose vocabulary is fixed (there is no label
// and no hint for it, so the row could only echo a raw key). Recorded here rather than silently
// dropped — the day a sixth artifact is real, it is one entry in each table above.
// ---------------------------------------------------------------------------

export interface ResourceRow {
  key: ResourceKey;
  label: string;
  hint: string;
  state: ResourceState;
}

export function resourceInventory(
  preview: RepoDeletePreview | null | undefined,
): ResourceRow[] {
  const byItem = new Map<string, string>();
  for (const item of preview?.items ?? []) byItem.set(item.item, item.state);
  return RESOURCE_ITEMS.map((key) => ({
    key,
    label: RESOURCE_LABEL[key],
    hint: RESOURCE_HINT[key],
    // `get` on an unreported key yields `undefined`, which `toResourceState` narrows to `unknown`.
    // That is the whole rule, and it is one call rather than a branch that could be written the
    // other way.
    state: toResourceState(byItem.get(key)),
  }));
}

// ---------------------------------------------------------------------------
// inventoryState — the PANEL's own read state.
//
// `forbidden` exists because `delete-preview` is OWNER-gated: it is the delete modal's own surface,
// and the route's comment says why the read is pinned at the same threshold as the DELETE it
// precedes. So a viewer or maintainer opening this tab gets a 403, and rendering that as five
// `unknown` rows — or worse, as an empty panel — would state something about the repository when the
// only thing established is something about the CALLER. It gets its own sentence naming the reason.
//
// Everything else that fails is `unread`. `loading` outranks both: a panel mid-read must not flash a
// refusal it is about to contradict.
//
// The 403 is matched on the literal `api/routes/projects.py` pins (`insufficient project role`),
// surfaced as `err.message` by the axios interceptor — the same literal `roleActionMessage` and its
// siblings key off.
// ---------------------------------------------------------------------------

export type InventoryState = 'loading' | 'forbidden' | 'unread' | 'data';

export function inventoryState(source: {
  loading: boolean;
  /** The raw `err.message`, or null on success. */
  error: string | null;
  preview: RepoDeletePreview | null;
}): InventoryState {
  if (source.loading) return 'loading';
  if (source.error !== null) {
    return /insufficient project role/i.test(source.error) ? 'forbidden' : 'unread';
  }
  return source.preview === null ? 'unread' : 'data';
}

/**
 * What the panel says instead of the inventory, or `null` when it renders the inventory.
 * `Record<InventoryState, …>` with no `default` — a fifth state is a `tsc` error here.
 */
export const INVENTORY_STATE_COPY: Record<
  InventoryState,
  { headline: string; detail: string } | null
> = {
  loading: {
    headline: 'Checking what exists…',
    detail: 'Probing each artifact this repository owns. Nothing is changed by this read.',
  },
  forbidden: {
    headline: 'You can’t see this repository’s artifacts',
    detail:
      'Enumerating them needs the Owner role on this project — the same standing the teardown itself needs. Ask an owner if you need this inventory.',
  },
  unread: {
    headline: 'Couldn’t check what exists',
    detail:
      'The reachability probe failed, so which artifacts exist is unknown — not that they are missing. Retry in a moment.',
  },
  data: null,
};
