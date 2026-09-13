/**
 * 材料核对与就诊准备(material-review@2)。
 *
 * 页面只说**业务语言**:核对目标、覆盖进展、一致项、差异、待确认的问题、来源。
 * gap / schema / 内部枚举与调试异常留在诊断信息里,不进正文。
 *
 * 三条状态轴分开显示(运行 / 交付 / 依据):「已结束 + 完整报告 + 依据存在冲突」
 * 是一个合法组合,旧界面用单个"状态"字段表达不出它。
 *
 * 本轮新增的四件事,都是为了让界面不撒谎:
 *   · 覆盖进展来自**实际处理记录**,不是"跑过几次";
 *   · 报告状态与任务状态分开("报告生成了"不等于"这件事做完了");
 *   · 提交回答默认只记成**回答或候选事实**,改当前记录要走单独的确认;
 *   · 来源里分得清"系统读的"和"模型读的",也分得清字段核对与解释。
 */
import React, { useMemo, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Link } from 'react-router-dom';
import { request, newIdempotencyKey } from '../../api/http';
import { productApi } from '../../api/product';
import { SafeMarkdown } from '../../components/safeMarkdown';
import { EvidenceDrawer } from '../../components/evidence';
import { inputClass, buttonClass } from '../materials/MaterialsPage';

export interface MaterialReviewAxes { run: string; delivery: string; evidence: string }
export interface MaterialReviewInputRequest {
  request_id: string; question_text: string; required_fields?: string[]; fields?: string[];
  why_needed?: string; related_question_ids?: string[]; status: string;
  subjects?: string[]; purpose?: string;
  target?: { material_ref?: string; subjects?: string[]; fields?: string[] };
}
export interface MaterialReviewCoverage {
  total: number; processed: number; items_covered: number; items_pending: number;
  items_unreadable: number; items_unmatched: number; items_insufficient: number;
  sources_read_by_system: number; sources_read_by_model: number;
  sources_unread: number; sources_invalid: number;
  unprocessed: string[]; truncated: string[]; failed: string[];
}
export interface MaterialReviewRequirement {
  requirement_id: string; kind: string; origin: string; text: string;
  required: boolean; status: string; reason?: string | null;
  field?: string | null; subject_refs?: string[]; question_refs?: string[];
}
export interface MaterialReviewFieldComparison {
  comparison_id: string; field: string; field_label: string;
  left_value: string | null; right_value: string | null;
  comparison_status: string; reason?: string | null;
}
export interface MaterialReviewUsage {
  runs?: number; calls?: number | null; tokens?: number | null;
  refused_calls?: number | null; measured?: boolean; reason?: string | null;
}
export interface MaterialReviewFinding {
  finding_id: string; finding_type: string; statement: string; origin: string;
  material_refs: string[]; evidence_refs: string[]; stale?: boolean;
}
export interface MaterialReviewReport {
  id: string; created_at: string; goal: string; contract_version: string;
  review_revision: number; delivery_status: string; evidence_status: string;
  run_status: string; axes: MaterialReviewAxes; markdown: string;
  sections: Record<string, string[]>; revision_diff: string[];
  gaps: Array<{ code: string; detail: string; fix: string }>;
  all_gaps?: Array<{ code: string; detail: string; fix: string }>;
  input_requests: MaterialReviewInputRequest[];
  safety_checks: Array<{ summary: string; status: string }>;
  evidence_refs: string[]; material_refs: string[];
  system_material_refs?: string[]; model_material_refs?: string[];
  unread_material_refs?: string[]; invalid_material_refs?: string[];
  coverage_progress?: MaterialReviewCoverage;
  requirements?: MaterialReviewRequirement[];
  field_comparisons?: Record<string, MaterialReviewFieldComparison[]>;
  usage?: MaterialReviewUsage;
  findings?: MaterialReviewFinding[];
  attribution?: {
    system: { materials_read: string[]; field_comparisons: number; coverage_pass_rounds: number };
    model: { cycles: number; materials_read: string[]; research_actions: unknown[] };
    unattributed: { materials_not_read: string[]; materials_failed_integrity: string[] };
  };
  cycle_note?: string; partial: boolean;
}

