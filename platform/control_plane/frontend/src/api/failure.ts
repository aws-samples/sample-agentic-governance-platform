import type { ErrorBody, HttpValidationError, ValidationError } from './openapi';

export type ErrorCode = NonNullable<ErrorBody['code']>;
export type ErrorDetail = ErrorBody['detail'] | HttpValidationError['detail'] | undefined;

export class ApiFailure extends Error {
  readonly status: number;
  readonly code: ErrorCode | null;
  readonly detail: ErrorDetail;
  constructor(status: number, code: ErrorCode | null, detail: ErrorDetail, message: string) {
    super(message);
    this.name = 'ApiFailure';
    this.status = status;
    this.code = code;
    this.detail = detail;
  }
}

export function isApiFailure(value: unknown): value is ApiFailure {
  return value instanceof ApiFailure;
}

export function formatApiErrorDetail(detail: ErrorDetail, fallback: string): string {
  if (typeof detail === 'string' && detail.length > 0) return detail;
  if (
    detail && typeof detail === 'object' && !Array.isArray(detail)
    && typeof detail.message === 'string'
  ) {
    const errs = Array.isArray(detail.errors) ? detail.errors.map(String) : [];
    return errs.length > 0 ? `${detail.message}: ${errs.join('; ')}` : detail.message;
  }
  if (Array.isArray(detail) && detail.length > 0) {
    return detail
      .map((item: ValidationError) => {
        const field = item.loc.filter((p) => p !== 'body').pop();
        return field ? `${field}: ${item.msg}` : item.msg;
      })
      .join('; ');
  }
  return fallback;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function isValidationError(item: unknown): item is ValidationError {
  return isRecord(item) && Array.isArray(item.loc) && typeof item.msg === 'string' && typeof item.type === 'string';
}

function detailOf(value: unknown): ErrorDetail {
  if (typeof value === 'string' || isRecord(value)) return value;
  if (Array.isArray(value) && value.every(isValidationError)) return value;
  return undefined;
}

export function errorBodyOf(data: unknown): { detail: ErrorDetail; code: ErrorCode | null } {
  if (!isRecord(data)) return { detail: undefined, code: null };
  return { detail: detailOf(data.detail), code: typeof data.code === 'string' ? (data.code as ErrorCode) : null };
}
