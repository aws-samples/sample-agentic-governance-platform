import { AxiosError, AxiosHeaders, type AxiosResponse } from 'axios';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { AUTH_401_HALT_MESSAGE, AUTH_401_RELOAD_MESSAGE } from './authRetry';
import { rejectResponse } from './client';
import { ApiFailure, formatApiErrorDetail, isApiFailure } from './failure';
import type { ValidationError } from './openapi';

function validationError(overrides: Partial<ValidationError> = {}): ValidationError {
  return {
    loc: ['body'],
    msg: 'Field required',
    type: 'missing',
    ...overrides,
  };
}

describe('formatApiErrorDetail — readable text for every backend `detail` shape', () => {
  it('passes a plain string detail through unchanged', () => {
    expect(formatApiErrorDetail('Agent not found', 'fallback')).toBe('Agent not found');
  });

  it('falls back to the given message when detail is missing or empty', () => {
    expect(formatApiErrorDetail(undefined, 'network error')).toBe('network error');
    expect(formatApiErrorDetail('', 'network error')).toBe('network error');
    expect(formatApiErrorDetail([], 'network error')).toBe('network error');
  });

  it('joins a validation-error list into readable field: message text', () => {
    const detail = [
      validationError({ loc: ['body', 'agent_arn'], msg: 'agent_arn is malformed or names an invalid AWS region' }),
      validationError({ loc: ['body', 'name'], msg: 'field required' }),
    ];
    expect(formatApiErrorDetail(detail, 'fallback')).toBe(
      'agent_arn: agent_arn is malformed or names an invalid AWS region; name: field required',
    );
  });

  it('drops the "body" segment of loc and names the field by the last remaining segment, the one at fault', () => {
    const detail = [validationError({ loc: ['body', 'nested', 'field'], msg: 'bad value' })];
    expect(formatApiErrorDetail(detail, 'fallback')).toBe('field: bad value');
  });

  it('falls back to the bare message when loc has no field segment', () => {
    const detail = [validationError({ loc: ['body'], msg: 'malformed request' })];
    expect(formatApiErrorDetail(detail, 'fallback')).toBe('malformed request');
  });

  it('joins an eval file-error object as "message: error; error", keeping the file and line each error carries', () => {
    const detail = {
      message: 'the eval files are invalid',
      errors: ['evals/evals.yaml:3: unknown key', 'evals/dataset.jsonl:7: missing id'],
    };
    expect(formatApiErrorDetail(detail, 'fallback')).toBe(
      'the eval files are invalid: evals/evals.yaml:3: unknown key; evals/dataset.jsonl:7: missing id',
    );
  });

  it('returns the bare message of a file-error object that lists no errors', () => {
    expect(formatApiErrorDetail({ message: 'the dataset is empty' }, 'fallback')).toBe(
      'the dataset is empty',
    );
  });
});

function responseError(status: number, data: unknown): AxiosError {
  const config = { headers: new AxiosHeaders() };
  const response: AxiosResponse = { data, status, statusText: '', headers: {}, config };
  return new AxiosError(`Request failed with status code ${status}`, AxiosError.ERR_BAD_REQUEST, config, undefined, response);
}

function rejectionOf(error: AxiosError): Promise<unknown> {
  return rejectResponse(error).catch((e: unknown) => e);
}

