export interface InvokeStageChoice {
  stages: string[];
  showSelector: boolean;
  /** `undefined` means the Run sends no `?stage=` and the backend resolves the scalar itself. */
  defaultStage: string | undefined;
}

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
