// T9 tests for the standalone Templates page (E28): the org-scoping state machine, the
// two empty states that must never collapse into one, and a SOURCE assertion that
// switching orgs actually refetches the catalog.
//
// Why the refetch test is a source assertion rather than a render test: vitest here
// collects only `src/**/*.test.ts` and there is no jsdom (see `vitest.config.ts` and the
// note in `githubLinkApi.test.ts`), so a `.tsx` is never mounted and its effects never
// run. The alternative — asserting on a hand-rolled mock of the fetch — would test the
// mock. Parsing the real dependency array out of the real component file tests the real
// thing, and fails when someone drops `connectionId` from it.
//
// Vite's `?raw` rather than `node:fs`: `tsconfig.app.json` sets `types: ["vite/client"]`
// only, so Node's typings are not in this project. The explicit `.tsx` extension is
// load-bearing — `./Templates` resolves to `Templates.ts` first, which on a
// case-insensitive filesystem is `templatesView.ts`'s neighbourhood, not the component.

import { describe, it, expect } from 'vitest';
import {
  CATALOG_EFFECT_DEPS,
  EMPTY_CATALOG_READ_ONLY_BODY,
  EMPTY_STATE_COPY,
  SHOW_FILTER_STRIP,
  TEMPLATES_SUBTITLE,
  TEMPLATE_RETRY_HINT,
  canMutateTemplates,
  classifyTemplateError,
  deleteTemplateConfirm,
  orgLabel,
  orgOptions,
  pickSelectedOrg,
  templatesBodyState,
  type ConnectionLike,
  type TemplateErrorKind,
  type TemplatesBodyInput,
} from './templatesView';
// The ADMIN rung, imported from the module that already NAMES it. `templatesView.ts` cannot
// import it (see `canMutateTemplates`), so the cross-check that the two agree lives here.
import { ROLE_LEVEL_ADMIN } from './settingsSections';
import templatesSrc from './Templates.tsx?raw';
import templateCardGridSrc from './TemplateCardGrid.tsx?raw';
import appSrc from '../../App.tsx?raw';
// The client, for the third of 4a's stale strings — the rollout slice's comment, which stated the
// same falsehood as the two visible ones and is the form E28B found hardest to notice.
import clientSrc from '../../api/client.ts?raw';

const GH = (id: string, org: string): ConnectionLike => ({ id, provider: 'github', org });

// A body input with nothing wrong: one connection, selected, loaded, two templates.
const HEALTHY: TemplatesBodyInput = {
  connectionsLoading: false,
  connectionsError: false,
  connectionCount: 1,
  connectionId: 'conn-1',
  catalogLoading: false,
  catalogError: null,
  templateCount: 2,
};

// ---------------------------------------------------------------------------
// Org picker
// ---------------------------------------------------------------------------

describe('orgLabel / orgOptions', () => {
  it('labels a connection as "<Provider> · <org>"', () => {
    expect(orgLabel(GH('c-1', 'acme-corp'))).toBe('GitHub · acme-corp');
    expect(orgLabel({ id: 'c-2', provider: 'gitlab', org: 'acme-eu' })).toBe('GitLab · acme-eu');
  });

  it('falls back to the raw provider rather than hiding an unknown one', () => {
    // A provider this build has not been taught about is still a real org the operator
    // picked. Rendering it as '—' would make the selected <option> unidentifiable.
    expect(orgLabel({ id: 'c-3', provider: 'bitbucket', org: 'acme' })).toBe('bitbucket · acme');
  });

  it('maps connections to option rows in the API order', () => {
    expect(orgOptions([GH('c-1', 'b-org'), GH('c-2', 'a-org')])).toEqual([
      { value: 'c-1', label: 'GitHub · b-org' },
      { value: 'c-2', label: 'GitHub · a-org' },
    ]);
  });
});

describe('pickSelectedOrg', () => {
  const rows = [GH('c-1', 'one'), GH('c-2', 'two')];

  it('defaults to the first connection so the grid is not empty on open', () => {
    expect(pickSelectedOrg(rows, '')).toBe('c-1');
  });

  it('KEEPS the human’s pick across a connections reload', () => {
    // The snap-back this prevents is indistinguishable from "the refetch showed me the
    // wrong org's templates" — unaffordable on a per-org catalog.
    expect(pickSelectedOrg(rows, 'c-2')).toBe('c-2');
  });

  it('drops a selection whose connection no longer exists', () => {
    // e.g. the org was deleted under Settings in another tab. Holding a dangling id
    // would leave the <select> showing nothing while the grid showed its cards.
    expect(pickSelectedOrg(rows, 'c-99')).toBe('c-1');
  });

  it('answers "" when there are no connections at all', () => {
    expect(pickSelectedOrg([], 'c-1')).toBe('');
  });
});

// ---------------------------------------------------------------------------
// Body state machine
// ---------------------------------------------------------------------------

describe('templatesBodyState', () => {
  it('renders the grid when an org is selected and returned templates', () => {
    expect(templatesBodyState(HEALTHY)).toBe('grid');
  });

  it('reports loading-orgs before the connections list arrives', () => {
    // Distinct from no-connection: "we have not asked yet" is not "you have none".
    expect(templatesBodyState({ ...HEALTHY, connectionsLoading: true, connectionCount: 0 })).toBe(
      'loading-orgs',
    );
  });

  it('reports no-connection when zero orgs are connected', () => {
    expect(templatesBodyState({ ...HEALTHY, connectionCount: 0, connectionId: '' })).toBe(
      'no-connection',
    );
  });

  it('lets no-connection outrank a stale catalog error', () => {
    // The error belonged to a connection that no longer exists; with no org there is no
    // catalog to have failed, so the error must not outlive it.
    expect(
      templatesBodyState({
        ...HEALTHY,
        connectionCount: 0,
        connectionId: '',
        catalogError: 'boom',
      }),
    ).toBe('no-connection');
  });

  it('reports no-selection when orgs exist but none is picked', () => {
    expect(templatesBodyState({ ...HEALTHY, connectionId: '' })).toBe('no-selection');
  });

  it('reports loading while the catalog request is in flight', () => {
    expect(templatesBodyState({ ...HEALTHY, catalogLoading: true, templateCount: 0 })).toBe(
      'loading',
    );
  });

  it('lets an in-flight fetch outrank the PREVIOUS org’s error', () => {
    // The divergence from the admin tab body this page replaces. There, `error` cleared
    // only on success, so switching orgs after a failure re-rendered the old error over
    // an in-flight fetch — accusing the newly-picked org of a failure that never happened
    // to it. An in-flight request has no verdict yet.
    expect(
      templatesBodyState({
        ...HEALTHY,
        catalogLoading: true,
        catalogError: 'previous org failed',
        templateCount: 0,
      }),
    ).toBe('loading');
  });

  it('reports error when a settled catalog fetch failed', () => {
    expect(
      templatesBodyState({ ...HEALTHY, catalogError: 'HTTP 500', templateCount: 0 }),
    ).toBe('error');
  });

  it('reports error even when the previous org’s templates are still in state', () => {
    // Showing stale cards under a failed fetch would report another org's catalog as
    // this one's.
    expect(templatesBodyState({ ...HEALTHY, catalogError: 'HTTP 403' })).toBe('error');
  });

  it('reports empty-catalog when a reachable org answered with zero rows', () => {
    expect(templatesBodyState({ ...HEALTHY, templateCount: 0 })).toBe('empty-catalog');
  });

  it('is total — every input combination lands on exactly one state', () => {
    const bools = [false, true];
    const seen = new Set<string>();
    for (const connectionsLoading of bools) {
      for (const connectionsError of bools) {
        for (const connectionCount of [0, 1]) {
          for (const connectionId of ['', 'conn-1']) {
            for (const catalogLoading of bools) {
              for (const catalogError of [null, 'boom']) {
                for (const templateCount of [0, 2]) {
                  const state = templatesBodyState({
                    connectionsLoading,
                    connectionsError,
                    connectionCount,
                    connectionId,
                    catalogLoading,
                    catalogError,
                    templateCount,
                  });
                  expect(typeof state).toBe('string');
                  seen.add(state);
                }
              }
            }
          }
        }
      }
    }
    // All eight members are reachable — a state no input can produce is dead code the
    // page would render never, and a reviewer would trust wrongly.
    expect([...seen].sort()).toEqual([
      'connections-unreadable',
      'empty-catalog',
      'error',
      'grid',
      'loading',
      'loading-orgs',
      'no-connection',
      'no-selection',
    ]);
  });
});

