import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { afterEach, describe, expect, it, vi } from 'vitest';
import client, { containmentApi } from '../../../api/client';
import { ContainCard, InvestigateCard, ProgressCard, SignalCard } from './ContainmentTab';
import { stripComments } from '../../../rawSource';
import { containmentView } from '../../../test/wire';
import tabRaw from './ContainmentTab.tsx?raw';
import invokeRaw from '../InvokePanel.tsx?raw';

const tabSrc = stripComments(tabRaw);
const invokeSrc = stripComments(invokeRaw);

afterEach(() => vi.restoreAllMocks());

const noop = () => {};

const CONTAINED = containmentView({
  state: 'CONTAINED',
  job: 'contained',
  since: '2026-10-16T10:44:05Z',
  by_name: 'Lars Svensson',
  by_oid: 'oid-lars',
  reason: 'tool misuse on openClaim',
  steps: [
    { id: 'refuse-invokes', status: 'ok', detail: 'invokes refused', native_ref: null, at: '2026-10-16T10:44:05Z' },
    { id: 'cedar-forbid', status: 'ok', detail: '1 gateway', native_ref: 'pol-1', at: '2026-10-16T10:44:09Z' },
    { id: 'runtime-deny', status: 'failed', detail: 'AccessDenied', native_ref: null, at: '2026-10-16T10:44:10Z' },
  ],
  audit: [
    { at: '2026-10-16T10:44:05Z', actor_name: 'Lars Svensson', action: 'contain', step: null, result: 'requested', detail: 'tool misuse on openClaim', native_ref: null },
    { at: '2026-10-16T10:44:09Z', actor_name: 'Lars Svensson', action: 'contain', step: 'cedar-forbid', result: 'ok', detail: '', native_ref: 'pol-1' },
  ],
  cloudtrail_url: 'https://console.aws.amazon.com/cloudtrail/home?region=us-east-1#/events?EventName=CreatePolicy',
});

function tag(html: string, testid: string): string {
  const m = html.match(new RegExp(`<[a-z]+[^>]*data-testid="${testid}"[^>]*>`));
  if (!m) throw new Error(`no element ${testid}`);
  return m[0];
}

describe('containmentApi', () => {
  it('hits the four agent-scoped routes', async () => {
    const get = vi.spyOn(client, 'get').mockResolvedValue({ data: CONTAINED });
    const put = vi.spyOn(client, 'put').mockResolvedValue({ data: { enabled: false } });
    const post = vi.spyOn(client, 'post').mockResolvedValue({ data: CONTAINED });
    expect(await containmentApi.get('a-1')).toBe(CONTAINED);
    expect(get).toHaveBeenCalledWith('/api/v1/agents/a-1/containment');
    await containmentApi.setMonitor('a-1', false);
    expect(put).toHaveBeenCalledWith('/api/v1/agents/a-1/containment/monitor', { enabled: false });
    await containmentApi.contain('a-1', 'tool misuse');
    expect(post).toHaveBeenCalledWith('/api/v1/agents/a-1/containment/contain', { reason: 'tool misuse' });
    await containmentApi.restore('a-1', 'false positive');
    expect(post).toHaveBeenCalledWith('/api/v1/agents/a-1/containment/restore', { reason: 'false positive' });
  });
});

describe('Signal card', () => {
  const props = { monitorBusy: false, canToggleMonitor: true, onToggleMonitor: noop, onOpenTraces: noop };

  it('renders real signals with the tool, the judge explanation and the detected time', () => {
    const html = renderToStaticMarkup(
      createElement(SignalCard, {
        ...props,
        view: containmentView({
          signals: [{
            session_id: 's-1', trace_id: 't-1', span_id: 'sp-1', tool: 'claims___openClaim', label: 'FAIL',
            explanation: 'Hid a note for agent B in the claim description',
            span_end: '2026-10-16T10:42:00Z', detected_at: '2026-10-16T10:42:14Z', source: 'agentcore-evaluations',
          }],
        }),
      }),
    );
    expect(html).toContain('claims___openClaim');
    expect(html).toContain('Hid a note for agent B in the claim description');
    expect(html).toContain('Detected in 14 s');
    expect(html).toContain('Signal source: AgentCore Evaluations (on-demand)');
    expect(html).toContain('native online evaluation, Langfuse, Opik');
    expect(tag(html, 'containment-monitor')).toContain('aria-checked="true"');
  });

  it('renders escaped and real newlines in the explanation as line breaks', () => {
    const html = renderToStaticMarkup(
      createElement(SignalCard, {
        ...props,
        view: containmentView({
          signals: [{
            session_id: 's-1', trace_id: 't-1', span_id: 'sp-1', tool: 'claims___openClaim', label: 'FAIL',
            explanation: 'First line\\nSecond line\nThird line',
            span_end: '2026-10-16T10:42:00Z', detected_at: '2026-10-16T10:42:14Z', source: 'agentcore-evaluations',
          }],
        }),
      }),
    );
    expect(html).not.toContain('\\n');
    expect(html).toContain('First line\nSecond line\nThird line');
    expect(html).toContain('whitespace-pre-line');
  });

  it('says what the monitor does when there is nothing to show', () => {
    const html = renderToStaticMarkup(createElement(SignalCard, { ...props, view: containmentView() }));
    expect(html).toContain('No signals. The monitor judges every tool call this agent makes.');
  });
});

