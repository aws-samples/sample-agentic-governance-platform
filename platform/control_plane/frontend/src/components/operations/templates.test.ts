import { describe, it, expect, expectTypeOf } from 'vitest';
import { ApiFailure, type ErrorCode } from '../../api/failure';
import { AUTH_401_HALT_MESSAGE } from '../../api/authRetry';
import { failureOf } from '../../api/loadState';
import {
  CATALOG_EFFECT_DEPS,
  EMPTY_CATALOG_READ_ONLY_BODY,
  EMPTY_STATE_COPY,
  INFRA_CATALOG_EFFECT_DEPS,
  SHOW_FILTER_STRIP,
  SHOW_TAB_BAR,
  TEMPLATES_SUBTITLE,
  TEMPLATES_TABS,
  TEMPLATE_ERROR_CODES,
  TEMPLATE_RETRY_HINT,
  canMutateTemplates,
  classifyTemplateError,
  classifyTemplateFailure,
  deleteTemplateConfirm,
  orgLabel,
  orgOptions,
  pickSelectedOrg,
  templatesBodyState,
  templatesTabId,
  templatesTabPanelId,
  type ConnectionLike,
  type TemplateErrorKind,
  type TemplatesBodyInput,
} from './templatesView';
import { ROLE_LEVEL_ADMIN } from './settingsSections';
import templatesRaw from './Templates.tsx?raw';
import templateCardGridRaw from './TemplateCardGrid.tsx?raw';
import clientRaw from '../../api/client.ts?raw';
import ts from 'typescript';
import { stripComments } from '../../rawSource';
import { PAGE_PATHS } from '../../routes';

const templatesSrc = stripComments(templatesRaw);
const templateCardGridSrc = stripComments(templateCardGridRaw);
const clientSrc = stripComments(clientRaw);
const componentSourcesRaw = Object.entries(
  import.meta.glob<string>('../**/*.{ts,tsx}', { query: '?raw', import: 'default', eager: true }),
).filter(([path]) => !path.endsWith('.test.ts'));
const componentSources = componentSourcesRaw.map(([path, src]) => [path, stripComments(src)] as const);

