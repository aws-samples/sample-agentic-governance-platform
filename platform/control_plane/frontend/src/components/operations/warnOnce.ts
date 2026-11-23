const warned = new Set<string>();

export function warnOnce(message: string, raw: string): void {
  const key = JSON.stringify([message, raw]);
  if (warned.has(key)) return;
  warned.add(key);
  console.warn(message, raw);
}