export interface MaterialReviewTask {
  id: string; goal_type: string; revision: number; status: string; goal?: string;
  case_id: string; waiting_reason: string | null; report_refs?: string[];
  partial_report_refs?: string[]; report_axes?: MaterialReviewAxes;
  report_revision?: number; degraded_label?: string | null; retry_available?: boolean;
  coverage_progress?: MaterialReviewCoverage;
  usage?: MaterialReviewUsage;
  read_attribution?: { system?: string[]; model?: string[]; unread?: string[] };
  missing_inputs?: Array<{ request_id: string; field?: string; fields?: string[];
    question: string; why_needed?: string; subjects?: string[]; purpose?: string;
    missing_fact?: string; blocks_requirement_ids?: string[];
    target?: { material_ref?: string } }>;
  runs?: Array<{ run_id: string; status?: string; workflow_run_id?: string }>;
  budget: { spent: number; limit: number };
}

const RUN: Record<string, string> = {
  running: '进行中', waiting_input: '等待您补充', ended: '已结束',
  cancelled: '已取消', failed: '未完成',
};
const DELIVERY: Record<string, string> = {
  none: '尚无报告', partial: '部分报告', complete: '完整报告',
};
const EVIDENCE: Record<string, string> = {
  verified: '依据已核实', conflicting: '依据存在冲突', insufficient: '依据不足',
};

const FIELD_LABELS: Record<string, string> = {
  dose: '剂量', schedule: '服用频次', date: '日期', unit: '单位', name: '药名',
  route: '给药途径', form: '剂型', strength: '规格',
};

// 补充的用途决定它会被记成什么。界面**说清楚**,而不是让用户猜自己填的那一格
// 到底改没改记录。
interface Purpose { label: string; hint: string; placeholder: string }
const MATERIAL_NOTE_PURPOSE: Purpose = {
  label: '材料说明',
  hint: '按您所说记录，不会改动当前药单',
  placeholder: '材料上实际写的是……',
};
const PURPOSE: Record<string, Purpose> = {
  material_note: MATERIAL_NOTE_PURPOSE,
  user_report: {
    label: '情况说明',
    hint: '按报告记录，需要核实，不会改动当前药单',
    placeholder: '目前的情况是……',
  },
  authoritative_update: {
    label: '修改当前记录',
    hint: '需要单独确认后才会改动当前药单',
    placeholder: '应当改成……',
  },
};
const purposeOf = (key?: string): Purpose =>
  (key ? PURPOSE[key] : undefined) ?? MATERIAL_NOTE_PURPOSE;

// 内部条目引用不在页面上原样展开：用户看到的是"第几条材料条目"，不是一串
// case:/item 的十六进制。要定位就点开抽屉看双方记录与来源位置。
const shortRef = (ref: string): string => `材料条目 ${(ref.split('/')[1] ?? ref).slice(0, 8)}…`;

function Axes({ axes }: { axes?: MaterialReviewAxes }): React.ReactElement | null {
  if (!axes) return null;
  return (
    <p className="mt-1 text-xs text-ink-muted" aria-label="本次任务状态">
      运行{RUN[axes.run] ?? axes.run} · {DELIVERY[axes.delivery] ?? axes.delivery} · {EVIDENCE[axes.evidence] ?? axes.evidence}
    </p>
  );
}

const REQ_STATUS: Record<string, string> = {
  satisfied: '已完成', pending: '尚未处理', awaiting_user: '等待您补充',
  awaiting_evidence: '需要依据资料调查', unsatisfied: '本次未能满足',
};

/**
 * **本次承诺解决什么**——页面的主视图。
 *
 * 它排在覆盖统计与归因数字前面，因为用户打开页面要看的第一个问题是"我托付的
 * 这件事做完了没有"。必需要求与可选建议分开列：把两者混成一张清单，会让"我还
 * 可以再查一件事"读起来和"你必须回答我"一模一样。
 */
