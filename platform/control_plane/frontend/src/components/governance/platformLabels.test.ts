import { afterEach, describe, it, expect, vi } from 'vitest';
import type { Platform, Verb, VerbReadiness } from '../../api/client';
import {
  PLATFORM_LABEL,
  PLATFORM_OPTIONS,
  platformLabel,
  platformLabelOr,
  platformHostLabel,
  mcpHostLabel,
  runtimeHandleField,
  isTabVisibleForPlatform,
  HIDDEN_TAB_IDS_BY_PLATFORM,
  bindingModeBadge,
  bindingModeLabel,
  BINDING_MODE_HINT,
  accessSurface,
  GRANTS_PENDING_NOTE,
  mcpDeliveryNote,
  MCP_NOT_DELIVERED_NOTE,
  agentObservability,
  workspaceObservability,
  tenantWorkspaceUrl,
  LANGFUSE_PROJECT_PRESENT_NOTE,
  LANGFUSE_PROJECT_ABSENT_NOTE,
  DATABRICKS_INFERENCE_TABLES_NOTE,
  DATABRICKS_AUDIT_LINK_LABEL,
  DRIFT_PANEL_TITLE,
  DRIFT_PANEL_NOTE,
  DRIFT_REASSERT_LABEL,
  DRIFT_REASSERT_CONFIRM,
  DRIFT_CLEAN_NOTE,
  DRIFT_UNAVAILABLE_NOTE,
  DRIFT_DIRECTION_NOTE,
  DRIFT_KIND_LABEL,
  GOVERNABLE_PLATFORM,
  INVENTORY_ONLY_NOTE,
  MARKETPLACE_PUBLISH,
  PUBLISH_REFUSAL_NOTE,
  READINESS_REASON_NOTE,
  READINESS_STATE_TINT,
  VERB_LABEL,
  inventoryOnlyBadge,
  invokeBadge,
  invokeReadiness,
  isInventoryOnly,
  publishRefusal,
  readinessBadge,
  identityStatusNote,
} from './platformLabels';
import type { AccessSurfaceSource, RuntimeHandleSource } from './platformLabels';

afterEach(() => vi.restoreAllMocks());

const ALL_PLATFORMS: Platform[] = [
  'aws_bedrock',
  'azure',
  'salesforce',
  'sap',
  'databricks',
  'google',
  'on_prem',
  'other',
];

describe('PLATFORM_LABEL — the one map', () => {
  it('covers every platform with both vocabularies non-empty', () => {
    for (const p of ALL_PLATFORMS) {
      expect(PLATFORM_LABEL[p], p).toBeDefined();
      expect(PLATFORM_LABEL[p].full.length, `${p}.full`).toBeGreaterThan(0);
      expect(PLATFORM_LABEL[p].host.length, `${p}.host`).toBeGreaterThan(0);
    }
  });

  it('carries no key that is not a platform (the map is exactly the enum)', () => {
    expect(Object.keys(PLATFORM_LABEL).sort()).toEqual([...ALL_PLATFORMS].sort());
  });

  it('preserves the labels the five deleted copies used — this is the consolidation fence', () => {
    expect(PLATFORM_LABEL.aws_bedrock.full).toBe('Amazon Bedrock AgentCore');
    expect(PLATFORM_LABEL.databricks.full).toBe('Databricks');
    expect(PLATFORM_LABEL.on_prem.full).toBe('On-prem');
    expect(PLATFORM_LABEL.other.full).toBe('Other');
    expect(PLATFORM_LABEL.aws_bedrock.host).toBe('AWS');
    expect(PLATFORM_LABEL.databricks.host).toBe('Databricks');
  });

  it('keeps the two vocabularies genuinely different where the graph needed them to be', () => {
    expect(PLATFORM_LABEL.aws_bedrock.host).not.toBe(PLATFORM_LABEL.aws_bedrock.full);
  });
});

describe('platformLabel / platformLabelOr', () => {
  it('returns the full product label', () => {
    expect(platformLabel('aws_bedrock')).toBe('Amazon Bedrock AgentCore');
    expect(platformLabel('databricks')).toBe('Databricks');
  });

  it('renders an em-dash for null/undefined — never the word "Other"', () => {
    expect(platformLabel(null)).toBe('—');
    expect(platformLabel(undefined)).toBe('—');
    expect(platformLabel(null)).not.toBe(PLATFORM_LABEL.other.full);
  });

  it('renders the raw word for a platform outside the union, without throwing', () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => undefined);
    expect(platformLabel('kubernetes' as never)).toBe('kubernetes');
    platformLabel('kubernetes' as never);
    expect(warn).toHaveBeenCalledTimes(1);
  });

  it('lets each surface word the absence itself, and words an unrecognised or prototype-named label the same way instead of throwing', () => {
    expect(platformLabelOr(null, 'Platform not set')).toBe('Platform not set');
    expect(platformLabelOr(undefined, 'Not set')).toBe('Not set');
    expect(platformLabelOr('databricks', 'Not set')).toBe('Databricks');
    expect(platformLabelOr('quantum_cloud' as Platform, 'Not set')).toBe('Not set');
    expect(platformLabelOr('constructor' as Platform, 'Not set')).toBe('Not set');
  });
});

describe('platformHostLabel — the graph node tag', () => {
  it('gives every known platform its short host', () => {
    for (const p of ALL_PLATFORMS) {
      expect(platformHostLabel(p), p).toBe(PLATFORM_LABEL[p].host);
    }
  });

  it('never captions a databricks node as AWS: the bug the one map was built to end', () => {
    expect(platformHostLabel('databricks')).toBe('Databricks');
    expect(platformHostLabel('databricks')).not.toBe('AWS');
  });

  it('returns empty (render no tag) for absent/unknown platforms rather than defaulting to AWS', () => {
    for (const bogus of ['', 'AWS', 'aws', 'Databricks', 'kubernetes', 'aws_bedrock ', 'OTHER']) {
      expect(platformHostLabel(bogus), bogus).toBe('');
    }
  });

  it('does not inherit Object.prototype keys as platforms', () => {
    for (const inherited of ['constructor', 'toString', 'hasOwnProperty', '__proto__']) {
      expect(platformHostLabel(inherited), inherited).toBe('');
    }
  });
});

