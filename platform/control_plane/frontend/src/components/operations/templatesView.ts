// Decision: docs/adr/frontend-absent-standing-renders-nothing.md
// Rationale: frontend/README.md "Tests are node-environment only, by design"
// Decision: docs/adr/frontend-refusals-classified-by-code.md
import { type ApiFailure, type ErrorCode, isApiFailure } from '../../api/failure';
import { refusalKind } from '../../api/refusalKind';

export interface ConnectionLike {
  id: string;
  provider: string;
  org: string;
}

const PROVIDER_LABEL: Record<string, string> = {
  github: 'GitHub',
  gitlab: 'GitLab',
};

export function orgLabel(connection: ConnectionLike): string {
  const provider = PROVIDER_LABEL[connection.provider] ?? connection.provider;
  return `${provider} · ${connection.org}`;
}

export function orgOptions(connections: readonly ConnectionLike[]): { value: string; label: string }[] {
  return connections.map((c) => ({ value: c.id, label: orgLabel(c) }));
}

export function pickSelectedOrg(
  connections: readonly ConnectionLike[],
  current: string,
): string {
  if (current && connections.some((c) => c.id === current)) return current;
  return connections[0]?.id ?? '';
}

const ROLE_LEVEL_ADMIN = 2;

export function canMutateTemplates(roleLevel: number): boolean {
  return roleLevel >= ROLE_LEVEL_ADMIN;
}

export type TemplatesBodyState =
  | 'loading-orgs'
  | 'connections-unreadable'
  | 'no-connection'
  | 'no-selection'
  | 'loading'
  | 'error'
  | 'empty-catalog'
  | 'grid';

export interface TemplatesBodyInput {
  connectionsLoading: boolean;
  connectionsError: boolean;
  connectionCount: number;
  connectionId: string;
  catalogLoading: boolean;
  catalogError: string | null;
  templateCount: number;
}

export function templatesBodyState(input: TemplatesBodyInput): TemplatesBodyState {
  if (input.connectionsLoading) return 'loading-orgs';
  if (input.connectionsError && input.connectionCount === 0) return 'connections-unreadable';
  if (input.connectionCount === 0) return 'no-connection';
  if (!input.connectionId) return 'no-selection';
  if (input.catalogLoading) return 'loading';
  if (input.catalogError !== null) return 'error';
  if (input.templateCount === 0) return 'empty-catalog';
  return 'grid';
}

export const SHOW_FILTER_STRIP: Record<TemplatesBodyState, boolean> = {
  'loading-orgs': false,
  'connections-unreadable': false,
  'no-connection': false,
  'no-selection': true,
  loading: true,
  error: true,
  'empty-catalog': true,
  grid: true,
};

export const SHOW_TAB_BAR: Record<TemplatesBodyState, boolean> = {
  'loading-orgs': false,
  'connections-unreadable': false,
  'no-connection': false,
  'no-selection': false,
  loading: true,
  error: true,
  'empty-catalog': true,
  grid: true,
};

export type LinkCta = { kind: 'link'; label: string; to: string };
export type UploadCta = { kind: 'upload'; label: string };
export type RetryCta = { kind: 'retry'; label: string };
export type NoCta = { kind: 'none' };
export type EmptyStateCta = LinkCta | UploadCta | RetryCta | NoCta;

export interface EmptyStateCopy<Cta extends EmptyStateCta = EmptyStateCta> {
  headline: string;
  body: string;
  cta: Cta;
}

export const EMPTY_STATE_COPY: {
  'connections-unreadable': EmptyStateCopy<RetryCta>;
  'no-connection': EmptyStateCopy<LinkCta>;
  'no-selection': EmptyStateCopy<NoCta>;
  'empty-catalog': EmptyStateCopy<UploadCta>;
} = {
  'connections-unreadable': {
    headline: 'Org connections could not be read',
    body: 'The list of connected orgs failed to load, so the template catalog cannot be scoped to one. This is not a report that no org is connected — the orgs you have are unaffected.',
    cta: { kind: 'retry', label: 'Try again' },
  },
  'no-connection': {
    headline: 'No org connected yet',
    body: 'Templates are registered per connected org, so there is nothing to list until an org is connected.',
    cta: { kind: 'link', label: 'Connect an org', to: '/ops/settings' },
  },
  'no-selection': {
    headline: 'No org selected',
    body: 'Pick an org above to browse the agent templates available in it.',
    cta: { kind: 'none' },
  },
  'empty-catalog': {
    headline: 'No templates in this org',
    body: 'Seed starter templates into this org, adopt an existing repository, or upload a .zip scaffold.',
    cta: { kind: 'upload', label: 'Upload template' },
  },
};