function Requirements({ items }: { items?: MaterialReviewRequirement[] }): React.ReactElement | null {
  if (!items?.length) return null;
  const required = items.filter(item => item.required);
  const optional = items.filter(item => !item.required);
  const done = required.filter(item => item.status === 'satisfied').length;
  return <div className="mt-3 rounded-lg border border-border px-3 py-2 text-sm" aria-label="本次承诺完成情况">
    <p className="font-medium">
      本次承诺解决 {required.length} 项，已完成 {done} 项
      {done === required.length && required.length > 0 && <span className="ml-1 text-primary-strong">· 已全部完成</span>}
    </p>
    <ul className="mt-1 space-y-1">
      {required.map(item => <li key={item.requirement_id} className="flex gap-2">
        <span className={item.status === 'satisfied' ? 'text-primary-strong' : 'text-caution'}>
          {item.status === 'satisfied' ? '✓' : '·'}
        </span>
        <span>
          {item.text}
          <span className="text-ink-muted">
            （{REQ_STATUS[item.status] ?? item.status}{item.reason ? `：${item.reason}` : ''}）
            {item.origin === 'user' && '〔您的要求〕'}
          </span>
        </span>
      </li>)}
    </ul>
    {optional.length > 0 && <div className="mt-2 border-t border-border pt-2">
      <p className="text-ink-muted">
        另有 {optional.length} 项可选的进一步调查（不影响本次是否完成）：
      </p>
      <ul className="mt-1 space-y-1 text-ink-muted">
        {optional.map(item => <li key={item.requirement_id}>· {item.text}</li>)}
      </ul>
    </div>}
  </div>;
}

/**
 * 逐字段的核对结果。
 *
 * "已核对"与"不可比较"分开摆：一条写着一致的记录，如果它的剂型、规格从来没比过，
 * 用户必须看得出来——以前那句话只出现在行尾的"未决问题"里。
 */
function FieldComparisons({ comparisons }: {
  comparisons?: Record<string, MaterialReviewFieldComparison[]>;
}): React.ReactElement | null {
  const entries = Object.entries(comparisons ?? {}).filter(([, rows]) => rows.length > 0);
  if (!entries.length) return null;
  return <details className="mt-2" open>
    <summary className="cursor-pointer text-sm">逐字段核对结果</summary>
    <div className="mt-1 space-y-2 text-sm">
      {entries.map(([ref, rows]) => <div key={ref}>
        <p className="text-xs text-ink-muted">{shortRef(ref)}</p>
        <div className="mt-1 overflow-x-auto">
          <table className="w-full min-w-[420px] text-left text-xs">
            <thead className="text-ink-muted">
              <tr><th>字段</th><th>材料</th><th>当前记录</th><th>结果</th></tr>
            </thead>
            <tbody>
              {rows.map(row => <tr key={row.comparison_id} className="border-t border-border">
                <td className="py-1 pr-2">{row.field_label}</td>
                <td className="py-1 pr-2">{row.left_value ?? '—'}</td>
                <td className="py-1 pr-2">{row.right_value ?? '—'}</td>
                <td className={`py-1 ${row.comparison_status === 'equal' ? 'text-primary-strong' : 'text-caution'}`}>
                  {STATUS_TEXT[row.comparison_status] ?? row.comparison_status}
                </td>
              </tr>)}
            </tbody>
          </table>
        </div>
      </div>)}
    </div>
  </details>;
}

/** 用量读数：**没测到就写 unknown，不写 0**——0 是一个测量结果。 */
function usageText(usage?: MaterialReviewUsage): string {
  if (!usage || !usage.measured) {
    return '未记录（' + (usage?.reason === 'provider_usage_unknown' ? '供应商未返回用量' : '本次没有可用的用量记录') + '）';
  }
  const calls = usage.calls ?? 'unknown';
  const tokens = usage.tokens ?? 'unknown';
  const refused = usage.refused_calls ?? 'unknown';
  return `调用 ${calls} 次、token ${tokens}、其中被限流拒绝 ${refused} 次`;
}