describe('Contain card', () => {
  const base = { view: containmentView(), actor: 'Lars Svensson', busy: false, canOperate: true, onReason: noop, onContain: noop };

  it('Contain stays disabled until the reason has 8 characters', () => {
    const short = renderToStaticMarkup(createElement(ContainCard, { ...base, reason: 'misuse' }));
    expect(tag(short, 'containment-contain')).toContain('disabled=""');
    const ok = renderToStaticMarkup(createElement(ContainCard, { ...base, reason: 'tool misuse on openClaim' }));
    expect(tag(ok, 'containment-contain')).not.toContain('disabled=""');
    expect(ok).toContain('Acting as');
    expect(ok).toContain('Lars Svensson');
    expect(ok).not.toContain('containment-contain-why');
  });

  it('the disabled reason is visible text tied to the button', () => {
    const short = renderToStaticMarkup(createElement(ContainCard, { ...base, reason: 'misuse' }));
    expect(tag(short, 'containment-contain')).toContain('aria-describedby="containment-contain-why"');
    expect(short).toMatch(/id="containment-contain-why"[^>]*>Give a reason \(at least 8 characters\)\.</);
  });

  it('a Viewer cannot contain or flip the monitor', () => {
    const html = renderToStaticMarkup(createElement(ContainCard, { ...base, canOperate: false, reason: 'tool misuse on openClaim' }));
    expect(tag(html, 'containment-contain')).toContain('disabled=""');
    expect(html).toContain('Contain needs the Operator role.');
    const sig = renderToStaticMarkup(createElement(SignalCard, { view: containmentView(), monitorBusy: false, canToggleMonitor: false, onToggleMonitor: noop, onOpenTraces: noop }));
    expect(tag(sig, 'containment-monitor')).toContain('disabled=""');
  });

  it('cannot contain twice', () => {
    const html = renderToStaticMarkup(createElement(ContainCard, { ...base, view: CONTAINED, reason: 'tool misuse on openClaim' }));
    expect(tag(html, 'containment-contain')).toContain('disabled=""');
  });
});

describe('Progress card', () => {
  it('renders the server statuses of the three steps', () => {
    const html = renderToStaticMarkup(createElement(ProgressCard, { view: CONTAINED }));
    for (const id of ['refuse-invokes', 'cedar-forbid', 'runtime-deny']) {
      expect(html).toContain(`data-testid="containment-step-${id}"`);
    }
    expect(tag(html, 'containment-step-refuse-invokes')).toContain('data-status="ok"');
    expect(tag(html, 'containment-step-runtime-deny')).toContain('data-status="failed"');
    expect(tag(html, 'containment-step-cedar-forbid')).toContain('data-status="ok"');
    expect(html).not.toContain('stop-sessions');
    expect(html).toContain('Running tasks lose their tools on their next call.');
    expect(html).toContain('AccessDenied');
    expect(html).toContain('Gateway policy (Cedar)');
  });
});

describe('Investigate card', () => {
  const base = { view: CONTAINED, restoreReason: 'false positive, reviewed', busy: false, canAdmin: true, onRestoreReason: noop, onRestore: noop, onOpenTraces: noop };

  it('the person who contained cannot restore, and the tooltip says why', () => {
    const html = renderToStaticMarkup(createElement(InvestigateCard, { ...base, user: { oid: 'oid-lars', email: 'lars@example.com' } }));
    const btn = tag(html, 'containment-restore');
    expect(btn).toContain('disabled=""');
    expect(btn).toContain('title="Restore requires a different person than the one who contained the agent."');
    expect(btn).toContain('aria-describedby="containment-restore-why"');
    expect(html).toMatch(/id="containment-restore-why"[^>]*>Restore requires a different person than the one who contained the agent\.</);
  });

  it('an Operator cannot restore: the visible reason says Admin', () => {
    const html = renderToStaticMarkup(createElement(InvestigateCard, { ...base, canAdmin: false, user: { oid: 'oid-jannis', email: 'j@example.com' } }));
    expect(tag(html, 'containment-restore')).toContain('disabled=""');
    expect(html).toContain('Restore needs the Admin role.');
  });

  it('a second person can restore', () => {
    const html = renderToStaticMarkup(createElement(InvestigateCard, { ...base, user: { oid: 'oid-jannis', email: 'jkambach@example.com' } }));
    expect(tag(html, 'containment-restore')).not.toContain('disabled=""');
  });

  it('shows the audit rows and the CloudTrail link on the Cedar row', () => {
    const html = renderToStaticMarkup(createElement(InvestigateCard, { ...base, user: null }));
    expect(html).toContain('Contain · Gateway policy (Cedar)');
    expect(html).toContain('tool misuse on openClaim');
    expect(tag(html, 'containment-cloudtrail')).toContain('EventName=CreatePolicy');
    expect(html).toContain('Tool calls and invokes are CloudTrail data events (off by default)');
  });

  it('renders the audit newest first', () => {
    const html = renderToStaticMarkup(createElement(InvestigateCard, { ...base, user: null }));
    expect(html.indexOf('Contain · Gateway policy (Cedar)')).toBeLessThan(html.indexOf('>Contain<'));
  });
});