describe('mcpHostLabel', () => {
  it('claims AWS only for the two AgentCore-native kinds', () => {
    expect(mcpHostLabel('gateway')).toBe('AWS');
    expect(mcpHostLabel('runtime')).toBe('AWS');
  });

  it('calls a standard MCP server External — the only thing that is known about it', () => {
    expect(mcpHostLabel('standard')).toBe('External');
  });

  it('returns empty for an unknown kind rather than guessing a host', () => {
    for (const bogus of ['', 'Gateway', 'databricks', 'lambda']) {
      expect(mcpHostLabel(bogus), bogus).toBe('');
    }
  });
});

describe('PLATFORM_OPTIONS — the filter menu', () => {
  it('lists every platform once, in the map order, with the full label', () => {
    expect(PLATFORM_OPTIONS.map((o) => o.value)).toEqual(ALL_PLATFORMS);
    for (const o of PLATFORM_OPTIONS) {
      expect(o.label, o.value).toBe(PLATFORM_LABEL[o.value].full);
    }
  });

  it('is order-stable so the filter menu does not reshuffle between builds', () => {
    expect(PLATFORM_OPTIONS[0].value).toBe('aws_bedrock');
    expect(PLATFORM_OPTIONS[PLATFORM_OPTIONS.length - 1].value).toBe('other');
  });
});

const src = (over: Partial<RuntimeHandleSource> = {}): RuntimeHandleSource => ({
  platform: null,
  agent_arn: null,
  runtime_handle: null,
  ...over,
});

const ARN = 'arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/abc';
const APP_URL = 'https://claims-agent-1234.aws.databricksapps.com';

describe('runtimeHandleField', () => {
  it('labels a Databricks handle App URL, never Agent ARN', () => {
    const f = runtimeHandleField(src({ platform: 'databricks', runtime_handle: APP_URL }));
    expect(f).toEqual({ label: 'App URL', value: APP_URL, copyLabel: 'app URL' });
  });

  it('still shows the App URL row (Not set) for a handle-less Databricks record: the field applies and is empty, and hiding it would hide the one value that makes the agent governed', () => {
    const f = runtimeHandleField(src({ platform: 'databricks' }));
    expect(f).not.toBeNull();
    expect(f!.label).toBe('App URL');
    expect(f!.value).toBeNull();
  });

  it('labels an AgentCore ARN Agent ARN', () => {
    const f = runtimeHandleField(src({ platform: 'aws_bedrock', agent_arn: ARN }));
    expect(f).toEqual({ label: 'Agent ARN', value: ARN, copyLabel: 'agent ARN' });
  });

  it('renders no row for an AgentCore agent that has no ARN yet: AgentDetail used to render an unconditional "Agent ARN — Not set" row', () => {
    expect(runtimeHandleField(src({ platform: 'aws_bedrock' }))).toBeNull();
  });

  it('renders NO row for a metadata-only agent on any other platform', () => {
    for (const p of ['azure', 'sap', 'salesforce', 'google', 'on_prem', 'other'] as Platform[]) {
      expect(runtimeHandleField(src({ platform: p })), p).toBeNull();
    }
    expect(runtimeHandleField(src())).toBeNull();
  });

  it('never captions a non-Databricks runtime handle as Agent ARN: agent_arn is parsed as a Bedrock ARN by the delete cascade and the status probe', () => {
    const f = runtimeHandleField(src({ platform: 'azure', runtime_handle: 'https://x.example' }));
    expect(f!.label).toBe('Runtime handle');
    expect(f!.label).not.toBe('Agent ARN');
    expect(f!.value).toBe('https://x.example');
  });

  it('prefers the ARN when a non-Databricks record somehow carries both', () => {
    const f = runtimeHandleField(src({ platform: 'aws_bedrock', agent_arn: ARN, runtime_handle: APP_URL }));
    expect(f!.label).toBe('Agent ARN');
    expect(f!.value).toBe(ARN);
  });

  it('never leaks an ARN onto a Databricks row even if the record carries one: platform wins', () => {
    const f = runtimeHandleField(src({ platform: 'databricks', runtime_handle: APP_URL, agent_arn: ARN }));
    expect(f!.label).toBe('App URL');
    expect(f!.value).toBe(APP_URL);
    expect(f!.value).not.toBe(ARN);
  });

  it('treats an empty-string handle as absent, not as a value', () => {
    expect(runtimeHandleField(src({ platform: 'databricks', runtime_handle: '' }))!.value).toBeNull();
    expect(runtimeHandleField(src({ platform: 'aws_bedrock', agent_arn: '' }))).toBeNull();
  });

  it('invariant: a returned row always has a non-empty label and copyLabel', () => {
    const cases: RuntimeHandleSource[] = [
      src({ platform: 'databricks' }),
      src({ platform: 'databricks', runtime_handle: APP_URL }),
      src({ platform: 'aws_bedrock', agent_arn: ARN }),
      src({ platform: 'azure', runtime_handle: 'https://x.example' }),
    ];
    for (const c of cases) {
      const f = runtimeHandleField(c)!;
      expect(f.label.length).toBeGreaterThan(0);
      expect(f.copyLabel.length).toBeGreaterThan(0);
    }
  });
});

describe('isTabVisibleForPlatform', () => {
  it('hides guardrails for a Databricks agent', () => {
    expect(isTabVisibleForPlatform('guardrails', 'databricks')).toBe(false);
  });

  it('shows guardrails for AgentCore and for every other platform', () => {
    for (const p of ALL_PLATFORMS.filter((p) => p !== 'databricks')) {
      expect(isTabVisibleForPlatform('guardrails', p), p).toBe(true);
    }
  });

  it('hides nothing when the platform is unset — a missing field is not evidence', () => {
    expect(isTabVisibleForPlatform('guardrails', null)).toBe(true);
    expect(isTabVisibleForPlatform('guardrails', undefined)).toBe(true);
  });

  it('leaves every tab that actually exists today visible on Databricks (overview, access, mcp-servers, deployment, traces, cost)', () => {
    for (const id of ['overview', 'access', 'mcp-servers', 'deployment', 'traces', 'cost']) {
      expect(isTabVisibleForPlatform(id, 'databricks'), id).toBe(true);
    }
  });

  it('shows the Containment tab only on AgentCore agents (unset platform hides nothing)', () => {
    for (const p of ALL_PLATFORMS) expect(isTabVisibleForPlatform('containment', p), p).toBe(p === 'aws_bedrock');
    expect(isTabVisibleForPlatform('containment', null)).toBe(true);
  });

  it('gates exactly one id, on exactly one platform (no accidental widening)', () => {
    expect(Object.keys(HIDDEN_TAB_IDS_BY_PLATFORM)).toEqual(['databricks']);
    expect(HIDDEN_TAB_IDS_BY_PLATFORM.databricks).toEqual(['guardrails']);
  });
});

