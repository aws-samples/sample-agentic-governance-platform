// Tests for platformLabels — the ONE platform-vocabulary module (E29/T9).
//
// Pure-function tests only: vitest collects `src/**/*.test.ts` in a node environment, so nothing
// here renders a component or asserts on markup. That constraint is why the module exists in this
// shape — every decision the four governance surfaces make is exported and pinned here.
import { describe, it, expect } from 'vitest';
import type { Platform } from '../../api/client';
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
  DRIFT_UNKNOWN_DIRECTION_NOTE,
  DRIFT_DIRECTION_NOTE,
  DRIFT_KIND_LABEL,
} from './platformLabels';
import type { AccessSurfaceSource, RuntimeHandleSource } from './platformLabels';

// Every member of the backend `Platform` enum, written out. Deliberately a LITERAL list rather
// than `Object.keys(PLATFORM_LABEL)`: derived from the map, the exhaustiveness tests below would
// pass no matter what the map contained. The `Platform[]` annotation makes a rename in
// `client.ts` a compile error here.
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
    // These four `full` strings are what AgentDetail / AgentsList / AgentsOverview / the wizard
    // each displayed before consolidation. If a future edit "tidies" one, the surfaces silently
    // change wording; that is what this test refuses.
    expect(PLATFORM_LABEL.aws_bedrock.full).toBe('Amazon Bedrock AgentCore');
    expect(PLATFORM_LABEL.databricks.full).toBe('Databricks');
    expect(PLATFORM_LABEL.on_prem.full).toBe('On-prem');
    expect(PLATFORM_LABEL.other.full).toBe('Other');
    // ...and these are what graphNodes' shorter map used.
    expect(PLATFORM_LABEL.aws_bedrock.host).toBe('AWS');
    expect(PLATFORM_LABEL.databricks.host).toBe('Databricks');
  });

  it('keeps the two vocabularies genuinely different where the graph needed them to be', () => {
    // The reason `host` exists at all: the node card cannot show the product name. If these ever
    // become equal, `host` has stopped earning its keep and the divergence should be re-argued
    // rather than quietly collapsed.
    expect(PLATFORM_LABEL.aws_bedrock.host).not.toBe(PLATFORM_LABEL.aws_bedrock.full);
  });
});

describe('platformLabel / platformLabelOr', () => {
  it('returns the full product label', () => {
    expect(platformLabel('aws_bedrock')).toBe('Amazon Bedrock AgentCore');
    expect(platformLabel('databricks')).toBe('Databricks');
  });

  it('renders an em-dash for null/undefined — never the word "Other"', () => {
    // `other` is a real registry value; "nobody said" must stay distinguishable from it.
    expect(platformLabel(null)).toBe('—');
    expect(platformLabel(undefined)).toBe('—');
    expect(platformLabel(null)).not.toBe(PLATFORM_LABEL.other.full);
  });

  it('lets each surface word the absence itself', () => {
    expect(platformLabelOr(null, 'Platform not set')).toBe('Platform not set');
    expect(platformLabelOr(undefined, 'Not set')).toBe('Not set');
    expect(platformLabelOr('databricks', 'Not set')).toBe('Databricks');
  });
});

