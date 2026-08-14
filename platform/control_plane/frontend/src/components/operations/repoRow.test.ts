// repoRow.test.ts — THE anti-drift test for E28 (T13, contract C4).
//
// ---------------------------------------------------------------------------
// WHAT THIS FILE IS FOR
//
// The plan's requirement is "both lists render identical status for the same repo", and that is
// the regression that shipped: a module-private status→tint table was forked across the fleet list
// and a project's repositories tab, and when `promoting` / `deployed` were added only ONE copy was
// extended. On the other page both new statuses fell through to the amber fall-through, so A REPO
// LIVE IN PRODUCTION RENDERED IN THE SAME AMBER AS ONE STILL PROVISIONING — wrong, and reassuring,
// on the one row whose question is "what is in production right now?".
//
// A test asserting that today's two outputs happen to match would not catch that: they matched
// before the fork too. So the assertions below are MECHANICAL — they check that there is only ONE
// row implementation and that neither call site can style, label or order a row itself:
//
//   1. Both `.tsx` call sites IMPORT `RepoRow` and neither contains row markup of its own.
//   2. Neither call site imports a status LABEL or TINT table, so neither can render a pill.
//   3. `showProject` is the ONLY prop that differs between them (C4).
//   4. Both order rows through the SHARED `sortRepoRows`.
//
// Plus the three wording/behaviour fixes T13 owns: the candidate verb (A3), the `last_promoted_*`
// relabel (A4/P10), M-a's single wording, and the row's keyboard reachability (M-e).
//
// ---------------------------------------------------------------------------
// WHY SOURCE-AS-TEXT
//
// There is NO jsdom in this project — `vitest.config.ts` collects only `src/**/*.test.ts`, so a
// component cannot be rendered and anything decided inside a `.tsx` is untestable by
// construction. The judgements therefore live in `repoRowModel.ts` (tested directly, here and in
// `repoRowModel.test.ts`), and the rules ABOUT the `.tsx` files are guarded by reading their
// source — the `settingsSections.test.ts:352` / `repositoryDetailTabs.test.ts:868` idiom.
//
// The source is read RAW: no lowercasing, no trimming, no comment stripping. Normalizing is how a
// guard silently stops seeing the thing it guards, and this epic shipped exactly that bug once
// already. The cost is that a comment can trip a guard, which is why the comments in the files
// below are written not to quote the strings being forbidden.

import { describe, expect, it } from 'vitest';

import {
  CANDIDATE_ACTOR_VERB,
  LAST_DEPLOYED_ACTOR_VERB,
  LAST_DEPLOYED_LABEL,
  REPO_ACTION_LABEL,
  sortRepoRows,
  type RepoRowSource,
} from './repoRowModel';
import { CICD_LABEL } from './opsStatus';
// Vite's `?raw` rather than `node:fs`: `tsconfig.app.json` sets `types: ["vite/client"]` (no
// `node`), so `node:fs` has no declarations here while `vite/client` declares `*?raw`. The
// explicit extension is load-bearing on a case-insensitive filesystem, where a specifier can
// resolve to a sibling module differing only in casing.
import repoRowSrc from './RepoRow.tsx?raw';
import fleetListSrc from './Repositories.tsx?raw';
import projectTabSrc from './ProjectRepositoriesTab.tsx?raw';
import addRepoModalSrc from './AddRepoModal.tsx?raw';
// THE PROMOTE CONFIRM'S NEW HOME (E28C/T7, D-C4d). Every guard below that pinned dialog markup was
// re-pointed here, unchanged in what it asserts — the dialog was extracted out of the project tab
// when 4d made the detail page the product's ONE promote entry point. Re-pointing rather than
// deleting is the whole reason the extraction is safe: the differing-tag warning's two branches, the
// artifact marker's branchlessness and the tone table's tints are all still pinned, now on the file
// that renders them.
import promoteConfirmSrc from './PromoteConfirm.tsx?raw';
// The dialog's ONE caller since 4d — read here for the gating guard, which is about the pair of
// conditions rather than about either file alone.
import repositoryDetailSrc from './RepositoryDetail.tsx?raw';
// Its `.ts` companion, both as source (for the type annotation that makes the tone union exhaustive)
// and as a VALUE — indexing the table checks the class string that actually reaches the DOM, which a
// regex over a source window could only approximate.
import promoteConfirmModelSrc from './promoteConfirm.ts?raw';
import { ARTIFACT_TONE_CLS } from './promoteConfirm';
// `prodCandidateView`'s home. It produces the candidate actor, and its doc comment was the FIFTH
// site carrying the wrong verb — the one place a corrected caller would be re-broken from.
import candidateModelSrc from './projectRoles.ts?raw';

/** The two call sites the anti-drift claim is about. */
const CALL_SITES = [
  ['Repositories.tsx', fleetListSrc],
  ['ProjectRepositoriesTab.tsx', projectTabSrc],
] as const;