describe('bindingModeBadge — the agent-detail badge, one string with the tenant surface', () => {
  it('uses the contract labels verbatim', () => {
    expect(bindingModeBadge('federation')!.label).toBe('Federation');
    expect(bindingModeBadge('sp_secret')!.label).toBe('SP secret');
    expect(bindingModeBadge('invoke_unavailable')!.label).toBe('Invoke unavailable — federation required');
  });

  it('is the SAME copy the tenant surface badges — one contract, one string', () => {
    expect(bindingModeBadge('federation')!.label).toBe(bindingModeLabel('federation'));
    expect(bindingModeBadge('sp_secret')!.label).toBe(bindingModeLabel('sp_secret'));
    expect(bindingModeBadge('invoke_unavailable')!.label).toBe(bindingModeLabel('invoke_unavailable'));
  });

  it('carries an actionable invoke_unavailable hint: the state (invoke refused, inventory works) and what federation needs', () => {
    const hint = bindingModeBadge('invoke_unavailable')!.hint;
    expect(hint).toBe(BINDING_MODE_HINT.invoke_unavailable);
    expect(hint).toContain('account-admin');
    expect(hint).toContain('user sync');
    expect(hint).toContain('Invoke is refused');
  });

  it('carries the sp_secret consequence sentence — the audit-attribution cost, stated', () => {
    const hint = bindingModeBadge('sp_secret')!.hint;
    expect(hint).toBe(BINDING_MODE_HINT.sp_secret);
    expect(hint).toContain('service principal in Databricks audit logs');
    expect(hint).toContain('not the individual caller');
  });

  it('carries the federation consequence sentence too — every mode explains itself', () => {
    expect(bindingModeBadge('federation')!.hint).toBe(BINDING_MODE_HINT.federation);
  });

  it('always supplies a hint, so the badge never relies on colour alone: a third mode once indexed the hint map through a two-value cast and got undefined', () => {
    for (const m of ['federation', 'sp_secret', 'invoke_unavailable']) {
      expect(bindingModeBadge(m)!.hint.length, m).toBeGreaterThan(0);
    }
  });

  it('tints all three modes differently — supported, degraded and refused are not peers', () => {
    const tints = ['federation', 'sp_secret', 'invoke_unavailable'].map((m) => bindingModeBadge(m)!.tint);
    expect(new Set(tints).size).toBe(3);
    expect(bindingModeBadge('federation')!.tint).toContain('emerald');
    expect(bindingModeBadge('invoke_unavailable')!.tint).not.toContain('emerald');
  });

  it('badges nothing for an AWS tenant, an unprobed record, or an unknown mode', () => {
    for (const m of ['', null, undefined, 'Federation', 'sp-secret', 'something_new']) {
      expect(bindingModeBadge(m as string | null | undefined), String(m)).toBeNull();
    }
  });
});

const acc = (over: Partial<AccessSurfaceSource> = {}): AccessSurfaceSource => ({
  platform: null,
  identity_status: 'pending',
  entra_sp_id: null,
  agent_arn: null,
  runtime_handle: null,
  ...over,
});

describe('accessSurface — an ARN-less Databricks agent must show its grants', () => {
  it('shows the grants table for a pending Databricks agent that has an SP: entra_sp_id && !agent_arn used to show the awaiting-runtime banner forever', () => {
    const s = accessSurface(
      acc({ platform: 'databricks', identity_status: 'pending', entra_sp_id: 'sp-1', runtime_handle: APP_URL }),
    );
    expect(s.kind).toBe('grants');
    expect(s.kind === 'grants' && s.notReadyNote).toBe(GRANTS_PENDING_NOTE);
  });

  it('shows the grants table for a provisioned Databricks agent, with no caveat', () => {
    const s = accessSurface(
      acc({ platform: 'databricks', identity_status: 'provisioned', entra_sp_id: 'sp-1', runtime_handle: APP_URL }),
    );
    expect(s).toEqual({ kind: 'grants', notReadyNote: null });
  });

  it('never shows awaiting-runtime for a Databricks agent, in any state', () => {
    for (const status of ['none', 'pending', 'provisioned', 'failed'] as const) {
      for (const sp of [null, 'sp-1']) {
        const s = accessSurface(acc({ platform: 'databricks', identity_status: status, entra_sp_id: sp }));
        expect(s.kind, `${status}/${sp}`).not.toBe('awaiting-runtime');
      }
    }
  });
});

describe('accessSurface — the AgentCore branch is a FENCE (behaviour unchanged)', () => {
  it('keeps awaiting-runtime for a pre-registered AgentCore agent (SP, no ARN)', () => {
    expect(
      accessSurface(acc({ platform: 'aws_bedrock', identity_status: 'pending', entra_sp_id: 'sp-1' })),
    ).toEqual({ kind: 'awaiting-runtime' });
  });

  it('shows grants for a provisioned AgentCore agent', () => {
    expect(
      accessSurface(acc({ platform: 'aws_bedrock', identity_status: 'provisioned', entra_sp_id: 'sp-1', agent_arn: ARN })),
    ).toEqual({ kind: 'grants', notReadyNote: null });
  });

  it('shows the provisioning spinner mid-provision, before the SP exists', () => {
    expect(accessSurface(acc({ platform: 'aws_bedrock', identity_status: 'pending' }))).toEqual({
      kind: 'provisioning',
    });
  });

  it('the stranded shape, pending + SP + ARN, is provisioning so Re-provision stays reachable: the SP is persisted before the steps that can fail, and a read-only grants table would offer no way out', () => {
    expect(
      accessSurface(
        acc({ platform: 'aws_bedrock', identity_status: 'pending', entra_sp_id: 'sp-1', agent_arn: ARN }),
      ),
    ).toEqual({ kind: 'provisioning' });
  });

  it('never leaves a pending SP-bearing agent on a recovery-less surface, on ANY platform', () => {
    for (const p of ALL_PLATFORMS) {
      const s = accessSurface(acc({ platform: p, identity_status: 'pending', entra_sp_id: 'sp-1', agent_arn: ARN }));
      expect(s.kind, p).toBe('provisioning');
    }
    expect(
      accessSurface(acc({ identity_status: 'pending', entra_sp_id: 'sp-1', agent_arn: ARN })).kind,
    ).toBe('provisioning');
  });

  it('an empty-string ARN is absent, so it does not divert a Databricks agent to the spinner', () => {
    const s = accessSurface(
      acc({ platform: 'databricks', identity_status: 'pending', entra_sp_id: 'sp-1', agent_arn: '' }),
    );
    expect(s.kind).toBe('grants');
  });

  it('shows no-identity for a metadata-only agent', () => {
    expect(accessSurface(acc({ identity_status: 'none' }))).toEqual({ kind: 'no-identity' });
  });

  it('shows failed for a failed provision', () => {
    expect(accessSurface(acc({ identity_status: 'failed' }))).toEqual({ kind: 'failed' });
  });
});