describe('the response interceptor rejects every non-401 failure as an ApiFailure', () => {
  it('a body without a code rejects with a null code and the detail as message', async () => {
    const failure = await rejectionOf(responseError(404, { detail: 'Agent not found' }));
    expect(failure).toBeInstanceOf(ApiFailure);
    expect(failure).toMatchObject({ status: 404, code: null, detail: 'Agent not found', message: 'Agent not found' });
  });

  it('a body with a code keeps the code and the status', async () => {
    const failure = await rejectionOf(responseError(409, { detail: 'An agent named x exists', code: 'agent.name_taken' }));
    expect(failure).toMatchObject({
      status: 409,
      code: 'agent.name_taken',
      detail: 'An agent named x exists',
      message: 'An agent named x exists',
    });
  });

  it('a validation 422 keeps the formatted join as message and a null code', async () => {
    const detail = [
      validationError({ loc: ['body', 'name'], msg: 'field required' }),
      validationError({ loc: ['body', 'agent_arn'], msg: 'malformed' }),
    ];
    const failure = await rejectionOf(responseError(422, { detail }));
    expect(failure).toMatchObject({ status: 422, code: null, detail, message: 'name: field required; agent_arn: malformed' });
  });

  it('a non-JSON body rejects with a null code, no detail and the transport message', async () => {
    const failure = await rejectionOf(responseError(502, '<html>Bad Gateway</html>'));
    expect(failure).toMatchObject({ status: 502, code: null, detail: undefined, message: 'Request failed with status code 502' });
  });

  it('a network failure with no response is status 0 with the transport message', async () => {
    const failure = await rejectionOf(new AxiosError('Network Error', AxiosError.ERR_NETWORK, { headers: new AxiosHeaders() }));
    expect(failure).toBeInstanceOf(ApiFailure);
    expect(failure).toMatchObject({ status: 0, code: null, detail: undefined, message: 'Network Error' });
  });

  it('a detail array whose items are not validation errors rejects with the transport message, not a TypeError', async () => {
    const failure = await rejectionOf(responseError(422, { detail: ['name', 7] }));
    expect(failure).toBeInstanceOf(ApiFailure);
    expect(failure).toMatchObject({ status: 422, code: null, detail: undefined, message: 'Request failed with status code 422' });
  });
});

function unauthorizedAfterRetry(): AxiosError {
  const config = { headers: new AxiosHeaders(), _agpForceRefreshRetried: true };
  const response: AxiosResponse = { data: { detail: 'Not authenticated' }, status: 401, statusText: '', headers: {}, config };
  return new AxiosError('Request failed with status code 401', AxiosError.ERR_BAD_REQUEST, config, undefined, response);
}

async function firstUnauthorizedRejection(): Promise<{ failure: unknown; FreshApiFailure: typeof ApiFailure }> {
  vi.resetModules();
  const { rejectResponse: freshReject } = await import('./client');
  const { ApiFailure: FreshApiFailure } = await import('./failure');
  const failure = await freshReject(unauthorizedAfterRetry()).catch((e: unknown) => e);
  return { failure, FreshApiFailure };
}

describe('the 401 arm rejects an ApiFailure that carries the copy and no code', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.resetModules();
  });

  it('the reload copy on the first 401 of the session, after the reload it authorises', async () => {
    const reload = vi.fn();
    const removeItem = vi.fn();
    vi.stubGlobal('window', { location: { reload } });
    vi.stubGlobal('localStorage', { getItem: () => null, setItem: () => undefined, removeItem });
    const { failure, FreshApiFailure } = await firstUnauthorizedRejection();
    expect(failure).toBeInstanceOf(FreshApiFailure);
    expect(failure).toMatchObject({ status: 401, code: null, detail: undefined, message: AUTH_401_RELOAD_MESSAGE });
    expect(removeItem).toHaveBeenCalledWith('auth_token');
    expect(removeItem).toHaveBeenCalledTimes(1);
    expect(reload).toHaveBeenCalledTimes(1);
  });

  it('the halt copy once the session has spent its reload', async () => {
    vi.stubGlobal('sessionStorage', { getItem: () => '1', setItem: () => undefined, removeItem: () => undefined });
    const { failure, FreshApiFailure } = await firstUnauthorizedRejection();
    expect(failure).toBeInstanceOf(FreshApiFailure);
    expect(failure).toMatchObject({ status: 401, code: null, detail: undefined, message: AUTH_401_HALT_MESSAGE });
  });

  it('a localStorage that refuses access still reloads and rejects the reload copy', async () => {
    const reload = vi.fn();
    const removeItem = vi.fn(() => { throw new Error('denied'); });
    vi.stubGlobal('window', { location: { reload } });
    vi.stubGlobal('localStorage', { removeItem });
    const { failure, FreshApiFailure } = await firstUnauthorizedRejection();
    expect(failure).toBeInstanceOf(FreshApiFailure);
    expect(failure).toMatchObject({ status: 401, code: null, message: AUTH_401_RELOAD_MESSAGE });
    expect(removeItem).toHaveBeenCalledTimes(1);
    expect(reload).toHaveBeenCalledTimes(1);
  });
});

describe('isApiFailure', () => {
  it('is true for an ApiFailure and false for an Error or a string', () => {
    expect(isApiFailure(new ApiFailure(404, null, 'Agent not found', 'Agent not found'))).toBe(true);
    expect(isApiFailure(new Error('Agent not found'))).toBe(false);
    expect(isApiFailure('Agent not found')).toBe(false);
  });
});
