const ARN_ACCOUNT = /\b(arn:[a-z0-9-]+:[a-z0-9-]*:[a-z0-9-]*:)(\d{12})(?=:)/g;

export function maskArn(value: string): string {
  return value.replace(ARN_ACCOUNT, (_m, head: string, account: string) => `${head}••••${account.slice(-4)}`);
}