describe('accessSurface — precedence and edges', () => {
  it('none and failed outrank a present SP: provisioning writes the SP before the steps that can fail, and a failed tab must offer recovery', () => {
    expect(accessSurface(acc({ identity_status: 'failed', entra_sp_id: 'sp-1', agent_arn: ARN })).kind).toBe('failed');
    expect(accessSurface(acc({ identity_status: 'none', entra_sp_id: 'sp-1' })).kind).toBe('no-identity');
  });

  it('provisioned outranks the awaiting-runtime shape: an agent whose runtimes live only in the agent_arns map is not undeployed', () => {
    expect(
      accessSurface(acc({ platform: 'aws_bedrock', identity_status: 'provisioned', entra_sp_id: 'sp-1' })).kind,
    ).toBe('grants');
  });

  it('keys awaiting-runtime on the POSITIVE platform test, not on a missing ARN', () => {
    for (const p of ALL_PLATFORMS.filter((p) => p !== 'aws_bedrock')) {
      const s = accessSurface(acc({ platform: p, identity_status: 'pending', entra_sp_id: 'sp-1' }));
      expect(s.kind, p).toBe('grants');
    }
    expect(accessSurface(acc({ identity_status: 'pending', entra_sp_id: 'sp-1' })).kind).toBe('grants');
  });

  it('treats an empty-string entra_sp_id as no SP', () => {
    expect(accessSurface(acc({ identity_status: 'pending', entra_sp_id: '' })).kind).toBe('provisioning');
  });

  it('invariant: notReadyNote is set iff the surface is grants on a not-yet-provisioned identity', () => {
    for (const p of ALL_PLATFORMS) {
      for (const status of ['none', 'pending', 'provisioned', 'failed'] as const) {
        for (const sp of [null, 'sp-1']) {
          for (const arn of [null, ARN]) {
            const s = accessSurface(acc({ platform: p, identity_status: status, entra_sp_id: sp, agent_arn: arn }));
            if (s.kind === 'grants') {
              expect(s.notReadyNote === null, `${p}/${status}/${sp}/${arn}`).toBe(status === 'provisioned');
            }
          }
        }
      }
    }
  });

  it('the pending note matches what the backend does (GET answers [] and POST 409 until both legs of _is_provisioned hold), with no false encouragement', () => {
    expect(GRANTS_PENDING_NOTE).toContain('cannot be read or changed yet');
    expect(GRANTS_PENDING_NOTE).toContain('once provisioning completes');
    expect(GRANTS_PENDING_NOTE).not.toMatch(/assignments are live/);
  });
});

describe('mcpDeliveryNote', () => {
  it('is the contract copy, verbatim', () => {
    expect(MCP_NOT_DELIVERED_NOTE).toBe(
      'Recorded in the registry — not delivered to the runtime on this platform yet.',
    );
    expect(MCP_NOT_DELIVERED_NOTE).toContain('—');
  });

  it('applies to databricks and to NOTHING else', () => {
    expect(mcpDeliveryNote('databricks')).toBe(MCP_NOT_DELIVERED_NOTE);
    for (const p of ALL_PLATFORMS.filter((p) => p !== 'databricks')) {
      expect(mcpDeliveryNote(p), p).toBeNull();
    }
  });

  it('says nothing for an absent platform', () => {
    expect(mcpDeliveryNote(null)).toBeNull();
    expect(mcpDeliveryNote(undefined)).toBeNull();
  });

  it('states BOTH halves — recorded here, not delivered there', () => {
    expect(MCP_NOT_DELIVERED_NOTE).toMatch(/recorded/i);
    expect(MCP_NOT_DELIVERED_NOTE).toMatch(/not delivered/i);
    expect(MCP_NOT_DELIVERED_NOTE).not.toMatch(/coming soon|will be supported|roadmap/i);
  });
});

describe('workspaceObservability', () => {
  const WS = 'https://dbc-test.cloud.databricks.com';

  it('builds the audit link from the workspace origin', () => {
    const w = workspaceObservability(WS);
    expect(w).not.toBeNull();
    expect(w!.workspaceUrl).toBe(WS);
    expect(w!.auditUrl.startsWith(`${WS}/`)).toBe(true);
    expect(w!.auditLabel).toBe(DATABRICKS_AUDIT_LINK_LABEL);
    expect(w!.inferenceNote).toBe(DATABRICKS_INFERENCE_TABLES_NOTE);
  });

  it('points at the system catalog, where the audit tables live', () => {
    expect(workspaceObservability(WS)!.auditUrl).toBe(`${WS}/explore/data/system`);
  });

  it('applies the write-side rule with no normalisation: trimming once made a value ending in a newline validate, which the write side refuses', () => {
    expect(workspaceObservability(`${WS}/`)).toBeNull();
    expect(workspaceObservability(`${WS} `)).toBeNull();
    expect(workspaceObservability(`${WS}\n`)).toBeNull();
  });

  it('EXECUTES the security boundary over hostile values — no href is ever built', () => {
    for (const hostile of [
      'javascript:alert(1)',
      'javascript:https://dbc-test.cloud.databricks.com',
      'data:text/html,<script>alert(1)</script>',
      'http://dbc-test.cloud.databricks.com',
      `${WS}\njavascript:alert(1)`,
      `${WS}\n`,
      `${WS} `,
      ` ${WS}`,
      `${WS}/explore/data/system`,
      `${WS}?o=1`,
      `${WS}#f`,
      `${WS}:443`,
      'https://user:pw@dbc-test.cloud.databricks.com',
      '//dbc-test.cloud.databricks.com',
      'dbc-test.cloud.databricks.com',
    ]) {
      expect(workspaceObservability(hostile), hostile).toBeNull();
    }
  });

  it('is null for an absent or blank URL', () => {
    expect(workspaceObservability(null)).toBeNull();
    expect(workspaceObservability(undefined)).toBeNull();
    expect(workspaceObservability('')).toBeNull();
    expect(workspaceObservability('   ')).toBeNull();
    expect(workspaceObservability('/')).toBeNull();
  });
});

