/**
 * 异步提交引擎(规格 §7 完整实现):
 * - 每次新提交生成 UUID 幂等键;重发复用同键同内容(含 session_id/occurred_at)。
 * - POST 202 → 轮询 GET(1s 起步、上限 5s、退避;后台标签页降频)。
 * - 网络断开 → 状态未知 → 恢复后继续查同一任务,不擅自重复提交。
 * - 失败任务:服务端 retry 接口(保留原事件身份)或用户明确的新提交。
 * - 同一会话的写操作串行执行;只读浏览不受影响。
 * - 未完成任务元数据保存在 sessionStorage(仅请求标识与内容,用于刷新恢复;
 *   不是患者数据库 —— 权威来源始终是服务端 SQLite)。
 */
import { ApiError, newIdempotencyKey } from './http';
import { api } from './client';
import type {
  EventRequest, EventResponseDto, FailedEventDto, RunProgressEventDto,
} from './types';

export type SubmissionStatus =
  | 'submitting'   // POST 在途
  | 'unknown'      // 网络失败/超时,服务端状态未知
  | 'queued'
  | 'processing'
  | 'committed'
  | 'failed'
  | 'rejected';    // 422/409 等受理层拒绝

export type CancelState = 'none' | 'requested' | 'cancelled' | 'already_final';

export interface SubmissionTask {
  key: string;
  sessionId: string;
  event: EventRequest;
  status: SubmissionStatus;
  result: EventResponseDto | null;
  /** 受理层拒绝(422 idempotency_key_reused / 409 previous_attempt_failed 等)。 */
  rejection: ApiError | null;
  /** 服务端明确的任务失败(GET 轮询 500)。 */
  failure: FailedEventDto | null;
  networkError: string | null;
  /** 超过 60s 仍未 committed 时置位:展示「仍在确认处理状态」。 */
  slow: boolean;
  replay: boolean;
  startedAt: string;
  finishedAt: string | null;
  /** Harness P2:服务端 run 标识(受理/轮询响应携带);有它才能接进度与取消。 */
  runId: string | null;
  /** 进度事件(游标补齐 + event_id 去重;仅粗粒度状态,无未审核内容)。 */
  progress: RunProgressEventDto[];
  progressSeq: number;
  cancelState: CancelState;
}

type Listener = () => void;

const STORE_KEY = 'mcp.activeSubmissions.v1';
const POLL_START_MS = 1000;
const POLL_MAX_MS = 5000;
const POLL_HIDDEN_MAX_MS = 15000;
const SLOW_THRESHOLD_MS = 60_000;

/** 需要与其他写操作串行的事件类型。 */
const WRITE_EVENT_TYPES = new Set([
  'register_profile', 'profile_update', 'medication_change', 'procedure_exposure',
]);

class SubmissionEngine {
  private tasks = new Map<string, SubmissionTask>();
  private cachedSnapshot: SubmissionTask[] = [];
  private listeners = new Set<Listener>();
  private pollers = new Map<string, { stop: () => void; wake?: () => void }>();
  private writeQueues = new Map<string, Promise<void>>();
  /** 提交完成回调(提交 → committed 后刷新相关查询)。 */
  onCommitted: ((task: SubmissionTask) => void) | null = null;

  constructor() {
    if (typeof window !== 'undefined') {
      window.addEventListener('visibilitychange', () => {
        if (document.visibilityState === 'visible') {
          // 恢复可见:立即轮询一次并恢复正常频率
          for (const poller of this.pollers.values()) poller.wake?.();
        }
      });
    }
  }

  subscribe(listener: Listener): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  private emit(): void {
    this.cachedSnapshot = [...this.tasks.values()].sort((a, b) =>
      a.startedAt < b.startedAt ? 1 : -1);
    for (const listener of this.listeners) listener();
  }

  /** 稳定快照(React 渲染用)。 */
  snapshot(): SubmissionTask[] {
    return this.cachedSnapshot;
  }

  task(key: string): SubmissionTask | undefined {
    return this.tasks.get(key);
  }

  /** 启动时恢复:sessionStorage 里的未完成任务继续轮询同一 key。 */
  restore(): void {
    let stored: { key: string; sessionId: string; event: EventRequest; startedAt: string; runId?: string | null }[] = [];
    try {
      stored = JSON.parse(window.sessionStorage.getItem(STORE_KEY) ?? '[]');
    } catch {
      stored = [];
    }
    for (const item of stored) {
      if (this.tasks.has(item.key)) continue;
      const task: SubmissionTask = {
        key: item.key, sessionId: item.sessionId, event: item.event,
        status: 'unknown', result: null, rejection: null, failure: null,
        networkError: null, slow: false, replay: false,
        startedAt: item.startedAt, finishedAt: null,
        runId: item.runId ?? null, progress: [], progressSeq: 0, cancelState: 'none',
      };
      this.tasks.set(item.key, task);
      this.startPolling(task);
    }
    this.emit();
    this.persist();
  }

