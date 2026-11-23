import { useCallback, useEffect, useState, type RefObject } from 'react';

/**
 * Returns a function that sends focus back to `target` once the render it requests has committed. Call it
 * in the same handler that closes the picker or dialog: the element `target` points at is focused after the
 * commit that mounts it again, however late React renders that commit. Each call focuses once; if `target`
 * is not mounted in that commit, the request lapses.
 */
export function useReturnFocus(target: RefObject<HTMLElement | null>): () => void {
  const [request, setRequest] = useState(0);

  useEffect(() => {
    if (request === 0) return;
    target.current?.focus();
  }, [request, target]);

  return useCallback(() => setRequest((n) => n + 1), []);
}