describe('agentObservability', () => {
  const WS = 'https://dbc-test.cloud.databricks.com';

  it('renders NO card for any platform but databricks', () => {
    for (const p of ALL_PLATFORMS.filter((p) => p !== 'databricks')) {
      expect(agentObservability({ platform: p, langfuse_project_id: 'p-1' }, WS), p).toBeNull();
    }
    expect(agentObservability({ langfuse_project_id: 'p-1' }, WS)).toBeNull();
    expect(agentObservability({ platform: null }, WS)).toBeNull();
  });

  it('reads the Langfuse answer straight off the record: provisioning fires platform-neutrally at registration, so the join field is on a Databricks envelope too', () => {
    const present = agentObservability({ platform: 'databricks', langfuse_project_id: 'clx1' }, WS);
    expect(present!.langfuse.provisioned).toBe(true);
    expect(present!.langfuse.note).toBe(LANGFUSE_PROJECT_PRESENT_NOTE);

    const absent = agentObservability({ platform: 'databricks', langfuse_project_id: null }, WS);
    expect(absent!.langfuse.provisioned).toBe(false);
    expect(absent!.langfuse.note).toBe(LANGFUSE_PROJECT_ABSENT_NOTE);
  });

  it('treats an empty-string project id as not provisioned', () => {
    const o = agentObservability({ platform: 'databricks', langfuse_project_id: '' }, WS);
    expect(o!.langfuse.provisioned).toBe(false);
  });

  it('still renders the card when the workspace URL is unresolvable', () => {
    const o = agentObservability({ platform: 'databricks', langfuse_project_id: 'clx1' });
    expect(o).not.toBeNull();
    expect(o!.workspace).toBeNull();
    expect(o!.langfuse.provisioned).toBe(true);
  });

  it('does not survive a hostile workspace URL into the card', () => {
    const o = agentObservability({ platform: 'databricks' }, 'javascript:alert(1)');
    expect(o!.workspace).toBeNull();
  });

  it('makes no claim that a trace was emitted, and no MLflow claim: AGP knows a project exists, the Traces tab knows whether anything landed', () => {
    for (const note of [LANGFUSE_PROJECT_PRESENT_NOTE, LANGFUSE_PROJECT_ABSENT_NOTE]) {
      expect(note).not.toMatch(/mlflow/i);
      expect(note).not.toMatch(/\b\d+ traces?\b/i);
      expect(note).not.toMatch(/is emitting|has emitted/i);
    }
    expect(DATABRICKS_INFERENCE_TABLES_NOTE).not.toMatch(/mlflow/i);
    expect(LANGFUSE_PROJECT_PRESENT_NOTE).toMatch(/traces tab/i);
  });

  it('names the inference tables as Databricks own, not AGP own', () => {
    expect(DATABRICKS_INFERENCE_TABLES_NOTE).toMatch(/inference tables/i);
    expect(DATABRICKS_INFERENCE_TABLES_NOTE).toMatch(/databricks/i);
  });
});

describe('identityStatusNote — failed sits permanently and must not read as pending', () => {
  it('says "failed" only for identity_status === "failed"', () => {
    expect(identityStatusNote('failed')).toBe('Identity provisioning failed');
  });

  it('says "pending" for none, pending, and an unrecognised/absent value', () => {
    expect(identityStatusNote('pending')).toBe('Identity provisioning pending');
    expect(identityStatusNote('none')).toBe('Identity provisioning pending');
    expect(identityStatusNote(undefined)).toBe('Identity provisioning pending');
  });
});

describe('tenantWorkspaceUrl — which workspace is THIS agent\'s?', () => {
  const WS = 'https://dbc-test.cloud.databricks.com';
  const OTHER = 'https://dbc-other.cloud.databricks.com';
  const db = (workspace_url: string) => ({
    workspace_url,
    workspace_id: '0',
    cloud: 'aws',
    region: '',
    account_id: '',
    sp_client_id: 'sp',
    sp_client_secret_arn: '',
  });
  const aws = { account_id: '123456789012', region: 'us-east-1' };

  it('answers when every Databricks stage agrees', () => {
    expect(tenantWorkspaceUrl({ dev: db(WS), prod: db(WS) })).toBe(WS);
    expect(tenantWorkspaceUrl({ onlyone: db(WS) })).toBe(WS);
  });

  it('refuses to GUESS when stages point at different workspaces', () => {
    expect(tenantWorkspaceUrl({ dev: db(WS), prod: db(OTHER) })).toBeNull();
  });

  it('survives a null stage config: a null stage value off /users/me once blanked AgentDetail for every platform', () => {
    expect(() => tenantWorkspaceUrl({ dev: null })).not.toThrow();
    expect(tenantWorkspaceUrl({ dev: null })).toBeNull();
    expect(tenantWorkspaceUrl({ dev: undefined })).toBeNull();
  });

  it('survives the JSON ROUND-TRIP shape this actually arrives in', () => {
    const wire = JSON.parse(`{"dev":null,"prod":${JSON.stringify(db(WS))}}`);
    expect(() => tenantWorkspaceUrl(wire)).not.toThrow();
    expect(tenantWorkspaceUrl(wire)).toBe(WS);
  });

  it('is unfazed by non-object stage values', () => {
    expect(() => tenantWorkspaceUrl({ a: 'x', b: 7, c: true, d: null })).not.toThrow();
    expect(tenantWorkspaceUrl({ a: 'x', b: 7, c: true, d: null })).toBeNull();
    expect(tenantWorkspaceUrl({ junk: 'x', prod: db(WS) })).toBe(WS);
  });

  it('is null for an AWS tenant, an empty map, and an absent map', () => {
    expect(tenantWorkspaceUrl({ dev: aws, prod: aws })).toBeNull();
    expect(tenantWorkspaceUrl({})).toBeNull();
    expect(tenantWorkspaceUrl(null)).toBeNull();
    expect(tenantWorkspaceUrl(undefined)).toBeNull();
  });

  it('ignores AWS stages mixed in rather than being defeated by them', () => {
    expect(tenantWorkspaceUrl({ dev: db(WS), legacy: aws })).toBe(WS);
  });

  it('reads no stage key, so renaming every key leaves the answer unchanged', () => {
    expect(tenantWorkspaceUrl({ 'zzz-9': db(WS), 'a b c': db(WS) })).toBe(WS);
  });

  it('does not validate — that is workspaceObservability job, one layer up', () => {
    expect(tenantWorkspaceUrl({ dev: db('javascript:alert(1)') })).toBe('javascript:alert(1)');
    expect(workspaceObservability(tenantWorkspaceUrl({ dev: db('javascript:alert(1)') }))).toBeNull();
  });
});