  private persist(): void {
    try {
      const active = [...this.tasks.values()]
        .filter((t) => !['committed', 'failed', 'rejected'].includes(t.status))
        .map((t) => ({
          key: t.key, sessionId: t.sessionId, event: t.event, startedAt: t.startedAt,
          runId: t.runId,
        }));
      window.sessionStorage.setItem(STORE_KEY, JSON.stringify(active));
    } catch {
      // 隐私模式等场景:恢复能力降级,不影响提交本身
    }
  }

  /**
   * 提交一次业务事件。同一会话的写操作串行;每次调用都是「一次明确的提交
   * 操作」—— 调用方为重试同一事件时应使用 retryFailed/retryNewKey 语义。
   */
  submit(event: EventRequest): string {
    const key = newIdempotencyKey();
    const task: SubmissionTask = {
      key, sessionId: event.session_id, event,
      status: 'submitting', result: null, rejection: null, failure: null,
      networkError: null, slow: false, replay: false,
      startedAt: new Date().toISOString(), finishedAt: null,
      runId: null, progress: [], progressSeq: 0, cancelState: 'none',
    };
    this.tasks.set(key, task);
    this.emit();
    this.persist();

    const run = async (): Promise<void> => {
      try {
        const acceptance = await api.submitEvent(event, key);
        this.applyAcceptance(task, acceptance);
      } catch (err) {
        this.applySubmitError(task, err);
      }
    };

    if (WRITE_EVENT_TYPES.has(event.event_type)) {
      const queue = this.writeQueues.get(event.session_id) ?? Promise.resolve();
      const next = queue.then(run, run); // 前一个失败不阻塞后续
      this.writeQueues.set(event.session_id, next.catch(() => undefined));
    } else {
      void run();
    }
    return key;
  }

  private applyAcceptance(task: SubmissionTask,
                          acceptance: AcceptanceLike): void {
    task.replay = acceptance.replay ?? false;
    if (acceptance.run_id) task.runId = acceptance.run_id;
    if (acceptance.status === 'committed' && acceptance.response) {
      // 重放 committed 的 POST 受理体或轮询结果
      task.status = 'committed';
      task.result = acceptance.response;
      if (task.result.run_status === 'cancelled') task.cancelState = 'cancelled';
      task.finishedAt = new Date().toISOString();
      this.emit();
      this.persist();
      this.onCommitted?.(task);
      return;
    }
    task.status = acceptance.status === 'processing' ? 'processing' : 'queued';
    this.emit();
    this.persist();
    this.startPolling(task);
  }

  private applySubmitError(task: SubmissionTask, err: unknown): void {
    if (err instanceof ApiError && err.kind === 'http') {
      task.status = 'rejected';
      task.rejection = err;
    } else if (err instanceof ApiError && err.kind === 'network') {
      // POST 响应丢失:状态未知,保留同键继续查(GET 同 key 找回)
      task.status = 'unknown';
      task.networkError = err.displayMessage();
      this.startPolling(task);
    } else {
      task.status = 'unknown';
      task.networkError = err instanceof Error ? err.message : String(err);
      this.startPolling(task);
    }
    this.emit();
    this.persist();
  }

  private startPolling(task: SubmissionTask): void {
    if (this.pollers.has(task.key)) return;
    let stopped = false;
    let wakeImpl: (() => void) | null = null;

    const wake = () => wakeImpl?.();
    this.pollers.set(task.key, { stop: () => { stopped = true; }, wake });

    const pollOnce = async (): Promise<'done' | 'continue'> => {
      try {
        const status = await api.eventStatus(task.key);
        task.networkError = null;
        task.slow = Date.now() - Date.parse(task.startedAt) > SLOW_THRESHOLD_MS;
        if ('run_id' in status && status.run_id) task.runId = status.run_id;
        if (status.status === 'committed') {
          task.status = 'committed';
          task.result = (status as CommittedLike).response ?? null;
          task.finishedAt = new Date().toISOString();
          // 终态由后端持久化结果确认:取消等状态以 run_status 为准,不靠动画推断。
          if (task.result?.run_status === 'cancelled') task.cancelState = 'cancelled';
          this.pollers.delete(task.key);
          this.emit();
          this.persist();
          this.onCommitted?.(task);
          return 'done';
        }
        if (status.status === 'failed') {
          task.status = 'failed';
          task.failure = status as FailedEventDto;
          task.finishedAt = new Date().toISOString();
          this.pollers.delete(task.key);
          this.emit();
          this.persist();
          return 'done';
        }
        task.status = status.status === 'processing' ? 'processing' : 'queued';
        this.emit();
        await this.syncProgress(task); // 失败不阻塞状态轮询
        return 'continue';
      } catch (err) {
        if (err instanceof ApiError && err.kind === 'http' && err.status === 404) {
          // 刷新后服务端不认识该 key:保留同一次尝试的语义,让用户重新确认。
          task.status = 'unknown';
          task.networkError = '服务端没有这个提交记录,请确认内容后重新提交。';
          this.pollers.delete(task.key);
          this.emit();
          this.persist();
          return 'done';
        }
        task.status = 'unknown';
        task.networkError = err instanceof ApiError
          ? err.displayMessage()
          : (err instanceof Error ? err.message : String(err));
        task.slow = Date.now() - Date.parse(task.startedAt) > SLOW_THRESHOLD_MS;
        this.emit();
        return 'continue'; // 网络恢复后继续查同一任务
      }
    };

    void (async () => {
      let delay = POLL_START_MS;
      let woke = false;
      wakeImpl = () => { woke = true; };
      while (!stopped) {
        const outcome = await pollOnce();
        if (outcome === 'done' || stopped) break;
        const hidden = document.visibilityState === 'hidden';
        const cap = hidden ? POLL_HIDDEN_MAX_MS : POLL_MAX_MS;
        delay = woke ? POLL_START_MS : Math.min(Math.round(delay * 1.5), cap);
        woke = false;
        await new Promise<void>((resolve) => {
          const timer = setTimeout(resolve, delay);
          wakeImpl = () => { clearTimeout(timer); resolve(); };
        });
      }
    })();
  }

