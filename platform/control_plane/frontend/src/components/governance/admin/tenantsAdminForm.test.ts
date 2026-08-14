import { describe, it, expect } from 'vitest';
import type { DatabricksStageConfig, PrincipalHit } from '../../../api/client';
import {
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
  draftFromTenant,
  draftPlatform,
  hasBeenProbed,
  isAccountAdminCredentialUsable,
  isDatabricksStage,
  isDatabricksStageComplete,
  isValidWorkspaceId,
  isValidWorkspaceUrl,
  platformLabel,
} from './tenantsAdminForm';

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
    it('defaults both stage regions and starts with no groups', () => {
      expect(EMPTY_TENANT_DRAFT.stages.dev.region).toBe('us-east-1');
      expect(EMPTY_TENANT_DRAFT.stages.prod.region).toBe('us-east-1');
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
    const hit: PrincipalHit = { id: 'g2', display_name: 'Claims Leads', type: 'group' };
    it('appends a new group', () => {
      const out = addGroup(VALID_DRAFT.groups, hit);
      expect(out.map((g) => g.id)).toEqual(['g1', 'g2']);
      expect(out[1].display_name).toBe('Claims Leads');
    });
    it('dedupes by id (no double-add)', () => {
      const dup: PrincipalHit = { id: 'g1', display_name: 'Claims Ops', type: 'group' };
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
    it('keeps only groups and drops already-selected ids', () => {
      const hits: PrincipalHit[] = [
        { id: 'u1', display_name: 'Ana', type: 'user' },
        { id: 'g1', display_name: 'Claims Ops', type: 'group' },
        { id: 'g2', display_name: 'Claims Leads', type: 'group' },
        { id: 'a1', display_name: 'Bot', type: 'agent' },
      ];
      const out = filterGroupHits(hits, VALID_DRAFT.groups);
      expect(out.map((h) => h.id)).toEqual(['g2']);
    });
  });

  // -------------------------------------------------------------------------
  // E29 — the Databricks branch. The AWS assertions above are the FENCE: they are
  // unmodified, and everything below must leave them passing.
  // -------------------------------------------------------------------------

  // A fake workspace, matching the plan's designated test fake. Not a real host.
  const FAKE_WORKSPACE = 'https://dbc-test.cloud.databricks.com';

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

  // A stage as it comes back from a read: a stored secret pointer, no plaintext secret.
  const STORED_ARN = 'arn:aws:secretsmanager:::secret:agp/databricks/dev-AbCdEf';
  const EDITED_DB_STAGE: DatabricksStageDraft = {
    ...VALID_DB_STAGE,
    sp_client_secret: '',
    sp_client_secret_arn: STORED_ARN,
    has_sp_secret: true,
  };

  // A COMPLETE Databricks draft — and since T14 that includes the account-admin credential:
  // federation is the model, not a mode, so a draft without one is incomplete by definition
  // (§3B / tenet 4). Tests that need the credential ABSENT spell the blanks out locally, so
  // "still requires name/LoB/group" keeps failing for its own reason rather than for this one.
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

  // The same draft with no account-admin credential typed and none stored — a CREATE that
  // cannot federate.
  const NO_ADMIN_DB_DRAFT: TenantDraft = {
    ...VALID_DB_DRAFT,
    account_admin_client_id: '',
    account_admin_secret: '',
  };

  describe('draftPlatform', () => {
    it('defaults an absent platform to aws (the pre-E29 hydration rule)', () => {
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

  // The security boundary. Every case below is EXECUTED against the real regex — the
  // point is not that the pattern looks right but that these specific inputs are refused.
  describe('isValidWorkspaceUrl', () => {
    it('accepts a plain https workspace origin', () => {
      expect(isValidWorkspaceUrl(FAKE_WORKSPACE)).toBe(true);
      expect(isValidWorkspaceUrl('https://adb-123.4.azuredatabricks.net')).toBe(true);
    });

    it('rejects a javascript: scheme', () => {
      expect(isValidWorkspaceUrl('javascript:alert(1)')).toBe(false);
      // Prefixed so the substring "https://" appears but the value does not START https.
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

    // The one JS `$` genuinely does NOT protect against in every engine/flag combination,
    // and the exact hole the backend's WORKSPACE_URL_RE comment warns about. A smuggled
    // second line must not ride along on a valid first one.
    it('rejects a trailing newline and anything smuggled after it', () => {
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

  describe('isAccountAdminCredentialUsable', () => {
    // Shape-only, deliberately: this predicate answers "is what was typed coherent", not
    // "is a credential present". `canSubmit` is where presence is required (T14).
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

    // T14 / §3B, the inversion: the account-admin credential used to be optional here, on the
    // reasoning that a credential-less tenant was "a perfectly valid sp_secret-mode tenant".
    // sp_secret is no longer an outcome the connect flow can produce, so a Databricks CREATE
    // without an account-admin credential is an incomplete form, not a lesser mode.
    it('REQUIRES an account-admin credential on create (federation is the model, not a mode)', () => {
      expect(canSubmit(NO_ADMIN_DB_DRAFT)).toBe(false);
      // ...and on CREATE only. The gate's honest job is to stop AGP creating an un-federatable
      // tenant silently, not to make an existing one un-administrable (§3B).
      expect(canSubmit(NO_ADMIN_DB_DRAFT, true)).toBe(true);
    });

    // ...but never forces re-entry of a secret that is already stored. The record's
    // `account_admin_secret_arn` is the only "one is stored" signal a client gets (the same
    // pointer-as-boolean idiom as `has_sp_secret`), and an edit that leaves both boxes blank
    // means "keep the stored one".
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

    // The fence for the fence: requiring the credential must not leak onto the AWS branch,
    // which has no account-admin concept at all.
    it('never requires an account-admin credential for an aws draft', () => {
      expect(canSubmit(VALID_DRAFT)).toBe(true);
      expect(canSubmit({ ...VALID_DRAFT, account_admin_client_id: '', account_admin_secret: '' })).toBe(true);
    });
    // The fence, stated as a test: a valid AWS draft does not depend on any E29 field, and
    // 12-digit account ids are NOT applied to the Databricks branch.
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
      const dev = payload.stages.dev as DatabricksStageConfig & { sp_client_secret?: string };
      expect(dev.workspace_url).toBe(FAKE_WORKSPACE);
      expect(dev.sp_client_id).toBe('spid');
      expect(dev.sp_client_secret).toBe('typed-secret');
      // The ARN is the backend's to write, never the client's to assert.
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
      const dev = payload.stages.dev as DatabricksStageConfig & { sp_client_secret?: string };
      expect('sp_client_secret' in dev).toBe(false);
    });

    // FIX ROUND 1 — the regression this pins is data LOSS, not a cosmetic omission. The
    // update path replaces the whole stage object server-side, so emitting an empty ARN
    // destroys the pointer to the stored secret; reproduced against the landed backend,
    // where a no-op edit turned a real ARN into "".
    it('preserves the stored secret ARN verbatim when the secret box is left blank', () => {
      const payload = buildCreatePayload({
        ...VALID_DB_DRAFT,
        databricks: { dev: EDITED_DB_STAGE, prod: EDITED_DB_STAGE },
      });
      const dev = payload.stages.dev as DatabricksStageConfig & { sp_client_secret?: string };
      expect(dev.sp_client_secret_arn).toBe(STORED_ARN);
      // And still no plaintext key — "blank means keep" needs BOTH halves to hold.
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
      const dev = payload.stages.dev as DatabricksStageConfig & { sp_client_secret?: string };
      expect(dev.sp_client_secret_arn).toBe(STORED_ARN);
      expect(dev.sp_client_secret).toBe('rotated-secret');
    });

    it('emits an empty ARN on create, where there is genuinely nothing to preserve', () => {
      const payload = buildCreatePayload(VALID_DB_DRAFT);
      const dev = payload.stages.dev as DatabricksStageConfig & { sp_client_secret?: string };
      expect(dev.sp_client_secret_arn).toBe('');
      expect(dev.sp_client_secret).toBe('typed-secret');
    });

    it('carries the ARN through an update payload too (the path that actually destroyed it)', () => {
      const payload = buildUpdatePayload({
        ...VALID_DB_DRAFT,
        databricks: { dev: EDITED_DB_STAGE, prod: EDITED_DB_STAGE },
      });
      const dev = payload.stages.dev as DatabricksStageConfig & { sp_client_secret?: string };
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
      const dev = payload.stages.dev as DatabricksStageConfig & { sp_client_secret?: string };
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

    // §3B, stated as a test: the form cannot produce an sp_secret outcome. It never asserts a
    // binding mode (the probe is the only writer), and the ONE input that decides the tenant's
    // mode — the account-admin credential — is now mandatory, so no submittable Databricks
    // draft exists whose payload lacks it.
    it('cannot produce an sp_secret outcome: no mode is asserted, and admin creds always travel', () => {
      const payload = buildCreatePayload(VALID_DB_DRAFT) as unknown as Record<string, unknown>;
      expect('binding_mode' in payload).toBe(false);
      expect(JSON.stringify(payload)).not.toContain('sp_secret');
      expect(payload.account_admin_client_id).toBe('admin-client-id');
      // And the only draft whose payload would omit them is one `canSubmit` refuses.
      expect(canSubmit(NO_ADMIN_DB_DRAFT)).toBe(false);
    });

    it('never sends client-asserted capabilities or a binding mode', () => {
      const payload = buildCreatePayload(VALID_DB_DRAFT) as unknown as Record<string, unknown>;
      expect('capabilities' in payload).toBe(false);
      expect('binding_mode' in payload).toBe(false);
      expect('federation_audience' in payload).toBe(false);
    });

    // The fence: an AWS draft's payload carries NO platform key, so the wire shape is
    // byte-identical to pre-E29 (the backend defaults an absent platform to aws).
    it('omits platform entirely for an aws draft', () => {
      const payload = buildCreatePayload(VALID_DRAFT) as unknown as Record<string, unknown>;
      expect('platform' in payload).toBe(false);
    });
  });

  describe('buildUpdatePayload (platform immutability)', () => {
    it('strips platform — a tenant can never be re-typed', () => {
      const payload = buildUpdatePayload(VALID_DB_DRAFT) as unknown as Record<string, unknown>;
      expect('platform' in payload).toBe(false);
      // Everything else still travels.
      expect(payload.name).toBe('Retail Claims');
      expect(payload.stages).toBeDefined();
    });
  });

  describe('bindingModeLabel (C-6 copy, verbatim)', () => {
    it('reads exactly "Federation" and "SP secret"', () => {
      expect(bindingModeLabel('federation')).toBe('Federation');
      expect(bindingModeLabel('sp_secret')).toBe('SP secret');
    });
    // T14: the third mode a tenant can now be in. The label says what is REFUSED and what
    // fixes it, in the badge itself — the operator should not have to hover to learn that
    // invoke does not work.
    it('reads exactly "Invoke unavailable — federation required" for invoke_unavailable', () => {
      expect(bindingModeLabel('invoke_unavailable')).toBe('Invoke unavailable — federation required');
    });
    it('is null for an aws tenant, an unknown value and an absent one', () => {
      expect(bindingModeLabel('')).toBeNull();
      expect(bindingModeLabel(undefined)).toBeNull();
      expect(bindingModeLabel('something_new')).toBeNull();
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
      expect(
        isDatabricksStage({
          workspace_url: FAKE_WORKSPACE,
          workspace_id: '0',
          cloud: 'aws',
          region: '',
          account_id: '',
          sp_client_id: '',
          sp_client_secret_arn: '',
        }),
      ).toBe(true);
    });

    it('answers false for null WITHOUT throwing (E29/T11 fix round 1)', () => {
      // The signature said `… | undefined` and the body tested `!== undefined`, so a `null` stage
      // config reached the property read and threw `TypeError: Cannot read properties of null`.
      // That is not a hypothetical shape: `stages` arrives as JSON off `/users/me`, where a key
      // with a null value is ordinary — and the crash blanked an entire page for every platform.
      // The parameter is now `unknown` and the guard is truthiness.
      expect(() => isDatabricksStage(null)).not.toThrow();
      expect(isDatabricksStage(null)).toBe(false);
    });

    it('answers false for primitives rather than throwing', () => {
      // `unknown` means callers may hand this anything. A property read is safe on every
      // non-nullish value, so these fall through the `typeof` to false.
      for (const v of ['', 'x', 0, 7, true, false, NaN]) {
        expect(isDatabricksStage(v), String(v)).toBe(false);
      }
    });
  });

  describe('databricksStageDraftFromConfig', () => {
    it('derives has_sp_secret from the stored ARN and NEVER seeds a secret', () => {
      const draft = databricksStageDraftFromConfig({
        workspace_url: FAKE_WORKSPACE,
        workspace_id: '99',
        cloud: 'azure',
        region: 'westeurope',
        account_id: 'acct-uuid',
        sp_client_id: 'spid',
        sp_client_secret_arn: 'arn:aws:secretsmanager:::secret:x',
      });
      expect(draft.has_sp_secret).toBe(true);
      expect(draft.sp_client_secret).toBe('');
      // The pointer is carried, not just the derived boolean — that omission is what let an
      // edit wipe the stored secret (fix round 1).
      expect(draft.sp_client_secret_arn).toBe('arn:aws:secretsmanager:::secret:x');
      expect(draft.workspace_id).toBe('99');
      expect(draft.cloud).toBe('azure');
    });
    it('has_sp_secret is false when no ARN is stored', () => {
      const draft = databricksStageDraftFromConfig({
        workspace_url: FAKE_WORKSPACE,
        workspace_id: '0',
        cloud: 'aws',
        region: '',
        account_id: '',
        sp_client_id: '',
        sp_client_secret_arn: '',
      });
      expect(draft.has_sp_secret).toBe(false);
    });
    it('yields a blank draft for an absent config', () => {
      const draft = databricksStageDraftFromConfig(undefined);
      expect(draft.workspace_url).toBe('');
      expect(draft.workspace_id).toBe('0');
      expect(draft.has_sp_secret).toBe(false);
    });
  });

  describe('draftFromTenant', () => {
    const DB_STAGE: DatabricksStageConfig = {
      workspace_url: FAKE_WORKSPACE,
      workspace_id: '77',
      cloud: 'aws',
      region: 'us-east-1',
      account_id: 'acct-uuid',
      sp_client_id: 'spid',
      sp_client_secret_arn: 'arn:aws:secretsmanager:::secret:x',
    };

    it('seeds a databricks tenant into the databricks half, with has-secret state', () => {
      const draft = draftFromTenant({
        name: 'DB Tenant',
        line_of_business: 'Claims',
        description: 'd',
        platform: 'databricks',
        stages: { dev: DB_STAGE, prod: DB_STAGE },
        entra_group_ids: ['g1'],
      });
      expect(draft.platform).toBe('databricks');
      expect(draft.databricks?.dev.workspace_url).toBe(FAKE_WORKSPACE);
      expect(draft.databricks?.dev.has_sp_secret).toBe(true);
      expect(draft.databricks?.dev.sp_client_secret).toBe('');
      expect(draft.groups).toEqual([{ id: 'g1', display_name: 'g1' }]);
    });

    it('never seeds the write-only account-admin credential', () => {
      const draft = draftFromTenant({
        name: 'DB Tenant',
        line_of_business: 'Claims',
        description: 'd',
        platform: 'databricks',
        stages: { dev: DB_STAGE, prod: DB_STAGE },
        entra_group_ids: [],
      });
      expect(draft.account_admin_client_id).toBe('');
      expect(draft.account_admin_secret).toBe('');
    });

    // T14: blank boxes plus "one is stored" is what lets an edit submit without re-typing a
    // secret. The stored-ness comes from the record's Secrets Manager POINTER — the same
    // pointer-as-boolean signal `has_sp_secret` is derived from — never from the secret itself.
    it('records that an account-admin credential is STORED, from the ARN pointer alone', () => {
      const base = {
        name: 'DB Tenant',
        line_of_business: 'Claims',
        description: 'd',
        platform: 'databricks' as const,
        stages: { dev: DB_STAGE, prod: DB_STAGE },
        entra_group_ids: [],
      };
      expect(
        draftFromTenant({ ...base, account_admin_secret_arn: 'arn:aws:secretsmanager:::secret:admin' })
          .has_account_admin_credential,
      ).toBe(true);
      expect(draftFromTenant({ ...base, account_admin_secret_arn: '' }).has_account_admin_credential).toBe(false);
      // Absent on the wire (a pre-E29 record, or a build before the field was projected) is
      // "none stored" — never an assumed one.
      expect(draftFromTenant(base).has_account_admin_credential).toBe(false);
    });

    it('hydrates a record with no platform as aws (zero migration)', () => {
      const draft = draftFromTenant({
        name: 'Legacy',
        line_of_business: 'Claims',
        description: '',
        stages: { dev: { account_id: '111122223333', region: 'us-east-1' } },
        entra_group_ids: [],
      });
      expect(draft.platform).toBe('aws');
      expect(draft.stages.dev.account_id).toBe('111122223333');
    });

    // The crash the client.ts OPEN SEAM note describes: a single-stage tenant used to throw
    // here on `undefined.account_id`. A blank stage is the honest reading, never a guess.
    it('yields a blank stage rather than throwing on a single-stage tenant', () => {
      const draft = draftFromTenant({
        name: 'One stage',
        line_of_business: 'Claims',
        description: '',
        platform: 'aws',
        stages: { dev: { account_id: '111122223333', region: 'us-east-1' } },
        entra_group_ids: [],
      });
      expect(draft.stages.prod.account_id).toBe('');
      expect(draft.stages.prod.region).toBe('us-east-1');
    });

    it('does not put databricks stages into the aws half, or vice versa', () => {
      const draft = draftFromTenant({
        name: 'DB Tenant',
        line_of_business: 'Claims',
        description: '',
        platform: 'databricks',
        stages: { dev: DB_STAGE, prod: DB_STAGE },
        entra_group_ids: [],
      });
      expect(draft.stages.dev.account_id).toBe('');
      expect(draft.databricks?.dev.account_id).toBe('acct-uuid');
    });

    // The end-to-end shape of the bug, exercised the way the modal actually does it:
    // record → draftFromTenant → (admin changes nothing) → buildUpdatePayload. This is the
    // assertion that would have caught the ARN wipe.
    it('a no-op edit round-trip preserves the stored secret ARN', () => {
      const draft = draftFromTenant({
        name: 'DB Tenant',
        line_of_business: 'Claims',
        description: '',
        platform: 'databricks',
        stages: { dev: DB_STAGE, prod: DB_STAGE },
        entra_group_ids: ['g1'],
      });
      const payload = buildUpdatePayload(draft);
      for (const stage of ['dev', 'prod']) {
        const s = payload.stages[stage] as DatabricksStageConfig & { sp_client_secret?: string };
        expect(s.sp_client_secret_arn).toBe(DB_STAGE.sp_client_secret_arn);
        expect('sp_client_secret' in s).toBe(false);
      }
    });

    it('round-trips a databricks tenant back to a submittable draft', () => {
      const draft = draftFromTenant({
        name: 'DB Tenant',
        line_of_business: 'Claims',
        description: '',
        platform: 'databricks',
        stages: { dev: DB_STAGE, prod: DB_STAGE },
        entra_group_ids: ['g1'],
        account_admin_secret_arn: 'arn:aws:secretsmanager:::secret:admin',
      });
      // Submittable with NO secret typed — neither the stage secrets nor the account-admin
      // credential — because both are already stored. This is the shape of a real no-op edit,
      // and T14's new requirement must not break it.
      expect(canSubmit(draft)).toBe(true);
    });

    // The other half of that, and the CREATE-only line: a tenant that has NO stored account-admin
    // credential (every Databricks tenant created before T14) stays EDITABLE — §3B makes
    // `invoke_unavailable` a supported, operable state, and "unlock federation later" is the
    // documented sequence, so a rename or an Entra-group add must not require first obtaining a
    // Tier-3 credential. The same draft as a CREATE is still refused.
    it('keeps a tenant with no stored account-admin credential editable, but refuses it on create', () => {
      const draft = draftFromTenant({
        name: 'DB Tenant',
        line_of_business: 'Claims',
        description: '',
        platform: 'databricks',
        stages: { dev: DB_STAGE, prod: DB_STAGE },
        entra_group_ids: ['g1'],
      });
      expect(canSubmit(draft, true)).toBe(true);
      expect(canSubmit(draft)).toBe(false);
      // Supplying the pair on an edit stays possible (and still has to be coherent).
      expect(
        canSubmit({ ...draft, account_admin_client_id: 'admin-id', account_admin_secret: 's' }, true),
      ).toBe(true);
      expect(canSubmit({ ...draft, account_admin_client_id: 'admin-id' }, true)).toBe(false);
    });
  });
});