const STATUS_TEXT: Record<string, string> = {
  equal: '一致', different: '不一致', missing_left: '材料未写',
  missing_right: '记录里没有', not_comparable: '无法可靠换算', invalid_value: '格式无效',
};

function CoverageSummary({ progress }: { progress?: MaterialReviewCoverage }): React.ReactElement | null {
  if (!progress || !progress.total) return null;
  const unreadable = (progress.items_unreadable ?? 0) + (progress.items_unmatched ?? 0);
  return <div className="mt-2 rounded-lg border border-border bg-surface-alt px-3 py-2 text-xs" aria-label="基础覆盖进展">
    <p className="font-medium text-ink">
      基础材料覆盖：{progress.processed}/{progress.total} 项已处理
    </p>
    <p className="mt-1 text-ink-muted">
      已核对 {progress.items_covered} · 依据不足 {progress.items_insufficient}
      {unreadable > 0 && ` · 读不到或无可比较记录 ${unreadable}`}
      {progress.items_pending > 0 && ` · 本次尚未处理 ${progress.items_pending}`}
    </p>
    <p className="mt-1 text-ink-muted">
      来源读取：系统读取 {progress.sources_read_by_system} 条 · 模型读取 {progress.sources_read_by_model} 条
      {progress.sources_invalid > 0 && ` · 校验未通过 ${progress.sources_invalid} 条`}
    </p>
    {(progress.failed?.length ?? 0) > 0 && <p className="mt-1 text-caution" role="status">
      读取失败：{progress.failed.map(shortRef).join('、')}（这一条没有核对到，不代表与当前记录一致）
    </p>}
    {(progress.truncated?.length ?? 0) > 0 && <p className="mt-1 text-caution" role="status">
      原文较长、本次只读了一部分：{progress.truncated.map(shortRef).join('、')}
    </p>}
  </div>;
}

/** 材料条目抽屉:回读**这个系统**看到的那一条(双方字段与来源位置)。 */
function MaterialItemDrawer({ caseId, itemRef, onClose }: {
  caseId: string; itemRef: string; onClose: () => void;
}): React.ReactElement {
  const query = useQuery({
    queryKey: ['material-review-item', caseId],
    queryFn: () => productApi.case(caseId),
  });
  const itemId = itemRef.split('/').slice(1).join('/');
  const item = query.data?.items.find((entry) => entry.item_id === itemId);
  const location = item?.candidate?.locations ? Object.values(item.candidate.locations)[0] : undefined;
  return <div className="mt-2 rounded-lg border border-border p-3 text-sm" role="dialog" aria-label="材料条目来源">
    <div className="flex items-start justify-between gap-2">
      <p className="font-medium">材料条目 {itemRef}</p>
      <button className={buttonClass} onClick={onClose}>关闭</button>
    </div>
    {query.isLoading && <p className="mt-2 text-ink-muted">读取中……</p>}
    {query.isError && <p className="mt-2 text-caution">这条材料的原文当前读不到。</p>}
    {item && <>
      <p className="mt-2 text-ink-muted">材料上写的（解析结果）：</p>
      <ul className="mt-1 space-y-1">
        {Object.entries(item.candidate?.fields ?? {}).filter(([, v]) => v).map(([k, v]) =>
          <li key={k}>{FIELD_LABELS[k] ?? k}：{String(v)}</li>)}
      </ul>
      <p className="mt-2 text-ink-muted">当前记录里对应的条目：</p>
      <ul className="mt-1 space-y-1">
        {(item.current ?? []).map((m) => <li key={m.ref}>{m.display_name} · {m.dose} · {m.schedule}（{m.ref}）</li>)}
        {(item.current ?? []).length === 0 && <li>没有对应的当前记录</li>}
      </ul>
      {location && <p className="mt-2 text-xs text-ink-muted">
        来源位置：第 {location.line ?? '—'} 行{location.column ? ` 第 ${location.column} 列` : ''}
        {location.page ? ` 第 ${location.page} 页` : ''}
      </p>}
      <p className="mt-2 text-xs text-ink-muted">材料是尚未确认的候选，不是患者事实。</p>
    </>}
  </div>;
}

