import { request, newIdempotencyKey } from './http';

export type Fields = Record<string, string | null>;
export interface Candidate { fields: Fields; original_fields: Fields; locations: Record<string, { line?: number; column?: number; bbox?: number[]; page?: number }>; corrections: unknown[] }
export interface ReconciliationItem { item_id: string; kind: string; status: string; issues: string[]; candidate: Candidate | null; current: { ref: string; display_name: string; dose: string; schedule: string }[]; receipt_id: string | null; safety_check?: { status?: string; run_id: string } }
export interface Reconciliation { case_id: string; document_id: string; created_at: string; base_revision: Record<string, number>; status: string; stale?: boolean; items: ReconciliationItem[] }
export const productApi = {
  template: () => request<{ csv: string }>('/v1/materials/template'),
  cases: () => request<{ items: Reconciliation[] }>('/v1/reconciliations'),
  case: (id: string) => request<Reconciliation>(`/v1/reconciliations/${id}`),
  import: (text: string, key: string) => request<Reconciliation>('/v1/materials/csv', { method: 'POST', body: { text, key } }),
  refresh: (id: string) => request<Reconciliation>(`/v1/reconciliations/${id}/refresh`, { method: 'POST' }),
  decide: (c: Reconciliation, item: ReconciliationItem, action: string, corrections: Record<string, unknown>, key: string, task_context?: {task_id: string; revision: number}) => request<Reconciliation>(`/v1/reconciliations/${c.case_id}/items/${item.item_id}`, { method: 'POST', body: { key, expected_revision: c.base_revision, action, corrections, task_context } }),
};
export { newIdempotencyKey };
