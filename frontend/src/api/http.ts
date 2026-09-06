/**
 * 统一 HTTP 客户端:状态码、JSON 解析、取消、错误结构与请求追踪。
 * 页面代码不得散落 fetch;所有请求经此发出。
 */

export type ErrorCategory = 'validation' | 'safety' | 'provider' | 'internal';

export interface ApiErrorBody {
  code: string;
  category: ErrorCategory;
  message: string;
  trace_id?: string;
  details?: Record<string, unknown>;
}

export class ApiError extends Error {
  readonly status: number;
  readonly body: ApiErrorBody | null;
  /** 网络层失败(服务不可达等)没有 status;业务 4xx/5xx 有。 */
  readonly kind: 'network' | 'http' | 'parse';

  constructor(kind: ApiError['kind'], status: number, body: ApiErrorBody | null, message: string) {
    super(message);
    this.name = 'ApiError';
    this.kind = kind;
    this.status = status;
    this.body = body;
  }

  /** 用户可读的错误摘要(不虚构 trace 编号)。 */
  displayMessage(): string {
    if (this.kind === 'network') return '服务暂时不可达,请确认后端已启动。';
    if (this.body) {
      const trace = this.body.trace_id ? `(追踪号 ${this.body.trace_id})` : '';
      return `${this.body.message}${trace}`;
    }
    return this.message;
  }
}

const BASE = import.meta.env.VITE_API_BASE ?? '';

export interface RequestOptions {
  method?: 'GET' | 'POST' | 'PUT' | 'DELETE';
  body?: unknown;
  headers?: Record<string, string>;
  signal?: AbortSignal;
}

/** 解析轮询 500 failed 体的另一种错误结构(无 error 包装)。 */
export interface FailedEventBody {
  event_key: string;
  status: 'failed';
  error_class?: string | null;
  error?: string;
}

export async function request<T>(path: string, opts: RequestOptions = {}): Promise<T> {
  const { method = 'GET', body, headers, signal } = opts;
  let response: Response;
  try {
    response = await fetch(`${BASE}${path}`, {
      method,
      headers: {
        ...(body !== undefined ? { 'Content-Type': 'application/json' } : {}),
        ...headers,
      },
      body: body !== undefined ? JSON.stringify(body) : undefined,
      signal,
    });
  } catch (err) {
    if (err instanceof DOMException && err.name === 'AbortError') throw err;
    throw new ApiError('network', 0, null, err instanceof Error ? err.message : String(err));
  }

  const text = await response.text();
  let parsed: unknown = null;
  if (text.length > 0) {
    try {
      parsed = JSON.parse(text);
    } catch {
      if (!response.ok) {
        throw new ApiError('http', response.status, null,
          `服务返回了无法解析的响应(HTTP ${response.status})。`);
      }
      throw new ApiError('parse', response.status, null, '服务返回了非 JSON 响应。');
    }
  }

  if (!response.ok) {
    const errBody = (parsed as { error?: ApiErrorBody } | null)?.error ?? null;
    throw new ApiError('http', response.status, errBody,
      errBody?.message ?? `请求失败(HTTP ${response.status})。`);
  }
  return parsed as T;
}

export function newIdempotencyKey(): string {
  if (typeof crypto !== 'undefined' && 'randomUUID' in crypto) return crypto.randomUUID();
  // 兜底:UUID v4 via Math.random(仅作降级,现代浏览器走 randomUUID)
  return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, (c) => {
    const r = (Math.random() * 16) | 0;
    const v = c === 'x' ? r : (r & 0x3) | 0x8;
    return v.toString(16);
  });
}