export function MaterialReviewTaskCard({ task, busy, onRun, resume }: {
  task: MaterialReviewTask; busy: boolean;
  onRun: (fn: () => Promise<unknown>) => Promise<void>;
  resume: (task: { id: string; revision: number }, action?: string) => Promise<unknown>;
}): React.ReactElement {
  const requests = task.missing_inputs ?? [];
  const reportId = task.report_refs?.at(-1) ?? task.partial_report_refs?.at(-1);
  const report = useQuery({
    queryKey: ['material-review-report', reportId],
    enabled: !!reportId,
    queryFn: () => request<MaterialReviewReport>(`/v1/material-review-reports/${reportId}`),
  });
  const [answers, setAnswers] = useState<Record<string, string>>({});
  const [note, setNote] = useState('');
  const [evidenceId, setEvidenceId] = useState<string | null>(null);
  const [openItem, setOpenItem] = useState<string | null>(null);
  const [error, setError] = useState('');
  const data = report.data;
  const progress = task.coverage_progress ?? data?.coverage_progress;

  const remaining = useMemo(
    () => (data?.sections?.['4. 仍待确认的问题'] ?? []).filter((line) => line.trim()),
    [data]);

  async function supplement() {
    // 默认只**记录回答**:材料上写的是什么、用户对当前情况的说明。它们不会改动
    // 当前药单——要改记录必须逐条勾选"确认修改",那会走既有的受控写入与确认流程。
    setError('');
    const payload: Array<Record<string, string>> = [];
    const answered: string[] = [];
    for (const item of blockingRequests) {
      const value = (answers[item.request_id] ?? '').trim();
      if (!value) continue;
      const subject = (item.subjects ?? [])[0];
      // 不知道这条补充属于哪条记录时**不写回**：把值挂到猜测出来的药名上，
      // 比不收这个值更糟。问题本身仍会留在报告里。
      if (!subject) continue;
      answered.push(item.request_id);
      const field = item.fields?.[0] ?? item.field ?? 'dose';
      payload.push({ request_id: item.request_id, field, value, kind: 'material_note' });
    }
    if (!payload.length) {
      setError('请至少填写一条内容。只回答其中一部分也可以，其余问题会继续保持开放。');
      return;
    }
    await request(`/v1/care-tasks/${task.id}/input`, {
      method: 'POST',
      body: {
        key: newIdempotencyKey(), revision: task.revision,
        answers: payload,
        review_request_ids: answered,
        semantic: note.trim() ? { chronic_condition: note.trim() } : undefined,
      },
    });
    setAnswers({});
    setNote('');
    await onRun(() => resume({ id: task.id, revision: task.revision + 1 }));
  }

  const modelAttended = (data?.attribution?.model?.cycles ?? 0) > 0;
  const unmetRequired = (data?.requirements ?? [])
    .filter(item => item.required && item.status !== 'satisfied');
  const optionalOpen = (data?.requirements ?? []).filter(item => !item.required);
  const blockingRequests = requests.filter(item => (item.blocks_requirement_ids ?? []).length > 0);
  const optionalRequests = requests.filter(item => (item.blocks_requirement_ids ?? []).length === 0);
  const awaiting = blockingRequests.length ? '等待补充' : null;

  return <article className="rounded-card border border-border bg-surface p-4">
    <div className="flex justify-between gap-2">
      <h3 className="font-medium">材料核对与就诊准备</h3>
      <span className="text-sm text-primary-strong">
        {task.status === 'ready' ? '可以继续' : RUN[task.report_axes?.run ?? ''] ?? task.status}
      </span>
    </div>
    {task.goal && <p className="mt-2 text-sm">核对目标:{task.goal}</p>}
    <Axes axes={task.report_axes} />

    {/* **先给结果**：本次承诺解决了什么。归因与读取次数退到详情里。 */}
    <Requirements items={data?.requirements} />

    {/* 完成时说清完成的范围；部分交付时说清缺哪一项、下一步要什么。 */}
    {data && data.delivery_status === 'complete' && <p className="mt-2 rounded-lg border border-border bg-surface-alt px-3 py-2 text-sm" role="status">
      本次任务已经完成：上面列出的必需事项全部满足。报告里的结论全部来自实际读过的来源。
      {optionalOpen.length > 0 && `另有 ${optionalOpen.length} 项可选调查没有做，不影响这次的结果。`}
    </p>}
    {data && data.delivery_status !== 'complete' && <p className="mt-2 rounded-lg border border-border px-3 py-2 text-sm" role="status">
      本次是<strong>部分结果</strong>：{unmetRequired.length > 0
        ? `还有 ${unmetRequired.length} 项您的要求没有完成——${unmetRequired.map(item => item.text).join('、')}。`
        : '还有一些交付要求没有满足。'}
      {blockingRequests.length > 0
        ? '下一步：回答下面列出的问题，系统会从这里继续，已经核对过的部分不会重做。'
        : '下一步：可以继续核对，或按报告里写明的范围与医生确认。'}
    </p>}
    <p className="mt-2 text-sm">{task.waiting_reason}</p>
    {/* 报告状态与任务状态**分开**说:报告可以是完整的,而这件事还没做完。 */}
    {data && <p className="mt-1 text-xs text-ink-muted">
      报告状态：{DELIVERY[data.delivery_status] ?? data.delivery_status}
      {awaiting && `（${awaiting}，回答后可以从这里继续）`}
      {data.review_revision > 1 && ` · 第 ${data.review_revision} 版`}
    </p>}

    {data && !modelAttended && <p className="mt-2 rounded-lg border border-border bg-surface-alt px-3 py-2 text-xs" role="status">
      这一版没有模型参与：以上结果来自代码完成的确定性字段核对。
      <strong>需要做语义判断的部分还没有处理</strong>，可以在模型可用时继续。
    </p>}

    {/* 默认展开:一份**部分报告**恰恰是用户最需要马上看到的东西——它写着还差
        什么、在等谁。折叠它会让"等待补充"看起来像"没有结果"。 */}
    {data && <details className="mt-3 border-t border-border pt-3" open={task.status !== 'cancelled'}>
      <summary className="cursor-pointer text-sm">
        {data.delivery_status === 'complete' ? '查看核对报告' : '查看部分报告'}
        {data.review_revision > 1 ? `(第 ${data.review_revision} 版)` : ''}
      </summary>
      <div className="my-3 min-w-0 [overflow-wrap:anywhere]"><SafeMarkdown text={data.markdown} /></div>

      {data.revision_diff?.length > 0 && <details className="mt-2" open={data.review_revision > 1}>
        <summary className="cursor-pointer text-sm">这份报告与上一版相比变了什么</summary>
        <ul className="mt-2 space-y-1 text-sm">
          {data.revision_diff.map((line, index) => <li key={index}>{line.replace(/^-\s*/, '')}</li>)}
        </ul>
        {remaining.length > 0 && <p className="mt-2 text-xs text-ink-muted">
          这一版仍然留有 {remaining.length} 条未决事项，逐条写在第 4 节里。
        </p>}
      </details>}

      <FieldComparisons comparisons={data.field_comparisons} />
      <Sources data={data} onEvidence={setEvidenceId} onItem={setOpenItem} />
      {openItem && <MaterialItemDrawer caseId={task.case_id} itemRef={openItem}
        onClose={() => setOpenItem(null)} />}

      <details className="mt-2">
        <summary className="cursor-pointer text-xs text-ink-muted">系统与模型的归因、用量</summary>
        <div className="mt-1"><CoverageSummary progress={progress} /></div>
        <ul className="mt-1 space-y-1 text-xs text-ink-muted">
          <li>代码完成的确定性工作：读取 {data.attribution?.system.materials_read.length ?? 0} 条材料、
            字段比较 {data.attribution?.system.field_comparisons ?? 0} 次</li>
          <li>模型做出的调查判断：{data.attribution?.model.cycles ?? 0} 轮
            （模型读回原文 {data.attribution?.model.materials_read.length ?? 0} 条）</li>
          <li>模型用量：{usageText(data.usage ?? task.usage)}</li>
          {(data.unread_material_refs?.length ?? 0) > 0 &&
            <li>未读取到的材料条目：{data.unread_material_refs!.map(shortRef).join('、')}</li>}
        </ul>
      </details>

      {(data.all_gaps ?? data.gaps ?? []).length > 0 && <details className="mt-2">
        <summary className="cursor-pointer text-xs text-ink-muted">
          诊断信息:尚未满足的交付要求({(data.all_gaps ?? data.gaps).length})
        </summary>
        <ul className="mt-1 space-y-1 text-xs text-ink-muted">
          {(data.all_gaps ?? data.gaps).map((gap, index) => <li key={index}>{gap.detail}</li>)}
        </ul>
      </details>}
    </details>}

    {evidenceId && <EvidenceDrawer evidenceId={evidenceId} onClose={() => setEvidenceId(null)} />}

    {optionalRequests.length > 0 && <details className="mt-3 border-t border-border pt-3">
      <summary className="cursor-pointer text-sm">
        可选的进一步调查（{optionalRequests.length} 项，不影响本次是否完成）
      </summary>
      <ul className="mt-2 space-y-1 text-sm text-ink-muted">
        {optionalRequests.map(item => <li key={item.request_id}>· {item.question}</li>)}
      </ul>
      <p className="mt-1 text-xs text-ink-muted">
        这些是系统建议还可以查的事，不是您必须回答的问题。想继续调查时再回答即可。
      </p>
    </details>}

    {blockingRequests.length > 0 && task.status === 'waiting_input' && <div className="mt-3 space-y-3 border-t border-border pt-3">
      <p className="text-sm font-medium">请补充以下内容</p>
      <p className="text-xs text-ink-muted">
        这里填的内容会**按您所说记录**，不会自动改动当前药单。只回答其中一部分也可以，
        没回答的问题会继续保持开放。
      </p>
      {blockingRequests.map(item => {
        const purpose = purposeOf(item.purpose);
        const fields = item.fields ?? (item.field ? [item.field] : []);
        return <div key={item.request_id} className="rounded-lg border border-border px-3 py-2 text-sm">
          <p>{item.question}</p>
          <p className="mt-1 text-xs text-ink-muted">
            对象：{(item.subjects ?? []).join('、') || '（未指明）'}
            {fields.length > 0 && ` · 待补：${fields.map((f) => FIELD_LABELS[f] ?? f).join('、')}`}
            {' · 用途：'}{purpose.label}
          </p>
          {item.why_needed && <p className="mt-1 text-xs text-ink-muted">{item.why_needed}</p>}
          <input className={inputClass} value={answers[item.request_id] ?? ''}
            aria-label={`补充 ${item.question}`}
            onChange={e => setAnswers(prev => ({ ...prev, [item.request_id]: e.target.value }))}
            placeholder={purpose.placeholder} />
          <p className="mt-1 text-xs text-ink-muted">{purpose.hint}</p>
        </div>;
      })}
      <label className="block text-sm">补充说明(可选)
        <input className={inputClass} value={note} onChange={e => setNote(e.target.value)}
          placeholder="例如:最近一次复查的情况" /></label>
      {error && <p className="text-sm text-caution" role="alert">{error}</p>}
      <button disabled={busy} className={buttonClass} onClick={() => void onRun(supplement)}>
        保存补充并重新核对
      </button>
      <p className="text-xs text-ink-muted">只会重新核对受影响的部分,报告会说明这一版改了什么。</p>
      {/* 要**改当前记录**是另一件事:它不做成回答的一部分,而是走既有的材料核对
          确认与写入流程——那里有原值、新值与来源的确认界面。在这里顺手写回记录,
          正是本轮要拆开的那种混淆。 */}
      <p className="text-xs text-ink-muted">
        如果确认当前记录本身需要修改，请到
        <Link className="mx-1 underline" to={`/materials?case=${encodeURIComponent(task.case_id)}`}>材料核对页</Link>
        逐条确认后再修改。
      </p>
    </div>}

    {task.degraded_label && <p className="mt-2 text-sm text-caution" role="status">{task.degraded_label}</p>}

    <div className="mt-3 flex gap-3">
      {!['completed', 'cancelled', 'failed', 'running'].includes(task.status) &&
        <button disabled={busy} className={buttonClass}
          onClick={() => void onRun(() => resume(task))}>继续核对</button>}
      {task.retry_available && ['completed', 'failed'].includes(task.status) &&
        <button disabled={busy} className={buttonClass}
          onClick={() => void onRun(() => resume(task))}>重新核对</button>}
      {!['completed', 'cancelled', 'failed'].includes(task.status) &&
        <button disabled={busy} className={buttonClass}
          onClick={() => void onRun(() => resume(task, 'cancel'))}>取消后续核对</button>}
    </div>
    <p className="mt-2 text-xs text-ink-muted">
      已保存的记录与部分报告在取消或失败后仍然保留;多次核对累计消耗任务预算(第 {task.budget.spent}/{task.budget.limit} 次)。
    </p>
  </article>;
}

