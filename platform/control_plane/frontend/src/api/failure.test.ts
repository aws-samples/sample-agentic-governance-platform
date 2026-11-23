import { describe, expect, it } from 'vitest';
import { errorBodyOf } from './failure';

describe('errorBodyOf — the interceptor reads an unknown body without trusting its shape', () => {
  it('keeps a string detail', () => {
    expect(errorBodyOf({ detail: 'Agent not found' })).toEqual({ detail: 'Agent not found', code: null });
  });

  it('keeps an object detail as the same object, the eval file-error shape included', () => {
    const detail = { message: 'the eval files are invalid', errors: ['evals/evals.yaml:3: unknown key'] };
    expect(errorBodyOf({ detail }).detail).toBe(detail);
    const other = { reason: 'kept as sent' };
    expect(errorBodyOf({ detail: other }).detail).toBe(other);
  });

  it('keeps a validation-error array, the empty one included', () => {
    const detail = [{ loc: ['body', 'name'], msg: 'field required', type: 'missing' }];
    expect(errorBodyOf({ detail }).detail).toBe(detail);
    const empty: unknown[] = [];
    expect(errorBodyOf({ detail: empty }).detail).toBe(empty);
  });

  it('drops an array whose items are not validation errors, without throwing', () => {
    expect(() => errorBodyOf({ detail: ['name', 7] })).not.toThrow();
    expect(errorBodyOf({ detail: ['name', 7] })).toEqual({ detail: undefined, code: null });
    expect(errorBodyOf({ detail: [null] }).detail).toBeUndefined();
    expect(errorBodyOf({ detail: [{ loc: 'body', msg: 'x' }] }).detail).toBeUndefined();
    expect(errorBodyOf({ detail: [{ loc: ['body'], msg: 7 }] }).detail).toBeUndefined();
    expect(errorBodyOf({ detail: [{ loc: ['body', 'name'], msg: 'field required' }] }).detail).toBeUndefined();
    expect(errorBodyOf({ detail: [{ loc: ['body', 'name'], msg: 'ok', type: 'missing' }, 'x'] }).detail).toBeUndefined();
  });

  it('gives no detail and no code for a missing, empty or non-object body', () => {
    for (const body of [undefined, null, '', '<html>Bad Gateway</html>', 502, [], {}]) {
      expect(errorBodyOf(body), String(body)).toEqual({ detail: undefined, code: null });
    }
    expect(errorBodyOf({ detail: null }).detail).toBeUndefined();
  });

  it('takes a string code and gives null for an absent or non-string one', () => {
    expect(errorBodyOf({ detail: 'An agent named x exists', code: 'agent.name_taken' })).toEqual({
      detail: 'An agent named x exists',
      code: 'agent.name_taken',
    });
    expect(errorBodyOf({ detail: 'x' }).code).toBeNull();
    expect(errorBodyOf({ detail: 'x', code: 7 }).code).toBeNull();
    expect(errorBodyOf({ detail: 'x', code: null }).code).toBeNull();
  });
});
