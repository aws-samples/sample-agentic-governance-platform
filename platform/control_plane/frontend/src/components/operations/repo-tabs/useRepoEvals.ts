import { useCallback, useEffect, useRef, useState } from 'react';

import { evalsApi, type EvalFiles, type EvalRun, type EvalRuns } from '../../../api/client';
import {
  POLL_MS,
  currentBuild as currentBuildOf,
  devEvalState,
  isRunActive,
  runBlockedReason,
  shouldAutoRun,
  shouldPoll,
  type DevEvalState,
} from './evaluationsLogic';

const autoRequested = new Set<string>();

const errText = (e: unknown): string => (e instanceof Error ? e.message : 'Something went wrong');

export interface RepoEvals {
  files: EvalFiles | null;
  filesError: string | null;
  runs: EvalRuns | null;
  runsError: string | null;
  now: number;
  live: boolean;
  starting: boolean;
  actionError: string | null;
  currentBuild: string | null;
  itemCount: number;
  fileErrors: number;
  blocked: string | null;
  devEval: DevEvalState | null;
  loadFiles: () => Promise<void>;
  startManual: () => Promise<EvalRun | null>;
}

/** Null ids read nothing; `mayWrite` gates the auto run; `onLiveChange` fires on each live edge so the page can re-read the eval gate. */
export function useRepoEvals(
  projectId: string | null,
  repoId: string | null,
  mayWrite: boolean,
  onLiveChange?: () => void,
): RepoEvals {
  const [files, setFiles] = useState<EvalFiles | null>(null);
  const [filesError, setFilesError] = useState<string | null>(null);
  const [runs, setRuns] = useState<EvalRuns | null>(null);
  const [runsError, setRunsError] = useState<string | null>(null);
  const [starting, setStarting] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [now, setNow] = useState(() => Date.now());

  const loadFiles = useCallback(async () => {
    if (!projectId || !repoId) return;
    try {
      setFiles(await evalsApi.get(projectId, repoId));
      setFilesError(null);
    } catch (e) {
      setFilesError(errText(e));
    }
  }, [projectId, repoId]);

  const loadRuns = useCallback(async () => {
    if (!projectId || !repoId) return;
    try {
      setRuns(await evalsApi.listRuns(projectId, repoId));
      setRunsError(null);
    } catch (e) {
      setRunsError(errText(e));
    }
    setNow(Date.now());
  }, [projectId, repoId]);

  useEffect(() => {
    void loadFiles();
    void loadRuns();
  }, [loadFiles, loadRuns]);

  const live = runs !== null && shouldPoll(runs.runs, runs.current_run, now);
  useEffect(() => {
    if (!live) return;
    const t = setInterval(() => void loadRuns(), POLL_MS);
    return () => clearInterval(t);
  }, [live, loadRuns]);
  const wasLive = useRef<boolean | null>(null);
  useEffect(() => {
    if (wasLive.current !== null && wasLive.current !== live) onLiveChange?.();
    wasLive.current = live;
  }, [live, onLiveChange]);

  const itemCount = files?.items.length ?? 0;
  const fileErrors = files?.errors.length ?? 0;
  const currentBuild = currentBuildOf(runs);

  const startManual = useCallback(async (): Promise<EvalRun | null> => {
    if (!projectId || !repoId) return null;
    setStarting(true);
    setActionError(null);
    try {
      const run = await evalsApi.startRun(projectId, repoId, 'manual');
      await loadRuns();
      return run ?? null;
    } catch (e) {
      setActionError(errText(e));
      return null;
    } finally {
      setStarting(false);
    }
  }, [projectId, repoId, loadRuns]);

  useEffect(() => {
    if (!projectId || !repoId || files === null || runs === null) return;
    const key = `${repoId}:${currentBuild}`;
    const requested = new Set(autoRequested.has(key) && currentBuild ? [currentBuild] : []);
    const go = shouldAutoRun({
      currentBuild,
      currentRun: runs.current_run,
      itemCount,
      fileErrors,
      mayWrite,
      anyActive: live || starting,
      requested,
    });
    if (!go) return;
    autoRequested.add(key);
    void evalsApi
      .startRun(projectId, repoId, 'auto')
      .then(() => loadRuns())
      .catch(() => undefined);
  }, [files, runs, currentBuild, live, starting, itemCount, fileErrors, mayWrite, projectId, repoId, loadRuns]);

  const blocked = runBlockedReason({
    mayWrite,
    itemCount,
    fileErrors,
    currentBuild,
    active: starting || isRunActive(runs?.current_run, now),
  });

  return {
    files, filesError, runs, runsError, now, live, starting, actionError, currentBuild,
    itemCount, fileErrors, blocked, devEval: devEvalState(runs, now), loadFiles, startManual,
  };
}