function useLoadDeps(src: string, call: RegExp): string[] {
  const loads = [...src.matchAll(/useLoad(?:<[^>]*>)?\(\s*\(\) =>([\s\S]*?),\s*\[([^\]]*)\],\s*'[^']*',?\s*\)/g)];
  if (loads.length === 0) throw new Error('no useLoad call found in Templates.tsx');
  const hits = loads.filter((m) => call.test(m[1]));
  if (hits.length !== 1) {
    throw new Error(`expected exactly 1 useLoad call whose load matches ${call}, found ${hits.length}`);
  }
  return hits[0][2]
    .split(',')
    .map((d) => d.trim())
    .filter((d) => d.length > 0);
}

const STATUS_LABEL_READ = /\b(?:CICD_LABEL|RUNTIME_LABEL)\s*\[|\bdelivery\.label\b|\bagentRuntimeLabel\(/;

function capitalizedStatusLabelSites(path: string, src: string): string[] {
  const file = ts.createSourceFile(path, src, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  const capitalizedConsts = [...src.matchAll(/const (\w+) =\s*['"`][^'"`]*\bcapitalize\b[^'"`]*['"`]/g)].map((m) => m[1]);
  const sites: string[] = [];
  const visit = (node: ts.Node) => {
    if (ts.isJsxElement(node)) {
      const className = node.openingElement.attributes.properties.find(
        (p) => ts.isJsxAttribute(p) && p.name.getText() === 'className',
      );
      const cls = className?.getText() ?? '';
      const capitalized =
        /\bcapitalize\b/.test(cls) || capitalizedConsts.some((name) => new RegExp(`\\b${name}\\b`).test(cls));
      if (capitalized && STATUS_LABEL_READ.test(node.children.map((c) => c.getText()).join(''))) {
        sites.push(`${path.split('/').pop()}:${file.getLineAndCharacterOfPosition(node.getStart()).line + 1}`);
      }
    }
    ts.forEachChild(node, visit);
  };
  visit(file);
  return sites;
}

const GH = (id: string, org: string): ConnectionLike => ({ id, provider: 'github', org });

const HEALTHY: TemplatesBodyInput = {
  connectionsLoading: false,
  connectionsError: false,
  connectionCount: 1,
  connectionId: 'conn-1',
  catalogLoading: false,
  catalogError: null,
  templateCount: 2,
};

describe('orgLabel / orgOptions', () => {
  it('labels a connection as "<Provider> · <org>"', () => {
    expect(orgLabel(GH('c-1', 'acme-corp'))).toBe('GitHub · acme-corp');
    expect(orgLabel({ id: 'c-2', provider: 'gitlab', org: 'acme-eu' })).toBe('GitLab · acme-eu');
  });

  it('falls back to the raw provider rather than hiding an unknown one', () => {
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
    expect(pickSelectedOrg(rows, 'c-2')).toBe('c-2');
  });

  it('drops a selection whose connection no longer exists', () => {
    expect(pickSelectedOrg(rows, 'c-99')).toBe('c-1');
  });

  it('answers "" when there are no connections at all', () => {
    expect(pickSelectedOrg([], 'c-1')).toBe('');
  });
});

describe('templatesBodyState', () => {
  it('renders the grid when an org is selected and returned templates', () => {
    expect(templatesBodyState(HEALTHY)).toBe('grid');
  });

  it('reports loading-orgs before the connections list arrives', () => {
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

describe('templatesBodyState — a failed connections read is distinct from having none', () => {
  it('reports connections-unreadable, never no-connection, when the read FAILED', () => {
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
    expect(templatesBodyState({ ...HEALTHY, connectionsError: true })).toBe('grid');
  });

  it('lets the in-flight read outrank its own previous failure', () => {
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
    expect(copy.cta).toEqual({ kind: 'retry', label: 'Try again' });
  });

  it('never claims the operator has no org connected', () => {
    const said = `${copy.headline} ${copy.body}`.toLowerCase();
    expect(said).not.toMatch(/no org connected/);
    expect(said).not.toMatch(/\bnothing to list\b/);
    expect(said).toMatch(/not a report that no org is connected/);
  });

  it('is DISTINCT from no-connection in headline, body and action', () => {
    const none = EMPTY_STATE_COPY['no-connection'];
    expect(copy.headline).not.toBe(none.headline);
    expect(copy.body).not.toBe(none.body);
    expect(copy.cta.kind).not.toBe(none.cta.kind);
  });
});

describe('SHOW_FILTER_STRIP — the org picker only where there is something to filter', () => {
  it('covers every body state with no default branch', () => {
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

describe('the two-catalog tab bar', () => {
  it('registers exactly the agent and infra catalogs', () => {
    expect(TEMPLATES_TABS.map((t) => t.key)).toEqual(['agent', 'infra']);
    expect(TEMPLATES_TABS.map((t) => t.label)).toEqual(['Agent templates', 'Infra templates']);
  });

  it('opens on the agent catalog', () => {
    expect(TEMPLATES_TABS[0].key).toBe('agent');
  });

  it('derives tab and panel ids that cannot collide', () => {
    for (const tab of TEMPLATES_TABS) {
      expect(templatesTabId(tab.key)).not.toBe(templatesTabPanelId(tab.key));
    }
    const ids = TEMPLATES_TABS.flatMap((t) => [templatesTabId(t.key), templatesTabPanelId(t.key)]);
    expect(new Set(ids).size).toBe(ids.length);
  });

  it('covers every body state with no default branch', () => {
    expect(Object.keys(SHOW_TAB_BAR).sort()).toEqual(Object.keys(SHOW_FILTER_STRIP).sort());
  });

  it('hides the bar wherever there is no selected org — both catalogs are org-scoped', () => {
    for (const state of [
      'loading-orgs',
      'connections-unreadable',
      'no-connection',
      'no-selection',
    ] as const) {
      expect(SHOW_TAB_BAR[state], state).toBe(false);
    }
  });

  it('shows the bar even when the AGENT catalog is empty, loading or unreadable', () => {
    for (const state of ['loading', 'error', 'empty-catalog', 'grid'] as const) {
      expect(SHOW_TAB_BAR[state], state).toBe(true);
    }
  });

  it('is not merely a copy of the filter strip’s table', () => {
    expect(SHOW_TAB_BAR['no-selection']).toBe(false);
    expect(SHOW_FILTER_STRIP['no-selection']).toBe(true);
  });

  it('keys both catalog reads on the selected org alone, and one Refresh reloads both', () => {
    expect(INFRA_CATALOG_EFFECT_DEPS).toEqual(CATALOG_EFFECT_DEPS);
    expect([...CATALOG_EFFECT_DEPS]).toEqual(['connectionId']);
    expect(templatesSrc).toMatch(/const refetch = \(\) => \{\s*setCardError\(null\);\s*reloadCatalog\(\);\s*reloadInfraCatalog\(\);\s*\};/);
  });

  it('renders both panels and the infra table’s columns', () => {
    for (const tab of TEMPLATES_TABS) {
      expect(templatesSrc, tab.key).toContain(`templatesTabPanelId('${tab.key}')`);
    }
    expect(templatesSrc).toContain('infraTemplatesApi');
    for (const column of ['Platform', 'IaC', 'Build', 'Repo', 'Version', 'Updated']) {
      expect(templatesSrc, column).toContain(`>${column}<`);
    }
  });

  it('offers NO mutation affordance on the infra tab', () => {
    const src = templatesSrc;
    const start = src.indexOf("templatesTabPanelId('infra')");
    expect(start).toBeGreaterThan(-1);
    const panel = src.slice(start, src.indexOf('{showUpload &&', start));
    expect(panel.length).toBeGreaterThan(500);
    expect(panel).not.toMatch(/infraTemplatesApi\s*\.\s*(upload|patch|remove|create|delete)\s*\(/);
    expect(panel).not.toMatch(/canMutate/);
  });
});

describe('the page renders the unreadable state and does not double-report the failure', () => {
  it('renders a branch for connections-unreadable with its retry', () => {
    expect(templatesSrc).toContain("'connections-unreadable'");
    expect(templatesSrc).toContain('reloadConnections');
  });

  it('re-runs the CONNECTIONS read, not the catalog one, on retry', () => {
    expect(useLoadDeps(templatesSrc, /connectionsApi\s*\.\s*list\s*\(/)).toEqual([]);
    const card = templatesSrc.slice(templatesSrc.indexOf("state === 'connections-unreadable' && ("));
    const retry = card.slice(0, card.indexOf('</StateCard>'));
    expect([...retry.matchAll(/onClick=\{(\w+)\}/g)].map((m) => m[1])).toEqual(['reloadConnections']);
  });

  it('does not render the banner and the card for the SAME failure', () => {
    expect(templatesSrc).toMatch(/connError && state !== 'connections-unreadable'/);
  });

  it('feeds the failure into the state machine, not only into a banner', () => {
    expect(templatesSrc).toContain('connectionsError:');
  });
});

describe('EMPTY_STATE_COPY', () => {
  it('gives no-connection a call-to-action that names where to go', () => {
    const copy = EMPTY_STATE_COPY['no-connection'];
    expect(copy.cta).toEqual({ kind: 'link', label: 'Connect an org', to: '/ops/settings' });
    expect(copy.headline.length).toBeGreaterThan(0);
    expect(copy.body.length).toBeGreaterThan(0);
  });

  it('points that call-to-action at a page in the route table', () => {
    const cta = EMPTY_STATE_COPY['no-connection'].cta;
    if (cta.kind !== 'link') throw new Error('expected a link CTA');
    expect(PAGE_PATHS).toContain(cta.to);
  });

  it('offers empty-catalog an upload, because uploading is what fixes it', () => {
    expect(EMPTY_STATE_COPY['empty-catalog'].cta).toEqual({
      kind: 'upload',
      label: 'Upload template',
    });
  });

  it('never lets no-connection and empty-catalog say the same thing', () => {
    const a = EMPTY_STATE_COPY['no-connection'];
    const b = EMPTY_STATE_COPY['empty-catalog'];
    expect(a.headline).not.toBe(b.headline);
    expect(a.body).not.toBe(b.body);
    expect(a.cta.kind).not.toBe(b.cta.kind);
  });

  it('renders both of them — the page cannot quietly ship only one', () => {
    expect(templatesSrc).toContain("'no-connection'");
    expect(templatesSrc).toContain("'empty-catalog'");
    expect(templatesSrc).toContain('EMPTY_STATE_COPY');
  });
});

describe('the Templates subtitle says what is true', () => {
  it('is the pinned sentence, verbatim', () => {
    expect(TEMPLATES_SUBTITLE).toBe(
      'Registered template pointers for this connection. Metadata lives on the AGP record; materializing reads the template repository.',
    );
  });

  it('no longer calls a template a template REPOSITORY', () => {
    expect(TEMPLATES_SUBTITLE).not.toMatch(/is a template repository/i);
    expect(TEMPLATES_SUBTITLE).toMatch(/pointer/i);
  });

  it('no longer claims materializing CREATES a repo from the template', () => {
    expect(TEMPLATES_SUBTITLE).not.toMatch(/creates a repo from/i);
    expect(TEMPLATES_SUBTITLE).toMatch(/materializing reads/i);
  });

  it('is the sentence the page actually renders — not a constant nothing reads', () => {
    expect(templatesSrc).toContain('TEMPLATES_SUBTITLE');
    expect(templatesSrc).toMatch(/subtitle=\{TEMPLATES_SUBTITLE\}/);
    expect(templatesSrc).not.toContain('Each template is a template repository');
  });
});

describe('the empty-catalog body names all three ways a template appears', () => {
  it('is the pinned sentence, verbatim', () => {
    expect(EMPTY_STATE_COPY['empty-catalog'].body).toBe(
      'Seed starter templates into this org, adopt an existing repository, or upload a .zip scaffold.',
    );
  });

  it('offers seed and adopt, not upload alone', () => {
    const body = EMPTY_STATE_COPY['empty-catalog'].body.toLowerCase();
    expect(body).toContain('seed');
    expect(body).toContain('adopt');
    expect(body).toContain('.zip');
  });

  it('no longer sends the operator to GitHub topics for metadata', () => {
    expect(EMPTY_STATE_COPY['empty-catalog'].body).not.toMatch(/topic/i);
  });
});

describe('the template upload body', () => {
  it('appends one multipart entry per aws_service and per tag, never a joined string: FastAPI Form([]) reads repeated keys', () => {
    expect(clientSrc).toMatch(/\(meta\.aws_services \?\? \[\]\)\.forEach\(\((\w+)\) => formData\.append\('aws_services', \1\)\);/);
    expect(clientSrc).toMatch(/\(meta\.tags \?\? \[\]\)\.forEach\(\((\w+)\) => formData\.append\('tags', \1\)\);/);
    expect(clientSrc).not.toMatch(/formData\.append\('(aws_services|tags)', [^)]*\.join\(/);
  });
});

describe('the provider-backed reconcile read is paid on the roll-out modal only', () => {
  it('is called from exactly one component, once, through a useLoad keyed on the connection; Retry reloads it', () => {
    const callers = componentSources
      .filter(([, src]) => /\.\s*reconcile\s*\(/.test(src))
      .map(([path]) => path.split('/').pop());
    expect(callers).toEqual(['RolloutTemplatesModal.tsx']);
    const modal = componentSources.find(([path]) => path.endsWith('/RolloutTemplatesModal.tsx'));
    expect(modal).toBeDefined();
    const modalSrc = modal?.[1] ?? '';
    expect([...modalSrc.matchAll(/\.\s*reconcile\s*\(/g)]).toHaveLength(1);
    const load = modalSrc.match(/const viewLoad = useLoad\(\(\) => rolloutApi\.reconcile\(connection\.id\), \[([^\]]*)\]/);
    expect(load).not.toBeNull();
    expect(
      (load?.[1] ?? '')
        .split(',')
        .map((d) => d.trim())
        .filter(Boolean),
    ).toEqual(['connection.id']);
    expect(modalSrc).toContain('const reloadView = viewLoad.reload;');
    expect(modalSrc).toMatch(/onClick=\{refetch\}/);
  });

  it('never reaches the Templates page: the page calls no reconcile, and the client routes reconcile under /rollout/ only, naming no provider listing', () => {
    expect(templatesSrc).not.toMatch(/\.\s*reconcile\s*\(/);
    const routes = [...clientSrc.matchAll(/`(\/api\/[^`]*reconcile[^`]*)`/g)].map((m) => m[1]);
    expect(routes).toEqual(['/api/v1/admin/connections/${connectionId}/rollout/reconcile']);
    expect(clientSrc).not.toMatch(/list_repos/);
  });
});

describe('switching orgs refetches the catalog', () => {
  it('keys the catalog read on the selected org', () => {
    expect(useLoadDeps(templatesSrc, /githubTemplatesApi\s*\.\s*list\s*\(/)).toEqual([...CATALOG_EFFECT_DEPS]);
  });

  it('keys the infra catalog read on the same org', () => {
    expect(useLoadDeps(templatesSrc, /infraTemplatesApi\s*\.\s*list\s*\(/)).toEqual([...INFRA_CATALOG_EFFECT_DEPS]);
  });

  it('blanks the previous org’s rows while the next catalog loads: the body keys on the loading kind and takes rows from a ready state only', () => {
    expect(templatesSrc).toMatch(/const catalogLoading = catalogState\.kind === 'loading';/);
    expect(templatesSrc).toMatch(/templatesBodyState\(\{[\s\S]*?\bcatalogLoading,[\s\S]*?\}\)/);
    expect(templatesSrc).toMatch(/const templates = catalogState\.kind === 'ready' \? catalogState\.data : \[\];/);
    expect(templatesSrc).toMatch(/const infraTemplates = infraCatalogState\.kind === 'ready' \? infraCatalogState\.data : \[\];/);
  });

  it('reads both catalogs through useLoad, whose cancelled flag guards a fast second switch, and keeps no fetch effect of its own', () => {
    expect(templatesSrc).toContain("import { useLoad } from '../../api/useLoad';");
    expect(templatesRaw).not.toMatch(/useEffect/);
  });

  it('scopes every catalog call to the selected connection', () => {
    const calls = [
      ...templatesSrc.matchAll(/githubTemplatesApi\s*\.\s*(\w+)\s*\(\s*([^,)]*)/g),
    ];
    expect(calls.map((c) => c[1]).sort()).toEqual(['list', 'patch', 'remove', 'upload']);
    for (const call of calls) {
      expect(call[2].trim()).toBe('connectionId');
    }
  });
});

describe('page frame and card-list extraction', () => {
  it('owns the OpsPage frame, title and back link — the interim tab body did not', () => {
    expect(templatesSrc).toContain('OpsPage');
    expect(templatesSrc).toContain('backTo="/ops"');
    expect(templatesSrc).toContain('title="Templates"');
  });

  it('renders the card grid from the SHARED component, not a second copy', () => {
    expect(templatesSrc).toContain('TemplateCardGrid');
    expect(templateCardGridSrc).toContain('md:grid-cols-2');
  });

  it('keeps the catalog card markup in EXACTLY ONE file, repo-wide', () => {
    const owners = componentSources
      .filter(([, src]) => src.includes('md:grid-cols-2') && src.includes('View repo ↗'))
      .map(([path]) => path.split('/').pop());
    expect(owners).toEqual(['TemplateCardGrid.tsx']);
  });

  it('leaves no importable TemplatesAdmin behind', () => {
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
    for (const verb of ['Approve', 'Grant', 'Classify', 'Deprecate']) {
      expect(templatesSrc).not.toContain(`>${verb}<`);
    }
  });
});

describe('the status labels render as the tables spell them', () => {
  it('no component applies the Tailwind capitalize class to an element that renders a CICD_LABEL or RUNTIME_LABEL read, comments included', () => {
    const sites = componentSourcesRaw
      .filter(([path]) => path.endsWith('.tsx'))
      .flatMap(([path, src]) => capitalizedStatusLabelSites(path, src));
    expect(sites).toEqual([]);
  });

  it('the guard sees a capitalize class carried by a shared constant, and an element nested inside the pill', () => {
    const offender = [
      "const PILL = 'inline-flex capitalize';",
      'export function P({ cicd }: { cicd: CicdStatus }) {',
      '  return <span className={`${PILL} x`}><span aria-hidden="true">●</span>{CICD_LABEL[cicd]}</span>;',
      '}',
    ].join('\n');
    expect(capitalizedStatusLabelSites('P.tsx', offender)).toEqual(['P.tsx:3']);
    const plain = offender.replace(' capitalize', '');
    expect(capitalizedStatusLabelSites('P.tsx', plain)).toEqual([]);
  });
});


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

const UPLOAD_GATE = 'canMutate ? (';
const UPLOAD_HANDLER = /setShowUpload\(true\)/g;

const UPLOAD_GATE_CLOSERS = [') : null}', ') : undefined}'];

function uploadCtasAreGated(src: string): boolean {
  const s = stripComments(src);
  const handlers = [...s.matchAll(UPLOAD_HANDLER)];
  if (handlers.length === 0) return false;
  if (s.split(UPLOAD_GATE).length - 1 !== handlers.length) return false;
  for (const m of handlers) {
    const gate = s.lastIndexOf(UPLOAD_GATE, m.index);
    if (gate === -1) return false;
    const ends = UPLOAD_GATE_CLOSERS.map((c) => s.indexOf(c, gate)).filter((i) => i !== -1);
    if (ends.length === 0) return false;
    const end = Math.min(...ends);
    if (m.index >= end) return false;
    const branch = s.slice(gate + UPLOAD_GATE.length, end);
    const buttons = branch.split('<button').length - 1;
    if (buttons < 1 || buttons > 2) return false;
  }
  return true;
}

describe('canMutateTemplates — the destructive verbs are ADMIN-only', () => {
  it('is pinned at the exact boundary: OPERATOR no, ADMIN yes', () => {
    expect(canMutateTemplates(0)).toBe(false);
    expect(canMutateTemplates(1)).toBe(false);
    expect(canMutateTemplates(2)).toBe(true);
    expect(canMutateTemplates(3)).toBe(true);
  });

  it('mirrors the ADMIN level settingsSections already names, so the two cannot drift', () => {
    expect(canMutateTemplates(ROLE_LEVEL_ADMIN)).toBe(true);
    expect(canMutateTemplates(ROLE_LEVEL_ADMIN - 1)).toBe(false);
  });

  it('treats a missing user as the lowest rung, never as permitted', () => {
    expect(canMutateTemplates(0)).toBe(false);
  });
});

describe('an OPERATOR gets no Edit, no Delete, no Upload', () => {
  it('gates the card Edit and Delete pair behind canMutate, not behind `disabled`', () => {
    expect(cardActionsAreGated(templateCardGridSrc)).toBe(true);
  });

  it('cannot render the destructive buttons unconditionally', () => {
    const s = templateCardGridSrc;
    expect(s).toMatch(/canMutate:\s*boolean/);
    const guard = s.indexOf('{canMutate && (');
    expect(guard).toBeGreaterThan(-1);
    for (const handler of ['onEdit(t)', 'onDelete(t)']) {
      expect(s.split(handler).length - 1).toBe(1);
      expect(s.indexOf(handler)).toBeGreaterThan(guard);
    }
  });

  it('keeps `disabled` for the in-flight request ONLY', () => {
    const s = templateCardGridSrc;
    expect(s).toContain('disabled={anyPending}');
    expect(s).not.toMatch(/disabled=\{[^}]*canMutate/);
  });

  it('gates the page-level Upload template button too', () => {
    const s = templatesSrc;
    expect(s).toContain('action={canMutate ?');
    expect([...s.matchAll(/setShowUpload\(true\)/g)]).toHaveLength(2);
    expect(uploadCtasAreGated(templatesSrc)).toBe(true);
  });

  it('passes the real predicate down to the grid, not a hardcoded true', () => {
    const s = templatesSrc;
    expect(s).toContain('canMutate={canMutate}');
    expect(s).toMatch(/const canMutate = canMutateTemplates\(/);
    expect(s).not.toMatch(/canMutate\s*=\s*(true|false)\b/);
  });

  it('reads the role from the same auth idiom every other Ops page uses', () => {
    const s = templatesSrc;
    expect(s).toContain('useUser');
    expect(s).toMatch(/user\?\.role_level \?\? 0/);
    expect(s).toContain('canMutateTemplates');
  });

  it('the guard cannot be satisfied by prose', () => {
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
    expect(stripComments('const u = "https://example.com/x"; // trailing')).toContain(
      'https://example.com/x',
    );
  });
});

describe('an ADMIN gets all three', () => {
  it('renders Edit, Delete and Upload at ADMIN level', () => {
    expect(canMutateTemplates(ROLE_LEVEL_ADMIN)).toBe(true);
    const s = templateCardGridSrc;
    expect(s).toContain('Edit');
    expect(s).toContain('Delete');
    expect(templatesSrc).toContain('Upload template');
  });
});

describe('the empty catalog does not instruct an operator to upload', () => {
  it('offers a read-only body that asks for nothing the caller cannot do', () => {
    expect(EMPTY_CATALOG_READ_ONLY_BODY.length).toBeGreaterThan(0);
    expect(EMPTY_CATALOG_READ_ONLY_BODY).not.toContain('.zip');
    expect(EMPTY_CATALOG_READ_ONLY_BODY).not.toBe(EMPTY_STATE_COPY['empty-catalog'].body);
  });

  it('picks between the two bodies on canMutate, and renders both', () => {
    const s = templatesSrc;
    expect(s).toContain('EMPTY_CATALOG_READ_ONLY_BODY');
    expect(s).toMatch(/canMutate\s*\?[\s\S]{0,160}EMPTY_CATALOG_READ_ONLY_BODY/);
  });
});

describe('deleteTemplateConfirm — the confirm describes DEREGISTRATION, not a repo deletion', () => {
  const said = deleteTemplateConfirm('claims-triage');

  it('names the template it is about', () => {
    expect(said).toContain('claims-triage');
  });

  it('NEVER claims the repository is removed — the exact sentence that shipped', () => {
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
    expect(said).toMatch(/left in place/i);
    expect(said).toMatch(/does not delete/i);
  });

  it('names the act as removal from the CATALOG, and says the consequence', () => {
    expect(said).toMatch(/catalog/i);
    expect(said).toMatch(/deregister/i);
    expect(said).toMatch(/materialized/i);
  });

  it('explains WHY the repository survives, so the behaviour is not arbitrary', () => {
    expect(said).toMatch(/public repository|mirror|another org/i);
  });

  it('is the ONLY confirm the page uses, and it is not written inline', () => {
    const s = templatesSrc;
    expect(s).toContain('deleteTemplateConfirm(t.name)');
    expect([...s.matchAll(/window\.confirm\(/g)]).toHaveLength(1);
    expect(s).not.toMatch(/window\.confirm\(\s*[`'"]/);
  });
});

function apiFailure({ status, code, detail = '', message = detail }: { status: number; code: ErrorCode | null; detail?: string; message?: string }): ApiFailure {
  return new ApiFailure(status, code, detail, message);
}

const ROUTE_REFUSALS: Record<TemplateErrorKind, ApiFailure> = {
  invalid_zip: apiFailure({ status: 422, code: 'github_template.invalid_zip', detail: 'Invalid template zip' }),
  invalid_input: apiFailure({ status: 422, code: 'github_template.invalid_input', detail: 'Invalid template metadata' }),
  not_found: apiFailure({ status: 404, code: 'github_template.not_found', detail: 'Template not found' }),
  github_error: apiFailure({ status: 502, code: 'github_template.github_error', detail: 'GitHub template operation failed' }),
  store_error: apiFailure({ status: 503, code: 'github_template.store_error', detail: 'Template catalog is temporarily unavailable' }),
};

describe('TEMPLATE_ERROR_CODES — typed on the generated ErrorCode', () => {
  it('satisfies Record<TemplateErrorKind, readonly ErrorCode[]>, so a renamed backend code fails tsc where this table names it', () => {
    expectTypeOf(TEMPLATE_ERROR_CODES).toExtend<Record<TemplateErrorKind, readonly ErrorCode[]>>();
  });

  it('names at least one code per kind and no code under two kinds', () => {
    const all = Object.values(TEMPLATE_ERROR_CODES).flat();
    for (const codes of Object.values(TEMPLATE_ERROR_CODES)) expect(codes.length).toBeGreaterThan(0);
    expect(new Set(all).size).toBe(all.length);
  });

  it('classifies by code alone: the detail under no code is unrecognised, a reworded detail under the code is not', () => {
    expect(classifyTemplateFailure(apiFailure({ status: 503, code: null, detail: 'Template catalog is temporarily unavailable' }))).toBeNull();
    expect(classifyTemplateFailure(apiFailure({ status: 503, code: 'github_template.store_error', detail: 'reworded' }))).toBe('store_error');
  });
});

describe('classifyTemplateError — a 503 reads as "wait", never as a bare failure', () => {
  it('recognizes every one of the five backend codes', () => {
    for (const [kind, failure] of Object.entries(ROUTE_REFUSALS)) {
      expect(classifyTemplateError(failure, 'fallback').kind, kind).toBe(kind);
    }
  });

  it('treats the store fault as RETRYABLE and says so', () => {
    const view = classifyTemplateError(ROUTE_REFUSALS.store_error, 'fallback');
    expect(view.retryable).toBe(true);
    expect(view.message).toMatch(/temporarily unavailable/i);
    expect(view.message).toContain(TEMPLATE_RETRY_HINT);
  });

  it('promises nothing was lost, so nobody re-uploads templates that already exist', () => {
    const view = classifyTemplateError(ROUTE_REFUSALS.store_error, 'fallback');
    expect(view.message).toMatch(/nothing was changed/i);
    expect(view.message).toMatch(/unaffected/i);
    expect(view.message.toLowerCase()).not.toContain('invalid');
  });

  it('NEVER shows a 422 as retryable, or a 503 as invalid input', () => {
    for (const kind of ['invalid_zip', 'invalid_input', 'not_found'] as const) {
      const view = classifyTemplateError(ROUTE_REFUSALS[kind], 'fallback');
      expect(view.retryable, kind).toBe(false);
      expect(view.message, kind).not.toContain(TEMPLATE_RETRY_HINT);
      expect(view.message.toLowerCase(), kind).not.toContain('temporarily unavailable');
    }
    expect(classifyTemplateError(ROUTE_REFUSALS.invalid_zip, 'f').message).toMatch(/zip/i);
    expect(classifyTemplateError(ROUTE_REFUSALS.invalid_input, 'f').message).toMatch(
      /metadata|name/i,
    );
  });

  it('treats the provider fault (502) as retryable too, but not as the store fault', () => {
    const view = classifyTemplateError(ROUTE_REFUSALS.github_error, 'fallback');
    expect(view.retryable).toBe(true);
    expect(view.message).not.toBe(
      classifyTemplateError(ROUTE_REFUSALS.store_error, 'fallback').message,
    );
    expect(view.message).toMatch(/provider/i);
  });

  it('gives every recognized kind a DISTINCT sentence', () => {
    const messages = Object.values(ROUTE_REFUSALS).map(
      (failure) => classifyTemplateError(failure, 'fallback').message,
    );
    expect(new Set(messages).size).toBe(messages.length);
  });

  it('falls back TERMINALLY on an unrecognized error, and keeps its message as sent', () => {
    expect(classifyTemplateError(new Error('HTTP 500'), 'fallback')).toEqual({ message: 'HTTP 500', retryable: false, kind: null });
    expect(
      classifyTemplateError(apiFailure({ status: 404, code: 'connection.not_found', detail: 'Connection not found' }), 'fallback'),
    ).toEqual({ message: 'Connection not found', retryable: false, kind: null });
  });

  it('uses the fallback only when the error carries no message at all', () => {
    expect(classifyTemplateError(new Error(''), 'fallback').message).toBe('fallback');
    expect(classifyTemplateError(new Error('   '), 'fallback').message).toBe('fallback');
    expect(classifyTemplateError(null, 'fallback').message).toBe('fallback');
    expect(classifyTemplateError(undefined, 'fallback').message).toBe('fallback');
  });

  it('shows the interceptor’s 401 copy as sent when the load state carries its codeless 401 failure', () => {
    const fallback = 'Failed to load templates.';
    const state = failureOf(new ApiFailure(401, null, undefined, AUTH_401_HALT_MESSAGE), fallback);
    expect(state.failure).toMatchObject({ status: 401, code: null, message: AUTH_401_HALT_MESSAGE });
    expect(classifyTemplateError(state.failure ?? state.message, fallback)).toEqual({
      message: AUTH_401_HALT_MESSAGE,
      retryable: false,
      kind: null,
    });
    const s = stripComments(templatesRaw);
    expect(s).toContain("classifyTemplateError(catalogState.failure ?? catalogState.message, 'Failed to load templates.')");
    expect(s).toContain(
      "classifyTemplateError(infraCatalogState.failure ?? infraCatalogState.message, 'Failed to load infra templates.')",
    );
  });
});

describe('the page degrades on a 503 rather than showing a bare failure', () => {
  it('classifies BOTH failure paths — the catalog read and the delete', () => {
    const s = templatesSrc;
    expect([...s.matchAll(/classifyTemplateError\(/g)].length).toBeGreaterThanOrEqual(2);
    expect(s).not.toMatch(/classifyTemplateError\(new Error\(/);
    expect(s).not.toMatch(/setError\(err instanceof Error/);
    expect(s).not.toMatch(/message: err instanceof Error/);
  });

  it('drives the error card’s TONE from `retryable`, not from the message text', () => {
    const s = templatesSrc;
    expect(s).toMatch(/error\.retryable/);
    expect(s).toMatch(/error\.retryable\s*\?\s*'border-amber/);
    expect(s).toContain('border-rose-200/70');
  });

  it('does not carry the difference in COLOUR alone', () => {
    const s = templatesSrc;
    expect(s).toMatch(/Template catalog temporarily unavailable/);
    expect(s).toMatch(/Couldn’t load templates/);
  });

  it('offers the retry ONLY where the remedy is a retry, and WITHHOLDS it otherwise', () => {
    const s = templatesSrc;
    const start = s.indexOf("state === 'error'");
    expect(start).toBeGreaterThan(-1);
    const card = s.slice(start, start + 2200);
    expect(card).toContain('error.message');
    const gate = card.indexOf('{error.retryable && (');
    expect(gate).toBeGreaterThan(-1);
    const retry = card.indexOf('onClick={refetch}');
    expect(retry).toBeGreaterThan(gate);
    expect([...card.matchAll(/onClick=\{refetch\}/g)]).toHaveLength(1);
    expect(stripComments('// wrapped in {error.retryable && ( so it is withheld')).not.toContain(
      'error.retryable',
    );
  });

  it('withholds nothing else — the operator is never stranded without a way to re-read', () => {
    expect(SHOW_FILTER_STRIP.error).toBe(true);
  });
});
