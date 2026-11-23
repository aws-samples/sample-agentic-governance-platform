import { describe, it, expect } from 'vitest';
import type { DatabricksStageConfigInput, PrincipalHit } from '../../../api/client';
import type {
  DatabricksStageConfigOutput,
  TenantView,
} from '../../../api/openapi';
import {
  BINDING_MODE_HINT,
  EMPTY_TENANT_DRAFT,
  type DatabricksStageDraft,
  type TenantDraft,
  isValidAccountId,
  canSubmit,
  buildCreatePayload,
  buildUpdatePayload,
  addGroup,
  removeGroup,
  groupsFromIds,
  filterGroupHits,
  bindingModeLabel,
  capabilityBadges,
  databricksStageDraftFromConfig,
  draftDatabricksStages,
  draftFromTenant,
  draftPlatform,
  hasBeenProbed,
  isAccountAdminCredentialUsable,
  isDatabricksStage,
  isDatabricksStageComplete,
  isValidWorkspaceId,
  isValidWorkspaceUrl,
  platformLabel,
  draftStageKeys,
  addStage,
  removeStage,
  nextSuggestedStageKey,
  compactBindingModeLabel,
  PRIVILEGE_STATEMENT,
  compareStageKeys,
  unionStageKeys,
} from './tenantsAdminForm';
import { principalHit, tenantStageConfig } from '../../../test/wire';

const FAKE_WORKSPACE = 'https://dbc-test.cloud.databricks.com';

function databricksStageConfigOutput(
  overrides: Partial<DatabricksStageConfigOutput> = {},
): DatabricksStageConfigOutput {
  return {
    workspace_url: FAKE_WORKSPACE,
    workspace_id: '0',
    cloud: 'aws',
    region: '',
    account_id: '',
    sp_client_id: '',
    sp_client_secret_arn: '',
    ...overrides,
  };
}

function tenantView(overrides: Partial<TenantView> = {}): TenantView {
  return {
    id: 'tenant-1',
    name: 'Retail Claims',
    line_of_business: 'Claims',
    description: '',
    platform: 'aws',
    stages: { dev: tenantStageConfig() },
    entra_group_ids: [],
    capabilities: {},
    binding_mode: '',
    readiness: {},
    account_admin_secret_arn: '',
    created_by: 'oid-admin',
    created_at: '2026-08-10T00:00:00Z',
    updated_at: '2026-08-10T00:00:00Z',
    ...overrides,
  };
}

const VALID_DRAFT: TenantDraft = {
  name: 'Retail Claims',
  line_of_business: 'Claims',
  description: 'Retail claims LoB',
  stages: {
    dev: {
      account_id: '111122223333',
      region: 'us-east-1',
      ecr_repo_uri: 'dev-ecr',
      push_role_arn: 'arn:aws:iam::111122223333:role/push',
      deploy_role_arn: 'arn:aws:iam::111122223333:role/deploy',
    },
    prod: {
      account_id: '444455556666',
      region: 'us-east-1',
      ecr_repo_uri: 'prod-ecr',
      push_role_arn: 'arn:aws:iam::444455556666:role/push',
      deploy_role_arn: 'arn:aws:iam::444455556666:role/deploy',
    },
  },
  groups: [{ id: 'g1', display_name: 'Claims Ops' }],
};