export const TEMPLATES_SUBTITLE =
  'Registered template pointers for this connection. Metadata lives on the AGP record; materializing reads the template repository.';

export const EMPTY_CATALOG_READ_ONLY_BODY =
  'This org has no agent templates yet. An admin adds them; once one exists it appears here and you can materialize it into a project.';

export function deleteTemplateConfirm(name: string): string {
  return (
    `Deregister template “${name}”? This removes it from the catalog so it can no longer be ` +
    'materialized into a project. Its repository is left in place — AGP does not delete it, ' +
    'because a template’s source may be a public repository or a mirror in another org.'
  );
}

export type TemplateErrorKind =
  | 'invalid_zip'
  | 'invalid_input'
  | 'not_found'
  | 'github_error'
  | 'store_error';

export const TEMPLATE_RETRY_HINT = 'Try again in a moment.';

export const TEMPLATE_ERROR_CODES = {
  invalid_zip: ['github_template.invalid_zip'],
  invalid_input: ['github_template.invalid_input'],
  not_found: ['github_template.not_found'],
  github_error: ['github_template.github_error'],
  store_error: ['github_template.store_error'],
} as const satisfies Record<TemplateErrorKind, readonly ErrorCode[]>;

const TEMPLATE_RETRYABLE_KINDS: readonly TemplateErrorKind[] = ['store_error', 'github_error'];

const TEMPLATE_MESSAGES: Record<TemplateErrorKind, string> = {
  store_error:
    'The template catalog is temporarily unavailable, so this could not be completed. Nothing was changed, and the templates you have are unaffected.',
  github_error:
    'The template operation failed at the provider rather than here, so nothing was changed on the catalog.',
  invalid_zip:
    'That file isn’t a valid template zip. Check it unzips and carries the scaffold at its root, then upload it again.',
  invalid_input:
    'The template metadata isn’t valid. A name must be lowercase letters, digits and hyphens, starting with a letter.',
  not_found:
    'That template is no longer in this org’s catalog — someone may have removed it. Refresh the catalog.',
};

export interface TemplateErrorView {
  message: string;
  retryable: boolean;
  kind: TemplateErrorKind | null;
}

export function classifyTemplateFailure(failure: ApiFailure): TemplateErrorKind | null {
  return refusalKind(TEMPLATE_ERROR_CODES, failure);
}

export function classifyTemplateError(err: unknown, fallback: string): TemplateErrorView {
  const raw = typeof err === 'string' ? err.trim() : err instanceof Error && err.message ? err.message.trim() : '';
  const kind = isApiFailure(err) ? classifyTemplateFailure(err) : null;
  if (kind === null) return { message: raw || fallback, retryable: false, kind: null };
  const retryable = TEMPLATE_RETRYABLE_KINDS.includes(kind);
  return {
    message: retryable ? `${TEMPLATE_MESSAGES[kind]} ${TEMPLATE_RETRY_HINT}` : TEMPLATE_MESSAGES[kind],
    retryable,
    kind,
  };
}

export const TEMPLATES_TABS: readonly { key: string; label: string }[] = [
  { key: 'agent', label: 'Agent templates' },
  { key: 'infra', label: 'Infra templates' },
] as const;

export function templatesTabId(key: string): string {
  return `templates-tab-${key}`;
}
export function templatesTabPanelId(key: string): string {
  return `templates-tabpanel-${key}`;
}

/** The catalog `useLoad` call's dependency array in `Templates.tsx`, which `templates.test.ts` reads out of the source; the name is the component's own. */
export const CATALOG_EFFECT_DEPS = ['connectionId'] as const;

/** The infra catalog `useLoad` call's dependency array, read the same way. */
export const INFRA_CATALOG_EFFECT_DEPS = ['connectionId'] as const;
