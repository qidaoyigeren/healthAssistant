/**
 * 一条答案的**可信性**展示(CONTRACT.md §3)。
 *
 * 这一块存在的原因只有一条:让"这条答案凭什么算数"和答案本身一起出现,
 * 并且**不把没核对的写成核对过的**。
 *
 * 三条不能违反的规则:
 *  1. **五个状态,不是四个**。四个 `status`(verified / candidate / stale /
 *     unsupported)之外还有"根本没有 assessment"这一种 —— 缺失一律显示为
 *     「未核实」,绝不默认 `verified`。
 *  2. `verified` 只说明依据在约定范围内核对过。文案里必须带上这句边界,
 *     不能渲染成"用药安全""风险已排除"。
 *  3. 展示来源与原文定位只走**服务端提供的授权入口**:`ev-` 前缀的内容寻址
 *     证据 id 走 `/v1/evidence/{id}`(服务端按认证作用域解析),`memory:` 引用
 *     走 `/v1/memory/item`。界面**不拼接**任何文件路径或自作主张的 URL,
 *     读不出的引用就如实显示成读不出。
 */
import React, { useState } from 'react';
import { Link2, ScrollText } from 'lucide-react';
import type { SafetyAnsweredPartDto } from '../../api/types';
import { Badge } from '../../components/ui';
import { EvidenceDrawer, MemoryRefDrawer } from '../../components/evidence';
import {
  VERIFIED_SCOPE_NOTICE, assessmentLabel, assessmentMeaning, assessmentTone,
  dependencyRefsText, locatorText, provenanceText,
} from './labels';

/**
 * 来源引用属于哪一类入口。
 *
 * 只认服务端 `_reference_is_visible` 接受的两种可解析形态:内容寻址的
 * `ev-` 证据 id,和 `memory:` 记忆引用。其余(含契约示例里那种 `evidence:8842`
 * 之类的自造前缀)一律当作**不可读取的不透明字符串** —— 界面不替它拼地址。
 */
function sourceKind(ref: string | null | undefined): 'evidence' | 'memory' | 'opaque' {
  if (!ref) return 'opaque';
  if (ref.startsWith('ev-')) return 'evidence';
  if (ref.startsWith('memory:')) return 'memory';
  return 'opaque';
}

/** 核验状态的徽标。缺失时给中性色 + 「未核实」,不给"通过"的观感。 */
export function AssessmentBadge({ part }: { part: SafetyAnsweredPartDto }): React.ReactElement {
  const status = part.assessment?.status;
  return (
    // data-assessment-status 让浏览器验收能读到"到底是五个状态里的哪一个",
    // 不依赖可见文案的措辞是否被改过。
    <span data-assessment-status={status ?? 'unassessed'} className="inline-flex">
      <Badge tone={assessmentTone(status)}>{assessmentLabel(status)}</Badge>
    </span>
  );
}

/**
 * 一条答案 + 它的依据。
 *
 * 依据(assessment)展示在答案**旁边**,不藏进折叠区 —— 它是照护者判断
 * "这条能不能信"的直接依据。真正该折叠的是工具调用与预算,那些在别处。
 */
export function AnswerPartLine({ part, index }: {
  part: SafetyAnsweredPartDto; index?: number;
}): React.ReactElement {
  const [openEvidence, setOpenEvidence] = useState<{ id: string; quote: string | null } | null>(null);
  const [openMemoryRef, setOpenMemoryRef] = useState<string | null>(null);
  const status = part.assessment?.status ?? null;
  const meaning = assessmentMeaning(part.assessment, part.provenance);
  const kind = sourceKind(part.assessment?.source_ref ?? part.source_ref);
  const ref = part.assessment?.source_ref ?? part.source_ref ?? null;

  return (
    <li className="rounded-lg border border-border bg-surface-alt/60 px-2.5 py-2" data-part-index={index}>
      <p className="text-ink-secondary">
        已经拿到：{part.value ?? '（这条答案没有记录内容）'}
        {part.field ? <span className="text-ink-muted">（字段：{part.field}）</span> : null}
      </p>

      <p className="mt-1 flex flex-wrap items-center gap-1.5">
        <AssessmentBadge part={part} />
        {provenanceText(part.provenance) && (
          <Badge tone="neutral">来源属性：{provenanceText(part.provenance)}</Badge>
        )}
        {part.source && <Badge tone="neutral">来源：{part.source}</Badge>}
      </p>

      <p className="mt-1 text-ink-secondary">
        <span className="font-medium">{meaning.headline}</span>
        <span className="mx-1 text-ink-muted">—</span>
        {meaning.detail}
      </p>

      {/* verified 的边界必须跟着出现,否则这四个字会被读成"安全"。 */}
      {status === 'verified' && (
        <p className="mt-1 text-xs text-ink-muted">{VERIFIED_SCOPE_NOTICE}</p>
      )}

      {status === 'stale' && (part.assessment?.dependency_refs?.length ?? 0) > 0 && (
        <p className="mt-1 text-xs text-danger">
          {dependencyRefsText(part.assessment?.dependency_refs)}
          这些版本已经不是当前版本，所以这条答案需要重新核对后才能继续用。
        </p>
      )}

      <p className="mt-1 text-xs text-ink-muted">{locatorText(part.assessment?.locator)}</p>

      <p className="mt-1 flex flex-wrap items-center gap-2 text-xs text-ink-muted">
        {ref ? (
          <>
            <span className="break-all font-mono">来源引用：{ref}</span>
            {kind === 'evidence' && (
              <button type="button"
                onClick={() => setOpenEvidence({ id: ref, quote: part.quote ?? part.value ?? null })}
                className="inline-flex items-center gap-1 rounded border border-border bg-surface px-2 py-0.5 text-primary hover:bg-primary-soft">
                <ScrollText size={11} aria-hidden /> 查看证据原文
              </button>
            )}
            {kind === 'memory' && (
              <button type="button" onClick={() => setOpenMemoryRef(ref)}
                className="inline-flex items-center gap-1 rounded border border-border bg-surface px-2 py-0.5 text-primary hover:bg-primary-soft">
                <Link2 size={11} aria-hidden /> 在应用内查看这条记录
              </button>
            )}
            {kind === 'opaque' && (
              <span>（这个引用形态没有对应的授权读取入口，应用内不读取，也不替它拼地址）</span>
            )}
          </>
        ) : (
          <span>这条答案没有记录来源引用。</span>
        )}
      </p>

      {(part.still_uncertain ?? []).length > 0 && (
        <p className="mt-1 text-xs text-caution">
          仍不能判断：{(part.still_uncertain ?? []).join('、')}
        </p>
      )}

      <EvidenceDrawer evidenceId={openEvidence?.id ?? null}
        quote={openEvidence?.quote ?? null}
        onClose={() => setOpenEvidence(null)} />
      <MemoryRefDrawer memoryRef={openMemoryRef} onClose={() => setOpenMemoryRef(null)} />
    </li>
  );
}

/** 一组答案。空数组时如实说"没有可展示的答案",不假装"都核对过了"。 */
export function AnswerPartList({ parts, emptyHint }: {
  parts: SafetyAnsweredPartDto[] | undefined; emptyHint: string;
}): React.ReactElement {
  const list = parts ?? [];
  if (list.length === 0) return <p className="text-xs text-ink-muted">{emptyHint}</p>;
  return (
    <ul className="mt-1 space-y-1.5">
      {list.map((part, index) => <AnswerPartLine key={index} part={part} index={index} />)}
    </ul>
  );
}
