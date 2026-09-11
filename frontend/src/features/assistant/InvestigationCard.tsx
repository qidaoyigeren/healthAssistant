import React, { useState } from 'react';
import type { AnswerBundleDto } from '../../api/types';
import { EvidenceDrawer } from '../../components/evidence';

const checks: Record<string, string> = {
  authority: '权威药单与关键事实', interaction_evidence: '标签证据', applicability: '材料适用条件',
};
const reasons: Record<string, string> = {
  checks_completed: '本轮必查项已完成', waiting_input: '等待补充记录', waiting_review: '分歧待专业核实',
  no_progress: '未获得新证据，已停止重复核查', budget_insufficient: '本轮预算不足，检查未完成',
  unrecoverable_failure: '工具或执行失败，已有结果保留', cancelled: '已取消后续核查',
};

export function InvestigationCard({ bundle }: { bundle?: AnswerBundleDto | null }): React.ReactElement | null {
  const [evidenceId, setEvidenceId] = useState<string | null>(null);
  const inv = bundle?.investigation;
  if (!inv) return null;
  const open = inv.gaps.filter((gap) => gap.status === 'open');
  return (
    <section aria-label="证据核查状态" className="mt-3 space-y-2 rounded-lg border border-border bg-surface-alt p-3 text-sm">
      <h3 className="font-semibold">已核查范围</h3>
      <ul className="space-y-1">
        {Object.entries(inv.checks).map(([key, status]) => <li key={key}>
          {checks[key] ?? key}：{status === 'checked' ? '已核查' : '未完成'}
        </li>)}
      </ul>
      <h3 className="font-semibold">待补充内容</h3>
      {open.length ? <ul className="space-y-1">{open.map((gap) => <li key={gap.gap_id}>{gap.description}</li>)}</ul>
        : <p>本契约内没有剩余缺口。</p>}
      <p><strong>终止原因：</strong>{reasons[inv.termination_reason ?? ''] ?? '尚未完成'}</p>
      {bundle?.multi_review && (
        <div aria-label="独立核查发现">
          <h3 className="font-semibold">独立核查（{bundle.multi_review.trigger === 'open_evidence_conflict' ? '证据冲突触发' : '大范围核查触发'}）</h3>
          {bundle.multi_review.status && bundle.multi_review.status !== 'completed' ? <p>独立核查未完成，不能据此判断没有分歧。</p> : bundle.multi_review.divergences.length > 0
            ? <ul className="space-y-1"><li key="div">存在未解决分歧（{bundle.multi_review.divergences.length} 项）：支持与反对证据并存时不以投票或平均消除，保留待专业核实。</li></ul>
            : <p>独立核查未发现新增分歧。</p>}
          <p className="text-xs text-ink-muted">核查发现与证据为只读结果，不构成临床审批。</p>
        </div>
      )}
      {inv.termination_reason === 'waiting_input' && <p className="text-ink-secondary">请先通过用药或档案页面补充并确认记录，再发送核查问题。已保存的本轮报告可随时回看。</p>}
      {inv.evidence_refs.length > 0 && <div className="flex flex-wrap gap-2">
        {inv.evidence_refs.map((ref, index) => <button key={ref} type="button" onClick={() => setEvidenceId(ref)}
          className="rounded border border-border px-2 py-1 text-primary-strong hover:bg-primary-soft">回读证据 {index + 1}</button>)}
      </div>}
      <p className="text-xs text-ink-muted">核查报告已保存不代表已证明无风险。规划模式：{inv.mode}；药单版本 {inv.patient_version.medications}。</p>
      {evidenceId && <EvidenceDrawer evidenceId={evidenceId} onClose={() => setEvidenceId(null)} />}
    </section>
  );
}