describe('platform-access drift copy', () => {
  it('covers every wire direction, exhaustively', () => {
    expect(Object.keys(DRIFT_DIRECTION_NOTE).sort()).toEqual(['missing_acl', 'unauthorized_acl']);
    expect(DRIFT_DIRECTION_NOTE.unauthorized_acl).toBe('Has platform access without an AGP grant');
    expect(DRIFT_DIRECTION_NOTE.missing_acl).toBe('AGP grant not enforced on the platform');
  });

  it('covers all three Databricks ACL principal kinds', () => {
    expect(Object.keys(DRIFT_KIND_LABEL).sort()).toEqual(['group', 'service_principal', 'user']);
  });

  it('states the destructive half in the confirm text: Re-assert replaces the app’s ACL, so hand-granted access is removed', () => {
    expect(DRIFT_REASSERT_CONFIRM).toMatch(/removed/);
    expect(DRIFT_REASSERT_CONFIRM.toLowerCase()).toContain('by hand');
  });

  it('never spells this feature "reconcile": that word belongs to the Graph read-your-writes overlay in the same tab', () => {
    const allCopy = [
      DRIFT_PANEL_TITLE,
      DRIFT_PANEL_NOTE,
      DRIFT_REASSERT_LABEL,
      DRIFT_REASSERT_CONFIRM,
      DRIFT_CLEAN_NOTE,
      DRIFT_UNAVAILABLE_NOTE,
      ...Object.values(DRIFT_DIRECTION_NOTE),
    ].join(' ').toLowerCase();
    expect(allCopy).not.toContain('reconcil');
  });

  it('does not claim the ACL is fine when it could not be checked', () => {
    expect(DRIFT_UNAVAILABLE_NOTE).not.toBe(DRIFT_CLEAN_NOTE);
    expect(DRIFT_UNAVAILABLE_NOTE.toLowerCase()).toContain('could not');
  });
});

describe('GOVERNABLE_PLATFORM / isInventoryOnly', () => {
  it('covers every platform label, so a ninth is a compile error not a hunt', () => {
    expect(Object.keys(GOVERNABLE_PLATFORM).sort()).toEqual([...ALL_PLATFORMS].sort());
  });

  it('mirrors the backend GOVERNABLE_PLATFORMS map exactly — two entries, the rest null', () => {
    expect(GOVERNABLE_PLATFORM.aws_bedrock).toBe('aws');
    expect(GOVERNABLE_PLATFORM.databricks).toBe('databricks');
    const governable = ALL_PLATFORMS.filter((p) => GOVERNABLE_PLATFORM[p] !== null);
    expect(governable.sort()).toEqual(['aws_bedrock', 'databricks']);
  });

  it('calls exactly the other six inventory-only', () => {
    const inventory = ALL_PLATFORMS.filter((p) => isInventoryOnly(p));
    expect(inventory.sort()).toEqual(['azure', 'google', 'on_prem', 'other', 'salesforce', 'sap']);
  });

  it('answers false for an absent platform and for a label this build never heard of, prototype names included: nobody set the platform is not evidence AGP cannot govern it', () => {
    expect(isInventoryOnly(null)).toBe(false);
    expect(isInventoryOnly(undefined)).toBe(false);
    expect(isInventoryOnly('quantum_cloud' as Platform)).toBe(false);
    expect(isInventoryOnly('constructor' as Platform)).toBe(false);
  });
});

describe('inventoryOnlyBadge', () => {
  it('is the contract copy, verbatim, and never colour-only', () => {
    expect(INVENTORY_ONLY_NOTE).toBe(
      'Registry entry only — AGP cannot govern or invoke agents on this platform',
    );
    const badge = inventoryOnlyBadge('salesforce')!;
    expect(badge.label).toBe('Inventory only');
    expect(badge.hint).toBe(INVENTORY_ONLY_NOTE);
    expect(badge.tint.length).toBeGreaterThan(0);
  });

  it('renders no badge for a governed platform, an absent one, or an unknown label', () => {
    expect(inventoryOnlyBadge('aws_bedrock')).toBeNull();
    expect(inventoryOnlyBadge('databricks')).toBeNull();
    expect(inventoryOnlyBadge(null)).toBeNull();
    expect(inventoryOnlyBadge('quantum_cloud' as Platform)).toBeNull();
  });

  it('does not borrow the refusal tint: an inventory-only record is a registry entry doing its job, not something broken', () => {
    expect(inventoryOnlyBadge('azure')!.tint).not.toContain('red');
  });
});

describe('publishRefusal — the label-keyed publish gate', () => {
  it('declares publish support per governable platform, exhaustively', () => {
    expect(Object.keys(MARKETPLACE_PUBLISH).sort()).toEqual(['aws', 'databricks']);
    expect(MARKETPLACE_PUBLISH.aws).toBe(true);
    expect(MARKETPLACE_PUBLISH.databricks).toBe(false);
  });

  it('permits the one publishable platform today', () => {
    expect(publishRefusal('aws_bedrock')).toBeNull();
  });

  it('refuses databricks with the existing wire kind, on the label alone: the old three-legged gate let a handle-less Databricks record reach a CTA the backend answers 409 to', () => {
    const refusal = publishRefusal('databricks')!;
    expect(refusal.kind).toBe('databricks_publish_unsupported');
    expect(refusal.note).toBe(PUBLISH_REFUSAL_NOTE.databricks_publish_unsupported);
    expect(refusal.note.length).toBeGreaterThan(0);
  });

  it('leaves inventory-only, absent and unparseable labels alone, as the backend does: those records have no governed publish path to refuse', () => {
    for (const p of ['azure', 'salesforce', 'sap', 'google', 'on_prem', 'other'] as Platform[]) {
      expect(publishRefusal(p), p).toBeNull();
    }
    expect(publishRefusal(null)).toBeNull();
    expect(publishRefusal(undefined)).toBeNull();
    expect(publishRefusal('quantum_cloud' as Platform)).toBeNull();
    expect(publishRefusal('constructor' as Platform)).toBeNull();
  });

  it('has copy for both refusal kinds', () => {
    expect(Object.keys(PUBLISH_REFUSAL_NOTE).sort()).toEqual([
      'databricks_publish_unsupported',
      'publish_unsupported_platform',
    ]);
    for (const note of Object.values(PUBLISH_REFUSAL_NOTE)) {
      expect(note.length).toBeGreaterThan(0);
    }
  });
});