describe('tenantsAdminForm', () => {
  describe('EMPTY_TENANT_DRAFT', () => {
    it('seeds a single dev stage with a default region and no groups: prod is the suggested add, not a forced second column', () => {
      expect(draftStageKeys(EMPTY_TENANT_DRAFT)).toEqual(['dev']);
      expect(EMPTY_TENANT_DRAFT.stages.dev.region).toBe('us-east-1');
      expect(EMPTY_TENANT_DRAFT.stages.dev.account_id).toBe('');
      expect(EMPTY_TENANT_DRAFT.groups).toEqual([]);
      expect(EMPTY_TENANT_DRAFT.name).toBe('');
    });
  });

  describe('isValidAccountId', () => {
    it('accepts exactly 12 digits', () => {
      expect(isValidAccountId('123456789012')).toBe(true);
    });
    it('rejects too short / too long / non-digit / empty', () => {
      expect(isValidAccountId('12345678901')).toBe(false);
      expect(isValidAccountId('1234567890123')).toBe(false);
      expect(isValidAccountId('12345678901a')).toBe(false);
      expect(isValidAccountId('')).toBe(false);
      expect(isValidAccountId('1234 5678901')).toBe(false);
    });
  });

  describe('canSubmit', () => {
    it('is true for a complete draft', () => {
      expect(canSubmit(VALID_DRAFT)).toBe(true);
    });
    it('requires a non-blank name', () => {
      expect(canSubmit({ ...VALID_DRAFT, name: '  ' })).toBe(false);
    });
    it('requires a non-blank line of business', () => {
      expect(canSubmit({ ...VALID_DRAFT, line_of_business: '' })).toBe(false);
    });
    it('requires at least one linked group', () => {
      expect(canSubmit({ ...VALID_DRAFT, groups: [] })).toBe(false);
    });
    it('requires a valid dev account id', () => {
      expect(
        canSubmit({
          ...VALID_DRAFT,
          stages: { ...VALID_DRAFT.stages, dev: { ...VALID_DRAFT.stages.dev, account_id: '123' } },
        }),
      ).toBe(false);
    });
    it('requires a valid prod account id', () => {
      expect(
        canSubmit({
          ...VALID_DRAFT,
          stages: { ...VALID_DRAFT.stages, prod: { ...VALID_DRAFT.stages.prod, account_id: 'abc' } },
        }),
      ).toBe(false);
    });
    it('is false when a stage account is not 12 digits', () => {
      const draft: TenantDraft = {
        ...EMPTY_TENANT_DRAFT,
        name: 'N',
        groups: [{ id: 'g', display_name: 'g' }],
        stages: {
          dev: { ...EMPTY_TENANT_DRAFT.stages.dev, account_id: 'bad' },
          prod: { ...EMPTY_TENANT_DRAFT.stages.prod, account_id: '222222222222' },
        },
      };
      expect(canSubmit(draft)).toBe(false);
    });
  });

  describe('buildCreatePayload', () => {
    it('trims text fields and maps groups to entra_group_ids', () => {
      const payload = buildCreatePayload({
        ...VALID_DRAFT,
        name: '  Retail Claims  ',
        line_of_business: ' Claims ',
        description: '  desc  ',
        stages: {
          dev: {
            account_id: ' 111122223333 ',
            region: ' us-east-1 ',
            ecr_repo_uri: ' dev-ecr ',
            push_role_arn: ' arn:dev:push ',
            deploy_role_arn: ' arn:dev:deploy ',
          },
          prod: {
            account_id: ' 444455556666 ',
            region: ' us-west-2 ',
            ecr_repo_uri: ' prod-ecr ',
            push_role_arn: ' arn:prod:push ',
            deploy_role_arn: ' arn:prod:deploy ',
          },
        },
        groups: [
          { id: 'g1', display_name: 'Claims Ops' },
          { id: 'g2', display_name: 'Claims Leads' },
        ],
      });
      expect(payload).toEqual({
        name: 'Retail Claims',
        line_of_business: 'Claims',
        description: 'desc',
        stages: {
          dev: {
            account_id: '111122223333',
            region: 'us-east-1',
            ecr_repo_uri: 'dev-ecr',
            push_role_arn: 'arn:dev:push',
            deploy_role_arn: 'arn:dev:deploy',
          },
          prod: {
            account_id: '444455556666',
            region: 'us-west-2',
            ecr_repo_uri: 'prod-ecr',
            push_role_arn: 'arn:prod:push',
            deploy_role_arn: 'arn:prod:deploy',
          },
        },
        entra_group_ids: ['g1', 'g2'],
      });
    });

    it('emits nested stages', () => {
      const draft: TenantDraft = {
        ...EMPTY_TENANT_DRAFT,
        name: 'N',
        line_of_business: 'L',
        groups: [{ id: 'g', display_name: 'g' }],
        stages: {
          dev: {
            account_id: '111111111111',
            region: 'us-east-1',
            ecr_repo_uri: 'u',
            push_role_arn: '',
            deploy_role_arn: '',
          },
          prod: {
            account_id: '222222222222',
            region: 'us-east-1',
            ecr_repo_uri: '',
            push_role_arn: '',
            deploy_role_arn: '',
          },
        },
      };
      const payload = buildCreatePayload(draft);
      expect(payload.stages.dev.account_id).toBe('111111111111');
      expect(payload.stages.prod.account_id).toBe('222222222222');
    });
  });

  describe('buildUpdatePayload', () => {
    it('produces the same full-body shape as create (PUT sends every field)', () => {
      expect(buildUpdatePayload(VALID_DRAFT)).toEqual(buildCreatePayload(VALID_DRAFT));
    });
  });

  describe('addGroup', () => {
    const hit: PrincipalHit = principalHit({ id: 'g2', display_name: 'Claims Leads', type: 'group' });
    it('appends a new group', () => {
      const out = addGroup(VALID_DRAFT.groups, hit);
      expect(out.map((g) => g.id)).toEqual(['g1', 'g2']);
      expect(out[1].display_name).toBe('Claims Leads');
    });
    it('dedupes by id (no double-add)', () => {
      const dup: PrincipalHit = principalHit({ id: 'g1', display_name: 'Claims Ops', type: 'group' });
      const out = addGroup(VALID_DRAFT.groups, dup);
      expect(out).toEqual(VALID_DRAFT.groups);
    });
    it('does not mutate the input list', () => {
      const input = [...VALID_DRAFT.groups];
      addGroup(input, hit);
      expect(input.map((g) => g.id)).toEqual(['g1']);
    });
  });

  describe('removeGroup', () => {
    it('removes by id and leaves the rest', () => {
      const groups = [
        { id: 'g1', display_name: 'A' },
        { id: 'g2', display_name: 'B' },
      ];
      expect(removeGroup(groups, 'g1').map((g) => g.id)).toEqual(['g2']);
    });
    it('is a no-op for an unknown id', () => {
      expect(removeGroup(VALID_DRAFT.groups, 'nope')).toEqual(VALID_DRAFT.groups);
    });
  });

  describe('groupsFromIds', () => {
    it('maps raw entra_group_ids to chips (display_name falls back to the id)', () => {
      expect(groupsFromIds(['g1', 'g2'])).toEqual([
        { id: 'g1', display_name: 'g1' },
        { id: 'g2', display_name: 'g2' },
      ]);
    });
  });

  describe('filterGroupHits', () => {
    it('keeps only groups and drops already-selected ids: the directory search has no kind filter, so groups-only is decided here', () => {
      const hits: PrincipalHit[] = [
        principalHit({ id: 'u1', display_name: 'Ana', type: 'user' }),
        principalHit({ id: 'g1', display_name: 'Claims Ops', type: 'group' }),
        principalHit({ id: 'g2', display_name: 'Claims Leads', type: 'group' }),
      ];
      const out = filterGroupHits(hits, VALID_DRAFT.groups);
      expect(out.map((h) => h.id)).toEqual(['g2']);
    });
  });

  const VALID_DB_STAGE: DatabricksStageDraft = {
    workspace_url: FAKE_WORKSPACE,
    workspace_id: '1234567890123456',
    cloud: 'aws',
    region: 'us-east-1',
    account_id: 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee',
    sp_client_id: 'sp-client-id',
    sp_client_secret: 'typed-secret',
    sp_client_secret_arn: '',
    has_sp_secret: false,
  };

  const STORED_ARN = 'arn:aws:secretsmanager:::secret:agp/databricks/dev-AbCdEf';
  const EDITED_DB_STAGE: DatabricksStageDraft = {
    ...VALID_DB_STAGE,
    sp_client_secret: '',
    sp_client_secret_arn: STORED_ARN,
    has_sp_secret: true,
  };

  const VALID_DB_DRAFT: TenantDraft = {
    name: 'Retail Claims',
    line_of_business: 'Claims',
    description: 'Retail claims LoB',
    platform: 'databricks',
    stages: EMPTY_TENANT_DRAFT.stages,
    databricks: { dev: { ...VALID_DB_STAGE }, prod: { ...VALID_DB_STAGE } },
    account_admin_client_id: 'admin-client-id',
    account_admin_secret: 'admin-secret',
    groups: [{ id: 'g1', display_name: 'Claims Ops' }],
  };

  const NO_ADMIN_DB_DRAFT: TenantDraft = {
    ...VALID_DB_DRAFT,
    account_admin_client_id: '',
    account_admin_secret: '',
  };

  describe('draftPlatform', () => {
    it('defaults an absent platform to aws for both the empty draft and a draft that never set one', () => {
      expect(draftPlatform(VALID_DRAFT)).toBe('aws');
      expect(draftPlatform(EMPTY_TENANT_DRAFT)).toBe('aws');
    });
    it('reads an explicit platform', () => {
      expect(draftPlatform(VALID_DB_DRAFT)).toBe('databricks');
    });
  });

  describe('platformLabel', () => {
    it('labels both platforms', () => {
      expect(platformLabel('aws')).toBe('AWS');
      expect(platformLabel('databricks')).toBe('Databricks');
    });
  });

  describe('isValidWorkspaceUrl — the security boundary, every hostile input executed against the real pattern', () => {
    it('accepts a plain https workspace origin', () => {
      expect(isValidWorkspaceUrl(FAKE_WORKSPACE)).toBe(true);
      expect(isValidWorkspaceUrl('https://adb-123.4.azuredatabricks.net')).toBe(true);
    });

    it('rejects a javascript: scheme', () => {
      expect(isValidWorkspaceUrl('javascript:alert(1)')).toBe(false);
      expect(isValidWorkspaceUrl('javascript:https://dbc-test.cloud.databricks.com')).toBe(false);
    });

    it('rejects plaintext http', () => {
      expect(isValidWorkspaceUrl('http://dbc-test.cloud.databricks.com')).toBe(false);
    });

    it('rejects a trailing slash (the record stores a bare origin)', () => {
      expect(isValidWorkspaceUrl(`${FAKE_WORKSPACE}/`)).toBe(false);
    });

    it('rejects an embedded space', () => {
      expect(isValidWorkspaceUrl('https://dbc test.cloud.databricks.com')).toBe(false);
      expect(isValidWorkspaceUrl(`${FAKE_WORKSPACE} `)).toBe(false);
      expect(isValidWorkspaceUrl(` ${FAKE_WORKSPACE}`)).toBe(false);
    });

    it('rejects a trailing newline and anything smuggled after it (the case a multiline flag on the pattern would let through)', () => {
      expect(isValidWorkspaceUrl(`${FAKE_WORKSPACE}\n`)).toBe(false);
      expect(isValidWorkspaceUrl(`${FAKE_WORKSPACE}\njavascript:alert(1)`)).toBe(false);
      expect(isValidWorkspaceUrl(`${FAKE_WORKSPACE}\r\n`)).toBe(false);
    });

    it('rejects a path, query, fragment, port or credentials', () => {
      expect(isValidWorkspaceUrl(`${FAKE_WORKSPACE}/api/2.0/apps`)).toBe(false);
      expect(isValidWorkspaceUrl(`${FAKE_WORKSPACE}?o=1`)).toBe(false);
      expect(isValidWorkspaceUrl(`${FAKE_WORKSPACE}#frag`)).toBe(false);
      expect(isValidWorkspaceUrl(`${FAKE_WORKSPACE}:443`)).toBe(false);
      expect(isValidWorkspaceUrl('https://user:pw@dbc-test.cloud.databricks.com')).toBe(false);
    });

    it('rejects an uppercase host (the backend regex is lowercase-only)', () => {
      expect(isValidWorkspaceUrl('https://DBC-TEST.cloud.databricks.com')).toBe(false);
    });

    it('rejects empty and scheme-only input', () => {
      expect(isValidWorkspaceUrl('')).toBe(false);
      expect(isValidWorkspaceUrl('https://')).toBe(false);
    });
  });

  describe('isValidWorkspaceId', () => {
    it('accepts a digits-string, including "0"', () => {
      expect(isValidWorkspaceId('0')).toBe(true);
      expect(isValidWorkspaceId('1234567890123456')).toBe(true);
    });
    it('rejects empty, non-digits, whitespace and a trailing newline', () => {
      expect(isValidWorkspaceId('')).toBe(false);
      expect(isValidWorkspaceId('12a')).toBe(false);
      expect(isValidWorkspaceId('12 34')).toBe(false);
      expect(isValidWorkspaceId('123\n')).toBe(false);
      expect(isValidWorkspaceId('-1')).toBe(false);
    });
    it('rejects non-ASCII digits (mirrors the backend re.ASCII)', () => {
      expect(isValidWorkspaceId('١٢٣')).toBe(false);
    });
  });

  describe('isDatabricksStageComplete', () => {
    it('is true for a complete stage', () => {
      expect(isDatabricksStageComplete(VALID_DB_STAGE)).toBe(true);
    });
    it('requires a valid workspace url', () => {
      expect(
        isDatabricksStageComplete({ ...VALID_DB_STAGE, workspace_url: 'http://nope.example' }),
      ).toBe(false);
    });
    it('requires a digits-only workspace id', () => {
      expect(isDatabricksStageComplete({ ...VALID_DB_STAGE, workspace_id: 'abc' })).toBe(false);
    });
    it('requires an sp_client_id', () => {
      expect(isDatabricksStageComplete({ ...VALID_DB_STAGE, sp_client_id: '   ' })).toBe(false);
    });
    it('requires a secret — typed now, or already stored', () => {
      const noSecret = { ...VALID_DB_STAGE, sp_client_secret: '', has_sp_secret: false };
      expect(isDatabricksStageComplete(noSecret)).toBe(false);
      expect(isDatabricksStageComplete({ ...noSecret, has_sp_secret: true })).toBe(true);
    });
    it('does NOT require the Databricks account id (federation-only, probe decides)', () => {
      expect(isDatabricksStageComplete({ ...VALID_DB_STAGE, account_id: '' })).toBe(true);
    });
  });

  describe('isAccountAdminCredentialUsable — shape only: is what was typed coherent, not is a credential present', () => {
    it('accepts the credential being absent entirely', () => {
      expect(isAccountAdminCredentialUsable(NO_ADMIN_DB_DRAFT)).toBe(true);
    });
    it('accepts both halves present', () => {
      expect(
        isAccountAdminCredentialUsable({
          ...VALID_DB_DRAFT,
          account_admin_client_id: 'admin-id',
          account_admin_secret: 'admin-secret',
        }),
      ).toBe(true);
    });
    it('refuses half a credential pair in either direction', () => {
      expect(
        isAccountAdminCredentialUsable({ ...NO_ADMIN_DB_DRAFT, account_admin_client_id: 'admin-id' }),
      ).toBe(false);
      expect(
        isAccountAdminCredentialUsable({ ...NO_ADMIN_DB_DRAFT, account_admin_secret: 'admin-secret' }),
      ).toBe(false);
    });
  });

  describe('canSubmit (databricks branch)', () => {
    it('is true for a complete databricks draft', () => {
      expect(canSubmit(VALID_DB_DRAFT)).toBe(true);
    });
    it('still requires name, LoB and a linked group', () => {
      expect(canSubmit({ ...VALID_DB_DRAFT, name: ' ' })).toBe(false);
      expect(canSubmit({ ...VALID_DB_DRAFT, line_of_business: '' })).toBe(false);
      expect(canSubmit({ ...VALID_DB_DRAFT, groups: [] })).toBe(false);
    });
    it('requires BOTH stages to be complete', () => {
      expect(
        canSubmit({
          ...VALID_DB_DRAFT,
          databricks: {
            dev: VALID_DB_STAGE,
            prod: { ...VALID_DB_STAGE, workspace_url: 'not-a-url' },
          },
        }),
      ).toBe(false);
    });
    it('refuses a hostile workspace url even when every other field is filled in', () => {
      for (const hostile of [
        'javascript:alert(1)',
        `${FAKE_WORKSPACE}/`,
        `${FAKE_WORKSPACE}\njavascript:alert(1)`,
        'http://dbc-test.cloud.databricks.com',
      ]) {
        expect(
          canSubmit({
            ...VALID_DB_DRAFT,
            databricks: {
              dev: { ...VALID_DB_STAGE, workspace_url: hostile },
              prod: VALID_DB_STAGE,
            },
          }),
        ).toBe(false);
      }
    });
    it('refuses half an account-admin credential pair', () => {
      expect(canSubmit({ ...VALID_DB_DRAFT, account_admin_client_id: '', account_admin_secret: 'orphan' })).toBe(
        false,
      );
      expect(canSubmit({ ...VALID_DB_DRAFT, account_admin_secret: '' })).toBe(false);
    });

    it('requires an account-admin credential on create and never on edit: without one the tenant cannot invoke, and an existing one must stay administrable', () => {
      expect(canSubmit(NO_ADMIN_DB_DRAFT)).toBe(false);
      expect(canSubmit(NO_ADMIN_DB_DRAFT, true)).toBe(true);
    });

    it('accepts blank boxes on an EDIT when a credential is already stored', () => {
      expect(canSubmit({ ...NO_ADMIN_DB_DRAFT, has_account_admin_credential: true })).toBe(true);
    });

    it('still refuses half a pair even when one is stored (a typo is not a choice)', () => {
      expect(
        canSubmit({
          ...NO_ADMIN_DB_DRAFT,
          has_account_admin_credential: true,
          account_admin_client_id: 'admin-id',
        }),
      ).toBe(false);
    });

    it('never requires an account-admin credential for an aws draft', () => {
      expect(canSubmit(VALID_DRAFT)).toBe(true);
      expect(canSubmit({ ...VALID_DRAFT, account_admin_client_id: '', account_admin_secret: '' })).toBe(true);
    });
    it('ignores the databricks half entirely for an aws draft', () => {
      expect(
        canSubmit({
          ...VALID_DRAFT,
          databricks: { dev: { ...VALID_DB_STAGE, workspace_url: 'garbage' }, prod: VALID_DB_STAGE },
        }),
      ).toBe(true);
    });
  });

  describe('buildCreatePayload (databricks branch)', () => {
    it('emits the databricks platform, trimmed stages, and the typed secret', () => {
      const payload = buildCreatePayload({
        ...VALID_DB_DRAFT,
        databricks: {
          dev: { ...VALID_DB_STAGE, workspace_url: `  ${FAKE_WORKSPACE}  `, sp_client_id: ' spid ' },
          prod: VALID_DB_STAGE,
        },
      });
      expect(payload.platform).toBe('databricks');
      const dev = payload.stages.dev as DatabricksStageConfigInput;
      expect(dev.workspace_url).toBe(FAKE_WORKSPACE);
      expect(dev.sp_client_id).toBe('spid');
      expect(dev.sp_client_secret).toBe('typed-secret');
      expect(dev.sp_client_secret_arn).toBe('');
    });

    it('OMITS sp_client_secret when the box was left empty (an edit keeps the stored one)', () => {
      const payload = buildCreatePayload({
        ...VALID_DB_DRAFT,
        databricks: {
          dev: { ...VALID_DB_STAGE, sp_client_secret: '', has_sp_secret: true },
          prod: { ...VALID_DB_STAGE, sp_client_secret: '', has_sp_secret: true },
        },
      });
      const dev = payload.stages.dev as DatabricksStageConfigInput;
      expect('sp_client_secret' in dev).toBe(false);
    });

    it('preserves the stored secret ARN verbatim when the secret box is left blank: the update replaces the whole stage object, so an empty ARN would destroy the stored secret', () => {
      const payload = buildCreatePayload({
        ...VALID_DB_DRAFT,
        databricks: { dev: EDITED_DB_STAGE, prod: EDITED_DB_STAGE },
      });
      const dev = payload.stages.dev as DatabricksStageConfigInput;
      expect(dev.sp_client_secret_arn).toBe(STORED_ARN);
      expect('sp_client_secret' in dev).toBe(false);
    });

    it('sends the OLD ARN alongside a newly typed secret (the backend decides the swap)', () => {
      const payload = buildCreatePayload({
        ...VALID_DB_DRAFT,
        databricks: {
          dev: { ...EDITED_DB_STAGE, sp_client_secret: 'rotated-secret' },
          prod: EDITED_DB_STAGE,
        },
      });
      const dev = payload.stages.dev as DatabricksStageConfigInput;
      expect(dev.sp_client_secret_arn).toBe(STORED_ARN);
      expect(dev.sp_client_secret).toBe('rotated-secret');
    });

    it('emits an empty ARN on create, where there is genuinely nothing to preserve', () => {
      const payload = buildCreatePayload(VALID_DB_DRAFT);
      const dev = payload.stages.dev as DatabricksStageConfigInput;
      expect(dev.sp_client_secret_arn).toBe('');
      expect(dev.sp_client_secret).toBe('typed-secret');
    });

    it('carries the ARN through an update payload too (the path that actually destroyed it)', () => {
      const payload = buildUpdatePayload({
        ...VALID_DB_DRAFT,
        databricks: { dev: EDITED_DB_STAGE, prod: EDITED_DB_STAGE },
      });
      const dev = payload.stages?.dev as DatabricksStageConfigInput;
      expect(dev.sp_client_secret_arn).toBe(STORED_ARN);
    });

    it('does not trim the secret itself (whitespace can be significant in a credential)', () => {
      const payload = buildCreatePayload({
        ...VALID_DB_DRAFT,
        databricks: {
          dev: { ...VALID_DB_STAGE, sp_client_secret: ' padded ' },
          prod: VALID_DB_STAGE,
        },
      });
      const dev = payload.stages.dev as DatabricksStageConfigInput;
      expect(dev.sp_client_secret).toBe(' padded ');
    });

    it('sends the account-admin credential only when both halves are present', () => {
      expect(buildCreatePayload(NO_ADMIN_DB_DRAFT).account_admin_client_id).toBeUndefined();
      const withAdmin = buildCreatePayload({
        ...VALID_DB_DRAFT,
        account_admin_client_id: ' admin-id ',
        account_admin_secret: 'admin-secret',
      });
      expect(withAdmin.account_admin_client_id).toBe('admin-id');
      expect(withAdmin.account_admin_secret).toBe('admin-secret');
    });

    it('cannot produce an sp_secret outcome: no mode is asserted, and admin creds always travel', () => {
      const payload = buildCreatePayload(VALID_DB_DRAFT) as unknown as Record<string, unknown>;
      expect('binding_mode' in payload).toBe(false);
      expect(JSON.stringify(payload)).not.toContain('sp_secret');
      expect(payload.account_admin_client_id).toBe('admin-client-id');
      expect(canSubmit(NO_ADMIN_DB_DRAFT)).toBe(false);
    });

    it('never sends client-asserted capabilities or a binding mode', () => {
      const payload = buildCreatePayload(VALID_DB_DRAFT) as unknown as Record<string, unknown>;
      expect('capabilities' in payload).toBe(false);
      expect('binding_mode' in payload).toBe(false);
      expect('federation_audience' in payload).toBe(false);
    });

    it('omits platform entirely for an aws draft', () => {
      const payload = buildCreatePayload(VALID_DRAFT) as unknown as Record<string, unknown>;
      expect('platform' in payload).toBe(false);
    });
  });

  describe('buildUpdatePayload (platform immutability)', () => {
    it('strips platform — a tenant can never be re-typed', () => {
      const payload = buildUpdatePayload(VALID_DB_DRAFT) as unknown as Record<string, unknown>;
      expect('platform' in payload).toBe(false);
      expect(payload.name).toBe('Retail Claims');
      expect(payload.stages).toBeDefined();
    });
  });

  describe('bindingModeLabel — the badge copy, verbatim, shared with the agent-detail badge and the onboarding doc', () => {
    it('reads exactly "Federation" and "SP secret"', () => {
      expect(bindingModeLabel('federation')).toBe('Federation');
      expect(bindingModeLabel('sp_secret')).toBe('SP secret');
    });
    it('reads exactly "Invoke unavailable — federation required" for invoke_unavailable: the badge itself says what is refused and what fixes it', () => {
      expect(bindingModeLabel('invoke_unavailable')).toBe('Invoke unavailable — federation required');
    });
    it('is null for an aws tenant, an unknown value and an absent one: a badge never renders a raw wire value', () => {
      expect(bindingModeLabel('')).toBeNull();
      expect(bindingModeLabel(undefined)).toBeNull();
      expect(bindingModeLabel('something_new')).toBeNull();
    });
  });

  describe('BINDING_MODE_HINT — what each mode means for the caller, in the badge title', () => {
    it('invoke_unavailable names what is refused, what still works, and the two things federation needs', () => {
      expect(BINDING_MODE_HINT.invoke_unavailable).toContain('Invoke is refused');
      expect(BINDING_MODE_HINT.invoke_unavailable).toContain('discovery, registration and observability still work');
      expect(BINDING_MODE_HINT.invoke_unavailable).toContain('account-admin credential');
      expect(BINDING_MODE_HINT.invoke_unavailable).toContain('user sync from Entra to Databricks');
    });
    it('sp_secret, a mode no connect flow produces any more, still states attribution to the service principal for the records that carry it', () => {
      expect(BINDING_MODE_HINT.sp_secret).toContain('service principal');
      expect(BINDING_MODE_HINT.sp_secret).toContain('not the individual caller');
    });
    it('federation states that the real caller is seen', () => {
      expect(BINDING_MODE_HINT.federation).toContain('see the real caller');
    });
  });

  describe('capabilityBadges', () => {
    it('is empty for an aws tenant (never probed, nothing to say)', () => {
      expect(capabilityBadges('aws', { can_discover: true })).toEqual([]);
    });
    it('returns the three probes in a fixed order', () => {
      const badges = capabilityBadges('databricks', {
        can_discover: true,
        account_admin: false,
        user_sync: true,
      });
      expect(badges.map((b) => b.key)).toEqual(['can_discover', 'account_admin', 'user_sync']);
      expect(badges.map((b) => b.label)).toEqual(['Discovery', 'Account admin', 'User sync']);
      expect(badges.map((b) => b.on)).toEqual([true, false, true]);
    });
    it('keeps "not probed" (undefined) distinct from "probed false"', () => {
      const badges = capabilityBadges('databricks', { can_discover: false });
      expect(badges[0].on).toBe(false);
      expect(badges[1].on).toBeUndefined();
      expect(capabilityBadges('databricks', undefined).every((b) => b.on === undefined)).toBe(true);
    });
  });

  describe('hasBeenProbed', () => {
    it('is false for absent and empty capabilities', () => {
      expect(hasBeenProbed(undefined)).toBe(false);
      expect(hasBeenProbed({})).toBe(false);
    });
    it('is true when a probe reported, even a negative one', () => {
      expect(hasBeenProbed({ can_discover: false })).toBe(true);
    });
  });

  describe('isDatabricksStage', () => {
    it('narrows on workspace_url', () => {
      expect(isDatabricksStage(undefined)).toBe(false);
      expect(isDatabricksStage(VALID_DRAFT.stages.dev)).toBe(false);
      expect(isDatabricksStage(databricksStageConfigOutput({ workspace_url: FAKE_WORKSPACE }))).toBe(true);
    });

    it('answers false for null without throwing: a null stage value off /users/me once blanked the page for every platform', () => {
      expect(() => isDatabricksStage(null)).not.toThrow();
      expect(isDatabricksStage(null)).toBe(false);
    });

    it('answers false for primitives rather than throwing', () => {
      for (const v of ['', 'x', 0, 7, true, false, NaN]) {
        expect(isDatabricksStage(v), String(v)).toBe(false);
      }
    });
  });

  describe('databricksStageDraftFromConfig', () => {
    it('derives has_sp_secret from the stored ARN and NEVER seeds a secret', () => {
      const draft = databricksStageDraftFromConfig(
        databricksStageConfigOutput({
          workspace_id: '99',
          cloud: 'azure',
          sp_client_secret_arn: 'arn:aws:secretsmanager:::secret:x',
        }),
      );
      expect(draft.has_sp_secret).toBe(true);
      expect(draft.sp_client_secret).toBe('');
      expect(draft.sp_client_secret_arn).toBe('arn:aws:secretsmanager:::secret:x');
      expect(draft.workspace_id).toBe('99');
      expect(draft.cloud).toBe('azure');
    });
    it('has_sp_secret is false when no ARN is stored', () => {
      const draft = databricksStageDraftFromConfig(databricksStageConfigOutput({ sp_client_secret_arn: '' }));
      expect(draft.has_sp_secret).toBe(false);
    });
    it('yields a blank draft for an absent config: workspace id "0", cloud aws, region blank (a Databricks region is not an AWS region, so none is guessed)', () => {
      const draft = databricksStageDraftFromConfig(undefined);
      expect(draft.workspace_url).toBe('');
      expect(draft.workspace_id).toBe('0');
      expect(draft.cloud).toBe('aws');
      expect(draft.region).toBe('');
      expect(draft.has_sp_secret).toBe(false);
    });
  });

  describe('draftFromTenant', () => {
    const DB_STAGE = databricksStageConfigOutput({
      workspace_url: FAKE_WORKSPACE,
      account_id: 'acct-uuid',
      sp_client_id: 'spid',
      sp_client_secret_arn: 'arn:aws:secretsmanager:::secret:x',
    });

    it('seeds a databricks tenant into the databricks half, with has-secret state', () => {
      const draft = draftFromTenant(
        tenantView({ platform: 'databricks', stages: { dev: DB_STAGE, prod: DB_STAGE }, entra_group_ids: ['g1'] }),
      );
      expect(draft.platform).toBe('databricks');
      expect(draft.databricks?.dev.workspace_url).toBe(FAKE_WORKSPACE);
      expect(draft.databricks?.dev.has_sp_secret).toBe(true);
      expect(draft.databricks?.dev.sp_client_secret).toBe('');
      expect(draft.groups).toEqual([{ id: 'g1', display_name: 'g1' }]);
    });

    it('never seeds the write-only account-admin credential', () => {
      const draft = draftFromTenant(tenantView({ platform: 'databricks', stages: { dev: DB_STAGE, prod: DB_STAGE } }));
      expect(draft.account_admin_client_id).toBe('');
      expect(draft.account_admin_secret).toBe('');
    });

    it('records that an account-admin credential is STORED, from the ARN pointer alone', () => {
      const base: Partial<TenantView> = { platform: 'databricks', stages: { dev: DB_STAGE, prod: DB_STAGE } };
      expect(
        draftFromTenant(tenantView({ ...base, account_admin_secret_arn: 'arn:aws:secretsmanager:::secret:admin' }))
          .has_account_admin_credential,
      ).toBe(true);
      expect(
        draftFromTenant(tenantView({ ...base, account_admin_secret_arn: '' })).has_account_admin_credential,
      ).toBe(false);
    });

    it('seeds exactly the stages the record carries — a single-stage tenant stays single', () => {
      const draft = draftFromTenant(
        tenantView({ stages: { dev: tenantStageConfig({ account_id: '111122223333' }) } }),
      );
      expect(draftStageKeys(draft)).toEqual(['dev']);
      expect(draft.stages.dev.account_id).toBe('111122223333');
      expect(draft.stages.prod).toBeUndefined();
    });

    it('seeds the dev stage for a record that carries no stages at all, so the draft is not an unsubmittable empty one', () => {
      const draft = draftFromTenant(tenantView({ stages: {} }));
      expect(draftStageKeys(draft)).toEqual(['dev']);
      expect(draft.stages.dev.account_id).toBe('');
      expect(draft.databricks?.dev.workspace_url).toBe('');
    });

    it('does not put databricks stages into the aws half, or vice versa', () => {
      const draft = draftFromTenant(tenantView({ platform: 'databricks', stages: { dev: DB_STAGE, prod: DB_STAGE } }));
      expect(draft.stages.dev.account_id).toBe('');
      expect(draft.databricks?.dev.account_id).toBe('acct-uuid');
    });

    it('a no-op edit round-trip (record → draft → update payload) preserves the stored secret ARN', () => {
      const draft = draftFromTenant(
        tenantView({ platform: 'databricks', stages: { dev: DB_STAGE, prod: DB_STAGE }, entra_group_ids: ['g1'] }),
      );
      const payload = buildUpdatePayload(draft);
      for (const stage of ['dev', 'prod']) {
        const s = payload.stages?.[stage] as DatabricksStageConfigInput;
        expect(s.sp_client_secret_arn).toBe(DB_STAGE.sp_client_secret_arn);
        expect('sp_client_secret' in s).toBe(false);
      }
    });

    it('round-trips a databricks tenant back to a submittable draft with no secret typed, because both are already stored', () => {
      const draft = draftFromTenant(
        tenantView({
          platform: 'databricks',
          stages: { dev: DB_STAGE, prod: DB_STAGE },
          entra_group_ids: ['g1'],
          account_admin_secret_arn: 'arn:aws:secretsmanager:::secret:admin',
        }),
      );
      expect(canSubmit(draft)).toBe(true);
    });

    it('keeps a tenant with no stored account-admin credential editable, but refuses it on create', () => {
      const draft = draftFromTenant(
        tenantView({ platform: 'databricks', stages: { dev: DB_STAGE, prod: DB_STAGE }, entra_group_ids: ['g1'] }),
      );
      expect(canSubmit(draft, true)).toBe(true);
      expect(canSubmit(draft)).toBe(false);
      expect(
        canSubmit({ ...draft, account_admin_client_id: 'admin-id', account_admin_secret: 's' }, true),
      ).toBe(true);
      expect(canSubmit({ ...draft, account_admin_client_id: 'admin-id' }, true)).toBe(false);
    });
  });

  describe('draftStageKeys', () => {
    it('derives the pair from a two-stage draft that carries no explicit list', () => {
      expect(draftStageKeys(VALID_DRAFT)).toEqual(['dev', 'prod']);
      expect(draftStageKeys(VALID_DB_DRAFT)).toEqual(['dev', 'prod']);
    });
    it('reads the platform-relevant map: databricks keys for a databricks draft', () => {
      const draft: TenantDraft = {
        ...VALID_DB_DRAFT,
        stages: { dev: EMPTY_TENANT_DRAFT.stages.dev },
        databricks: { dev: VALID_DB_STAGE, staging: VALID_DB_STAGE, prod: VALID_DB_STAGE },
      };
      expect(draftStageKeys(draft)).toEqual(['dev', 'staging', 'prod']);
    });
    it('honours an explicit stageKeys list', () => {
      expect(draftStageKeys({ ...VALID_DRAFT, stageKeys: ['prod'] })).toEqual(['prod']);
    });
  });

  describe('draftDatabricksStages', () => {
    it('materialises a blank Databricks stage for every listed key, so a key added on the AWS side has a twin and a platform toggle never lands on undefined', () => {
      const draft: TenantDraft = {
        ...VALID_DB_DRAFT,
        stageKeys: ['dev', 'staging'],
        databricks: { dev: VALID_DB_STAGE },
      };
      const stages = draftDatabricksStages(draft);
      expect(Object.keys(stages)).toEqual(['dev', 'staging']);
      expect(stages.dev).toEqual(VALID_DB_STAGE);
      expect(stages.staging.workspace_url).toBe('');
      expect(stages.staging.workspace_id).toBe('0');
    });
    it('reads every listed key as a blank stage for a draft that carries no databricks map at all', () => {
      const stages = draftDatabricksStages({ ...VALID_DRAFT, platform: 'databricks', stageKeys: ['dev', 'prod'] });
      expect(Object.keys(stages)).toEqual(['dev', 'prod']);
      expect(stages.prod.sp_client_id).toBe('');
      expect(stages.prod.has_sp_secret).toBe(false);
    });
  });

  describe('arbitrary stage keys round-trip (draftFromTenant -> buildCreatePayload)', () => {
    it('preserves an open set of stage names, in order', () => {
      const draft = draftFromTenant(
        tenantView({
          stages: {
            dev: tenantStageConfig({ account_id: '111111111111' }),
            staging: tenantStageConfig({ account_id: '222222222222', region: 'us-west-2' }),
            prod: tenantStageConfig({ account_id: '333333333333', region: 'eu-west-1' }),
          },
        }),
      );
      expect(draftStageKeys(draft)).toEqual(['dev', 'staging', 'prod']);
      const payload = buildCreatePayload(draft);
      expect(Object.keys(payload.stages)).toEqual(['dev', 'staging', 'prod']);
      expect(payload.stages.staging.account_id).toBe('222222222222');
      expect(payload.stages.prod.region).toBe('eu-west-1');
    });

    it('round-trips arbitrary databricks stage keys too', () => {
      const DB = databricksStageConfigOutput({ workspace_url: FAKE_WORKSPACE });
      const draft = draftFromTenant(tenantView({ platform: 'databricks', stages: { dev: DB, staging: DB } }));
      expect(draftStageKeys(draft)).toEqual(['dev', 'staging']);
      const payload = buildUpdatePayload(draft);
      expect(Object.keys(payload.stages ?? {})).toEqual(['dev', 'staging']);
      const staging = payload.stages?.staging as DatabricksStageConfigInput;
      expect(staging.workspace_url).toBe(FAKE_WORKSPACE);
    });
  });

  describe('addStage / removeStage', () => {
    const single = (): TenantDraft =>
      draftFromTenant(tenantView({ stages: { dev: tenantStageConfig() } }));

    it('appends a blank stage to BOTH maps (so a platform toggle keeps it)', () => {
      const two = addStage(single(), 'prod');
      expect(draftStageKeys(two)).toEqual(['dev', 'prod']);
      expect(two.stages.prod.account_id).toBe('');
      expect(two.stages.prod.region).toBe('us-east-1');
      expect(two.databricks?.prod.workspace_url).toBe('');
    });

    it('is non-mutating and a no-op for a blank or duplicate key', () => {
      const one = single();
      expect(addStage(one, 'dev')).toBe(one);
      expect(addStage(one, '   ')).toBe(one);
      expect(draftStageKeys(one)).toEqual(['dev']);
    });

    it('removeStage drops a key from every map', () => {
      const back = removeStage(addStage(single(), 'prod'), 'prod');
      expect(draftStageKeys(back)).toEqual(['dev']);
      expect(back.stages.prod).toBeUndefined();
      expect(back.databricks?.prod).toBeUndefined();
    });

    it('removeStage refuses to remove the last remaining stage', () => {
      const one = single();
      expect(removeStage(one, 'dev')).toBe(one);
    });
  });

  describe('nextSuggestedStageKey', () => {
    it('offers prod until it is present, then nothing', () => {
      const one = draftFromTenant(tenantView({ stages: { dev: tenantStageConfig() } }));
      expect(nextSuggestedStageKey(one)).toBe('prod');
      expect(nextSuggestedStageKey(addStage(one, 'prod'))).toBeNull();
    });
  });

  describe('canSubmit — at least one stage, every listed stage complete', () => {
    it('accepts a single complete dev stage — prod is no longer forced', () => {
      const one = draftFromTenant(
        tenantView({ stages: { dev: tenantStageConfig({ account_id: '111122223333' }) }, entra_group_ids: ['g1'] }),
      );
      expect(canSubmit(one)).toBe(true);
    });
    it('still refuses a draft with no stages at all', () => {
      expect(canSubmit({ ...VALID_DRAFT, stageKeys: [], stages: {} })).toBe(false);
    });
    it('refuses when any LISTED stage is incomplete (an added stage must be filled)', () => {
      const one = draftFromTenant(
        tenantView({ stages: { dev: tenantStageConfig({ account_id: '111122223333' }) }, entra_group_ids: ['g1'] }),
      );
      expect(canSubmit(addStage(one, 'prod'))).toBe(false);
    });
    it('databricks: one complete stage is enough', () => {
      const draft: TenantDraft = {
        ...VALID_DB_DRAFT,
        stageKeys: ['dev'],
        databricks: { dev: VALID_DB_STAGE },
      };
      expect(canSubmit(draft)).toBe(true);
    });
  });

  describe('compactBindingModeLabel — the dense-list pill shows the short half; the verbatim label is unchanged', () => {
    it('shortens invoke_unavailable to "Invoke unavailable"', () => {
      expect(compactBindingModeLabel('invoke_unavailable')).toBe('Invoke unavailable');
      expect(bindingModeLabel('invoke_unavailable')).toBe('Invoke unavailable — federation required');
    });
    it('leaves the already-short labels verbatim', () => {
      expect(compactBindingModeLabel('federation')).toBe('Federation');
      expect(compactBindingModeLabel('sp_secret')).toBe('SP secret');
    });
    it('is null for aws, unknown and absent modes (like bindingModeLabel)', () => {
      expect(compactBindingModeLabel('')).toBeNull();
      expect(compactBindingModeLabel(undefined)).toBeNull();
      expect(compactBindingModeLabel('something_new')).toBeNull();
    });
  });

  describe('PRIVILEGE_STATEMENT — the per-platform privilege copy above the credential fields', () => {
    it('is at most three sentences per platform', () => {
      for (const p of ['aws', 'databricks'] as const) {
        const sentences = PRIVILEGE_STATEMENT[p].split('.').filter((s) => s.trim().length > 0);
        expect(sentences.length).toBeGreaterThan(0);
        expect(sentences.length).toBeLessThanOrEqual(3);
      }
    });
    it('databricks reuses the federation-required vocabulary', () => {
      expect(PRIVILEGE_STATEMENT.databricks).toContain('invoke is refused');
      expect(PRIVILEGE_STATEMENT.databricks.toLowerCase()).toContain('user sync');
    });
    it('aws says the push role is trusted to GitHub Actions and never assumed by AGP', () => {
      expect(PRIVILEGE_STATEMENT.aws).toContain('The push role is never assumed by AGP');
      expect(PRIVILEGE_STATEMENT.aws).toContain('GitHub Actions');
    });
    it('aws states what the roles unlock and names the inventory-only fallback', () => {
      expect(PRIVILEGE_STATEMENT.aws.toLowerCase()).toContain('inventory-only');
      expect(PRIVILEGE_STATEMENT.aws).toContain('deploy');
    });
  });

  describe('compareStageKeys / unionStageKeys — stage columns in an order that is a function of the names, not of load order', () => {
    function tenantWithStages(keys: string[]): TenantView {
      const stages: TenantView['stages'] = {};
      for (const k of keys) stages[k] = tenantStageConfig();
      return tenantView({ stages });
    }

    it('sorts dev first and prod last regardless of insertion order', () => {
      expect(['prod', 'dev'].sort(compareStageKeys)).toEqual(['dev', 'prod']);
      expect(['dev', 'prod'].sort(compareStageKeys)).toEqual(['dev', 'prod']);
    });

    it('sorts every other stage name alphabetically between dev and prod', () => {
      expect(['prod', 'staging', 'dev', 'canary'].sort(compareStageKeys)).toEqual([
        'dev',
        'canary',
        'staging',
        'prod',
      ]);
    });

    it('unionStageKeys is independent of which tenant loads first (the reported flap)', () => {
      const a = [tenantWithStages(['prod', 'dev']), tenantWithStages(['dev'])];
      const b = [tenantWithStages(['dev']), tenantWithStages(['prod', 'dev'])];
      expect(unionStageKeys(a)).toEqual(['dev', 'prod']);
      expect(unionStageKeys(b)).toEqual(['dev', 'prod']);
    });

    it('unions stage keys across tenants without duplicates', () => {
      const tenants = [tenantWithStages(['dev']), tenantWithStages(['dev', 'prod', 'canary'])];
      expect(unionStageKeys(tenants)).toEqual(['dev', 'canary', 'prod']);
    });
  });
});