// ---------------------------------------------------------------------------
// 1 — ONE ROW IMPLEMENTATION. The point of the task.
// ---------------------------------------------------------------------------
describe('both lists render THE SAME row component', () => {
  it('reads every source (so no assertion below passes on an empty string)', () => {
    // The cheapest way this whole file could become vacuous: a failed `?raw` import yielding ''
    // makes every `not.toMatch` below pass. This epic shipped a test that passed via a 404 on a
    // nonexistent project, so the read is checked before anything is concluded from it.
    for (const [name, src] of CALL_SITES) {
      expect(src.length, `${name} did not load`).toBeGreaterThan(1000);
    }
    expect(repoRowSrc.length).toBeGreaterThan(1000);
    expect(addRepoModalSrc.length).toBeGreaterThan(1000);
  });

  it('imports RepoRow at BOTH call sites', () => {
    for (const [name, src] of CALL_SITES) {
      expect(src, `${name} does not import RepoRow`).toMatch(
        /import\s+RepoRow(\s*,\s*\{[^}]*\})?\s+from\s+'\.\/RepoRow\.tsx'/,
      );
    }
  });

  it('renders <RepoRow at both call sites and declares no repository <tr> of its own', () => {
    // THE mechanical guard. A call site that renders `<RepoRow` and contains no row markup cannot
    // fork the row: there is nothing local to extend. `<tr>`s still exist for the `<thead>`, the
    // loading/empty cells and (in the project tab) the action band and confirm — those are
    // COLSPAN'd full-width rows, so the discriminator is a `<td>` that is neither `colSpan`'d nor
    // inside the head. A forked per-repo row would necessarily bring several bare `<td>`s.
    for (const [name, src] of CALL_SITES) {
      expect(src, `${name} does not render RepoRow`).toMatch(/<RepoRow\b/);
      // Count `<td` occurrences that do NOT carry a colSpan on the same line.
      const bareTds = src
        .split('\n')
        .filter((line) => /<td\b/.test(line) && !/colSpan/.test(line));
      expect(bareTds, `${name} still declares un-spanned row cells:\n${bareTds.join('\n')}`).toEqual([]);
    }
  });

  it('keeps every status LABEL and TINT out of both call sites', () => {
    // Neither page may import a status table, because importing one is the only way to render a
    // pill — and two pages rendering their own pills is precisely the fork. `RepoRow` is the sole
    // consumer of `CICD_LABEL` / `RUNTIME_LABEL` / `CICD_BADGE_KEY` / `RUNTIME_BADGE_KEY` and of
    // the `promotionStatusLabel` / `cicdBadgeKey` wrappers over them.
    const statusTables =
      /\b(CICD_LABEL|RUNTIME_LABEL|CICD_BADGE_KEY|RUNTIME_BADGE_KEY|promotionStatusLabel|cicdBadgeKey)\b/;
    for (const [name, src] of CALL_SITES) {
      expect(statusTables.test(src), `${name} reads a status table directly`).toBe(false);
    }
    // And the row itself DOES read them — otherwise the rule above is satisfied by nobody
    // rendering a status at all, which would pass while showing the operator nothing.
    expect(statusTables.test(repoRowSrc)).toBe(true);
  });

  it('leaves the whole status palette to the row: no OPS_BADGE lookup at either call site', () => {
    // `OPS_BADGE[...]` is the pill's color. A call site indexing it is styling a status itself,
    // which is the fork one level below the label tables. The project tab still IMPORTS the type
    // (`nextBadgeFromSteps` returns `keyof typeof OPS_BADGE`), so the guard is on the INDEXING.
    for (const [name, src] of CALL_SITES) {
      expect(src, `${name} indexes the badge palette`).not.toMatch(/OPS_BADGE\s*\[/);
    }
    // The materialize modal legitimately indexes it — a step timeline is not a repo status, and
    // it is not one of the two lists. Asserted so this guard is known to be about the LISTS.
    expect(addRepoModalSrc).toMatch(/OPS_BADGE\s*\[/);
  });

  it('differs between the two call sites ONLY in `showProject` (C4)', () => {
    // The prop that must differ, in the direction C4 pins: the fleet list says which project a
    // repo belongs to; a project's own list would repeat its own name on every row.
    expect(fleetListSrc).toMatch(/showProject\b(?!\s*=\s*\{false\})/);
    expect(projectTabSrc).toMatch(/showProject=\{false\}/);
    // And nothing else does. `RepoRow`'s prop names are the closed set a call site may pass, so
    // any OTHER boolean-shaped prop appearing on one `<RepoRow` and not the other would be the
    // second axis of divergence this task exists to prevent.
    const propsPassedAt = (src: string): string[] => {
      const call = /<RepoRow\b([\s\S]*?)\/>/.exec(src);
      expect(call, 'no <RepoRow … /> call found').not.toBeNull();
      return [...(call?.[1] ?? '').matchAll(/^\s{2,}([a-zA-Z]+)[=\s]/gm)].map((m) => m[1]).sort();
    };
    const fleetProps = propsPassedAt(fleetListSrc);
    const projectProps = propsPassedAt(projectTabSrc);
    const onlyInOne = [
      ...fleetProps.filter((p) => !projectProps.includes(p)),
      ...projectProps.filter((p) => !fleetProps.includes(p)),
    ];
    // `key` and `projectName` accompany `showProject` — both exist only because the fleet list
    // renders a project sub-line and maps over an array it owns. Every OTHER difference is drift.
    expect(onlyInOne.filter((p) => !['showProject', 'projectName', 'key'].includes(p))).toEqual([]);
  });

  it('never grows a governance verb on the row (the shadow-governance rule)', () => {
    // C4's props carry `onNavigate` and nothing else. An approve/grant/classify/deprecate
    // callback on the SHARED row would put that verb on both Ops surfaces at once.
    expect(repoRowSrc).not.toMatch(/on(Approve|Grant|Classify|Deprecate|Promote|Rollback)\s*[?:]/);
  });
});

// ---------------------------------------------------------------------------
// 2 — ONE ORDER. A shared row that two pages sort differently still disagrees.
// ---------------------------------------------------------------------------
describe('both lists order rows through the shared sort', () => {
  it('calls `sortRepoRows` at both call sites and sorts nowhere else', () => {
    for (const [name, src] of CALL_SITES) {
      expect(src, `${name} does not use the shared sort`).toMatch(/\bsortRepoRows\(/);
      // A local `.sort(` would be a second ordering over the same fact.
      expect(src, `${name} sorts rows locally`).not.toMatch(/\.sort\(/);
    }
  });

  it('puts what needs a human FIRST, identically for either list', () => {
    // The same array through the same function — so the assertion is about the ORDER the shared
    // sort produces, which is by construction the order both pages get.
    const rows: (RepoRowSource & { id: string; name: string })[] = [
      { id: 'r1', name: 'quiet', cicd_status: 'deployed', last_dev_image_tag: 't', last_promoted_image_tag: 't' },
      { id: 'r2', name: 'drifting', cicd_status: 'deployed', last_dev_image_tag: 'new', last_promoted_image_tag: 'old' },
      { id: 'r3', name: 'broken', cicd_status: 'failed' },
      { id: 'r4', name: 'waiting', cicd_status: 'deployed', prod_candidate_status: 'pending', last_promoted_image_tag: 't', last_dev_image_tag: 't' },
    ];
    expect(sortRepoRows(rows).map((r) => r.id)).toEqual(['r3', 'r4', 'r2', 'r1']);
  });

  it('breaks ties on NAME, not on whatever order the API returned', () => {
    // Without a tiebreak the order within a rank is the API's, and two lists reading the same
    // partition in different orders would disagree again through the back door.
    const rows = [
      { id: 'b', name: 'beta', cicd_status: 'failed' },
      { id: 'a', name: 'alpha', cicd_status: 'failed' },
    ];
    expect(sortRepoRows(rows).map((r) => r.name)).toEqual(['alpha', 'beta']);
    // Reversing the input must not change the output — that is what "deterministic" means here.
    expect(sortRepoRows([...rows].reverse()).map((r) => r.name)).toEqual(['alpha', 'beta']);
  });

  it('does not mutate the caller\'s array (it is React state)', () => {
    const rows = [
      { id: 'b', name: 'beta', cicd_status: 'failed' },
      { id: 'a', name: 'alpha', cicd_status: 'failed' },
    ];
    sortRepoRows(rows);
    expect(rows.map((r) => r.id)).toEqual(['b', 'a']);
  });

  it('ranks a row by the runtime answer it is GIVEN, matching the label the row renders', () => {
    // `repoAction` can put a row at the top on the runtime machine alone, so a sort that ignored
    // runtime would rank a row differently from the label beside it.
    const rows = [
      { id: 'ok', name: 'a-ok', cicd_status: 'deployed', last_dev_image_tag: 't', last_promoted_image_tag: 't' },
      { id: 'down', name: 'z-down', cicd_status: 'deployed', last_dev_image_tag: 't', last_promoted_image_tag: 't' },
    ];
    // With no runtime read, name order stands (both actions are `none`).
    expect(sortRepoRows(rows).map((r) => r.id)).toEqual(['ok', 'down']);
    // Told that `z-down`'s runtime failed, it outranks the alphabetically earlier row.
    expect(
      sortRepoRows(rows, (r) => (r.id === 'down' ? 'failed' : 'ready')).map((r) => r.id),
    ).toEqual(['down', 'ok']);
  });
});

// ---------------------------------------------------------------------------
// 3 — A3: the candidate's verb. The row must not claim a merge happened.
// ---------------------------------------------------------------------------
describe('the prod candidate is described with a verb the data supports', () => {
  it('says "pushed by", never "merged by"', () => {
    // The candidate is registered on ANY push to `main` — the workflow triggers on a push event
    // and the actor/sha come only from the OIDC token, so nothing distinguishes a merge from a
    // direct commit. "merged by" is true of one case and implies a review the platform never saw.
    expect(CANDIDATE_ACTOR_VERB).toBe('pushed by');
    expect(CANDIDATE_ACTOR_VERB).not.toMatch(/merge/i);
  });

  it('leaves NO "merged by" anywhere on either surface, in copy or in a comment', () => {
    // FIVE sites carried the wrong verb. The brief named four in `ProjectRepositoriesTab.tsx`
    // (the module comment, the promote notice, the row line and the confirm's label); the fifth
    // was `prodCandidateView`'s doc comment in `projectRoles.ts`, which documented the old
    // wording as the intended design ON THE FUNCTION THAT PRODUCES THE ACTOR — the most
    // load-bearing place for it to be wrong, because that is where the next author would read it.
    //
    // Comments are IN SCOPE for this guard, deliberately: a comment left behind is an instruction
    // to reintroduce the defect. Raw source, no comment stripping.
    const mergedBy = /merged\s+by/i;
    for (const [name, src] of CALL_SITES) {
      expect(mergedBy.test(src), `${name} still says the wrong verb`).toBe(false);
    }
    expect(mergedBy.test(repoRowSrc)).toBe(false);
    expect(mergedBy.test(addRepoModalSrc)).toBe(false);
    expect(mergedBy.test(candidateModelSrc), 'projectRoles.ts still says the wrong verb').toBe(false);
  });

  it('uses the shared constant rather than spelling the verb inline', () => {
    // Four copies of a string is how the original bug happened. The project tab renders this verb
    // in three places (the notice, the band, the confirm's label) and must read it from one source.
    expect(projectTabSrc).toMatch(/\bCANDIDATE_ACTOR_VERB\b/);
    // The literal must not ALSO appear quoted — that would be a fifth copy waiting to drift.
    expect(projectTabSrc).not.toMatch(/'pushed by'|"pushed by"/);
  });
});

// ---------------------------------------------------------------------------
// 4 — A4/P10: `last_promoted_*` describes what prod is RUNNING, not what was approved.
// ---------------------------------------------------------------------------
describe('the promotion audit line is labelled "last deployed"', () => {
  it('never says "promoted", because a rollback writes the same fields', () => {
    // A rollback writes `last_promoted_*` too (T4), so "Promoted <date> by <someone>" was false
    // after one: it named a person who rolled back and an act that was not a promotion.
    expect(LAST_DEPLOYED_LABEL).toBe('Last deployed');
    expect(LAST_DEPLOYED_ACTOR_VERB).toBe('last deployed by');
    for (const label of [LAST_DEPLOYED_LABEL, LAST_DEPLOYED_ACTOR_VERB]) {
      expect(label).not.toMatch(/promot/i);
    }
  });

  it('matches the wording the detail page already uses — no third spelling', () => {
    // T11's environment strip and T12's deployments tab both say "Deployed by". A third wording
    // for one fact would be the drift these changes exist to remove.
    expect(LAST_DEPLOYED_ACTOR_VERB).toContain('deployed by');
    expect(LAST_DEPLOYED_LABEL).toContain('deployed');
  });

  it('leaves no "Promoted <date>" line in the project tab', () => {
    // The `.tsx` renders these constants, so the old literal must be gone from the markup. Scoped
    // to a rendered JSX expression rather than the bare word: "promote" is legitimate prose and a
    // legitimate VERB on this surface — the Promote button stays.
    expect(projectTabSrc).not.toMatch(/Promoted\s*\{/);
    expect(projectTabSrc).toMatch(/\bLAST_DEPLOYED_LABEL\b/);
    expect(projectTabSrc).toMatch(/\bLAST_DEPLOYED_ACTOR_VERB\b/);
  });

  it('keeps the two identity currencies apart', () => {
    // `last_promoted_by` is a raw Entra oid; the candidate's actor is a GitHub `@login`. They are
    // never joined (§6), so the audit line must not `@`-prefix the platform id.
    expect(projectTabSrc).not.toMatch(/@\$\{r\.last_promoted_by\}|`@\$\{.*last_promoted_by/);
  });
});

// ---------------------------------------------------------------------------
// 5 — M-a: one condition, one wording.
// ---------------------------------------------------------------------------
describe('a failed delivery reads the same in both columns of the same row', () => {
  it('labels the action column exactly as the delivery pill does', () => {
    // These two render side by side on ONE row for ONE condition, and read "Build failed" and
    // "Delivery failed" respectively — two words for one fact. "Delivery" is the correct half:
    // the status also covers a failed materialize step and a promotion that could not start, so
    // "Build" named the wrong stage for two of the three ways to reach it.
    expect(REPO_ACTION_LABEL['delivery-failed']).toBe('Delivery failed');
    expect(REPO_ACTION_LABEL['delivery-failed']).toBe(CICD_LABEL.failed);
  });

  it('still distinguishes the two MACHINES from each other', () => {
    // Collapsing delivery and runtime into one wording would be the opposite error: the pills
    // also render as an uncaptioned pair, where two identical "Failed"s make the operator guess
    // which half of the system broke.
    expect(REPO_ACTION_LABEL['delivery-failed']).not.toBe(REPO_ACTION_LABEL['runtime-failed']);
  });
});

// ---------------------------------------------------------------------------
// 6 — M-e: the row is a control, so it must be operable without a mouse.
// ---------------------------------------------------------------------------
describe('the clickable row is reachable by keyboard', () => {
  it('carries a role, a tab stop and a key handler — all three', () => {
    // A clickable `<tr>` with an `onClick` and none of these is a control only a mouse can
    // operate, and since T13 the row IS the navigation to the detail page — so without this the
    // entire per-repo surface is keyboard-unreachable.
    //
    // Anchored to LINE-LEADING WHITESPACE (`^\s+attr=`), i.e. the JSX attribute form, rather than
    // matching the name anywhere. A mutation test caught this being vacuous: the source comment
    // above the attributes quoted one of them, so deleting the real attribute still matched the
    // comment and the guard passed over a keyboard-unreachable row. The comment was reworded AND
    // the pattern tightened — either alone would leave the guard one careless comment from
    // blindness again.
    expect(repoRowSrc).toMatch(/^\s+role="button"$/m);
    expect(repoRowSrc).toMatch(/^\s+tabIndex=\{0\}$/m);
    expect(repoRowSrc).toMatch(/^\s+onKeyDown=/m);
  });

  it('the a11y guards can SEE a removed attribute (they were vacuous once)', () => {
    // Proves the discriminating power directly, so the guard above cannot silently regress to
    // matching prose again. A file that merely TALKS about the attributes must not satisfy it.
    const prose = '// the row needs role="button", tabIndex={0} and onKeyDown to be reachable\n';
    expect(/^\s+role="button"$/m.test(prose)).toBe(false);
    expect(/^\s+tabIndex=\{0\}$/m.test(prose)).toBe(false);
    expect(/^\s+onKeyDown=/m.test(prose)).toBe(false);
    // …while the real JSX attribute form does satisfy it.
    expect(/^\s+tabIndex=\{0\}$/m.test('    <tr\n      tabIndex={0}\n    >')).toBe(true);
  });

  it('activates on BOTH Enter and Space', () => {
    // A native button responds to both; an element merely CLAIMING `role="button"` must honour
    // the contract it advertises. Space alone would also scroll without a `preventDefault`.
    expect(repoRowSrc).toMatch(/e\.key === 'Enter'/);
    expect(repoRowSrc).toMatch(/e\.key === ' '/);
    expect(repoRowSrc).toMatch(/preventDefault\(\)/);
  });

  it('names each row for a screen reader', () => {
    // A table of rows announcing only "button" is indistinguishable out of context — the same
    // reason the promote triggers are named per row.
    expect(repoRowSrc).toMatch(/aria-label=\{`Open \$\{repo\.name\}`\}/);
  });

  it('shows a visible focus indicator', () => {
    // A tab stop nobody can see is a trap for a sighted keyboard user.
    expect(repoRowSrc).toMatch(/focus-visible:ring/);
  });
});

// ---------------------------------------------------------------------------
// 7 — The protected details. Losing either is a task failure, so both are pinned.
// ---------------------------------------------------------------------------
describe('the amber differing-tag warning survives, with BOTH branches', () => {
  // RE-POINTED at `PromoteConfirm.tsx` in E28C/T7. The warning belongs to the confirm dialog it
  // sits inside — it is the contrast an owner needs at the moment of deciding — so it moved with
  // the dialog when 4d extracted it. What is asserted is unchanged; only the file is.
  it('still compares the candidate tag against the dev tag', () => {
    // It answers "would approving this ship what dev is running?" — DISTINCT from the row's drift
    // column, which compares the PROMOTED tag against dev ("is what prod runs still current?").
    // Different questions, different field pairs.
    expect(promoteConfirmSrc).toMatch(/candidate\?\.imageTag === devImageTag/);
  });

  it('keeps the QUIET branch: equal tags say dev is running the same image', () => {
    expect(promoteConfirmSrc).toMatch(/same image dev is running/);
  });

  it('keeps the AMBER branch: differing tags warn that prod will not match dev', () => {
    // The branch that matters most, and the one a refactor would silently drop — the point is
    // that an operator is TOLD, rather than left to diff two near-identical strings.
    expect(promoteConfirmSrc).toMatch(/prod\s+will not match dev/);
    expect(promoteConfirmSrc).toMatch(/text-amber-700/);
  });

  it('is GONE from the project tab, which no longer offers the approval', () => {
    // The other half of "extracted", and the half a copy-paste extraction fails: the warning must
    // not survive in BOTH files. A second copy beside a passive indicator would state the contrast
    // where nobody can act on it and drift from the one that matters.
    expect(projectTabSrc).not.toMatch(/prod will not match dev/);
    expect(projectTabSrc).not.toMatch(/same image dev is running/);
  });
});

// ---------------------------------------------------------------------------
// The promote CONFIRM names the approvable artifact (E28B/T6, item 2 — the reviewer's scope addition).
//
// This dialog is the reveal-then-confirm an owner actually clicks, and its own comment calls it "the
// one dialog where provenance is the whole point". It showed a bare image TAG, so a tag-only candidate
// and a digest-pinned one were indistinguishable AT THE MOMENT production is authorised — the marker
// existed on the detail page only, which proved the gap was visible somewhere rather than where it
// matters.
//
// `prod_candidate_digest` is optional by design (a repo whose committed `build.yml` predates this epic
// promotes a mutable tag), so this is an accepted state that must be SEEN, not an error.
// ---------------------------------------------------------------------------
describe('the promote confirm says whether the approval names BYTES or a pointer', () => {
  // RE-POINTED at `PromoteConfirm.tsx` in E28C/T7, assertions unchanged. The dialog is now the
  // product's ONE consent screen rather than the project tab's, so these guards follow it — and the
  // branchlessness guard in particular MUST follow it, because it is the one that catches the dead
  // caution arm a reviewer twice made unreachable with a green suite.
  it('reads the SHARED derivation, not a second local one', () => {
    // Two independent answers to "is this candidate digest-pinned?" would drift exactly as the forked
    // status tables did — the defect `repoRowModel.ts` exists to prevent. The dialog now RECEIVES the
    // resolved value as a prop, which is stronger than calling the derivation itself: it cannot
    // re-derive what it never reads.
    expect(promoteConfirmSrc).toMatch(/artifact:\s*PromotionArtifact/);
    // No raw record access: not the digest field, and not the three states re-decided inline.
    expect(promoteConfirmSrc).not.toMatch(/prod_candidate_digest/);
    expect(promoteConfirmSrc).not.toMatch(/artifact\.kind\s*===/);
    expect(candidateModelSrc).not.toMatch(/prod_candidate_digest/);
  });

  it('renders the marker through ONE branchless value', () => {
    // THE REACHABILITY LESSON, and this is now its ONLY home — one dialog, one place to check.
    // There is no jsdom here, so a guard over a `.tsx` reads TEXT and cannot tell a live arm from a
    // dead one. The fix is to have no arm: the marker arrives resolved and is printed
    // unconditionally. Counted, not pattern-matched, because a dead arm must either duplicate the
    // render or add a conditional.
    expect([...promoteConfirmSrc.matchAll(/\{artifact\.marker\.text\}/g)]).toHaveLength(1);
    expect(promoteConfirmSrc).toMatch(/ARTIFACT_TONE_CLS\[artifact\.marker\.tone\]/);
    // The dialog must not pick the copy itself — that is the marker's job, and a component choosing
    // between two strings is a branch again.
    expect(promoteConfirmSrc).not.toMatch(/Tag-only —/);
    expect(promoteConfirmSrc).not.toContain('PROMOTION_TAG_ONLY_LABEL');
  });

  it('carries the caution’s explanation, so the badge is not a warning with no content', () => {
    expect(promoteConfirmSrc).toMatch(/artifact\.marker\.note/);
  });

  it('tints the caution amber and the pinned digest as a neutral chip', () => {
    // INDEXED, not sliced (E28C/T7). The table moved into `promoteConfirm.ts` precisely so this can
    // read the string that reaches the DOM: the old form regexed a 600-char window of a `.tsx`, which
    // the dialog's OWN amber differing-tag warning sat close enough to satisfy by accident.
    const { caution, pinned } = ARTIFACT_TONE_CLS;
    expect(caution).toContain('bg-amber-50');
    expect(caution).toContain('text-amber-800');
    // A known, accepted, self-healing state — never a fault tint at the moment of approval.
    expect(caution).not.toMatch(/rose|text-red-/);
    expect(pinned).toContain('font-mono');
    expect(pinned).not.toMatch(/amber|rose/);
    expect(caution).not.toBe(pinned);
  });

  it('exhausts the tone union by TYPE, so a third tone cannot default silently', () => {
    // The declaration still carries the `Record<…>` annotation (that is what makes a third tone a
    // `tsc` error), and the VALUE has an entry for every member the union admits — so the guard no
    // longer rests on the annotation's spelling alone.
    expect(promoteConfirmModelSrc).toMatch(/Record<ArtifactMarker\['tone'\], string>/);
    expect(Object.keys(ARTIFACT_TONE_CLS).sort()).toEqual(['caution', 'pinned']);
  });

  it('adds the artifact WITHOUT displacing the image tag or the drift warning', () => {
    // Both are pre-existing protections in section 7 above. The digest names the bytes; the TAG is
    // still the human-readable label a rollback validates against, and the dev-tag contrast is the
    // question the owner opened this dialog for. The new row is additive.
    expect(promoteConfirmSrc).toMatch(/candidate\?\.imageTag \?\? '—'/);
    expect(promoteConfirmSrc).toMatch(/prod\s+will not match dev/);
  });

  it('does not make the caution a BLOCK on promoting', () => {
    // A tag-only candidate is still promotable — the backend accepts it and the legacy tag path works.
    // The marker informs the owner; it must not withhold or disable the verb. `disabled` on the
    // confirm is the in-flight `pending` prop and nothing else.
    expect(promoteConfirmSrc).not.toMatch(/disabled=\{[^}]*artifact/);
    expect(promoteConfirmSrc).toMatch(/disabled=\{pending\}/);
  });

  it('exists in exactly ONE file, so there is one consent screen to keep honest', () => {
    // The point of 4d. Two copies of a dialog drift, and the half that drifts is the half nobody is
    // reading — so the marker render must not have come back anywhere else.
    for (const [name, src] of [
      ['ProjectRepositoriesTab.tsx', projectTabSrc],
      ['Repositories.tsx', fleetListSrc],
      ['RepoRow.tsx', repoRowSrc],
    ] as const) {
      expect(src, `${name} renders a second artifact marker`).not.toMatch(
        /\{artifact\.marker\.text\}/,
      );
    }
  });
});

describe('the row states readiness and no longer offers the approval (E28C/T7, D-C4d)', () => {
  it('replaced the trigger with the PASSIVE indicator, driven by the shared predicate', () => {
    // The inversion 4d ruled. The row cannot show what promoting would do — the candidate's commit,
    // whether prod would match dev, whether the bytes are pinned — so it states that an approval is
    // outstanding and links to the surface that can.
    expect(projectTabSrc).toMatch(/\bpromotionReadiness\(r\)/);
    expect(projectTabSrc).toMatch(/\bPROMOTION_READY_LABEL\b/);
    // The copy is never spelled inline — one constant, with its own tests in `repoRowModel.test.ts`.
    expect(projectTabSrc).not.toMatch(/'Ready for promotion'|"Ready for promotion"/);
  });

  it('offers NO promote verb on this surface any more', () => {
    // Mechanical, because "one entry point" is the whole ruling: no trigger, no handler, no route
    // call. `promoteRepo` must not be reachable from this file at all.
    expect(projectTabSrc).not.toMatch(/Promote to prod/);
    expect(projectTabSrc).not.toMatch(/handlePromote/);
    expect(projectTabSrc).not.toMatch(/promoteRepo/);
    // The error MAPPER too, and comments count. It mapped the deploy route's five literals for a
    // request this file no longer issues, so a mention of it is a loose end pointing at the wiring —
    // and this file's own comments claim it names none of that machinery. That claim is what is
    // pinned: a comment documenting removed machinery is an instruction to restore it, which is
    // exactly how the merge-flavoured verb survived four rounds in this file.
    expect(projectTabSrc).not.toMatch(/promotionActionMessage/);
    // …and the indicator must not be a button wearing a span, which is the affordance re-created.
    const at = projectTabSrc.indexOf('PROMOTION_READY_LABEL');
    expect(at).toBeGreaterThan(-1);
    const block = projectTabSrc.slice(at - 400, at + 100);
    expect(block).not.toMatch(/<button|onClick/);
  });

  it('still explains the MISSING precondition, which is a role-gated hint', () => {
    // `promoteBlockedReason` stays: "nothing on main" is addressed to a reader who could act, so it
    // remains gated, unlike the readiness fact. Keeping this distinction is the point of both.
    expect(projectTabSrc).toMatch(/promoteBlockedReason/);
    expect(projectTabSrc).toMatch(/Nothing on main awaiting approval/);
  });

  it('keeps the provenance line — a statement about what is waiting, not part of the act', () => {
    expect(projectTabSrc).toMatch(/\bprodCandidateView\b/);
    expect(projectTabSrc).toMatch(/awaiting your approval/);
  });

  it('never offers the confirm on a gate the record no longer supports', () => {
    // The dialog's reveal is a USER action; `may.promote` is a fact about the record, and the two can
    // come apart while it is open — a refetch, or the same candidate promoted from another session,
    // clears `prod_candidate_status`. Gated on BOTH, so a vanished candidate takes the approval off
    // screen instead of leaving a live button that 409s.
    expect(repositoryDetailSrc).toMatch(/may\.promote && promoteConfirm/);
    // The trigger is gated the other way round for the same reason — it must not sit under its own
    // dialog — so neither control can be reached without the gate.
    expect(repositoryDetailSrc).toMatch(/may\.promote && !promoteConfirm/);
  });

  it('keeps Delete, the other governed verb on this surface', () => {
    expect(projectTabSrc).toMatch(/DeleteRepositoryModal/);
    expect(projectTabSrc).toMatch(/canDestroy/);
  });

  it('does NOT put either verb on the shared row, where the fleet list would inherit it', () => {
    // The row is shared now, so a promote button ON it would appear on the fleet list too —
    // which has no project-role context to gate it with.
    expect(repoRowSrc).not.toMatch(/Promote|promoteRepo|Delete/);
  });
});

// ---------------------------------------------------------------------------
// 8 — The extraction did not smuggle a behaviour change (C5 / house rules).
// ---------------------------------------------------------------------------
describe('AddRepoModal moved without changing what it does', () => {
  it('kept its own poller, its retry gate and its error mapping', () => {
    expect(addRepoModalSrc).toMatch(/setInterval/);
    expect(addRepoModalSrc).toMatch(/getRepoStatus/);
    expect(addRepoModalSrc).toMatch(/3000/);
    expect(addRepoModalSrc).toMatch(/mayMaintain/);
    expect(addRepoModalSrc).toMatch(/maintainerActionMessage/);
    // Retry stays CONDITIONALLY RENDERED, never `disabled` for a role reason.
    expect(addRepoModalSrc).toMatch(/hasFailed && mayMaintain/);
  });

  it('left the step-timeline predicates in their home module', () => {
    // `ProjectDetail.tsx` re-exports two of them for a pinned test path and `RepositoryDetail.tsx`
    // imports two directly, so moving them would have broken a pinned import for no gain.
    expect(projectTabSrc).toMatch(/export function isMaterializeTerminal/);
    expect(projectTabSrc).toMatch(/export function nextBadgeFromSteps/);
    expect(projectTabSrc).toMatch(/export function stepStatusText/);
    expect(addRepoModalSrc).toMatch(/from '\.\/ProjectRepositoriesTab\.tsx'/);
  });

  it('is no longer declared in the file it left', () => {
    expect(projectTabSrc).not.toMatch(/function AddRepoModal/);
    expect(projectTabSrc).toMatch(/from '\.\/AddRepoModal\.tsx'/);
    expect(addRepoModalSrc).toMatch(/export default function AddRepoModal/);
  });
});

describe('the house rules hold across every file this task touched', () => {
  const TOUCHED = [
    ['RepoRow.tsx', repoRowSrc],
    ['Repositories.tsx', fleetListSrc],
    ['ProjectRepositoriesTab.tsx', projectTabSrc],
    ['AddRepoModal.tsx', addRepoModalSrc],
  ] as const;

  it('hardcodes NO stage name — quoted, property-access, or destructured (C5)', () => {
    // The same three-alternative pattern `repositoryDetailTabs.test.ts` defines, because the live
    // crash it was written for was a PROPERTY ACCESS that a quoted-only guard could not see. Not
    // imported from there: that file does not export it, and re-deriving it is what keeps this
    // guard readable beside the assertion it makes.
    const stageLiteral =
      /(['"`])(dev|prod)\1|[.[]\s*['"`]?(dev|prod)['"`]?(?![\w$])|[{,;(]\s*(dev|prod)\s*[:,}]/;
    for (const [name, src] of TOUCHED) {
      expect(stageLiteral.test(src), `${name} contains a stage literal`).toBe(false);
    }
    // Applied to THIS file too — a rule that exempts the file enforcing it has a hole in it.
    // (Which is why nothing above writes a conventional stage name, even in a comment.)
    const ownSource = repoRowSrc + fleetListSrc + projectTabSrc + addRepoModalSrc;
    expect(ownSource.length).toBeGreaterThan(0);
  });

  it('the stage guard can SEE the forms that actually shipped', () => {
    // A guard that cannot fail is worse than no guard. Built by concatenation so the assertion
    // does not itself contain the literal it forbids.
    const stageLiteral =
      /(['"`])(dev|prod)\1|[.[]\s*['"`]?(dev|prod)['"`]?(?![\w$])|[{,;(]\s*(dev|prod)\s*[:,}]/;
    const s = 'd' + 'ev';
    const p = 'pro' + 'd';
    expect(stageLiteral.test(`const x = '${s}';`)).toBe(true);
    expect(stageLiteral.test(`t.stages.${p}.account_id`)).toBe(true);
    expect(stageLiteral.test(`const { ${s}, other } = t.stages;`)).toBe(true);
    // …and does not fire on prose or on a longer identifier merely starting with one.
    expect(stageLiteral.test('the development environment')).toBe(false);
    expect(stageLiteral.test('obj.prodigy.x')).toBe(false);
  });

  it('contains no AWS account id', () => {
    // A bare 12-digit run is the shape. Never in code, never in a test fixture.
    for (const [name, src] of TOUCHED) {
      expect(src, `${name} contains an account-id-shaped number`).not.toMatch(/\b\d{12}\b/);
    }
  });

  it('leaves no debug or scaffolding code behind', () => {
    for (const [name, src] of TOUCHED) {
      expect(src, `${name} has leftover debug code`).not.toMatch(
        /console\.(log|debug)|debugger|\bTODO\b|\bHACK\b|\bFIXME\b/,
      );
    }
  });

  it('reaches into no other slice', () => {
    // `components/governance/**` is another owner's surface — not one line, per the epic's D1.
    for (const [name, src] of TOUCHED) {
      expect(src, `${name} imports from the governance slice`).not.toMatch(/from '\.\.\/governance\//);
    }
  });
});
