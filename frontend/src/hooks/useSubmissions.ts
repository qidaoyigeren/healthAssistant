import { useCallback, useEffect, useState, useSyncExternalStore } from 'react';
import { submissions } from '../api/submissions';
import type { SubmissionTask } from '../api/submissions';
import type { EventRequest } from '../api/types';

const SESSION_KEY = 'mcp.sessionId.v1';
const EMPTY_TASKS: SubmissionTask[] = [];

function loadSessionId(): string {
  try {
    const existing = window.sessionStorage.getItem(SESSION_KEY);
    if (existing) return existing;
    const created = `web-${crypto.randomUUID()}`;
    window.sessionStorage.setItem(SESSION_KEY, created);
    return created;
  } catch {
    return `web-${crypto.randomUUID()}`;
  }
}

/** 当前会话标识(每个浏览器标签页一个;「开启新会话」会更换它)。 */
export function useSessionId(): [string, (next?: string) => string] {
  const [sessionId, setSessionId] = useState<string>(loadSessionId);
  const newSession = useCallback((next?: string) => {
    const id = next ?? `web-${crypto.randomUUID()}`;
    try {
      window.sessionStorage.setItem(SESSION_KEY, id);
    } catch {
      // 忽略:内存态仍会更新
    }
    setSessionId(id);
    return id;
  }, []);
  return [sessionId, newSession];
}

export function useSubmissions(): SubmissionTask[] {
  return useSyncExternalStore(
    (onChange) => submissions.subscribe(onChange),
    () => submissions.snapshot(),
    () => EMPTY_TASKS,
  );
}

export function useSubmissionAction() {
  useEffect(() => {
    // 首次挂载时恢复未完成任务(同 key 继续轮询)
    submissions.restore();
  }, []);
  return {
    submit: useCallback((event: EventRequest) => submissions.submit(event), []),
    stopWaiting: useCallback((key: string) => submissions.stopWaiting(key), []),
    retryOnServer: useCallback((key: string) => submissions.retryOnServer(key), []),
    resubmitAsNew: useCallback(
      (key: string, event: EventRequest) => submissions.resubmitAsNew(key, event), []),
  };
}