describe('platformHostLabel — the graph node tag', () => {
  it('gives every known platform its short host', () => {
    for (const p of ALL_PLATFORMS) {
      expect(platformHostLabel(p), p).toBe(PLATFORM_LABEL[p].host);
    }
  });

  it('NEVER captions a databricks node as AWS — the E29 bug, executed', () => {
    expect(platformHostLabel('databricks')).toBe('Databricks');
    expect(platformHostLabel('databricks')).not.toBe('AWS');
  });

  it('returns empty (render no tag) for absent/unknown platforms rather than defaulting to AWS', () => {
    // The graph reads `data.metadata.platform` off a Record<string, unknown>, so an unvalidated
    // value really can arrive. A default of 'AWS' here would state a hosting fact nobody recorded.
    for (const bogus of ['', 'AWS', 'aws', 'Databricks', 'kubernetes', 'aws_bedrock ', 'OTHER']) {
      expect(platformHostLabel(bogus), bogus).toBe('');
    }
  });

  it('does not inherit Object.prototype keys as platforms', () => {
    // `platform in PLATFORM_LABEL` would be true for inherited keys if the guard were naive about
    // it; an agent whose metadata carried "constructor" must not render a tag.
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

// ---------------------------------------------------------------------------
// runtimeHandleField — App URL vs Agent ARN, and the row that must not render
// ---------------------------------------------------------------------------

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

  it('still shows the App URL row (Not set) for a handle-less Databricks record', () => {
    // A Databricks record with no handle is a legitimate metadata-only registration — the field
    // APPLIES and is empty, which is a different statement from "this field does not exist here".
    // Suppressing it would hide the one value an operator must supply to make the agent governed.
    const f = runtimeHandleField(src({ platform: 'databricks' }));
    expect(f).not.toBeNull();
    expect(f!.label).toBe('App URL');
    expect(f!.value).toBeNull();
  });

  it('labels an AgentCore ARN Agent ARN', () => {
    const f = runtimeHandleField(src({ platform: 'aws_bedrock', agent_arn: ARN }));
    expect(f).toEqual({ label: 'Agent ARN', value: ARN, copyLabel: 'agent ARN' });
  });

  it('renders NO row for an AgentCore agent that has no ARN yet — no empty ARN row', () => {
    // THE FIX. AgentDetail used to render an unconditional "Agent ARN — Not set" row.
    expect(runtimeHandleField(src({ platform: 'aws_bedrock' }))).toBeNull();
  });

  it('renders NO row for a metadata-only agent on any other platform', () => {
    for (const p of ['azure', 'sap', 'salesforce', 'google', 'on_prem', 'other'] as Platform[]) {
      expect(runtimeHandleField(src({ platform: p })), p).toBeNull();
    }
    expect(runtimeHandleField(src())).toBeNull(); // platform unset entirely
  });

  it('never captions a non-Databricks runtime handle as Agent ARN', () => {
    // Shape-not-yet-real but the failure would be silent and expensive: `agent_arn` is parsed as a
    // Bedrock ARN by the delete cascade and the status probe, so labelling an arbitrary handle
    // "Agent ARN" trains an operator to paste it where it will be mis-parsed.
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

  it('never leaks an ARN onto a Databricks row even if the record carries one', () => {
    // Should not happen (the wizard refuses to set `agent_arn` on the Databricks branch), which is
    // exactly why it is pinned: platform wins, so a stray ARN cannot re-caption the row.
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

// ---------------------------------------------------------------------------
// Platform-gated tabs
// ---------------------------------------------------------------------------

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

  it('leaves every tab that actually exists today visible on Databricks', () => {
    // The live tab set (agentDetailTabs.ALL_TABS). None of these is platform-gated: grants,
    // MCP servers, traces and cost are all real for a Databricks agent.
    for (const id of ['overview', 'access', 'mcp-servers', 'deployment', 'traces', 'cost']) {
      expect(isTabVisibleForPlatform(id, 'databricks'), id).toBe(true);
    }
  });

  it('gates exactly one id, on exactly one platform (no accidental widening)', () => {
    expect(Object.keys(HIDDEN_TAB_IDS_BY_PLATFORM)).toEqual(['databricks']);
    expect(HIDDEN_TAB_IDS_BY_PLATFORM.databricks).toEqual(['guardrails']);
  });
});

// ---------------------------------------------------------------------------
// Binding-mode badge — C-6 copy
// ---------------------------------------------------------------------------

describe('bindingModeBadge (C-6)', () => {
  it('uses the C-6 labels verbatim', () => {
    expect(bindingModeBadge('federation')!.label).toBe('Federation');
    expect(bindingModeBadge('sp_secret')!.label).toBe('SP secret');
    expect(bindingModeBadge('invoke_unavailable')!.label).toBe('Invoke unavailable — federation required');
  });

  it('is the SAME copy the tenant surface badges — one contract, one string', () => {
    // C-6 pins these words on both surfaces. Asserting equality with `bindingModeLabel` (rather
    // than re-typing the literals) is what makes a second spelling impossible.
    expect(bindingModeBadge('federation')!.label).toBe(bindingModeLabel('federation'));
    expect(bindingModeBadge('sp_secret')!.label).toBe(bindingModeLabel('sp_secret'));
    expect(bindingModeBadge('invoke_unavailable')!.label).toBe(bindingModeLabel('invoke_unavailable'));
  });

  // T14 / §3B: what a non-federatable tenant gets is honesty, not a downgrade — it can still
  // discover, register, catalogue and observe, but invoke is refused, and the hint names what
  // federation needs so the operator has an action rather than a verdict.
  it('carries an ACTIONABLE invoke_unavailable hint naming the credential and user sync', () => {
    const hint = bindingModeBadge('invoke_unavailable')!.hint;
    expect(hint).toBe(BINDING_MODE_HINT.invoke_unavailable);
    expect(hint).toContain('account-admin');
    expect(hint).toContain('user sync');
    // The state, said plainly: inventory works, invoke does not.
    expect(hint).toContain('Invoke is refused');
  });

  it('carries the sp_secret consequence sentence — the audit-attribution cost, stated', () => {
    const hint = bindingModeBadge('sp_secret')!.hint;
    expect(hint).toBe(BINDING_MODE_HINT.sp_secret);
    // The load-bearing claim, matched on substance so a punctuation variant cannot silently
    // change the meaning: WHO the call is attributed to, and who it is NOT.
    expect(hint).toContain('service principal in Databricks audit logs');
    expect(hint).toContain('not the individual caller');
  });

  it('carries the federation consequence sentence too — every mode explains itself', () => {
    expect(bindingModeBadge('federation')!.hint).toBe(BINDING_MODE_HINT.federation);
  });

  // EVERY mode, including the one added by T14: `hint` is a required field, and the old
  // implementation cast the mode to a two-value union before indexing the hint map — which
  // handed a third mode `hint: undefined` and would have thrown on `.length` here.
  it('always supplies a hint, so the badge never relies on colour alone', () => {
    for (const m of ['federation', 'sp_secret', 'invoke_unavailable']) {
      expect(bindingModeBadge(m)!.hint.length, m).toBeGreaterThan(0);
    }
  });

  it('tints all three modes differently — supported, degraded and refused are not peers', () => {
    const tints = ['federation', 'sp_secret', 'invoke_unavailable'].map((m) => bindingModeBadge(m)!.tint);
    expect(new Set(tints).size).toBe(3);
    // Federation keeps emerald (the intended path); the refused mode must not borrow it.
    expect(bindingModeBadge('federation')!.tint).toContain('emerald');
    expect(bindingModeBadge('invoke_unavailable')!.tint).not.toContain('emerald');
  });

  it('badges nothing for an AWS tenant, an unprobed record, or an unknown mode', () => {
    for (const m of ['', null, undefined, 'Federation', 'sp-secret', 'something_new']) {
      expect(bindingModeBadge(m as string | null | undefined), String(m)).toBeNull();
    }
  });
});

// ---------------------------------------------------------------------------
// accessSurface — the suppression bug
// ---------------------------------------------------------------------------

const acc = (over: Partial<AccessSurfaceSource> = {}): AccessSurfaceSource => ({
  platform: null,
  identity_status: 'pending',
  entra_sp_id: null,
  agent_arn: null,
  runtime_handle: null,
  ...over,
});

describe('accessSurface — THE BUG: an ARN-less Databricks agent must show its grants', () => {
  it('shows the grants table for a pending Databricks agent that has an SP', () => {
    // Before the fix this returned the "Identity ready — awaiting runtime" banner FOREVER, because
    // `entra_sp_id && !agent_arn` is permanently true for a platform that never has an ARN.
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

  it('THE STRANDED SHAPE: pending + SP + ARN → provisioning, so Re-provision stays reachable', () => {
    // The case this block previously never covered (it had SP-no-ARN, provisioned, and no-SP), and
    // the one a first cut of `accessSurface` got wrong by returning a read-only grants table.
    //
    // This is the NORMAL mid-provision failure, not a hypothetical: `provision_identity` persists
    // `entra_sp_id` with status `pending` BEFORE steps 2-3 (agent_identity_service.py ~:343), and
    // `is_agentcore` requires `agent_arn` — so an ECS task death mid-provision leaves exactly this.
    // `AccessTab`'s STALE_PENDING_MS / isPendingStranded machinery then offers Re-provision, which
    // is the ONLY way out. A grants table offers nothing.
    expect(
      accessSurface(
        acc({ platform: 'aws_bedrock', identity_status: 'pending', entra_sp_id: 'sp-1', agent_arn: ARN }),
      ),
    ).toEqual({ kind: 'provisioning' });
  });

  it('never leaves a pending SP-bearing agent on a recovery-less surface, on ANY platform', () => {
    // Same root cause, swept: an azure/sap/other record with the SP+ARN shape must also keep the
    // spinner + Re-provision path the old code gave it, rather than a read-only table.
    for (const p of ALL_PLATFORMS) {
      const s = accessSurface(acc({ platform: p, identity_status: 'pending', entra_sp_id: 'sp-1', agent_arn: ARN }));
      expect(s.kind, p).toBe('provisioning');
    }
    // ...including a record with no platform at all.
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
  it('none and failed outrank a present SP', () => {
    // Provisioning writes `entra_sp_id` before the steps that can fail, so a failed agent may well
    // carry one; its tab must offer recovery, not a table of grants that may not be enforceable.
    expect(accessSurface(acc({ identity_status: 'failed', entra_sp_id: 'sp-1', agent_arn: ARN })).kind).toBe('failed');
    expect(accessSurface(acc({ identity_status: 'none', entra_sp_id: 'sp-1' })).kind).toBe('no-identity');
  });

  it('provisioned outranks the awaiting-runtime shape', () => {
    // An AgentCore agent that reached `provisioned` without an ARN scalar (runtimes known only via
    // the E28A `agent_arns` map) must not be told its runtime is undeployed.
    expect(
      accessSurface(acc({ platform: 'aws_bedrock', identity_status: 'provisioned', entra_sp_id: 'sp-1' })).kind,
    ).toBe('grants');
  });

  it('keys awaiting-runtime on the POSITIVE platform test, not on a missing ARN', () => {
    // The whole point of the fix: no platform inherits the banner merely by lacking a field it was
    // never going to have. Only `aws_bedrock` can produce it.
    for (const p of ALL_PLATFORMS.filter((p) => p !== 'aws_bedrock')) {
      const s = accessSurface(acc({ platform: p, identity_status: 'pending', entra_sp_id: 'sp-1' }));
      expect(s.kind, p).toBe('grants');
    }
    // ...including a record with no platform at all.
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

  it('the pending note matches what the BACKEND actually does — no false encouragement', () => {
    // `grants.py::_is_provisioned` is `identity_status == 'provisioned' AND entra_sp_id`, so while
    // the note is showing, GET returns [] and POST answers 409. An earlier draft of this string
    // said the assignments were "live", which was wrong in both directions. These assertions pin
    // the corrected meaning: grants cannot be read OR changed yet, and it is temporary.
    expect(GRANTS_PENDING_NOTE).toContain('cannot be read or changed yet');
    expect(GRANTS_PENDING_NOTE).toContain('once provisioning completes');
    // The retracted claim must not come back.
    expect(GRANTS_PENDING_NOTE).not.toMatch(/assignments are live/);
  });
});

// ---------------------------------------------------------------------------
// mcpDeliveryNote — the C-6 "recorded, not delivered" caveat (E29/T11)
// ---------------------------------------------------------------------------

describe('mcpDeliveryNote', () => {
  it('is contract C-6 VERBATIM', () => {
    // The plan pins this sentence character-for-character. A reworded "improvement" here is a
    // contract break, so the literal is asserted rather than a substring of it.
    expect(MCP_NOT_DELIVERED_NOTE).toBe(
      'Recorded in the registry — not delivered to the runtime on this platform yet.',
    );
    // The em dash and the typographic apostrophe-free wording are part of the pinned string.
    expect(MCP_NOT_DELIVERED_NOTE).toContain('—');
  });

  it('applies to databricks and to NOTHING else', () => {
    expect(mcpDeliveryNote('databricks')).toBe(MCP_NOT_DELIVERED_NOTE);
    for (const p of ALL_PLATFORMS.filter((p) => p !== 'databricks')) {
      expect(mcpDeliveryNote(p), p).toBeNull();
    }
  });

  it('says nothing for an absent platform', () => {
    // A record nobody set a platform on is not evidence that delivery is missing, and a spurious
    // caveat would undermine a mechanism that works. Positive test only.
    expect(mcpDeliveryNote(null)).toBeNull();
    expect(mcpDeliveryNote(undefined)).toBeNull();
  });

  it('states BOTH halves — recorded here, not delivered there', () => {
    // Either half alone is a lie. "Recorded" without "not delivered" is the failure the note
    // exists for; "not delivered" without "recorded" would read as a refused grant.
    expect(MCP_NOT_DELIVERED_NOTE).toMatch(/recorded/i);
    expect(MCP_NOT_DELIVERED_NOTE).toMatch(/not delivered/i);
    // And it does NOT promise a delivery path is coming.
    expect(MCP_NOT_DELIVERED_NOTE).not.toMatch(/coming soon|will be supported|roadmap/i);
  });
});

// ---------------------------------------------------------------------------
// Observability pointers — read-only, and the workspace URL is an href sink
// ---------------------------------------------------------------------------

describe('workspaceObservability', () => {
  // The epic's designated obvious fake (Global Constraints).
  const WS = 'https://dbc-test.cloud.databricks.com';

  it('builds the audit link from the workspace origin', () => {
    const w = workspaceObservability(WS);
    expect(w).not.toBeNull();
    expect(w!.workspaceUrl).toBe(WS);
    // The link is the origin plus one workspace-relative path — nothing is fetched to build it.
    expect(w!.auditUrl.startsWith(`${WS}/`)).toBe(true);
    expect(w!.auditLabel).toBe(DATABRICKS_AUDIT_LINK_LABEL);
    expect(w!.inferenceNote).toBe(DATABRICKS_INFERENCE_TABLES_NOTE);
  });

  it('points at the system catalog, where the audit tables live', () => {
    // Named rather than guessed at a table path: `system.access.audit` and friends are reached
    // through Catalog Explorer's `system` entry, which is a stable workspace UI route.
    expect(workspaceObservability(WS)!.auditUrl).toBe(`${WS}/explore/data/system`);
  });

  it('applies the write side rule with NO normalization — a first cut got this wrong', () => {
    // The first cut trimmed whitespace and stripped trailing slashes before validating, on the
    // reasoning that both are human-edit artifacts rather than attacks. The test below caught the
    // cost: `.trim()` made a value ending in a NEWLINE validate, and a trailing newline is exactly
    // what the write side refuses (the one hole an unanchored `$` leaves). Every normalization on
    // the read side is a way to accept something the write side rejected, so there is none — and
    // these three assertions are what keep one from creeping back.
    expect(workspaceObservability(`${WS}/`)).toBeNull();
    expect(workspaceObservability(`${WS} `)).toBeNull();
    expect(workspaceObservability(`${WS}\n`)).toBeNull();
  });

  it('EXECUTES the security boundary over hostile values — no href is ever built', () => {
    // This value comes off a tenant record and lands in an `href`, where `javascript:` and
    // `data:` URLs execute on click. Every case here is run against the real validator; the
    // point is not that the code looks careful but that these specific inputs produce no link.
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
    // A lone slash trims to empty rather than becoming an origin.
    expect(workspaceObservability('/')).toBeNull();
  });
});

describe('agentObservability', () => {
  const WS = 'https://dbc-test.cloud.databricks.com';

  it('renders NO card for any platform but databricks', () => {
    // A scope statement, not a claim that AgentCore agents have nothing to show — they have the
    // Traces and Cost tabs, and a card repeating "a project exists" above them would be noise.
    for (const p of ALL_PLATFORMS.filter((p) => p !== 'databricks')) {
      expect(agentObservability({ platform: p, langfuse_project_id: 'p-1' }, WS), p).toBeNull();
    }
    expect(agentObservability({ langfuse_project_id: 'p-1' }, WS)).toBeNull();
    expect(agentObservability({ platform: null }, WS)).toBeNull();
  });

  it('reads the Langfuse answer STRAIGHT OFF THE RECORD', () => {
    // Provisioning fires platform-neutrally at registration, so the join field is on a Databricks
    // envelope too. Its presence IS the answer — nothing is fetched to produce it.
    const present = agentObservability({ platform: 'databricks', langfuse_project_id: 'clx1' }, WS);
    expect(present!.langfuse.provisioned).toBe(true);
    expect(present!.langfuse.note).toBe(LANGFUSE_PROJECT_PRESENT_NOTE);

    const absent = agentObservability({ platform: 'databricks', langfuse_project_id: null }, WS);
    expect(absent!.langfuse.provisioned).toBe(false);
    expect(absent!.langfuse.note).toBe(LANGFUSE_PROJECT_ABSENT_NOTE);
  });

  it('treats an empty-string project id as not provisioned', () => {
    // Emptiness, not falsiness, is the failure — but an empty id is not a project either.
    const o = agentObservability({ platform: 'databricks', langfuse_project_id: '' }, WS);
    expect(o!.langfuse.provisioned).toBe(false);
  });

  it('still renders the card when the workspace URL is unresolvable', () => {
    // The common case for a viewer who cannot resolve the agent's tenant. The Langfuse half is
    // knowable from the record alone, so dropping the whole card would hide what AGP does know.
    const o = agentObservability({ platform: 'databricks', langfuse_project_id: 'clx1' });
    expect(o).not.toBeNull();
    expect(o!.workspace).toBeNull();
    expect(o!.langfuse.provisioned).toBe(true);
  });

  it('does not survive a hostile workspace URL into the card', () => {
    // The gate is the same one `workspaceObservability` executes; asserted through the agent
    // entry point too, because that is the path the page actually takes.
    const o = agentObservability({ platform: 'databricks' }, 'javascript:alert(1)');
    expect(o!.workspace).toBeNull();
  });

  it('makes NO claim that a trace was emitted, and no MLflow claim', () => {
    // Design §4 is explicit: pointers only, no MLflow bridging, no Databricks read. AGP knows a
    // project EXISTS; whether anything landed in it is the Traces tab's question, asked against
    // the real data. Copy that promised otherwise would be the fabrication this card avoids.
    for (const note of [LANGFUSE_PROJECT_PRESENT_NOTE, LANGFUSE_PROJECT_ABSENT_NOTE]) {
      expect(note).not.toMatch(/mlflow/i);
      expect(note).not.toMatch(/\b\d+ traces?\b/i);
      expect(note).not.toMatch(/is emitting|has emitted/i);
    }
    expect(DATABRICKS_INFERENCE_TABLES_NOTE).not.toMatch(/mlflow/i);
    // The present-note points at the surface that CAN answer it.
    expect(LANGFUSE_PROJECT_PRESENT_NOTE).toMatch(/traces tab/i);
  });

  it('names the inference tables as Databricks own, not AGP own', () => {
    // The distinction the card exists for: per-request logging lives in the customer's Unity
    // Catalog, reachable only by a query AGP is not authorised to run.
    expect(DATABRICKS_INFERENCE_TABLES_NOTE).toMatch(/inference tables/i);
    expect(DATABRICKS_INFERENCE_TABLES_NOTE).toMatch(/databricks/i);
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
  // An AWS stage carries no `workspace_url` — the field the narrowing keys on.
  const aws = { account_id: '123456789012', region: 'us-east-1' };

  it('answers when every Databricks stage agrees', () => {
    // An agent record names no stage, so the answer is only available when it does not depend on
    // knowing one. Agreement across stages is exactly that condition.
    expect(tenantWorkspaceUrl({ dev: db(WS), prod: db(WS) })).toBe(WS);
    expect(tenantWorkspaceUrl({ onlyone: db(WS) })).toBe(WS);
  });

  it('refuses to GUESS when stages point at different workspaces', () => {
    // The failure this prevents is not a crash: it is an operator following a link to the wrong
    // workspace audit log, finding no record of their agent, and concluding it never ran.
    expect(tenantWorkspaceUrl({ dev: db(WS), prod: db(OTHER) })).toBeNull();
  });

  it('SURVIVES a null stage config — the fix-round-1 crash (reviewer-executed)', () => {
    // The exact repro. A first cut called `isDatabricksStage(config as never)` and read the
    // property off a second cast, so `null` slipped the predicate's `!== undefined` guard and threw
    // `TypeError: Cannot read properties of null` — which, because this runs BEFORE the Databricks
    // check, blanked the whole AgentDetail page for EVERY platform. `toThrow` is asserted first so
    // a regression fails as a crash rather than as a wrong value.
    expect(() => tenantWorkspaceUrl({ dev: null })).not.toThrow();
    expect(tenantWorkspaceUrl({ dev: null })).toBeNull();
    expect(tenantWorkspaceUrl({ dev: undefined })).toBeNull();
  });

  it('survives the JSON ROUND-TRIP shape this actually arrives in', () => {
    // Not a hand-built object: `stages` is JSON off `/users/me`, and a key with a null value
    // round-trips exactly like this. The valid sibling must still be found — a null stage is a hole
    // in the map, not a reason to abandon the answer.
    const wire = JSON.parse(`{"dev":null,"prod":${JSON.stringify(db(WS))}}`);
    expect(() => tenantWorkspaceUrl(wire)).not.toThrow();
    expect(tenantWorkspaceUrl(wire)).toBe(WS);
  });

  it('is unfazed by non-object stage values', () => {
    // The predicate reads a property off whatever it is given; only null/undefined throw, and the
    // guard excludes both. Primitives answer false through the `typeof`.
    expect(() => tenantWorkspaceUrl({ a: 'x', b: 7, c: true, d: null })).not.toThrow();
    expect(tenantWorkspaceUrl({ a: 'x', b: 7, c: true, d: null })).toBeNull();
    // And a primitive alongside a real stage does not defeat the real one.
    expect(tenantWorkspaceUrl({ junk: 'x', prod: db(WS) })).toBe(WS);
  });

  it('is null for an AWS tenant, an empty map, and an absent map', () => {
    expect(tenantWorkspaceUrl({ dev: aws, prod: aws })).toBeNull();
    expect(tenantWorkspaceUrl({})).toBeNull();
    expect(tenantWorkspaceUrl(null)).toBeNull();
    expect(tenantWorkspaceUrl(undefined)).toBeNull();
  });

  it('ignores AWS stages mixed in rather than being defeated by them', () => {
    // A tenant is single-platform by contract, but the projected `stages` union is structural and
    // a mixed map must degrade to the Databricks answer, not to null.
    expect(tenantWorkspaceUrl({ dev: db(WS), legacy: aws })).toBe(WS);
  });

  it('reads no stage KEY, so C5 cannot be violated by this decision', () => {
    // The keys are arbitrary (E28/D8 opened the axis; C5 forbids a stage literal in `frontend/`).
    // Renaming every key must not change the answer — proof the function reads only configs.
    expect(tenantWorkspaceUrl({ 'zzz-9': db(WS), 'a b c': db(WS) })).toBe(WS);
  });

  it('does not validate — that is workspaceObservability job, one layer up', () => {
    // Kept deliberately separate: this function answers WHICH workspace, the href builder answers
    // whether the value may become a link. A hostile value passes through here and is refused
    // there, so the two concerns cannot be half-applied.
    expect(tenantWorkspaceUrl({ dev: db('javascript:alert(1)') })).toBe('javascript:alert(1)');
    expect(workspaceObservability(tenantWorkspaceUrl({ dev: db('javascript:alert(1)') }))).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// Drift copy (E29/T13e, §3A) — the words the Access tab's drift panel renders
// ---------------------------------------------------------------------------

describe('platform-access drift copy', () => {
  it('covers every wire direction, exhaustively', () => {
    // Exhaustive by type at compile time; asserted here so a direction added to the union with a
    // placeholder string is still caught by a human reading a diff.
    expect(Object.keys(DRIFT_DIRECTION_NOTE).sort()).toEqual(['missing_acl', 'unauthorized_acl']);
    expect(DRIFT_DIRECTION_NOTE.unauthorized_acl).toBe('Has platform access without an AGP grant');
    expect(DRIFT_DIRECTION_NOTE.missing_acl).toBe('AGP grant not enforced on the platform');
  });

  it('covers all three Databricks ACL principal kinds', () => {
    expect(Object.keys(DRIFT_KIND_LABEL).sort()).toEqual(['group', 'service_principal', 'user']);
  });

  it('states the destructive half in the confirm text — hand-granted access is removed', () => {
    // Re-assert is a PUT: it REPLACES the app's ACL. An operator who has not been told that could
    // destroy access someone else deliberately granted, so the sentence is load-bearing.
    expect(DRIFT_REASSERT_CONFIRM).toMatch(/removed/);
    expect(DRIFT_REASSERT_CONFIRM.toLowerCase()).toContain('by hand');
  });

  it('never spells this feature "reconcile" — that word belongs to the Graph overlay', () => {
    // accessGrantsReconcile.ts owns "reconcile" for read-your-writes consistency. Two meanings on
    // one word in one tab is the drift this naming rule prevents (§3A says so explicitly).
    const allCopy = [
      DRIFT_PANEL_TITLE,
      DRIFT_PANEL_NOTE,
      DRIFT_REASSERT_LABEL,
      DRIFT_REASSERT_CONFIRM,
      DRIFT_CLEAN_NOTE,
      DRIFT_UNAVAILABLE_NOTE,
      DRIFT_UNKNOWN_DIRECTION_NOTE,
      ...Object.values(DRIFT_DIRECTION_NOTE),
    ].join(' ').toLowerCase();
    expect(allCopy).not.toContain('reconcil');
  });

  it('does not claim the ACL is fine when it could not be checked', () => {
    expect(DRIFT_UNAVAILABLE_NOTE).not.toBe(DRIFT_CLEAN_NOTE);
    expect(DRIFT_UNAVAILABLE_NOTE.toLowerCase()).toContain('could not');
  });
});