// ---------------------------------------------------------------------------
// A FAILED connections read is NOT "you have no org" (E28 final review, FR-4)
//
// The defect: `connectionsApi.list()` rejecting set the error banner AND left the array empty, so
// `connectionCount === 0` fell through to `no-connection`. The page then told ONE failure TWICE —
// a rose "couldn't load org connections" above a full-page "No org connected yet" card whose CTA
// sent an operator who already HAS orgs to go create one — and suppressed the filter strip, taking
// away the picker they could have retried from.
// ---------------------------------------------------------------------------

describe('templatesBodyState — a failed connections read is distinct from having none', () => {
  it('reports connections-unreadable, never no-connection, when the read FAILED', () => {
    // The exact live shape: the read rejected, so the list is empty and nothing is selected.
    expect(
      templatesBodyState({
        ...HEALTHY,
        connectionsError: true,
        connectionCount: 0,
        connectionId: '',
      }),
    ).toBe('connections-unreadable');
  });

  it('still reports no-connection when the read SUCCEEDED with zero orgs', () => {
    // Both directions, or the fix has only moved the dishonesty: an operator who genuinely has no
    // org must still be told to connect one.
    expect(
      templatesBodyState({
        ...HEALTHY,
        connectionsError: false,
        connectionCount: 0,
        connectionId: '',
      }),
    ).toBe('no-connection');
  });

  it('keeps rows the page already holds when a REFETCH fails', () => {
    // Data in hand outranks a later read's failure — replacing a populated grid with an error card
    // would lose information the page has. The banner covers this case instead.
    expect(templatesBodyState({ ...HEALTHY, connectionsError: true })).toBe('grid');
  });

  it('lets the in-flight read outrank its own previous failure', () => {
    // Same precedence argument as `catalogLoading` over `catalogError`: a request in flight has no
    // verdict yet, so a retry must not keep showing the failure it is retrying.
    expect(
      templatesBodyState({
        ...HEALTHY,
        connectionsLoading: true,
        connectionsError: true,
        connectionCount: 0,
        connectionId: '',
      }),
    ).toBe('loading-orgs');
  });
});

describe('the connections-unreadable copy says nothing the failure did not establish', () => {
  const copy = EMPTY_STATE_COPY['connections-unreadable'];

  it('offers a RETRY, not a go-create-an-org link', () => {
    // The whole defect in one assertion: the state this replaces sent an operator who already has
    // orgs to a page to create one. The thing that failed is a read, so the action is re-reading.
    expect(copy.cta).toEqual({ kind: 'retry', label: 'Try again' });
  });

  it('never claims the operator has no org connected', () => {
    // The `no-connection` sentence, asserted absent. This is the claim the page used to make over
    // this failure, and it is the one thing the failure establishes nothing about.
    const said = `${copy.headline} ${copy.body}`.toLowerCase();
    expect(said).not.toMatch(/no org connected/);
    expect(said).not.toMatch(/\bnothing to list\b/);
    // …and it says so explicitly, so a reader is not left to infer it.
    expect(said).toMatch(/not a report that no org is connected/);
  });

  it('is DISTINCT from no-connection in headline, body and action', () => {
    // The same three-channel guard the two original empty states carry, for the same reason: these
    // have different fixes (retry vs. connect an org), so a copy edit that merges them must fail
    // here rather than in a live test.
    const none = EMPTY_STATE_COPY['no-connection'];
    expect(copy.headline).not.toBe(none.headline);
    expect(copy.body).not.toBe(none.body);
    expect(copy.cta.kind).not.toBe(none.cta.kind);
  });
});

describe('SHOW_FILTER_STRIP — the org picker only where there is something to filter', () => {
  it('covers every body state with no default branch', () => {
    // The table exists because this WAS a chain of `!==`, and adding a state to the union landed it
    // silently on the `true` side: a dead `<select>` reading "No orgs connected" directly above a
    // card explaining the org list could not be read.
    expect(Object.keys(SHOW_FILTER_STRIP).sort()).toEqual([
      'connections-unreadable',
      'empty-catalog',
      'error',
      'grid',
      'loading',
      'loading-orgs',
      'no-connection',
      'no-selection',
    ]);
  });

  it('hides the picker in every state where the org list is empty or unknown', () => {
    expect(SHOW_FILTER_STRIP['loading-orgs']).toBe(false);
    expect(SHOW_FILTER_STRIP['no-connection']).toBe(false);
    expect(SHOW_FILTER_STRIP['connections-unreadable']).toBe(false);
  });

  it('shows it in every state that HAS orgs to switch between', () => {
    for (const state of ['no-selection', 'loading', 'error', 'empty-catalog', 'grid'] as const) {
      expect(SHOW_FILTER_STRIP[state], state).toBe(true);
    }
  });
});