const ALL_VERBS: Verb[] = [
  'discover',
  'invoke',
  'mirror',
  'deploy',
  'publish',
  'guardrails',
  'mcp_delivery',
  'observability',
];

const ALL_REASONS: VerbReadiness['reason'][] = [
  '',
  'unsupported',
  'deploy_role_not_assumable',
  'probe_unreachable',
  'invoke_unavailable',
];

function verbReadiness(overrides: Partial<VerbReadiness> = {}): VerbReadiness {
  return { verb: 'deploy', state: 'ready', reason: '', stage: null, ...overrides };
}

describe('readiness vocabulary', () => {
  it('names all eight verbs and all three states', () => {
    expect(Object.keys(VERB_LABEL).sort()).toEqual([...ALL_VERBS].sort());
    for (const v of ALL_VERBS) expect(VERB_LABEL[v].length, v).toBeGreaterThan(0);
    expect(Object.keys(READINESS_STATE_TINT).sort()).toEqual([
      'not_applicable',
      'ready',
      'unavailable',
    ]);
  });

  it('has a note for every reason the backend can send, and none for the empty one', () => {
    expect(Object.keys(READINESS_REASON_NOTE).sort()).toEqual([...ALL_REASONS].sort());
    expect(READINESS_REASON_NOTE['']).toBe('');
    for (const r of ALL_REASONS.filter((x) => x !== '')) {
      expect(READINESS_REASON_NOTE[r].length, r).toBeGreaterThan(0);
    }
  });

  it('spells mirror and mcp_delivery out in full, so neither collides with the Graph overlay’s "reconcile" nor reads as a delivered grant', () => {
    expect(VERB_LABEL.mirror).toBe('Mirror platform access');
    expect(VERB_LABEL.mcp_delivery).toBe('MCP delivery to the runtime');
  });

  it('says AGP could not verify for probe_unreachable, never that the verb does not work', () => {
    expect(READINESS_REASON_NOTE.probe_unreachable).toBe('AGP could not verify it for this tenant.');
    expect(READINESS_REASON_NOTE.probe_unreachable.toLowerCase()).not.toContain('does not work');
  });

  it('takes the invoke_unavailable line from BINDING_MODE_HINT', () => {
    expect(READINESS_REASON_NOTE.invoke_unavailable).toBe(BINDING_MODE_HINT.invoke_unavailable);
  });
});

describe('readinessBadge', () => {
  it('always says something — a badge with no consequence is decoration', () => {
    for (const v of ALL_VERBS) {
      for (const r of ALL_REASONS) {
        const state = r === '' ? 'ready' : 'unavailable';
        const badge = readinessBadge(v, verbReadiness({ verb: v, state, reason: r }));
        expect(badge.label.length, `${v}/${r}`).toBeGreaterThan(0);
        expect(badge.hint.length, `${v}/${r}`).toBeGreaterThan(0);
        expect(badge.tint.length, `${v}/${r}`).toBeGreaterThan(0);
      }
      const na = readinessBadge(v, verbReadiness({ state: 'not_applicable', reason: '' }));
      expect(na.hint.length, v).toBeGreaterThan(0);
    }
  });

  it('tints by state and names the verb', () => {
    expect(readinessBadge('deploy', verbReadiness({ state: 'ready', reason: '' })).tint).toContain('emerald');
    expect(
      readinessBadge('deploy', verbReadiness({ state: 'unavailable', reason: 'probe_unreachable' })).tint,
    ).toContain('red');
    const na = readinessBadge('mirror', verbReadiness({ state: 'not_applicable', reason: '' }));
    expect(na.tint).not.toContain('red');
    expect(na.label).toContain(VERB_LABEL.mirror);
  });

  it('does not describe a not-applicable verb as something to fix: mirror on AWS does not exist there, it is not missing', () => {
    const hint = readinessBadge('mirror', verbReadiness({ state: 'not_applicable', reason: '' })).hint;
    expect(hint.toLowerCase()).not.toContain('could not');
  });

  it('names the affected environment when the verdict is stage-scoped, and only then', () => {
    const scoped = readinessBadge('deploy', verbReadiness({ state: 'unavailable', reason: 'deploy_role_not_assumable', stage: 'obvious-stage' }));
    expect(scoped.hint).toContain('obvious-stage');
    const unscoped = readinessBadge('deploy', verbReadiness({ state: 'unavailable', reason: 'deploy_role_not_assumable' }));
    expect(unscoped.hint).not.toContain('obvious-stage');
  });

  it('renders the unsupported pair the backend actually sends', () => {
    const badge = readinessBadge('publish', verbReadiness({ state: 'unavailable', reason: 'unsupported' }));
    expect(badge.tint).toContain('red');
    expect(badge.hint).toBe(READINESS_REASON_NOTE.unsupported);
  });

  it('survives a prototype-polluting reason and renders no native-code text: both wire keys are looked up as unvalidated', () => {
    for (const reason of ['constructor', 'toString', '__proto__', 'hasOwnProperty']) {
      const badge = readinessBadge('invoke', verbReadiness({ state: 'unavailable', reason: reason as VerbReadiness['reason'] }));
      expect(typeof badge.hint, reason).toBe('string');
      expect(badge.hint.toLowerCase(), reason).not.toContain('native code');
      expect(badge.hint.toLowerCase(), reason).not.toContain('function');
      expect(badge.hint, reason).not.toContain(reason);
      expect(badge.hint.length, reason).toBeGreaterThan(0);
      expect(badge.tint, reason).toBe(READINESS_STATE_TINT.unavailable);
    }
  });

  it('fails closed on an unrecognised state rather than painting it green', () => {
    for (const state of ['constructor', 'partially_ready', '']) {
      const badge = readinessBadge('deploy', verbReadiness({ state: state as never, reason: '' }));
      expect(badge.tint, state).toBe(READINESS_STATE_TINT.unavailable);
      expect(badge.tint, state).not.toContain('emerald');
      expect(badge.hint.length, state).toBeGreaterThan(0);
    }
  });

  it('survives a verb key this build does not know', () => {
    const badge = readinessBadge('constructor' as Verb, verbReadiness({ state: 'ready', reason: '' }));
    expect(badge.hint.toLowerCase()).not.toContain('native code');
    expect(badge.label).not.toContain('constructor');
    expect(badge.hint.length).toBeGreaterThan(0);
  });
});

