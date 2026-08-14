// invokeStage — WHICH RUNTIME the Test-invoke panel's Run button reaches (E36/T2, fix round 1).
//
// Pure, framework-free, `.test.ts`-pinned, and it takes two primitives rather than an `Agent`
// so it stays importable from a test without axios — the `opsStatus.ts` / `cedarPosture.ts`
// precedent in this same tree. vitest collects only `src/**/*.test.ts`, so `InvokePanel.tsx`
// is build-gated while the decision it renders is test-gated.
//
// ---------------------------------------------------------------------------
// WHY THIS MODULE EXISTS
//
// T2 added `?stage=` to `POST /agents/{id}/invoke` and a selector to the panel. The first
// implementation pre-selected `Object.keys(agent_arns).sort()[0]` — the ALPHABETICALLY FIRST
// stage. For a stage set like `{"prod","staging"}` (legal: `stage` is free-form by D8, and
// `models/repository.py` even defaults a stage field to `"prod"`) that made **prod** the
// no-interaction default of a panel labelled *Test invoke*, where before T2 the same click
// reached whatever the `agent_arn` scalar named — "whichever stage deployed last".
//
// So an additive parameter silently RE-POINTED the panel's default. The fix is to derive the
// default from evidence the record already holds instead of from alphabetical accident: the
// stage whose ARN IS the scalar. That mirrors the sanctioned server-side derivation in
// `services/agent_identity_service.py` (`next((s for s, a in arns.items() if a == arn), …)`,
// how `runtime_status` reports which stage a stage-less probe actually read), and it obeys the
// repo's standing rule that a stage must never be GUESSED — `resolve_runtime_arns` refuses to
// caption a scalar `"dev"` because "naming it `dev` would be a fabrication", and the FE's
// `runtimeScope` treats any non-`unknown` stage as proof a reading is attributable.
//
// ---------------------------------------------------------------------------
// WHY THE DEFAULT CAN BE ABSENT
//
// `undefined` means "send no `?stage=`", and that is a real answer, not a gap:
//
//   • ONE STAGE OR NONE has nothing to choose. A legacy record (absent/empty map) owns one
//     runtime nobody can attribute to a stage — offering it a stage would invent evidence and
//     the backend answers `404 unknown stage` for exactly that reason. The published contract
//     is that the panel passes a stage when the agent owns MORE THAN ONE runtime, so these
//     calls stay stage-less and the backend keeps its pre-E36 behaviour byte for byte.
//
//   • A SCALAR THAT NAMES NO MAP ENTRY (or no scalar at all) is unattributable. Falling back
//     to a key would reintroduce the very guess this module removes, so we omit the parameter
//     and let the backend resolve the scalar itself, which is precisely the pre-T2 target.
// ---------------------------------------------------------------------------

/** The panel's stage decision — options, whether to offer them, and the no-interaction target. */
export interface InvokeStageChoice {
  /**
   * The agent's own stage keys, ALPHABETICALLY ORDERED — the selector's options, and the only
   * stages the panel may send. Keyed off `agent_arns` alone, never the `agent_arn` scalar.
   * Empty for a legacy record.
   */
  stages: string[];
  /** Render the selector only for MORE THAN ONE runtime: the ambiguous case `?stage=` exists for. */
  showSelector: boolean;
  /**
   * The stage a Run sends when the operator has not touched the selector — the stage the
   * `agent_arn` scalar names, i.e. the runtime a stage-less invoke would have reached anyway.
   * `undefined` ⇒ SEND NO `?stage=` (see "why the default can be absent" above).
   */
  defaultStage: string | undefined;
}

/**
 * Decide the panel's stage options and its no-interaction target from the agent record.
 *
 * Both inputs are taken straight off the wire shape (`Agent.agent_arns` / `Agent.agent_arn`),
 * including their `null | undefined` variants, so no caller has to pre-clean them.
 *
 * The scalar must be non-empty to attribute anything: an empty `agent_arn` names no runtime,
 * and matching it against an equally empty map entry would caption a corrupt record with a
 * stage. Such a record keeps the stage-less path, where the route's own `502 agent ARN is
 * malformed` guard reports it honestly.
 */
export function invokeStageChoice(
  agentArns: Record<string, string> | null | undefined,
  agentArn: string | null | undefined,
): InvokeStageChoice {
  const arns = agentArns ?? {};
  const stages = Object.keys(arns).sort();
  const showSelector = stages.length > 1;
  const defaultStage =
    showSelector && agentArn ? stages.find((s) => arns[s] === agentArn) : undefined;
  return { stages, showSelector, defaultStage };
}
