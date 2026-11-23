// Decision: docs/adr/frontend-loading-state.md
import { useCallback, useEffect, useRef, useState } from 'react';

import { failureOf, type LoadState, patchAction, patchSettled, resolveLoadState, type Settled } from './loadState';

const DEFAULT_ERROR_TEXT = 'Request failed';

export interface Load<T> {
  state: LoadState<T>;
  reload: () => void;
  patch: (update: (data: T) => T) => void;
}

function carriedData<T>(previous: Settled<T> | null): T | null {
  if (previous === null) return null;
  return previous.outcome.kind === 'ready' ? previous.outcome.data : previous.lastData;
}

export function useLoad<T>(load: () => Promise<T>, deps: readonly unknown[], errorText = DEFAULT_ERROR_TEXT): Load<T> {
  const [settled, setSettled] = useState<Settled<T> | null>(null);
  const [nonce, setNonce] = useState(0);
  const state = resolveLoadState(settled, deps, nonce);
  const latest = useRef({ deps, nonce, state });
  latest.current = { deps, nonce, state };
  const reloadAfterSettle = useRef(false);
  const reload = useCallback(() => setNonce((n) => n + 1), []);

  useEffect(() => {
    let cancelled = false;
    load()
      .then((data) => {
        if (cancelled) return;
        setSettled({ deps, nonce, outcome: { kind: 'ready', data }, lastData: data });
        if (reloadAfterSettle.current) {
          reloadAfterSettle.current = false;
          reload();
        }
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        setSettled((previous) => ({
          deps,
          nonce,
          outcome: { kind: 'error', ...failureOf(err, errorText) },
          lastData: carriedData(previous),
        }));
        if (reloadAfterSettle.current) {
          reloadAfterSettle.current = false;
          reload();
        }
      });
    return () => {
      cancelled = true;
      reloadAfterSettle.current = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps -- useLoad forwards its caller's deps array, which the rule checks at the call site through additionalHooks; expires 2027-11-04; added E54/T2
  }, [...deps, nonce]);

  const patch = useCallback(
    (update: (data: T) => T) => {
      const current = latest.current;
      switch (patchAction(current.state.kind)) {
        case 'apply':
          setSettled((s) => patchSettled(s, current.deps, current.nonce, update));
          break;
        case 'defer':
          reloadAfterSettle.current = true;
          break;
        case 'reload':
          reload();
          break;
      }
    },
    [reload],
  );

  return { state, reload, patch };
}