describe('invokeReadiness — the shim for backends that predate readiness', () => {
  it('prefers the projection when the backend sent one: a stale federation word loses to a verdict AGP could not verify', () => {
    expect(
      invokeReadiness({
        readiness: { invoke: verbReadiness({ verb: 'invoke', state: 'unavailable', reason: 'probe_unreachable' }) },
        binding_mode: 'federation',
      }),
    ).toEqual(verbReadiness({ verb: 'invoke', state: 'unavailable', reason: 'probe_unreachable' }));
  });

  it('derives readiness from binding_mode when the projection lacks it (old backends)', () => {
    expect(invokeReadiness({ binding_mode: 'federation' })).toEqual(verbReadiness({ verb: 'invoke', state: 'ready', reason: '' }));
    expect(invokeReadiness({ binding_mode: 'sp_secret' })).toEqual(verbReadiness({ verb: 'invoke', state: 'ready', reason: '' }));
    expect(invokeReadiness({ binding_mode: 'invoke_unavailable' })).toEqual(verbReadiness({ verb: 'invoke', state: 'unavailable', reason: 'invoke_unavailable' }));
  });

  it('falls back to the mode when readiness is present but carries no invoke verdict', () => {
    expect(
      invokeReadiness({
        readiness: { deploy: verbReadiness({ state: 'ready', reason: '' }) },
        binding_mode: 'sp_secret',
      }),
    ).toEqual(verbReadiness({ verb: 'invoke', state: 'ready', reason: '' }));
  });

  it('answers null when there is nothing to claim', () => {
    expect(invokeReadiness({})).toBeNull();
    expect(invokeReadiness({ binding_mode: '' })).toBeNull();
    expect(invokeReadiness({ binding_mode: null })).toBeNull();
    expect(invokeReadiness({ binding_mode: 'moon_phase' })).toBeNull();
    expect(invokeReadiness({ binding_mode: 'constructor' })).toBeNull();
  });

  it('hands out a fresh verdict per call, so one caller’s mutation cannot rewrite another’s answer', () => {
    const a = invokeReadiness({ binding_mode: 'federation' })!;
    const b = invokeReadiness({ binding_mode: 'federation' })!;
    expect(a).not.toBe(b);
  });
});

describe('invokeBadge — unchanged visuals for today’s states', () => {
  it('is byte-identical to bindingModeBadge for every mode, with no readiness', () => {
    const modes = ['federation', 'sp_secret', 'invoke_unavailable', '', null, undefined, 'moon_phase'];
    for (const mode of modes) {
      expect(invokeBadge({ binding_mode: mode }), String(mode)).toEqual(bindingModeBadge(mode));
    }
  });

  it('keeps the sp_secret cost line when readiness agrees that invoke works', () => {
    const badge = invokeBadge({
      readiness: { invoke: verbReadiness({ verb: 'invoke', state: 'ready', reason: '' }) },
      binding_mode: 'sp_secret',
    })!;
    expect(badge.label).toBe('SP secret');
    expect(badge.tint).toContain('amber');
    expect(badge.hint).toBe(BINDING_MODE_HINT.sp_secret);
  });

  it('lets readiness overrule a mode that disagrees with it', () => {
    const badge = invokeBadge({
      readiness: { invoke: verbReadiness({ verb: 'invoke', state: 'unavailable', reason: 'probe_unreachable' }) },
      binding_mode: 'federation',
    })!;
    expect(badge.tint).toContain('red');
    expect(badge.hint).toBe(READINESS_REASON_NOTE.probe_unreachable);
    expect(badge.label).not.toBe('Federation');
  });

  it('badges an unsupported invoke from readiness alone, with no mode on the record', () => {
    const badge = invokeBadge({
      readiness: { invoke: verbReadiness({ verb: 'invoke', state: 'unavailable', reason: 'unsupported' }) },
    })!;
    expect(badge.tint).toContain('red');
    expect(badge.hint).toBe(READINESS_REASON_NOTE.unsupported);
  });

  it('renders nothing when neither source claims anything', () => {
    expect(invokeBadge({})).toBeNull();
    expect(invokeBadge({ readiness: {} })).toBeNull();
  });

  it('does not let a mode word stand in for a different refusal with the same state: invoke_unavailable advice over an unsupported platform is work that cannot help', () => {
    const badge = invokeBadge({
      readiness: { invoke: verbReadiness({ verb: 'invoke', state: 'unavailable', reason: 'unsupported' }) },
      binding_mode: 'invoke_unavailable',
    })!;
    expect(badge.hint).toBe(READINESS_REASON_NOTE.unsupported);
    expect(badge.hint).not.toBe(BINDING_MODE_HINT.invoke_unavailable);
    expect(badge).not.toEqual(bindingModeBadge('invoke_unavailable'));
  });

  it('keeps the mode badge when state AND reason agree', () => {
    expect(
      invokeBadge({
        readiness: { invoke: verbReadiness({ verb: 'invoke', state: 'unavailable', reason: 'invoke_unavailable' }) },
        binding_mode: 'invoke_unavailable',
      }),
    ).toEqual(bindingModeBadge('invoke_unavailable'));
  });

  it('never lets a mode badge swallow the affected environment', () => {
    const badge = invokeBadge({
      readiness: {
        invoke: verbReadiness({ verb: 'invoke', state: 'unavailable', reason: 'invoke_unavailable', stage: 'obvious-stage' }),
      },
      binding_mode: 'invoke_unavailable',
    })!;
    expect(badge.hint).toContain('obvious-stage');
  });
});