describe('after Restore: wording, the confirm and ARN masking', () => {
  const ARN = 'arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/claims_rogue_dev-O6o1UGFR4c';
  const RESTORED = containmentView({
    ...CONTAINED,
    state: 'RESTORED',
    job: 'restored',
    steps: [
      { id: 'refuse-invokes', status: 'ok', detail: 'AGP accepts invokes again', native_ref: null, at: '2026-10-16T11:00:06Z' },
      { id: 'cedar-forbid', status: 'ok', detail: 'mcp-1: deleted (pol-1)', native_ref: null, at: '2026-10-16T11:00:03Z' },
      { id: 'runtime-deny', status: 'ok', detail: `${ARN}: ok`, native_ref: ARN, at: '2026-10-16T11:00:05Z' },
    ],
    audit: [
      ...CONTAINED.audit,
      { at: '2026-10-16T11:00:00Z', actor_name: 'jkambach', action: 'restore', step: null, result: 'requested', detail: 'false positive', native_ref: null },
      { at: '2026-10-16T11:00:05Z', actor_name: 'jkambach', action: 'restore', step: 'runtime-deny', result: 'ok', detail: `${ARN}: ok`, native_ref: ARN },
    ],
  });

  it('after Restore the progress card uses restore wording and says "Restored in N s"', () => {
    const html = renderToStaticMarkup(createElement(ProgressCard, { view: RESTORED }));
    expect(html).toContain('Restored in 6 s');
    expect(html).toContain('AGP accepts invokes again');
    expect(html).toContain('Gateway forbid removed');
    expect(html).toContain('Runtime accepts direct invokes again');
    expect(html).not.toContain('Refuse invokes through AGP (409)');
    expect(html).not.toContain('Forbid every tool call');
    expect(html).not.toContain('Running tasks lose their tools');
  });

  it('Restore opens a confirm that names the lifted controls and the audit record', () => {
    expect(tabSrc).toMatch(/onClick=\{\(\) => setConfirming\(true\)\}/);
    expect(tabSrc).toContain('Restore this agent?');
    expect(tabSrc).toMatch(/Restore lifts all \$\{STEP_ORDER\.length\} controls: \$\{STEP_ORDER\.map\(stepControl\)/);
    expect(tabSrc).toContain('Your name and reason go on the audit record.');
    expect(tabSrc).toMatch(/setConfirming\(false\);\s*props\.onRestore\(\);/);
  });

  it('ARNs show with the account id masked; the full value stays in the title', () => {
    const progress = renderToStaticMarkup(createElement(ProgressCard, { view: RESTORED }));
    const audit = renderToStaticMarkup(createElement(InvestigateCard, { view: RESTORED, user: null, restoreReason: '', busy: false, canAdmin: true, onRestoreReason: noop, onRestore: noop, onOpenTraces: noop }));
    for (const html of [progress, audit]) {
      expect(html).toContain('arn:aws:bedrock-agentcore:us-east-1:••••:runtime/claims_rogue_dev-O6o1UGFR4c');
      expect(html.replace(/title="[^"]*"/g, '')).not.toContain('123456789012');
      expect(html).toContain(`title="${ARN}`);
    }
  });
});

describe('no simulation left', () => {
  it('drops the preview clock, the sample incident and the Planned banner; reads the live view and polls on one setTimeout chain, never setInterval', () => {
    expect(tabRaw).not.toMatch(/PLAYBACK_SPEED|progressAt|sampleSignals|SampleBadge|PlannedBanner|run in preview/);
    expect(tabSrc).toMatch(/containmentApi\.get\(/);
    expect(tabRaw).not.toMatch(/setInterval/);
  });
});

describe('invoke panel refusals', () => {
  it('reads the kind off the failure: contained is the red card pointing at Restore, unassigned the Entra access card; no sentence is parsed', () => {
    expect(invokeSrc).toMatch(/const refusal = failure === null \? null : classifyInvokeRefusal\(failure\);/);
    expect(invokeSrc).toMatch(/setBlocked\(refusal === 'unassigned'\);/);
    expect(invokeSrc).toMatch(/setContained\(refusal === 'contained'\);/);
    expect(invokeSrc).toContain('This agent is contained. Restore it on the Containment tab.');
    expect(invokeSrc).toContain('You don’t have access to this agent');
    expect(invokeSrc).not.toMatch(/\.test\(message\)/);
    expect(invokeSrc).not.toContain('errorStatus');
  });
});
