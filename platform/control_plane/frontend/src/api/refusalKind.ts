import type { ApiFailure, ErrorCode } from './failure';

export type RefusalCodes<K extends string> = Readonly<Record<K, readonly ErrorCode[]>>;

function isKindOf<K extends string>(table: RefusalCodes<K>, key: string): key is K {
  return Object.prototype.hasOwnProperty.call(table, key);
}

export function refusalKind<K extends string>(table: RefusalCodes<K>, failure: ApiFailure): K | null {
  const code = failure.code;
  if (code === null) return null;
  for (const key of Object.keys(table)) {
    if (isKindOf(table, key) && table[key].includes(code)) return key;
  }
  return null;
}
