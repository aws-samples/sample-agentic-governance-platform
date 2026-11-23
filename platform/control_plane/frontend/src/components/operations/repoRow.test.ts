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
import {
  GATEWAY_DEPLOYED_LABEL,
  MCP_KIND_CHIP_LABEL,
  repoKindChip,
  rowActionForKind,
} from './repositoryDetailTabs';
import repoRowRaw from './RepoRow.tsx?raw';
import fleetListRaw from './Repositories.tsx?raw';
import projectTabRaw from './ProjectRepositoriesTab.tsx?raw';
import materializeStepsRaw from './materializeSteps.ts?raw';
import addRepoModalRaw from './AddRepoModal.tsx?raw';
import promoteConfirmRaw from './PromoteConfirm.tsx?raw';
import repositoryDetailRaw from './RepositoryDetail.tsx?raw';
import overviewTabRaw from './repo-tabs/OverviewTab.tsx?raw';
import accessTabRaw from './repo-tabs/AccessTab.tsx?raw';
import repoSummaryCardRaw from './RepoSummaryCard.tsx?raw';
import repoFieldsRaw from './RepoFields.tsx?raw';
import useRepositoryDetailRaw from './useRepositoryDetail.ts?raw';
import promoteConfirmModelRaw from './promoteConfirmModel.ts?raw';
import { ARTIFACT_TONE_CLS } from './promoteConfirmModel';
import candidateModelRaw from './projectRoles.ts?raw';
import { stripComments } from '../../rawSource';

const repoRowSrc = stripComments(repoRowRaw);
const fleetListSrc = stripComments(fleetListRaw);
const projectTabSrc = stripComments(projectTabRaw);
const materializeStepsSrc = stripComments(materializeStepsRaw);
const addRepoModalSrc = stripComments(addRepoModalRaw);
const promoteConfirmSrc = stripComments(promoteConfirmRaw);
const repositoryDetailSrc = [repositoryDetailRaw, overviewTabRaw, accessTabRaw, repoSummaryCardRaw, repoFieldsRaw, useRepositoryDetailRaw].map(stripComments).join('\n');
const promoteConfirmModelSrc = stripComments(promoteConfirmModelRaw);
const candidateModelSrc = stripComments(candidateModelRaw);

const CALL_SITES = [
  ['Repositories.tsx', fleetListSrc],
  ['ProjectRepositoriesTab.tsx', projectTabSrc],
] as const;