/** 来源清单:从发现回跳到**双方记录与材料位置**,并分清谁读的、谁解释的。 */
function Sources({ data, onEvidence, onItem }: {
  data: MaterialReviewReport;
  onEvidence: (id: string) => void;
  onItem: (ref: string) => void;
}): React.ReactElement | null {
  const findings = (data.findings ?? []).filter((f) => !f.stale);
  const systemRefs = data.system_material_refs ?? [];
  const modelRefs = data.model_material_refs ?? [];
  const evidenceRefs = data.evidence_refs ?? [];
  if (!findings.length && !systemRefs.length && !modelRefs.length && !evidenceRefs.length) return null;
  return <div className="mt-2 border-t border-border pt-2">
    <p className="text-sm">本次报告引用到的来源</p>

    {findings.length > 0 && <ul className="mt-1 space-y-1 text-sm">
      {findings.map((finding) => <li key={finding.finding_id}>
        <button className="text-left underline decoration-dotted" onClick={() => finding.material_refs[0] && onItem(finding.material_refs[0])}>
          {finding.statement}
        </button>
        <span className="ml-1 text-xs text-ink-muted">
          （{finding.origin === 'system' ? '代码核对字段得出' : '模型提出，需人工确认'}）
        </span>
      </li>)}
    </ul>}

    {systemRefs.length > 0 && <div className="mt-2 text-xs text-ink-muted">
      <p>系统读过的材料条目（确定性字段核对）：</p>
      <div className="mt-1 flex flex-wrap gap-2">
        {systemRefs.map(ref => <button key={ref} className={buttonClass}
          onClick={() => onItem(ref)}>{shortRef(ref)}</button>)}
      </div>
    </div>}

    {modelRefs.length > 0 && <div className="mt-2 text-xs text-ink-muted">
      <p>模型读回原文的材料条目：</p>
      <div className="mt-1 flex flex-wrap gap-2">
        {modelRefs.map(ref => <button key={ref} className={buttonClass}
          onClick={() => onItem(ref)}>{shortRef(ref)}</button>)}
      </div>
    </div>}

    {(data.unread_material_refs?.length ?? 0) > 0 && <p className="mt-2 text-xs text-caution" role="status">
      本次没有读取到的材料条目：{data.unread_material_refs!.map(shortRef).join('、')}
    </p>}
    {(data.invalid_material_refs?.length ?? 0) > 0 && <p className="mt-2 text-xs text-caution" role="status">
      完整性校验未通过的材料条目：{data.invalid_material_refs!.map(shortRef).join('、')}（不能支撑结论）
    </p>}

    {evidenceRefs.length > 0 && <div className="mt-2">
      <p className="text-xs text-ink-muted">回读到的说明书依据：</p>
      <div className="mt-1 flex flex-wrap gap-2" aria-label="查看来源">
        {evidenceRefs.map((ref, index) => <button key={ref} className={buttonClass}
          onClick={() => onEvidence(ref)}>回读说明书依据 {index + 1}</button>)}
      </div>
    </div>}
  </div>;
}
