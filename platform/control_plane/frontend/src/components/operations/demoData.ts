export interface ModelOption {
  id: string;
  label: string;
  provider: 'Anthropic' | 'Google' | 'OpenAI';
  inputPer1k: number;
  outputPer1k: number;
}

export const MODELS: ModelOption[] = [
  { id: 'claude-opus-4-8', label: 'Claude Opus 4.8', provider: 'Anthropic', inputPer1k: 0.015, outputPer1k: 0.075 },
  { id: 'gemini-2-5-pro', label: 'Gemini 2.5 Pro', provider: 'Google', inputPer1k: 0.00125, outputPer1k: 0.005 },
  { id: 'gpt-5', label: 'GPT-5', provider: 'OpenAI', inputPer1k: 0.00125, outputPer1k: 0.01 },
];

export interface Tenant {
  id: string;
  account: string;
  name: string;
  businessUnit: string;
  region: string;
  kind: 'sandbox' | 'production';
  status: 'active' | 'provisioning';
}

export const TENANTS: Tenant[] = [
  { id: 'aws://4471-2093-8856', account: 'aws://4471-2093-8856', name: 'Claims Sandbox', businessUnit: 'Insurance', region: 'us-east-1', kind: 'sandbox', status: 'active' },
  { id: 'aws://4471-2093-9921', account: 'aws://4471-2093-9921', name: 'FNOL Sandbox', businessUnit: 'Insurance', region: 'us-west-2', kind: 'sandbox', status: 'active' },
  { id: 'aws://5582-3104-7733', account: 'aws://5582-3104-7733', name: 'Claims Production', businessUnit: 'Insurance', region: 'us-east-1', kind: 'production', status: 'active' },
  { id: 'aws://5582-3104-8810', account: 'aws://5582-3104-8810', name: 'Contact Center Production', businessUnit: 'Customer Operations', region: 'eu-west-1', kind: 'production', status: 'provisioning' },
];

export function estimateCost(model: ModelOption, inTok: number, outTok: number): number {
  return (inTok / 1000) * model.inputPer1k + (outTok / 1000) * model.outputPer1k;
}