describe('the page renders the unreadable state and does not double-report the failure', () => {
  it('renders a branch for connections-unreadable with its retry', () => {
    // A `Record` entry nothing reads is copy that never appears on screen — the same guard the two
    // original empty states carry.
    expect(templatesSrc).toContain("'connections-unreadable'");
    expect(templatesSrc).toContain('setConnNonce');
  });

  it('re-runs the CONNECTIONS read, not the catalog one, on retry', () => {
    // The retry has to re-run the read that failed. The catalog effect cannot: it is gated on a
    // selected org, which is exactly what a failed connections read leaves the page without. So the
    // connections effect carries its own nonce in its dependency array.
    const effects = [...templatesSrc.matchAll(/useEffect\(\(\) => \{([\s\S]*?)\n {2}\}, \[([^\]]*)\]\);/g)];
    const connectionsEffect = effects.filter((m) => /connectionsApi\s*\.\s*list\s*\(/.test(m[1]));
    expect(connectionsEffect).toHaveLength(1);
    expect(
      connectionsEffect[0][2]
        .split(',')
        .map((d) => d.trim())
        .filter(Boolean),
    ).toEqual(['connNonce']);
  });

  it('does not render the banner and the card for the SAME failure', () => {
    // The defect was two unconditional siblings. The banner must yield to the body state that owns
    // the failure — matched on the guard rather than on a rendering, because there is no jsdom here.
    expect(templatesSrc).toMatch(/connError && state !== 'connections-unreadable'/);
  });

  it('feeds the failure into the state machine, not only into a banner', () => {
    // Without this the machine cannot tell a failed read from a successful empty one, which is the
    // root of the finding rather than a symptom of it.
    expect(templatesSrc).toContain('connectionsError:');
  });
});

// ---------------------------------------------------------------------------
// The two empty states are DISTINCT
// ---------------------------------------------------------------------------

describe('EMPTY_STATE_COPY', () => {
  it('gives no-connection a call-to-action that names where to go', () => {
    const copy = EMPTY_STATE_COPY['no-connection'];
    expect(copy.cta).toEqual({ kind: 'link', label: 'Connect an org', to: '/ops/settings' });
    expect(copy.headline.length).toBeGreaterThan(0);
    expect(copy.body.length).toBeGreaterThan(0);
  });

  it('points that call-to-action at a route App.tsx actually serves', () => {
    // A dead CTA is worse than no CTA: it is an instruction the product cannot honour.
    const cta = EMPTY_STATE_COPY['no-connection'].cta;
    if (cta.kind !== 'link') throw new Error('expected a link CTA');
    expect(appSrc).toContain(`path="${cta.to}"`);
  });

  it('offers empty-catalog an upload, because uploading is what fixes it', () => {
    expect(EMPTY_STATE_COPY['empty-catalog'].cta).toEqual({
      kind: 'upload',
      label: 'Upload template',
    });
  });

  it('never lets no-connection and empty-catalog say the same thing', () => {
    // These are the two "no cards" states and they have DIFFERENT fixes: connect an org
    // vs. upload a zip. Collapsing them sends a user with no connection hunting for an
    // Upload button that cannot work. Assert all three channels differ, so a copy edit
    // that merges them fails here rather than in a live test.
    const a = EMPTY_STATE_COPY['no-connection'];
    const b = EMPTY_STATE_COPY['empty-catalog'];
    expect(a.headline).not.toBe(b.headline);
    expect(a.body).not.toBe(b.body);
    expect(a.cta.kind).not.toBe(b.cta.kind);
  });

  it('renders both of them — the page cannot quietly ship only one', () => {
    // A `Record` entry nothing reads is copy that never appears on screen.
    expect(templatesSrc).toContain("'no-connection'");
    expect(templatesSrc).toContain("'empty-catalog'");
    expect(templatesSrc).toContain('EMPTY_STATE_COPY');
  });
});

// ---------------------------------------------------------------------------
// 4a — THE TWO FALSE STRINGS, replaced and pinned (E28C/T6)
//
// Both of these shipped as lies for an epic, and the reason they could is the whole argument for
// this file: the subtitle lived inline in `Templates.tsx` where no test reads it, and the empty
// state's body was in `templatesView.ts` but nothing asserted its CLAIMS — only that it differed
// from its neighbour. So the tests below pin the new sentences verbatim AND assert the specific
// falsehoods as absences, because a copy edit that reintroduces the old mechanism words is exactly
// how this rots again.
// ---------------------------------------------------------------------------

describe('4a — the Templates subtitle says what is true after D-C2', () => {
  it('is the pinned sentence, verbatim', () => {
    expect(TEMPLATES_SUBTITLE).toBe(
      'Registered template pointers for this connection. Metadata lives on the AGP record; materializing reads the template repository.',
    );
  });

  it('no longer calls a template a template REPOSITORY', () => {
    // The `is_template` filter is gone (D-C1/D-C2) and a template's source may be a repository
    // outside this org. "Each template is a template repository" was the claim, and it is the one
    // that made an operator look for a GitHub flag that no longer exists.
    expect(TEMPLATES_SUBTITLE).not.toMatch(/is a template repository/i);
    expect(TEMPLATES_SUBTITLE).toMatch(/pointer/i);
  });

  it('no longer claims materializing CREATES a repo from the template', () => {
    // Materialize READS the template repository at use-time now. "creates a repo from it"
    // described generate-from-template, the mechanism D-C2 replaced.
    expect(TEMPLATES_SUBTITLE).not.toMatch(/creates a repo from/i);
    expect(TEMPLATES_SUBTITLE).toMatch(/materializing reads/i);
  });

  it('is the sentence the page actually renders — not a constant nothing reads', () => {
    // The failure mode this whole pin exists for: a true constant beside a false literal. So the
    // page must reference the constant AND must not carry a hand-written subtitle string.
    expect(templatesSrc).toContain('TEMPLATES_SUBTITLE');
    expect(templatesSrc).toMatch(/subtitle=\{TEMPLATES_SUBTITLE\}/);
    expect(templatesSrc).not.toContain('Each template is a template repository');
  });
});

describe('4a — the empty-catalog body names all three ways a template appears', () => {
  it('is the pinned sentence, verbatim', () => {
    expect(EMPTY_STATE_COPY['empty-catalog'].body).toBe(
      'Seed starter templates into this org, adopt an existing repository, or upload a .zip scaffold.',
    );
  });

  it('offers seed and adopt, not upload alone', () => {
    // The cold start had ONE stated answer (upload) while the platform has three. Naming the two
    // this epic makes real is the difference between an empty page and a pointer.
    const body = EMPTY_STATE_COPY['empty-catalog'].body.toLowerCase();
    expect(body).toContain('seed');
    expect(body).toContain('adopt');
    expect(body).toContain('.zip');
  });

  it('no longer sends the operator to GitHub topics for metadata', () => {
    // "Its framework, AWS services and tags ride on the template repository's topics" described a
    // mechanism D-C1 deleted: that metadata is on the AGP record. The sentence pointed an operator
    // at the provider to explain fields AGP stores itself.
    expect(EMPTY_STATE_COPY['empty-catalog'].body).not.toMatch(/topic/i);
  });
});