describe('both lists render THE SAME row component', () => {
  it('reads every source (so no assertion below passes on an empty string)', () => {
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
    for (const [name, src] of CALL_SITES) {
      expect(src, `${name} does not render RepoRow`).toMatch(/<RepoRow\b/);
      const bareTds = src
        .split('\n')
        .filter((line) => /<td\b/.test(line) && !/colSpan/.test(line));
      expect(bareTds, `${name} still declares un-spanned row cells:\n${bareTds.join('\n')}`).toEqual([]);
    }
  });

  it('keeps every status LABEL and TINT out of both call sites', () => {
    const statusTables =
      /\b(CICD_LABEL|RUNTIME_LABEL|CICD_BADGE_KEY|RUNTIME_BADGE_KEY|promotionStatusLabel|cicdBadgeKey)\b/;
    for (const [name, src] of CALL_SITES) {
      expect(statusTables.test(src), `${name} reads a status table directly`).toBe(false);
    }
    expect(statusTables.test(repoRowSrc)).toBe(true);
  });

  it('leaves the whole status palette to the row: no OPS_BADGE lookup at either call site', () => {
    for (const [name, src] of CALL_SITES) {
      expect(src, `${name} indexes the badge palette`).not.toMatch(/OPS_BADGE\s*\[/);
    }
    expect(addRepoModalSrc).toMatch(/OPS_BADGE\s*\[/);
  });

  it('differs between the two call sites ONLY in `showProject`', () => {
    expect(fleetListSrc).toMatch(/showProject\b(?!\s*=\s*\{false\})/);
    expect(projectTabSrc).toMatch(/showProject=\{false\}/);
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
    expect(onlyInOne.filter((p) => !['showProject', 'projectName', 'key'].includes(p))).toEqual([]);
  });

  it('never grows a governance verb on the row (the shadow-governance rule)', () => {
    expect(repoRowSrc).not.toMatch(/on(Approve|Grant|Classify|Deprecate|Promote|Rollback)\s*[?:]/);
  });
});

describe('both lists order rows through the shared sort', () => {
  it('calls `sortRepoRows` at both call sites and sorts nowhere else', () => {
    for (const [name, src] of CALL_SITES) {
      expect(src, `${name} does not use the shared sort`).toMatch(/\bsortRepoRows\(/);
      expect(src, `${name} sorts rows locally`).not.toMatch(/\.sort\(/);
    }
  });

  it('puts what needs a human FIRST, identically for either list', () => {
    const rows: (RepoRowSource & { id: string; name: string })[] = [
      { id: 'r1', name: 'quiet', cicd_status: 'deployed', last_dev_image_tag: 't', last_promoted_image_tag: 't' },
      { id: 'r2', name: 'drifting', cicd_status: 'deployed', last_dev_image_tag: 'new', last_promoted_image_tag: 'old' },
      { id: 'r3', name: 'broken', cicd_status: 'failed' },
      { id: 'r4', name: 'waiting', cicd_status: 'deployed', prod_candidate_status: 'pending', last_promoted_image_tag: 't', last_dev_image_tag: 't' },
    ];
    expect(sortRepoRows(rows).map((r) => r.id)).toEqual(['r3', 'r4', 'r2', 'r1']);
  });

  it('breaks ties on NAME, not on whatever order the API returned', () => {
    const rows: (RepoRowSource & { id: string; name: string })[] = [
      { id: 'b', name: 'beta', cicd_status: 'failed' },
      { id: 'a', name: 'alpha', cicd_status: 'failed' },
    ];
    expect(sortRepoRows(rows).map((r) => r.name)).toEqual(['alpha', 'beta']);
    expect(sortRepoRows([...rows].reverse()).map((r) => r.name)).toEqual(['alpha', 'beta']);
  });

  it('does not mutate the caller\'s array (it is React state)', () => {
    const rows: (RepoRowSource & { id: string; name: string })[] = [
      { id: 'b', name: 'beta', cicd_status: 'failed' },
      { id: 'a', name: 'alpha', cicd_status: 'failed' },
    ];
    sortRepoRows(rows);
    expect(rows.map((r) => r.id)).toEqual(['b', 'a']);
  });

  it('ranks a row by the runtime answer it is GIVEN, matching the label the row renders', () => {
    const rows: (RepoRowSource & { id: string; name: string })[] = [
      { id: 'ok', name: 'a-ok', cicd_status: 'deployed', last_dev_image_tag: 't', last_promoted_image_tag: 't' },
      { id: 'down', name: 'z-down', cicd_status: 'deployed', last_dev_image_tag: 't', last_promoted_image_tag: 't' },
    ];
    expect(sortRepoRows(rows).map((r) => r.id)).toEqual(['ok', 'down']);
    expect(
      sortRepoRows(rows, (r) => (r.id === 'down' ? 'failed' : 'ready')).map((r) => r.id),
    ).toEqual(['down', 'ok']);
  });
});

describe('the prod candidate is described with a verb the data supports', () => {
  it('says "pushed by", never "merged by"', () => {
    expect(CANDIDATE_ACTOR_VERB).toBe('pushed by');
    expect(CANDIDATE_ACTOR_VERB).not.toMatch(/merge/i);
  });

  it('leaves NO "merged by" in the code of either surface, nor in the module that produces the actor', () => {
    const mergedBy = /merged\s+by/i;
    for (const [name, src] of CALL_SITES) {
      expect(mergedBy.test(src), `${name} still says the wrong verb`).toBe(false);
    }
    expect(mergedBy.test(repoRowSrc)).toBe(false);
    expect(mergedBy.test(addRepoModalSrc)).toBe(false);
    expect(mergedBy.test(candidateModelSrc), 'projectRoles.ts still says the wrong verb').toBe(false);
  });

  it('uses the shared constant rather than spelling the verb inline', () => {
    expect(projectTabSrc).toMatch(/\bCANDIDATE_ACTOR_VERB\b/);
    expect(projectTabSrc).not.toMatch(/'pushed by'|"pushed by"/);
  });
});

describe('the promotion audit line is labelled "last deployed"', () => {
  it('never says "promoted", because a rollback writes the same fields', () => {
    expect(LAST_DEPLOYED_LABEL).toBe('Last deployed');
    expect(LAST_DEPLOYED_ACTOR_VERB).toBe('last deployed by');
    for (const label of [LAST_DEPLOYED_LABEL, LAST_DEPLOYED_ACTOR_VERB]) {
      expect(label).not.toMatch(/promot/i);
    }
  });

  it('matches the wording the detail page already uses — no third spelling', () => {
    expect(LAST_DEPLOYED_ACTOR_VERB).toContain('deployed by');
    expect(LAST_DEPLOYED_LABEL).toContain('deployed');
  });

  it('leaves no "Promoted <date>" line in the project tab', () => {
    expect(projectTabSrc).not.toMatch(/Promoted\s*\{/);
    expect(repoRowSrc).not.toMatch(/Promoted\s*\{/);
  });

  it('renders the audit line in the shared row, on every deployed row of both lists', () => {
    expect(repoRowSrc).toMatch(/\bLAST_DEPLOYED_LABEL\b/);
    expect(repoRowSrc).toMatch(/\bLAST_DEPLOYED_ACTOR_VERB\b/);
    expect(repoRowSrc).toMatch(/entraActorName\(repo\.last_promoted_by/);
    expect(projectTabSrc).not.toMatch(/\bLAST_DEPLOYED_LABEL\b/);
  });

  it('keeps the two identity currencies apart', () => {
    for (const src of [projectTabSrc, repoRowSrc]) {
      expect(src).not.toMatch(/@\$\{(r|repo)\.last_promoted_by\}|`@\$\{.*last_promoted_by/);
    }
  });
});

describe('a failed delivery reads the same in both columns of the same row', () => {
  it('labels the action column exactly as the delivery pill does', () => {
    expect(REPO_ACTION_LABEL['delivery-failed']).toBe('Delivery failed');
    expect(REPO_ACTION_LABEL['delivery-failed']).toBe(CICD_LABEL.failed);
  });

  it('still distinguishes the two MACHINES from each other', () => {
    expect(REPO_ACTION_LABEL['delivery-failed']).not.toBe(REPO_ACTION_LABEL['runtime-failed']);
  });
});

describe('the clickable row is reachable by keyboard', () => {
  it('carries a role, a tab stop and a key handler — all three', () => {
    expect(repoRowSrc).toMatch(/^\s+role="button"$/m);
    expect(repoRowSrc).toMatch(/^\s+tabIndex=\{0\}$/m);
    expect(repoRowSrc).toMatch(/^\s+onKeyDown=/m);
  });

  it('the a11y guards can SEE a removed attribute and are not satisfied by prose', () => {
    const prose = '// the row needs role="button", tabIndex={0} and onKeyDown to be reachable\n';
    expect(/^\s+role="button"$/m.test(prose)).toBe(false);
    expect(/^\s+tabIndex=\{0\}$/m.test(prose)).toBe(false);
    expect(/^\s+onKeyDown=/m.test(prose)).toBe(false);
    expect(/^\s+tabIndex=\{0\}$/m.test('    <tr\n      tabIndex={0}\n    >')).toBe(true);
  });

  it('activates on BOTH Enter and Space', () => {
    expect(repoRowSrc).toMatch(/e\.key === 'Enter'/);
    expect(repoRowSrc).toMatch(/e\.key === ' '/);
    expect(repoRowSrc).toMatch(/preventDefault\(\)/);
  });

  it('names each row for a screen reader', () => {
    expect(repoRowSrc).toMatch(/aria-label=\{`Open \$\{repo\.name\}`\}/);
  });

  it('shows a visible focus indicator', () => {
    expect(repoRowSrc).toMatch(/focus-visible:ring/);
  });
});

describe('the amber differing-tag warning survives, with BOTH branches', () => {
  it('still compares the candidate tag against the dev tag', () => {
    expect(promoteConfirmSrc).toMatch(/candidate\?\.imageTag === devImageTag/);
  });

  it('keeps the QUIET branch: equal tags say dev is running the same image', () => {
    expect(promoteConfirmSrc).toMatch(/same \{isRevision[^}]*\} dev is running/);
  });

  it('keeps the AMBER branch: differing tags warn that prod will not match dev', () => {
    expect(promoteConfirmSrc).toMatch(/prod\s+will not match dev/);
    expect(promoteConfirmSrc).toMatch(/text-amber-700/);
  });

  it('is GONE from the project tab, which no longer offers the approval', () => {
    expect(projectTabSrc).not.toMatch(/prod will not match dev/);
    expect(projectTabSrc).not.toMatch(/same image dev is running/);
  });

  it('labels the field "Revision" vs. "Image" from the candidate tag shape, not a new flag', () => {
    expect(promoteConfirmSrc).toMatch(/isCommitShaTag\(candidate\?\.imageTag\)/);
    expect(promoteConfirmSrc).toMatch(/isRevision\s*\?\s*'Revision'\s*:\s*'Image'/);
  });

  it('names the differing branch\'s tag "revision" too, and abbreviates a commit sha there', () => {
    expect(promoteConfirmSrc).toMatch(/isRevision\s*\?\s*'revision'\s*:\s*'image'/);
    expect(promoteConfirmSrc).toMatch(/isCommitShaTag\(devImageTag\)\s*\?\s*shortCommitSha\(devImageTag\)/);
  });
});

describe('the promote confirm says whether the approval names BYTES or a pointer', () => {
  it('reads the SHARED derivation, not a second local one', () => {
    expect(promoteConfirmSrc).toMatch(/artifact:\s*PromotionArtifact/);
    expect(promoteConfirmSrc).not.toMatch(/prod_candidate_digest/);
    expect(promoteConfirmSrc).not.toMatch(/artifact\.kind\s*===/);
    expect(candidateModelSrc).not.toMatch(/prod_candidate_digest/);
  });

  it('renders the marker through ONE branchless value', () => {
    expect([...promoteConfirmSrc.matchAll(/\{artifact\.marker\.text\}/g)]).toHaveLength(1);
    expect(promoteConfirmSrc).toMatch(/ARTIFACT_TONE_CLS\[artifact\.marker\.tone\]/);
    expect(promoteConfirmSrc).not.toMatch(/Tag-only —/);
    expect(promoteConfirmSrc).not.toContain('PROMOTION_TAG_ONLY_LABEL');
  });

  it('carries the caution’s explanation, so the badge is not a warning with no content', () => {
    expect(promoteConfirmSrc).toMatch(/artifact\.marker\.note/);
  });

  it('tints the caution amber and the pinned digest as a neutral chip', () => {
    const { caution, pinned } = ARTIFACT_TONE_CLS;
    expect(caution).toContain('bg-amber-50');
    expect(caution).toContain('text-amber-800');
    expect(caution).not.toMatch(/rose|text-red-/);
    expect(pinned).toContain('font-mono');
    expect(pinned).not.toMatch(/amber|rose/);
    expect(caution).not.toBe(pinned);
  });

  it('exhausts the tone union by TYPE, so a third tone cannot default silently', () => {
    expect(promoteConfirmModelSrc).toMatch(/Record<ArtifactMarker\['tone'\], string>/);
    expect(Object.keys(ARTIFACT_TONE_CLS).sort()).toEqual(['caution', 'pinned']);
  });

  it('adds the artifact WITHOUT displacing the image tag or the drift warning', () => {
    expect(promoteConfirmSrc).toMatch(/candidate\?\.imageTag \?\? '—'/);
    expect(promoteConfirmSrc).toMatch(/prod\s+will not match dev/);
  });

  it('does not make the caution a BLOCK on promoting', () => {
    expect(promoteConfirmSrc).not.toMatch(/disabled=\{[^}]*artifact/);
    expect(promoteConfirmSrc).toMatch(/disabled=\{pending\}/);
  });

  it('exists in exactly ONE file, so there is one consent screen to keep honest', () => {
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

describe('the row states readiness and no longer offers the approval', () => {
  it('replaced the trigger with the PASSIVE indicator, driven by the shared predicate', () => {
    expect(projectTabSrc).toMatch(/\brepoActionBand\(r, promoteState, inFlight\)/);
    expect(projectTabSrc).toMatch(/\bPROMOTION_READY_LABEL\b/);
    expect(projectTabSrc).not.toMatch(/'Ready for promotion'|"Ready for promotion"/);
  });

  it('offers NO promote verb on this surface any more', () => {
    expect(projectTabSrc).not.toMatch(/Promote to prod/);
    expect(projectTabSrc).not.toMatch(/handlePromote/);
    expect(projectTabSrc).not.toMatch(/promoteRepo/);
    expect(projectTabSrc).not.toMatch(/promotionActionMessage/);
    const at = projectTabSrc.indexOf('PROMOTION_READY_LABEL');
    expect(at).toBeGreaterThan(-1);
    const block = projectTabSrc.slice(at - 400, at + 100);
    expect(block).not.toMatch(/<button|onClick/);
  });

  it('renders the band only for actionable states — no "nothing waiting" line', () => {
    expect(projectTabSrc).toMatch(/promoteBlockedReason/);
    expect(projectTabSrc).not.toMatch(/Nothing on main awaiting approval/);
  });

  it('keeps the provenance line — a statement about what is waiting, not part of the act', () => {
    expect(projectTabSrc).toMatch(/\bprodCandidateView\b/);
    expect(projectTabSrc).toMatch(/awaiting your approval/);
  });

  it('never offers the confirm on a gate the record no longer supports', () => {
    expect(repositoryDetailSrc).toMatch(/may\.promote && promoteConfirm/);
    expect(repositoryDetailSrc).toMatch(/may\.promote && !promoteConfirm/);
  });

  it('offers Delete on the detail page only, not in the list', () => {
    expect(projectTabSrc).not.toMatch(/DeleteRepositoryModal|canDestroy|setDeleteTarget/);
    expect(repositoryDetailSrc).toMatch(/DeleteRepositoryModal/);
  });

  it('does NOT put either verb on the shared row, where the fleet list would inherit it', () => {
    expect(repoRowSrc).not.toMatch(/Promote|promoteRepo|Delete/);
  });
});

describe('AddRepoModal moved without changing what it does', () => {
  it('kept its own poller, its retry gate and its error mapping', () => {
    expect(addRepoModalSrc).toMatch(/setInterval/);
    expect(addRepoModalSrc).toMatch(/getRepoStatus/);
    expect(addRepoModalSrc).toMatch(/3000/);
    expect(addRepoModalSrc).toMatch(/mayMaintain/);
    expect(addRepoModalSrc).toMatch(/maintainerActionMessage/);
    expect(addRepoModalSrc).toMatch(/hasFailed && mayMaintain/);
  });

  it('left the step-timeline predicates in their home module', () => {
    expect(materializeStepsSrc).toMatch(/export function isMaterializeTerminal/);
    expect(materializeStepsSrc).toMatch(/export function nextBadgeFromSteps/);
    expect(materializeStepsSrc).toMatch(/export function stepStatusText/);
    expect(addRepoModalSrc).toMatch(/from '\.\/materializeSteps\.ts'/);
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

  it('hardcodes NO stage name — quoted, property-access, or destructured', () => {
    const stageLiteral =
      /(['"`])(dev|prod)\1|[.[]\s*['"`]?(dev|prod)['"`]?(?![\w$])|[{,;(]\s*(dev|prod)\s*[:,}]/;
    for (const [name, src] of TOUCHED) {
      expect(stageLiteral.test(src), `${name} contains a stage literal`).toBe(false);
    }
    const ownSource = repoRowSrc + fleetListSrc + projectTabSrc + addRepoModalSrc;
    expect(ownSource.length).toBeGreaterThan(0);
  });

  it('the stage guard can SEE the forms that actually shipped', () => {
    const stageLiteral =
      /(['"`])(dev|prod)\1|[.[]\s*['"`]?(dev|prod)['"`]?(?![\w$])|[{,;(]\s*(dev|prod)\s*[:,}]/;
    const s = 'd' + 'ev';
    const p = 'pro' + 'd';
    expect(stageLiteral.test(`const x = '${s}';`)).toBe(true);
    expect(stageLiteral.test(`t.stages.${p}.account_id`)).toBe(true);
    expect(stageLiteral.test(`const { ${s}, other } = t.stages;`)).toBe(true);
    expect(stageLiteral.test('the development environment')).toBe(false);
    expect(stageLiteral.test('obj.prodigy.x')).toBe(false);
  });

  it('contains no AWS account id', () => {
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
    for (const [name, src] of TOUCHED) {
      expect(src, `${name} imports from the governance slice`).not.toMatch(/from '\.\.\/governance\//);
    }
  });
});

describe('RepoRow per kind', () => {
  const agentRow = { artifact_kind: 'agent' as const, gateway_arn: null };
  const mcpBuilt = { artifact_kind: 'mcp_server' as const, gateway_arn: 'arn:aws:bedrock-agentcore:us-east-1:000000000000:gateway/g-1' };
  const mcpNew = { artifact_kind: 'mcp_server' as const, gateway_arn: null };

  it('only MCP rows carry a kind chip', () => {
    expect(repoKindChip(agentRow)).toBeNull();
    expect(repoKindChip({})).toBeNull();
    expect(repoKindChip(mcpNew)).toBe(MCP_KIND_CHIP_LABEL);
    expect(MCP_KIND_CHIP_LABEL).toBe('MCP server');
  });

  it('agent rows keep repoAction verbatim', () => {
    for (const a of ['approval-pending', 'drift', 'never-deployed', 'delivery-failed', 'none'] as const) {
      expect(rowActionForKind(agentRow, a)).toBe(a);
    }
  });

  it('MCP rows drop the prod-shaped actions, keyed on whether a gateway exists', () => {
    expect(rowActionForKind(mcpNew, 'never-deployed')).toBe('never-deployed');
    expect(rowActionForKind(mcpBuilt, 'never-deployed')).toBe('none');
    expect(rowActionForKind(mcpBuilt, 'approval-pending')).toBe('none');
    expect(rowActionForKind(mcpBuilt, 'drift')).toBe('none');
    expect(rowActionForKind(mcpBuilt, 'delivery-failed')).toBe('delivery-failed');
  });

  it('the row renders the chip and the gateway label for MCP repos', () => {
    expect(repoRowSrc).toMatch(/repoKindChip\(repo\)/);
    expect(repoRowSrc).toMatch(/gatewayStatus\(repo\)/);
    expect(repoRowSrc).toMatch(/rowActionForKind\(repo, repoAction\(/);
    expect(GATEWAY_DEPLOYED_LABEL).toMatch(/^Gateway/);
  });
});