  /** 用户选择「停止等待」:只停止本端的轮询展示,不是取消服务器任务。 */
  stopWaiting(key: string): void {
    this.pollers.get(key)?.stop();
    this.pollers.delete(key);
    const task = this.tasks.get(key);
    if (task && !['committed', 'failed'].includes(task.status)) {
      task.status = 'unknown';
      this.emit();
      this.persist();
    }
  }

  /**
   * Harness P2:拉取进度事件(带游标断线续传 + event_id 客户端去重)。
   * 进度失败不影响任务状态轮询;snapshot 响应时重置去重窗口。
   */
  private async syncProgress(task: SubmissionTask): Promise<void> {
    if (!task.runId) return;
    try {
      const page = await api.runProgress(task.runId, task.progressSeq);
      if (page.snapshot) task.progress = [];
      const seen = new Set(task.progress.map((e) => e.event_id));
      const fresh = page.events.filter((e) => {
        if (seen.has(e.event_id)) return false;
        seen.add(e.event_id);
        return true;
      });
      task.progress = [...task.progress, ...fresh].slice(-20);
      task.progressSeq = Math.max(task.progressSeq, page.latest_seq);
      this.emit();
    } catch {
      // 进度不可用(如服务端关闭了进度事件):保持静默,状态轮询仍在。
    }
  }

  /**
   * Harness P2:明确且幂等的服务端取消。取消只终止尚未执行的工作;已提交
   * 的领域记录不会回滚,终态以服务端持久化结果为准。
   */
  async cancelOnServer(key: string, reason?: string): Promise<void> {
    const task = this.tasks.get(key);
    if (!task?.runId) return;
    try {
      const outcome = await api.cancelRun(task.runId, reason);
      // Polling may confirm the terminal state while this request is in
      // flight. A late acknowledgement must not replace that confirmation.
      task.cancelState = task.result?.run_status === 'cancelled' ? 'cancelled'
        : outcome.cancel_state === 'unknown_run' ? 'none' : outcome.cancel_state;
    } catch (err) {
      // 404(无此运行)等:如实展示,不改变本地任务语义。
      task.networkError = err instanceof ApiError ? err.displayMessage() : String(err);
    }
    this.emit();
    this.persist();
    this.startPolling(task); // 取消后继续轮询,直到服务端确认终态
  }

  /** 服务端重试失败任务(保留原事件身份;需 ops 角色,本地模式具备)。 */
  async retryOnServer(key: string): Promise<void> {
    const task = this.tasks.get(key);
    if (!task) return;
    task.status = 'queued';
    task.failure = null;
    task.networkError = null;
    this.emit();
    this.persist();
    try {
      await api.retryEvent(key);
    } catch (err) {
      // 409 retry_not_available 等:如实回落到 unknown,由用户决定下一步
      task.status = 'unknown';
      task.networkError = err instanceof ApiError ? err.displayMessage() : String(err);
    }
    this.emit();
    this.persist();
    this.startPolling(task);
  }

  /** 用户明确的新提交(新 key,内容重新确认)。 */
  resubmitAsNew(key: string, event: EventRequest): string {
    this.tasks.delete(key);
    this.emit();
    this.persist();
    return this.submit(event);
  }
}

interface AcceptanceLike {
  run_id?: string;
  status: 'queued' | 'processing' | 'committed';
  response?: EventResponseDto;
  replay?: boolean;
}

interface CommittedLike {
  response?: EventResponseDto;
}

export const submissions = new SubmissionEngine();