describe('4a — the stale claim is gone from the CLIENT COMMENT too', () => {
  it('no longer describes the rollout slice in terms of the deleted flags', () => {
    // The same lie in comment form (`client.ts:2052-2057`), and comments are where two of E28B's
    // three stale strings lived — so the sweep includes them deliberately. `reconcileView.test.ts`
    // owns the field-NAME grep; this asserts the DESCRIPTION is corrected, i.e. the comment now
    // explains `state` rather than a boolean plus a forced-infra flag.
    //
    // The old sentences are matched by SHAPE rather than quoted verbatim, because quoting them
    // would put the grep-forbidden names into this file's own text — and this test file is
    // excluded from that sweep, which is exactly the loophole that lets a guard pass while the
    // defect it names lives on in the thing it guards.
    expect(clientSrc).not.toMatch(/select\w*: false/);
    expect(clientSrc).not.toMatch(/reports which base\s+templates already exist/i);
    expect(clientSrc).not.toMatch(/always rolled out unless it\s*\/*\s*already exists/i);
    // …and it really did gain the replacement, so this cannot pass by the slice being deleted.
    expect(clientSrc).toContain('registered_missing');
    expect(clientSrc).toMatch(/unregistered_present/);
  });

  it('states the cost model where the caller reads it', () => {
    // The rule that keeps the Templates page instant is a property of WHO CALLS reconcile, so it
    // belongs beside the function rather than only in the design doc.
    expect(clientSrc).toMatch(/list_repos/);
    expect(clientSrc).toMatch(/Templates page stays registry-only/i);
  });
});

// ---------------------------------------------------------------------------
// Switching orgs refetches — asserted against the real component source
// ---------------------------------------------------------------------------

/**
 * Pull the dependency array off the `useEffect` that fetches the catalog.
 *
 * Throws rather than returning a default when it cannot find one: a parser that silently
 * answers `[]` would turn "the effect was renamed" into a passing test, which is exactly
 * the normalize-away-the-property failure this suite is written to avoid.
 */
function catalogEffectDeps(src: string): string[] {
  const effects = [...src.matchAll(/useEffect\(\(\) => \{([\s\S]*?)\n {2}\}, \[([^\]]*)\]\);/g)];
  if (effects.length === 0) throw new Error('no top-level useEffect found in Templates.tsx');
  // `\s*` around the dot, not a literal `githubTemplatesApi.list`: the source is written in
  // the fluent style the other Ops pages use, with the call chained on the NEXT line.
  const catalog = effects.filter((m) => /githubTemplatesApi\s*\.\s*list\s*\(/.test(m[1]));
  if (catalog.length !== 1) {
    throw new Error(
      `expected exactly 1 useEffect calling githubTemplatesApi.list, found ${catalog.length}`,
    );
  }
  return catalog[0][2]
    .split(',')
    .map((d) => d.trim())
    .filter((d) => d.length > 0);
}

describe('switching orgs refetches the catalog', () => {
  it('keys the catalog effect on the selected org AND the refetch nonce', () => {
    // THE test for this page. Without `connectionId` in these deps the page renders org
    // #1's cards under org #2's name: no error, no empty state, just a confident wrong
    // answer. Mutation-checked — removing either dep fails this.
    expect(catalogEffectDeps(templatesSrc)).toEqual([...CATALOG_EFFECT_DEPS]);
  });

  it('clears the previous org’s rows when the selection changes', () => {
    // The deps are necessary but not sufficient: between the switch and the response the
    // grid must not keep painting the old org's cards. The effect sets loading before it
    // fetches, and `templatesBodyState` renders 'loading' — asserted above — but only if
    // the effect resets on the no-connection path too.
    expect(templatesSrc).toContain('setTemplates([])');
  });

  it('guards the in-flight response against a fast second switch', () => {
    // Pick org A, pick org B before A responds: without the cancelled guard A's late
    // response overwrites B's cards. Same guard idiom as Repositories.tsx / Projects.tsx.
    expect(templatesSrc).toContain('cancelled');
  });

  it('scopes every catalog call to the selected connection', () => {
    // A call that omitted the id would read some server-chosen default org — a
    // cross-org read on a page whose entire contract is "one org at a time".
    // `\s*` spans the fluent line break, and the count assertion keeps this from
    // passing vacuously if the call style changes and the regex stops matching.
    const calls = [
      ...templatesSrc.matchAll(/githubTemplatesApi\s*\.\s*(\w+)\s*\(\s*([^,)]*)/g),
    ];
    expect(calls.map((c) => c[1]).sort()).toEqual(['list', 'patch', 'remove', 'upload']);
    for (const call of calls) {
      expect(call[2].trim()).toBe('connectionId');
    }
  });
});

// ---------------------------------------------------------------------------
// The page owns the frame; the card list is shared
// ---------------------------------------------------------------------------

describe('page frame and card-list extraction', () => {
  it('owns the OpsPage frame, title and back link — the interim tab body did not', () => {
    // The defect this task fixes: `/ops/templates` pointed at a tab BODY, so it rendered
    // frame-less and heading-less because OperationsAdmin owned the frame.
    expect(templatesSrc).toContain('OpsPage');
    expect(templatesSrc).toContain('backTo="/ops"');
    expect(templatesSrc).toContain('title="Templates"');
  });

  it('renders the card grid from the SHARED component, not a second copy', () => {
    expect(templatesSrc).toContain('TemplateCardGrid');
    expect(templateCardGridSrc).toContain('md:grid-cols-2');
  });

  it('keeps the catalog card markup in EXACTLY ONE file, repo-wide', () => {
    // T9b's whole point. The catalog used to render from two places with two different
    // chromes (a page and an admin tab body), and the tab body was the one without a
    // frame or a heading. Asserting only "the page imports the grid" would still pass if
    // someone re-inlined a second grid elsewhere, so this sweeps every source file and
    // demands a single owner of the card markup.
    const owners = Object.entries(
      import.meta.glob<string>('../**/*.{ts,tsx}', { query: '?raw', import: 'default', eager: true }),
    )
      .filter(([path]) => !path.endsWith('.test.ts'))
      .filter(([, src]) => src.includes('md:grid-cols-2') && src.includes('View repo ↗'))
      .map(([path]) => path.split('/').pop());
    expect(owners).toEqual(['TemplateCardGrid.tsx']);
  });

  it('leaves no importable TemplatesAdmin behind', () => {
    // The tab body is deleted (T9b), not merely unreferenced: keeping it alive just to
    // export the two dialogs is what kept the catalog reachable at two paths. A dangling
    // module would also let a future page import the old surface back into existence.
    const modules = Object.keys(
      import.meta.glob('../**/*.{ts,tsx}', { query: '?raw', eager: false }),
    );
    expect(modules.filter((p) => p.includes('TemplatesAdmin'))).toEqual([]);
  });

  it('gives the org picker a real labelled <select>, not a styled div', () => {
    expect(templatesSrc).toMatch(/<label[^>]*htmlFor="[^"]*"/);
    expect(templatesSrc).toContain('<select');
  });

  it('adds no approve/grant/classify/deprecate affordance', () => {
    // The shadow-governance failure mode: governance verbs are Jorge's surface, never an
    // Operations page's.
    for (const verb of ['Approve', 'Grant', 'Classify', 'Deprecate']) {
      expect(templatesSrc).not.toContain(`>${verb}<`);
    }
  });
});

// ---------------------------------------------------------------------------
// Role gate on the destructive verbs (finding #14)
// ---------------------------------------------------------------------------

/**
 * Strip comments before a source assertion.
 *
 * Load-bearing, not tidiness: E28 shipped FIVE source-as-text guards that their own
 * explanatory comment satisfied, because the comment quoted the very string the guard
 * searched for. A guard that its own documentation can pass is not a guard. Everything
 * below matches on comment-free source, and `the guard cannot be satisfied by prose`
 * asserts that property directly.
 *
 * Removes `/* … *\/` blocks (which is also what empties a `{/* … *\/}` JSX comment) and
 * whole lines that are line comments. Deliberately does NOT touch `//` mid-line, so a URL
 * or an SVG path inside a string literal survives intact.
 */
function stripComments(src: string): string {
  return src
    .replace(/\/\*[\s\S]*?\*\//g, '')
    .split('\n')
    .filter((line) => !line.trim().startsWith('//'))
    .join('\n');
}

/**
 * Does `TemplateCardGrid`'s Edit/Delete pair sit behind the `canMutate` gate?
 *
 * Ordering + occurrence counting rather than "the guard appears somewhere": a second,
 * ungated copy of the actions row would pass a mere `toContain`, and re-introducing the
 * unconditional buttons above the guard would too.
 */
function cardActionsAreGated(src: string): boolean {
  const s = stripComments(src);
  const guard = s.indexOf('{canMutate && (');
  if (guard === -1) return false;
  for (const handler of ['onEdit(t)', 'onDelete(t)']) {
    if (s.split(handler).length - 1 !== 1) return false;
    if (s.indexOf(handler) < guard) return false;
  }
  return true;
}

/** The ternary that opens an upload CTA's gate — the `(` is load-bearing, see below. */
const UPLOAD_GATE = 'canMutate ? (';
const UPLOAD_HANDLER = /setShowUpload\(true\)/g;

/**
 * Does EVERY upload entry point sit behind its OWN gate?
 *
 * The naive shape — "the role name appears somewhere in the N characters before the handler" —
 * is vacuous on this page, and was: the empty-catalog CTA has a sibling attribute that swaps
 * the empty-state body on the same role predicate, so a wide preceding window is satisfied by
 * that COPY ternary and the button's own gate can be deleted with the suite still green. Two
 * things close it:
 *
 *  - The needle is the ternary that opens a JSX branch, `(` included. The copy swap picks
 *    between two string constants, so it cannot supply one.
 *  - The gate must be the button's IMMEDIATE parent: exactly one `<button` between the two,
 *    and a window tight enough (the real ones measure 50 and 66 characters) that the OTHER
 *    CTA's gate thousands of characters up the file cannot stand in for a deleted one.
 *
 * Plus a count: one opener per handler. Strip one CTA's gate and the count breaks even before
 * proximity is consulted, and a third ungated upload button breaks it the other way.
 *
 * AMENDED IN E28C/T6, and the amendment is a narrowing of the heuristic, not of the property. Both
 * gates now open a `<div>` holding TWO admin buttons — upload beside "Roll out templates" in the
 * heading (labelled "Reconcile org" until E28D unified the vocabulary) and beside "Seed or adopt
 * templates" in the empty state — because 4a's new copy names three ways
 * a template appears and the page could only reach one of them. The old shape assertions were
 * written when each gate wrapped exactly one button, so both of them mis-fired on a correctly gated
 * page: the `<button` count is now 2 per gate, and the reconcile button's ~470 characters of markup
 * sit inside the window when upload is the SECOND child.
 *
 * What replaces them keeps every property the original bought, and one more:
 *   • the count still pins one gate per handler (a deleted gate, or a third ungated button, fails);
 *   • the handler must still be INSIDE its own gate's JSX branch — enforced now by finding the
 *     branch's closing token and requiring the handler before it, which is stricter than a
 *     character window because it cannot be satisfied by a nearby-but-outside sibling;
 *   • every `<button` inside the branch is required to be admin-only, so ADDING an ungated button
 *     to one of these divs fails even though the upload handler itself is still gated. The old
 *     `=== 1` count refused that by accident; this refuses it on purpose.
 */
const UPLOAD_GATE_CLOSERS = [') : null}', ') : undefined}'];

function uploadCtasAreGated(src: string): boolean {
  const s = stripComments(src);
  const handlers = [...s.matchAll(UPLOAD_HANDLER)];
  if (handlers.length === 0) return false;
  if (s.split(UPLOAD_GATE).length - 1 !== handlers.length) return false;
  for (const m of handlers) {
    const gate = s.lastIndexOf(UPLOAD_GATE, m.index);
    if (gate === -1) return false;
    // The end of THIS gate's branch: the first closer after the gate opens. The handler must fall
    // before it, or it belongs to something else that merely follows a gate.
    const ends = UPLOAD_GATE_CLOSERS.map((c) => s.indexOf(c, gate)).filter((i) => i !== -1);
    if (ends.length === 0) return false;
    const end = Math.min(...ends);
    if (m.index >= end) return false;
    // Every button in the branch is an admin's. Enumerated rather than counted, so the two-button
    // rows this task adds pass while an ungated third button anywhere in the branch fails.
    const branch = s.slice(gate + UPLOAD_GATE.length, end);
    const buttons = branch.split('<button').length - 1;
    if (buttons < 1 || buttons > 2) return false;
  }
  return true;
}

describe('canMutateTemplates — the destructive verbs are ADMIN-only', () => {
  it('is pinned at the exact boundary: OPERATOR no, ADMIN yes', () => {
    // The backend ladder (core/rbac.py): VIEWER=0, OPERATOR=1, ADMIN=2. PATCH/DELETE/POST on
    // /github-templates are all `require_role(Role.ADMIN)`; only GET is OPERATOR. So an
    // OPERATOR is precisely the exposed population — they pass the read and see the grid.
    expect(canMutateTemplates(0)).toBe(false);
    expect(canMutateTemplates(1)).toBe(false);
    expect(canMutateTemplates(2)).toBe(true);
    expect(canMutateTemplates(3)).toBe(true);
  });

  it('mirrors the ADMIN level settingsSections already names, so the two cannot drift', () => {
    // `templatesView.ts` may not IMPORT that constant — it is deliberately client-free, and
    // `settingsSections.ts` mounts .tsx bodies that pull axios in. The cross-check therefore
    // lives here, where a test may import anything: change either number and this fails.
    expect(canMutateTemplates(ROLE_LEVEL_ADMIN)).toBe(true);
    expect(canMutateTemplates(ROLE_LEVEL_ADMIN - 1)).toBe(false);
  });

  it('treats a missing user as the lowest rung, never as permitted', () => {
    // The `user?.role_level ?? 0` fallback: while `useUser()` is still loading there is no
    // role, and "unknown" must not render an admin control that then 403s.
    expect(canMutateTemplates(0)).toBe(false);
  });
});

describe('an OPERATOR gets no Edit, no Delete, no Upload', () => {
  // There is no jsdom here, so "gets no button" is asserted as the conjunction that makes it
  // true: the predicate is false at OPERATOR (above), and each of the three affordances is
  // rendered only inside that predicate's guard (below). Neither half is sufficient alone.

  it('gates the card Edit and Delete pair behind canMutate, not behind `disabled`', () => {
    expect(cardActionsAreGated(templateCardGridSrc)).toBe(true);
  });

  it('cannot render the destructive buttons unconditionally', () => {
    const s = stripComments(templateCardGridSrc);
    // The prop must be a real boolean in the contract — a component that shrugged and
    // defaulted it would gate nothing.
    expect(s).toMatch(/canMutate:\s*boolean/);
    // The guard must EXIST before anything is compared against its position. Asserted
    // separately because `indexOf` answers -1 for an absent needle, and `> -1` is true of
    // every offset — so the loop below would pass vacuously on a component whose guard had
    // been deleted outright. Caught by mutating the guard away, which is exactly the edit
    // this test is here to stop.
    const guard = s.indexOf('{canMutate && (');
    expect(guard).toBeGreaterThan(-1);
    // Both handlers live inside that one guard, so neither button has an ungated path.
    for (const handler of ['onEdit(t)', 'onDelete(t)']) {
      expect(s.split(handler).length - 1).toBe(1);
      expect(s.indexOf(handler)).toBeGreaterThan(guard);
    }
  });

  it('keeps `disabled` for the in-flight request ONLY', () => {
    // Two different axes, and E28's frontend rule is that they must not be conflated:
    // `disabled` means "a mutation is in flight", absence means "you do not have the
    // standing". Greying a button someone can never use advertises the dead end.
    const s = stripComments(templateCardGridSrc);
    expect(s).toContain('disabled={anyPending}');
    expect(s).not.toMatch(/disabled=\{[^}]*canMutate/);
  });

  it('gates the page-level Upload template button too', () => {
    const s = stripComments(templatesSrc);
    expect(s).toContain('action={canMutate ?');
    // There are exactly two upload entry points — the heading action and the empty-catalog CTA
    // — and BOTH are asserted, because the second is the one the spec never listed and so the
    // one most likely to be re-broken. A third, ungated one added later breaks the count.
    expect([...s.matchAll(/setShowUpload\(true\)/g)]).toHaveLength(2);
    expect(uploadCtasAreGated(templatesSrc)).toBe(true);
  });

  it('passes the real predicate down to the grid, not a hardcoded true', () => {
    // The gate is only as good as the value handed to it. `canMutate={canMutate}` on the one
    // call site, and the local bound from the predicate — without this, `canMutate={true}`
    // would satisfy every other assertion in this file while restoring the original bug.
    const s = stripComments(templatesSrc);
    expect(s).toContain('canMutate={canMutate}');
    expect(s).toMatch(/const canMutate = canMutateTemplates\(/);
    expect(s).not.toMatch(/canMutate\s*=\s*(true|false)\b/);
  });

  it('reads the role from the same auth idiom every other Ops page uses', () => {
    const s = stripComments(templatesSrc);
    expect(s).toContain('useUser');
    expect(s).toMatch(/user\?\.role_level \?\? 0/);
    expect(s).toContain('canMutateTemplates');
  });

  it('the guard cannot be satisfied by prose', () => {
    // The E28 trap, asserted rather than hoped for: a comment quoting the guard must not
    // make the guard pass. If `stripComments` or the matcher ever weakens, this goes red.
    const prose = [
      '// The actions row sits behind {canMutate && ( so an operator never sees it.',
      '{/* Wrapped in {canMutate && ( — Edit and Delete are ADMIN-only. */}',
      '<button onClick={() => onEdit(t)}>Edit</button>',
      '<button onClick={() => onDelete(t)}>Delete</button>',
    ].join('\n');
    expect(cardActionsAreGated(prose)).toBe(false);
    expect(stripComments(prose)).not.toContain('canMutate');
  });

  it('the upload guard cannot be satisfied by prose either, nor by a sibling ternary', () => {
    // Same trap, second guard — and one more, the one that actually bit here: a NEIGHBOURING
    // attribute switching on the same predicate must not stand in for a button's own gate.
    // Both fixtures carry ungated upload buttons and both must be refused.
    const quoted = [
      '// Both upload buttons are wrapped in the admin ternary before their `(`.',
      '{/* Gated on the role predicate — ADMIN-only. */}',
      '<button type="button" onClick={() => setShowUpload(true)}>Upload template</button>',
    ].join('\n');
    expect(uploadCtasAreGated(quoted)).toBe(false);

    const sibling = [
      "  body={canMutate ? EMPTY_STATE_COPY['empty-catalog'].body : EMPTY_CATALOG_READ_ONLY_BODY}",
      '>',
      '  <button type="button" onClick={() => setShowUpload(true)}>Upload</button>',
    ].join('\n');
    expect(uploadCtasAreGated(sibling)).toBe(false);
  });

  it('does not keep an SVG path or a URL from surviving the stripper', () => {
    // The stripper must not be so eager that it corrupts the source it is checking:
    // `//` inside a string literal is data, not a comment.
    expect(stripComments('const u = "https://example.com/x"; // trailing')).toContain(
      'https://example.com/x',
    );
  });
});

describe('an ADMIN gets all three', () => {
  it('renders Edit, Delete and Upload at ADMIN level', () => {
    expect(canMutateTemplates(ROLE_LEVEL_ADMIN)).toBe(true);
    // The affordances still EXIST — this task removes them for the wrong audience, it does
    // not delete the capability. Their markup is still in the component, inside the gate.
    const s = stripComments(templateCardGridSrc);
    expect(s).toContain('Edit');
    expect(s).toContain('Delete');
    expect(stripComments(templatesSrc)).toContain('Upload template');
  });
});

describe('the empty catalog does not instruct an operator to upload', () => {
  it('offers a read-only body that asks for nothing the caller cannot do', () => {
    // The card whose entire text is "upload a .zip to create the first template" is the same
    // over-promise as the button, in prose. An operator seeing an empty org gets a statement
    // of fact instead of an instruction they cannot honour.
    expect(EMPTY_CATALOG_READ_ONLY_BODY.length).toBeGreaterThan(0);
    expect(EMPTY_CATALOG_READ_ONLY_BODY).not.toContain('.zip');
    expect(EMPTY_CATALOG_READ_ONLY_BODY).not.toBe(EMPTY_STATE_COPY['empty-catalog'].body);
  });

  it('picks between the two bodies on canMutate, and renders both', () => {
    const s = stripComments(templatesSrc);
    expect(s).toContain('EMPTY_CATALOG_READ_ONLY_BODY');
    expect(s).toMatch(/canMutate\s*\?[\s\S]{0,160}EMPTY_CATALOG_READ_ONLY_BODY/);
  });
});

// ---------------------------------------------------------------------------
// DELETE DEREGISTERS — the confirm must say what the verb now does (E28B/T6, item 1)
//
// The shipped copy read "Its GitHub repository will be permanently removed from the org", and
// E28B/T2 changed the verb underneath it: the route removes the CATALOG RECORD and leaves the
// repository completely alone. So an admin was asked to authorise an irreversible teardown, the
// platform did something else, and nothing reported the difference — a verb whose meaning moved
// while its message stayed put, which is this epic's own defect class. It must not ship.
// ---------------------------------------------------------------------------

describe('deleteTemplateConfirm — the confirm describes DEREGISTRATION, not a repo deletion', () => {
  const said = deleteTemplateConfirm('claims-triage');

  it('names the template it is about', () => {
    expect(said).toContain('claims-triage');
  });

  it('NEVER claims the repository is removed — the exact sentence that shipped', () => {
    // Asserted as an ABSENCE, because the failure mode here is a reassuring word nobody checked.
    // Each phrase below is one the old copy contained or a paraphrase a well-meaning edit would
    // reach for; a confirm carrying any of them is again promising something the route does not do.
    const lower = said.toLowerCase();
    for (const claim of [
      'permanently removed',
      'permanently delete',
      'will be deleted',
      'removed from the org',
      'delete the repository',
      'repository will be',
    ]) {
      expect(lower, claim).not.toContain(claim);
    }
  });

  it('states positively that the repository is LEFT IN PLACE', () => {
    // The absence above is not sufficient: silence about the repository leaves an admin to assume
    // whatever they assumed before. The sentence has to say it.
    expect(said).toMatch(/left in place/i);
    expect(said).toMatch(/does not delete/i);
  });

  it('names the act as removal from the CATALOG, and says the consequence', () => {
    expect(said).toMatch(/catalog/i);
    expect(said).toMatch(/deregister/i);
    // What the admin actually loses: the template can no longer be materialized.
    expect(said).toMatch(/materialized/i);
  });

  it('explains WHY the repository survives, so the behaviour is not arbitrary', () => {
    // `source_url` may name a public repo or a mirror in another org — deleting the repository
    // behind a pointer AGP does not own is unsafe, and an admin who does not know that will read
    // the deregistration as a bug and go delete the repo by hand.
    expect(said).toMatch(/public repository|mirror|another org/i);
  });

  it('is the ONLY confirm the page uses, and it is not written inline', () => {
    // The copy lived in the `.tsx`, where vitest never mounts the component and `window.confirm` is
    // never called — so the sentence was untestable, which is how it went stale unnoticed. The page
    // must call the pinned function and hold no second confirm string of its own.
    const s = stripComments(templatesSrc);
    expect(s).toContain('deleteTemplateConfirm(t.name)');
    expect([...s.matchAll(/window\.confirm\(/g)]).toHaveLength(1);
    // No inline sentence: a `window.confirm` taking a template literal is the shape of the defect.
    expect(s).not.toMatch(/window\.confirm\(\s*[`'"]/);
  });
});

// ---------------------------------------------------------------------------
// T2's `store_error` → 503 (E28B/T6, item 4)
//
// A registry fault is now a 503 and validation faults correctly answer 422 instead of sharing one
// bucket with it. The console could not tell them apart, because the axios interceptor replaces the
// AxiosError with `new Error(response.data.detail)` — by the time a component holds the rejection the
// STATUS IS GONE. The fixed `detail` literal is what makes classification possible at all.
// ---------------------------------------------------------------------------

/**
 * The routes' FIXED detail literals, keyed by the `.kind` they are emitted for.
 *
 * Keep BYTE-IDENTICAL to `api/routes/github_templates.py`'s `_ERROR_DETAIL`. Recovering the KIND
 * (rather than asserting two membership arrays) is what makes each literal INDEPENDENTLY observable
 * — `githubLink.ts`'s reasoning: a fall-through also answers `{retryable: false}`, so without the
 * kind, literals could be deleted from the table with every test still green.
 */
const ROUTE_DETAILS: Record<TemplateErrorKind, string> = {
  invalid_zip: 'Invalid template zip',
  invalid_input: 'Invalid template metadata',
  not_found: 'Template not found',
  github_error: 'GitHub template operation failed',
  store_error: 'Template catalog is temporarily unavailable',
};

describe('classifyTemplateError — a 503 reads as "wait", never as a bare failure', () => {
  it('recognizes every one of the five backend literals — none may drift unnoticed', () => {
    // A backend reword silently degrades this copy to a raw-message fall-through, and the
    // fall-through looks healthy. Recovering the kind is what makes that observable.
    for (const [kind, detail] of Object.entries(ROUTE_DETAILS)) {
      expect(classifyTemplateError(new Error(detail), 'fallback').kind, detail).toBe(kind);
    }
  });

  it('treats the store fault as RETRYABLE and says so', () => {
    // THE ITEM. `store_error` is ours and transient: the remedy is to wait, and a 503 rendered as a
    // terminal failure tells an admin to go fix something that is not theirs and not broken.
    const view = classifyTemplateError(new Error(ROUTE_DETAILS.store_error), 'fallback');
    expect(view.retryable).toBe(true);
    expect(view.message).toMatch(/temporarily unavailable/i);
    expect(view.message).toContain(TEMPLATE_RETRY_HINT);
  });

  it('promises nothing was lost, so nobody re-uploads templates that already exist', () => {
    // The specific confusion `list_templates` refuses when it raises `store_error` rather than
    // returning an empty list: "you have no templates" would invite a re-upload. The console's copy
    // has to hold that line too, or the backend's care is undone at the last step.
    const view = classifyTemplateError(new Error(ROUTE_DETAILS.store_error), 'fallback');
    expect(view.message).toMatch(/nothing was changed/i);
    expect(view.message).toMatch(/unaffected/i);
    // …and it must not accuse the caller. This is the "misleading invalid input" half of the item.
    expect(view.message.toLowerCase()).not.toContain('invalid');
  });

  it('NEVER shows a 422 as retryable, or a 503 as invalid input', () => {
    // Both directions, or the fix has only moved the dishonesty. Retrying the same zip or the same
    // metadata returns the same answer, so inviting a retry there is advice the product cannot
    // honour — and the two 4xx sentences must not borrow the store fault's reassurance.
    for (const kind of ['invalid_zip', 'invalid_input', 'not_found'] as const) {
      const view = classifyTemplateError(new Error(ROUTE_DETAILS[kind]), 'fallback');
      expect(view.retryable, kind).toBe(false);
      expect(view.message, kind).not.toContain(TEMPLATE_RETRY_HINT);
      expect(view.message.toLowerCase(), kind).not.toContain('temporarily unavailable');
    }
    // The 422 sentences say whose fault it is and what to do — they are not merely "not the 503".
    expect(classifyTemplateError(new Error(ROUTE_DETAILS.invalid_zip), 'f').message).toMatch(/zip/i);
    expect(classifyTemplateError(new Error(ROUTE_DETAILS.invalid_input), 'f').message).toMatch(
      /metadata|name/i,
    );
  });

  it('treats the provider fault (502) as retryable too, but not as the store fault', () => {
    const view = classifyTemplateError(new Error(ROUTE_DETAILS.github_error), 'fallback');
    expect(view.retryable).toBe(true);
    // Distinct sentence: a provider blip and an unreadable catalog have the same remedy and
    // different causes, and collapsing them would misreport where the fault is.
    expect(view.message).not.toBe(
      classifyTemplateError(new Error(ROUTE_DETAILS.store_error), 'fallback').message,
    );
    expect(view.message).toMatch(/provider/i);
  });

  it('gives every recognized kind a DISTINCT sentence', () => {
    // A shared sentence is two states the operator cannot tell apart, which is the whole finding
    // one level up.
    const messages = Object.values(ROUTE_DETAILS).map(
      (d) => classifyTemplateError(new Error(d), 'fallback').message,
    );
    expect(new Set(messages).size).toBe(messages.length);
  });

  it('falls back TERMINALLY on an unrecognized error, and keeps its message', () => {
    // An unknown failure is more likely a bug than contention, so inviting a retry would be a guess
    // presented as advice. `kind: null` is what distinguishes this from a matched terminal literal.
    const view = classifyTemplateError(new Error('HTTP 500'), 'fallback');
    expect(view).toEqual({ message: 'HTTP 500', retryable: false, kind: null });
  });

  it('uses the fallback only when the error carries no message at all', () => {
    expect(classifyTemplateError(new Error(''), 'fallback').message).toBe('fallback');
    expect(classifyTemplateError(new Error('   '), 'fallback').message).toBe('fallback');
    // A non-Error rejection (a thrown string, an axios shape that slipped the interceptor) has no
    // `.message` to trust — it must not be stringified into the UI.
    expect(classifyTemplateError('boom', 'fallback').message).toBe('fallback');
    expect(classifyTemplateError(null, 'fallback').message).toBe('fallback');
    expect(classifyTemplateError(undefined, 'fallback').message).toBe('fallback');
  });

  it('cannot be fooled by a prototype member arriving as the detail', () => {
    // The reason the lookup is a `Map`. An object literal would resolve `'toString'` /
    // `'constructor'` to `Object.prototype`'s member and classify it as a recognized kind whose
    // value is a FUNCTION — which then reaches the UI as a message.
    for (const key of ['toString', 'constructor', 'hasOwnProperty', '__proto__']) {
      const view = classifyTemplateError(new Error(key), 'fallback');
      expect(view.kind, key).toBeNull();
      expect(typeof view.message, key).toBe('string');
      expect(view.message, key).toBe(key);
    }
  });
});

describe('the page degrades on a 503 rather than showing a bare failure', () => {
  it('classifies BOTH failure paths — the catalog read and the delete', () => {
    // Either left raw is the finding still live on that path. The read is the important one (a store
    // fault must not render as an empty catalog), and delete is the one whose own copy this task
    // rewrote, so both are asserted.
    const s = stripComments(templatesSrc);
    expect([...s.matchAll(/classifyTemplateError\(/g)].length).toBeGreaterThanOrEqual(2);
    // The raw-message shapes must be GONE from both, or the honest branch sits beside the raw one.
    expect(s).not.toMatch(/setError\(err instanceof Error/);
    expect(s).not.toMatch(/message: err instanceof Error/);
  });

  it('drives the error card’s TONE from `retryable`, not from the message text', () => {
    // A page that re-derived the verdict by matching on prose would be back to the drift this table
    // removes. Anchored on the property access, which a comment cannot supply.
    const s = stripComments(templatesSrc);
    expect(s).toMatch(/error\.retryable/);
    // A caution, not a fault: the retryable branch must reach for amber, and rose must remain for
    // the failures that really are faults.
    expect(s).toMatch(/error\.retryable\s*\?\s*'border-amber/);
    expect(s).toContain('border-rose-200/70');
  });

  it('does not carry the difference in COLOUR alone', () => {
    // Not every reader has that channel. The headline has to change too — and "couldn't load" over a
    // transient store fault reads as a verdict on the catalog.
    const s = stripComments(templatesSrc);
    expect(s).toMatch(/Template catalog temporarily unavailable/);
    expect(s).toMatch(/Couldn’t load templates/);
  });

  it('offers the retry ONLY where the remedy is a retry, and WITHHOLDS it otherwise', () => {
    // A review finding, and the same defect class this task exists to fix: the button rendered
    // UNCONDITIONALLY while two comments claimed `retryable` decided whether a retry was offered. So
    // a terminal `not_found` / `invalid_zip` / `invalid_input` offered an action that cannot succeed —
    // re-reading the same missing template returns the same 404 — and the test that used to live here
    // asserted only that a Retry existed, never that it could be withheld.
    //
    // The withholding is what needed pinning, so it is asserted STRUCTURALLY: the button must sit
    // inside the `retryable` guard, not merely somewhere in the card.
    const s = stripComments(templatesSrc);
    const start = s.indexOf("state === 'error'");
    expect(start).toBeGreaterThan(-1);
    const card = s.slice(start, start + 2200);
    expect(card).toContain('error.message');
    // The gate exists, and the Retry is INSIDE it — ordering, not co-presence, because a button above
    // the guard would satisfy a bare `toContain` while still over-promising.
    const gate = card.indexOf('{error.retryable && (');
    expect(gate).toBeGreaterThan(-1);
    const retry = card.indexOf('onClick={refetch}');
    expect(retry).toBeGreaterThan(gate);
    // Exactly ONE retry entry point on this card, so a second ungated copy breaks the count.
    expect([...card.matchAll(/onClick=\{refetch\}/g)]).toHaveLength(1);
    // …and the gate cannot be satisfied by prose quoting it.
    expect(stripComments('// wrapped in {error.retryable && ( so it is withheld')).not.toContain(
      'error.retryable',
    );
  });

  it('withholds nothing else — the operator is never stranded without a way to re-read', () => {
    // Gating the Retry is only defensible because another one survives: the org filter strip carries
    // its own Refresh, and `SHOW_FILTER_STRIP.error` is `true`, so a terminal failure still leaves a
    // way to re-run the read. Asserted against the real table rather than assumed, because if that
    // entry ever flips to `false` the gate above becomes a dead end.
    expect(SHOW_FILTER_STRIP.error).toBe(true);
  });
});
