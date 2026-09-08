/**
 * 「记录 → 检查结果 → 证据」联动组件。
 * 来源规则(memory:* 与本地文件指针都不构造成可请求 URL):
 * - http(s):// 来源:新标签打开(用户主动点击)。
 * - memory:* 引用:应用内详情抽屉(服务端 /v1/memory/item 精确版本解析)。
 * - 本地文件路径 / 语料内部 URI:只展示为「本地检测器溯源」说明,不发起读取。
 */
import React, { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import {
  ArrowLeftRight, BookOpen, ExternalLink, FileSearch, Landmark, Link2,
  ScrollText,
} from 'lucide-react';
import { api } from '../api/client';
import { request } from '../api/http';
import type {
  ConflictDto, EvidenceReadDto, SourceRefDto, WarningDto,
} from '../api/types';
import {
  Badge, ConfidenceBadge, DetailDrawer, EmptyState, ErrorState,
  LoadingBlock, SectionTitle, SeverityBadge, TimeText,
} from './ui';

function isHttpUrl(uri: string | null | undefined): boolean {
  return !!uri && /^https?:\/\//i.test(uri);
}

function isMemoryRef(uri: string | null | undefined): boolean {
  return !!uri && uri.startsWith('memory:');
}

function isLocalPointer(uri: string | null | undefined): boolean {
  // 盘符路径 / 反斜杠路径 / data: 语料指针 —— 只说明,不读取
  return !!uri && !isHttpUrl(uri) && !isMemoryRef(uri);
}

// ---- memory ref 详情抽屉 ------------------------------------------------------

export function MemoryRefDrawer({ memoryRef, onClose }: {
  memoryRef: string | null; onClose: () => void;
}): React.ReactElement {
  const [drillRef, setDrillRef] = useState<string | null>(null);
  const activeRef = drillRef ?? memoryRef;
  const query = useQuery({
    queryKey: ['memoryItem', activeRef],
    queryFn: ({ signal }) => api.memoryItem(activeRef!, signal),
    enabled: !!activeRef,
    retry: false,
  });

  return (
    <DetailDrawer open={!!memoryRef} onOpenChange={(open) => { if (!open) { setDrillRef(null); onClose(); } }}
      title={drillRef ? (
        <button type="button" onClick={() => setDrillRef(null)}
          className="inline-flex items-center gap-1 text-sm text-primary">
          <ArrowLeftRight size={14} aria-hidden /> 返回上级 · {drillRef}
        </button>
      ) : `记录详情 · ${memoryRef ?? ''}`}>
      {query.isPending && <LoadingBlock />}
      {query.isError && (
        <ErrorState error={query.error}
          title="无法解析这条引用(可能是版本不匹配或引用不存在)" />
      )}
      {query.data && <MemoryItemDetail data={query.data} onOpenRef={setDrillRef} />}
    </DetailDrawer>
  );
}

function MemoryItemDetail({ data, onOpenRef }: {
  data: Awaited<ReturnType<typeof api.memoryItem>>;
  onOpenRef: (ref: string) => void;
}): React.ReactElement {
  const { item, layer } = data;
  return (
    <div className="space-y-4 text-sm">
      <dl className="space-y-2">
        <Field label="层">{layer}</Field>
        {layer === 'semantic' && (
          <>
            <Field label="类别">{String(item.namespace ?? '未记录')}</Field>
            <Field label="内容">{prettyValue(item.value)}</Field>
            <Field label="状态">{String(item.status ?? '未记录')}</Field>
            <Field label="核实状态">{verificationLabel(String(item.verification_status ?? ''))}</Field>
            <Field label="生效时间">
              {item.valid_from ? <TimeText iso={String(item.valid_from)} /> : '未记录(生效时间不明)'}
            </Field>
            <Field label="记录时间"><TimeText iso={String(item.created_at ?? '')} /></Field>
            <Field label="版本">v{String(item.version ?? '?')}</Field>
          </>
        )}
        {layer === 'medication' && (
          <>
            <Field label="药名">{String(item.display_name ?? '未记录')}</Field>
            <Field label="剂量">{String(item.dose ?? '未记录')}</Field>
            <Field label="途径">{String(item.route ?? '未记录')}</Field>
            <Field label="频次">{String(item.schedule ?? '未记录')}</Field>
            <Field label="状态">{String(item.status ?? '未记录')}</Field>
            <Field label="开始时间"><TimeText iso={String(item.start_at ?? '')} /></Field>
            {item.end_at ? <Field label="结束时间"><TimeText iso={String(item.end_at)} /></Field> : null}
            <Field label="版本">v{String(item.version ?? '?')}</Field>
          </>
        )}
        {layer === 'episodic' && (
          <>
            <Field label="事件类型">{String(item.event_type ?? '未记录')}</Field>
            <Field label="实际发生时间"><TimeText iso={String(item.occurred_at ?? '')} /></Field>
            <Field label="系统记录时间"><TimeText iso={String(item.recorded_at ?? '')} /></Field>
            <Field label="原始报告">{String(item.payload && (item.payload as Record<string, unknown>).reported_text || item.text || '未记录')}</Field>
          </>
        )}
        {layer === 'conflict' && (
          <>
            <Field label="冲突类型">{String(item.conflict_type ?? '未记录')}</Field>
            <Field label="说明">{String(item.description ?? '')}</Field>
            <Field label="状态">{String(item.status ?? '未记录')}</Field>
          </>
        )}
        {layer === 'conclusion' && (
          <>
            <Field label="结论">{String(item.text ?? '')}</Field>
            <Field label="状态">{String(item.status ?? '未记录')}</Field>
          </>
        )}
      </dl>

      {(layer === 'conflict' || (typeof item.left_ref === 'string' && typeof item.right_ref === 'string')) && (
        <div className="grid grid-cols-1 gap-2 rounded-lg border border-border bg-surface-alt p-3">
          {(['left_ref', 'right_ref'] as const).map((side) => {
            const value = item[side];
            return typeof value === 'string' ? (
              <button key={side} type="button" onClick={() => onOpenRef(value)}
                className="inline-flex items-center gap-1.5 rounded-lg border border-border bg-surface px-3 py-2 text-left font-mono text-xs hover:bg-primary-soft">
                <Link2 size={13} aria-hidden />
                {side === 'left_ref' ? '甲方记录' : '乙方证据'}:{value}
              </button>
            ) : null;
          })}
        </div>
      )}

      <div>
        <h3 className="mb-1 font-medium">处理记录(审计)</h3>
        {data.audit_log.length === 0 ? (
          <p className="text-ink-muted">没有已落库的处理记录。</p>
        ) : (
          <ul className="space-y-1.5">
            {data.audit_log.map((entry) => (
              <li key={entry.id} className="rounded-lg border border-border px-3 py-2">
                <span className="font-medium">{entry.action}</span>
                <span className="ml-2 text-xs text-ink-muted">
                  <TimeText iso={entry.created_at} /> · {entry.actor || entry.source || '系统'}
                </span>
                {entry.details && Object.keys(entry.details).length > 0 && (
                  <pre className="mt-1 overflow-x-auto whitespace-pre-wrap break-all rounded bg-code-bg p-2 font-mono text-xs">
                    {JSON.stringify(entry.details, null, 2)}
                  </pre>
                )}
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}

function verificationLabel(status: string): string {
  if (status === 'verified') return '记录已核实(见处理记录中的核实依据)';
  if (status === 'disputed') return '存在待核实信息';
  return '按报告记录';
}

export function Field({ label, children }: {
  label: string; children: React.ReactNode;
}): React.ReactElement {
  return (
    <div className="grid grid-cols-[7.5rem_1fr] gap-2">
      <dt className="text-ink-muted">{label}</dt>
      <dd className="min-w-0 break-words">{children}</dd>
    </div>
  );
}

function prettyValue(value: unknown): string {
  if (value == null) return '未记录';
  if (typeof value === 'object') return JSON.stringify(value);
  return String(value);
}

// ---- 证据原文抽屉(Product P1)-------------------------------------------------

/**
 * 受控证据原文回读:按 evidence_id 分页读取不可变证据记录(服务端复验哈希)。
 * 高亮规则:只在原文中找到引用摘录的**精确字符串**时标记;找不到时明确降级
 * 为"未能定位",绝不按语义相似高亮。
 */
export function EvidenceDrawer({ evidenceId, quote, onClose }: {
  evidenceId: string | null; quote?: string | null; onClose: () => void;
}): React.ReactElement {
  return <EvidenceDrawerContent key={evidenceId ?? 'closed'}
    evidenceId={evidenceId} quote={quote} onClose={onClose} />;
}

function EvidenceDrawerContent({ evidenceId, quote, onClose }: {
  evidenceId: string | null; quote?: string | null; onClose: () => void;
}): React.ReactElement {
  const [pages, setPages] = useState<EvidenceReadDto[]>([]);
  const PAGE = 2000;
  const lastPage = pages.length > 0 ? pages[pages.length - 1] : undefined;
  const nextOffset = lastPage ? lastPage.offset + lastPage.returned_chars : 0;
  const query = useQuery({
    queryKey: ['evidenceRead', evidenceId, nextOffset],
    queryFn: ({ signal }) => api.evidenceRead(evidenceId!, nextOffset, PAGE, signal),
    enabled: !!evidenceId,
    retry: false,
  });
  // 关闭抽屉时清空已读页,下次打开重新按当前记录读取。
  React.useEffect(() => { if (!evidenceId) setPages([]); }, [evidenceId]);
  const fresh = query.data;
  const known = pages.some((p) => p.evidence_id === fresh?.evidence_id && p.offset === fresh.offset);
  const all: EvidenceReadDto[] = known ? [...pages] : (fresh ? [...pages, fresh] : pages);
  const fullText = all.map((p) => p.content).join('');
  const meta = fresh?.source;
  const total = fresh?.total_chars;
  const lastAll = all.length > 0 ? all[all.length - 1] : undefined;
  const complete = lastAll !== undefined && !lastAll.truncated;
  const located = !!quote && fullText.includes(quote);
  const quotePos = located && quote ? fullText.indexOf(quote) : -1;

  return (
    <DetailDrawer open={!!evidenceId} onOpenChange={(open) => { if (!open) onClose(); }}
      title={`证据原文 · ${evidenceId ?? ''}`}>
      {query.isPending && <LoadingBlock label="读取证据原文…" />}
      {query.isError && (
        <ErrorState error={query.error}
          title="证据原文不可用(可能已过期清理、权限范围不符或完整性校验失败)" />
      )}
      {fresh && !query.isError && (
        <div className="space-y-3 text-sm">
          <dl className="space-y-2 rounded-lg border border-border bg-surface-alt p-3">
            <Field label="来源类型">{meta?.source_type ?? '未记录'}</Field>
            <Field label="来源地址">
              {meta?.uri
                ? (isHttpUrl(meta.uri)
                  ? <a href={meta.uri} target="_blank" rel="noopener noreferrer"
                      className="break-all font-mono text-xs text-primary underline">{meta.uri}</a>
                  : <span className="break-all font-mono text-xs">{meta.uri}</span>)
                : '未记录'}
            </Field>
            <Field label="来源版本">{meta?.corpus_version ?? '未知(来源未记录版本)'}</Field>
            <Field label="检索时间">{meta?.retrieved_at ? <TimeText iso={meta.retrieved_at} /> : '未记录'}</Field>
            <Field label="完整性">{fresh.integrity === 'verified' ? '已通过哈希校验' : fresh.integrity}</Field>
            <Field label="长度">{fresh.total_chars} 字符</Field>
          </dl>

          <div>
            <h3 className="mb-1 flex items-center gap-1 font-medium">
              <ScrollText size={14} aria-hidden /> 原文内容
            </h3>
            {all.length === 0 ? (
              <LoadingBlock />
            ) : (
              <pre className="max-h-96 overflow-auto whitespace-pre-wrap break-words rounded-lg border border-border bg-surface-alt p-3 text-sm leading-relaxed">
                {quote && located ? (
                  <>
                    {fullText.slice(0, quotePos)}
                    <mark className="rounded bg-caution-soft px-0.5 text-inherit" data-testid="evidence-highlight">
                      {fullText.slice(quotePos, quotePos + quote.length)}
                    </mark>
                    {fullText.slice(quotePos + quote.length)}
                  </>
                ) : fullText}
              </pre>
            )}
            {quote && !located && (
              <p className="mt-1 text-xs text-ink-muted">
                未能在已读取的原文中精确定位该引用摘录——不做语义近似高亮;可用"继续读取"查看剩余部分。
              </p>
            )}
            <p className="mt-1 text-xs text-ink-muted">
              {complete
                ? '已读取全部原文。'
                : `已读取 ${fullText.length}/${total ?? '?'} 字符。`}
            </p>
            {!complete && (
              <button type="button"
                onClick={() => { if (fresh && !known) setPages([...pages, fresh]); }}
                disabled={!fresh || known}
                className="mt-2 rounded-lg border border-border px-3 py-1.5 text-sm hover:bg-surface-alt disabled:opacity-50">
                继续读取
              </button>
            )}
          </div>
          <p className="text-xs text-ink-muted">
            原文为不可变证据记录(内容寻址、读取时复验哈希);它佐证"说明书/来源写了什么",不构成医学结论。
          </p>
          {evidenceId && quote && <ClaimAssessment evidenceId={evidenceId} quote={quote} />}
        </div>
      )}
    </DetailDrawer>
  );
}

function ClaimAssessment({ evidenceId, quote }: { evidenceId: string; quote: string }): React.ReactElement {
  const [entities, setEntities] = useState('');
  const [result, setResult] = useState<{status: string; unresolved: string[]} | null>(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  async function assess() {
    setBusy(true); setError(''); setResult(null);
    try { setResult(await request('/v1/evidence/assess-claim', { method: 'POST', body: { evidence_id: evidenceId, quote, entities: entities.split(/[,，]/).map(s => s.trim()).filter(Boolean), conditions_known: true } })); }
    catch (e) { setError(e instanceof Error ? e.message : '核对失败'); }
    finally { setBusy(false); }
  }
  return <details className="border-t border-border pt-3"><summary className="cursor-pointer text-sm font-medium">核对摘录中的相互作用表述</summary><p className="my-2 text-xs text-ink-muted">本地文字规则仅筛查原文表述。引用真实不代表结论成立，也不能判断是否适用于患者。</p><label className="block text-sm">药名（逗号分隔）<input className="my-2 w-full rounded border border-border p-2" value={entities} onChange={e => { setEntities(e.target.value); setResult(null); }} /></label><button disabled={busy || !entities.trim()} className="rounded border border-border px-3 py-2 text-sm disabled:opacity-50" onClick={() => void assess()}>核对这段摘录</button>{error && <p role="alert">{error}</p>}{result && <p role="status" className="mt-2 text-sm">{({supported:'原文有支持相互作用的明确表述；仍需核实适用条件',contradicted:'原文包含否定相互作用的表述；不能据此认定无风险',insufficient:'依据不足：引用、实体或适用条件未满足核对要求'} as Record<string,string>)[result.status]}</p>}</details>;
}

/** 预警状态 → 用户可理解标签(Product P1 状态展示)。 */
export function alertStatusInfo(status: string, opts: {
  evidenceAvailable: boolean; recheckStatus?: string | null; hasSuccessor?: boolean;
}): { label: string; tone: 'primary' | 'caution' | 'neutral' | 'danger' } {
  if (status === 'stale') {
    return { label: '依据变化 · 待重查(不等于风险解除)', tone: 'caution' };
  }
  if (status === 'current' && !opts.evidenceAvailable) {
    return { label: '证据不足(无来源关联,需人工核对)', tone: 'neutral' };
  }
  if (status === 'current' && opts.recheckStatus === 'done' && opts.hasSuccessor) {
    return { label: '当前有效(已按最新事实重查)', tone: 'primary' };
  }
  if (status === 'current') {
    return { label: '当前有效', tone: 'primary' };
  }
  return { label: `状态:${status}`, tone: 'neutral' };
}

// ---- 来源展示 ---------------------------------------------------------------

export function SourceRefList({ sources, emptyHint = '这条记录没有可核对的来源。' }: {
  sources: SourceRefDto[] | undefined; emptyHint?: string;
}): React.ReactElement {
  const [openRef, setOpenRef] = useState<string | null>(null);
  const [openEvidence, setOpenEvidence] = useState<{ id: string; quote: string | null } | null>(null);
  if (!sources || sources.length === 0) {
    return <p className="text-sm text-ink-muted">{emptyHint}</p>;
  }
  return (
    <>
      <ul className="space-y-2">
        {sources.map((source, index) => (
          <SourceRefItem key={index} source={source} onOpenRef={setOpenRef}
            onOpenEvidence={(id, quote) => setOpenEvidence({ id, quote })} index={index} />
        ))}
      </ul>
      <MemoryRefDrawer memoryRef={openRef} onClose={() => setOpenRef(null)} />
      <EvidenceDrawer evidenceId={openEvidence?.id ?? null}
        quote={openEvidence?.quote ?? null}
        onClose={() => setOpenEvidence(null)} />
    </>
  );
}

function SourceRefItem({ source, onOpenRef, onOpenEvidence, index }: {
  source: SourceRefDto; onOpenRef: (ref: string) => void;
  onOpenEvidence: (evidenceId: string, quote: string | null) => void; index: number;
}): React.ReactElement {
  const uri = source.uri ?? null;
  const quote = source.quote ?? source.text ?? null;
  return (
    <li className="rounded-lg border border-border bg-surface-alt p-3">
      <div className="flex flex-wrap items-center gap-2">
        <SourceTypeBadge sourceType={source.source_type} />
        {source.retrieval && (
          <span className="text-xs text-ink-muted">检索方式:{source.retrieval}</span>
        )}
        {source.evidence_id && (
          <button type="button"
            onClick={() => onOpenEvidence(source.evidence_id!, quote)}
            data-testid={`evidence-open-${index}`}
            className="ml-auto inline-flex items-center gap-1 rounded border border-border bg-surface px-2 py-1 text-xs text-primary hover:bg-primary-soft">
            <ScrollText size={12} aria-hidden /> 查看证据原文
          </button>
        )}
      </div>
      {isHttpUrl(uri) && (
        <a href={uri!} target="_blank" rel="noopener noreferrer"
          className="mt-1 inline-flex items-center gap-1 break-all font-mono text-xs text-primary underline">
          <ExternalLink size={12} aria-hidden />
          {uri}
        </a>
      )}
      {isMemoryRef(uri) && (
        <button type="button" onClick={() => onOpenRef(uri!)}
          className="mt-1 inline-flex items-center gap-1 rounded border border-border bg-surface px-2 py-1 font-mono text-xs text-primary hover:bg-primary-soft">
          <Link2 size={12} aria-hidden /> 在应用内查看记录:{uri}
        </button>
      )}
      {isLocalPointer(uri) && (
        <p className="mt-1 flex items-start gap-1 text-xs text-ink-muted">
          <FileSearch size={12} aria-hidden />
          本地检测器溯源(本地文件指针,应用内不直接读取文件;该来源没有中文说明书原文)。
        </p>
      )}
      {quote ? (
        <blockquote className="mt-2 border-l-2 border-primary/40 pl-2 text-sm leading-relaxed text-ink-secondary">
          「{quote}」
          {source.evidence_id && (
            <span className="ml-1 align-middle text-xs text-ink-muted">(摘录;完整原文与版本见"查看证据原文")</span>
          )}
        </blockquote>
      ) : (
        <p className="mt-2 text-xs text-ink-muted">
          {source.evidence_id
            ? '该来源没有保存原文摘录,可通过"查看证据原文"读取。'
            : '原文不可用:这条历史记录没有关联的证据原文(系统不会虚构原文)。'}
        </p>
      )}
      {/* index 仅用于稳定 key */}
      <span hidden>{index}</span>
    </li>
  );
}

function SourceTypeBadge({ sourceType }: { sourceType: string | undefined }): React.ReactElement {
  const known: Record<string, { label: string; icon: React.ReactNode }> = {
    drug_label_or_kegg: { label: '说明书 / KEGG 佐证', icon: <BookOpen size={12} aria-hidden /> },
    local_detector_provenance: { label: '本地检测器溯源', icon: <FileSearch size={12} aria-hidden /> },
    agent_inference_disclosure: { label: '系统推理说明(需人工判断)', icon: <Landmark size={12} aria-hidden /> },
  };
  const knownEntry = sourceType ? known[sourceType] : undefined;
  if (!knownEntry) {
    return (
      <Badge tone="neutral">{sourceType || '来源类型未记录'}</Badge>
    );
  }
  return <Badge tone="primary" icon={knownEntry.icon}>{knownEntry.label}</Badge>;
}

// ---- 预警卡片(事件结果 / 总览 / 预警中心复用)-----------------------------------

export function WarningCard({ warning, defaultOpen = false }: {
  warning: WarningDto; defaultOpen?: boolean;
}): React.ReactElement {
  const [open, setOpen] = useState(defaultOpen);
  const isIndividual = warning.drug_b === '患者个体风险';
  const sources: SourceRefDto[] = [
    ...(warning.citations ?? []),
    ...(warning.additional_sources ?? []),
  ];
  return (
    <article className="rounded-card border border-border bg-surface">
      <div className="flex flex-wrap items-center gap-2 px-4 py-3">
        <SeverityBadge severity={warning.severity} />
        <span className="font-medium">
          {warning.drug_a}{isIndividual ? '' : ` × ${warning.drug_b}`}
        </span>
        <ConfidenceBadge confidence={warning.confidence} />
        <button type="button" onClick={() => setOpen(!open)}
          aria-expanded={open}
          className="ml-auto inline-flex items-center gap-1 rounded-lg border border-border px-2.5 py-1 text-sm hover:bg-surface-alt">
          <Landmark size={13} aria-hidden />
          {open ? '收起证据' : '查看风险描述与证据'}
        </button>
      </div>
      {open && (
        <div className="space-y-3 border-t border-border px-4 py-3 text-sm">
          <Field label="机制">{warning.mechanism ?? '未记录'}</Field>
          <Field label="可能影响">{warning.effect ?? '未记录'}</Field>
          <Field label="处置参考">{warning.management ?? '未记录'}</Field>
          <div>
            <h4 className="mb-1 font-medium">来源与原文</h4>
            <SourceRefList sources={sources} />
          </div>
          {warning.audit_trail?.warning_memory && (
            <p className="font-mono text-xs text-ink-muted">
              情景记录:{warning.audit_trail.warning_memory}
            </p>
          )}
          <p className="text-xs text-ink-muted">
            以上为已记录的风险信息,不是用药指令;如有疑问请咨询医生/药师。
          </p>
        </div>
      )}
    </article>
  );
}

// ---- 冲突并排展示 ------------------------------------------------------------

export function ConflictSides({ conflict }: { conflict: ConflictDto }): React.ReactElement {
  return (
    <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
      {([['left_ref', '甲方记录(患者/照护报告)'], ['right_ref', '乙方证据(记录/说明书佐证)']] as const)
        .map(([side, label]) => (
          <div key={side} className="rounded-card border border-border bg-surface-alt p-3">
            <p className="mb-1 flex items-center gap-1 text-xs font-medium text-ink-muted">
              <ArrowLeftRight size={12} aria-hidden /> {label}
            </p>
            <RefSummaryLine refString={conflict[side]} />
          </div>
        ))}
    </div>
  );
}

function RefSummaryLine({ refString }: { refString: string }): React.ReactElement {
  const query = useQuery({
    queryKey: ['memoryItem', refString],
    queryFn: ({ signal }) => api.memoryItem(refString, signal),
    retry: false,
  });
  if (query.isPending) return <LoadingBlock label="读取记录…" />;
  if (query.isError) {
    return <p className="text-sm text-danger">引用无法解析:{refString}</p>;
  }
  const item = query.data.item as Record<string, unknown>;
  return (
    <div>
      <p className="font-mono text-xs text-ink-muted">{refString} · v{String(item.version ?? '?')}</p>
      <p className="mt-1 text-sm">
        {String(item.display_name ?? prettyValue(item.value) ?? item.text ?? '记录内容见详情')}
      </p>
    </div>
  );
}

export function ConflictCard({ conflict, onOpenRef, footer }: {
  conflict: ConflictDto;
  onOpenRef?: (ref: string) => void;
  footer?: React.ReactNode;
}): React.ReactElement {
  return (
    <article className="rounded-card border border-border bg-surface">
      <div className="px-4 py-3">
        <div className="flex flex-wrap items-center gap-2">
          <Badge tone="caution" icon={<ArrowLeftRight size={13} aria-hidden />}>待核实</Badge>
          <span className="text-sm text-ink-muted">{conflict.conflict_type}</span>
          <span className="ml-auto"><TimeText iso={conflict.created_at} prefix="创建于 " /></span>
        </div>
        <p className="mt-2 text-sm">{conflict.description}</p>
        <div className="mt-2 flex flex-wrap gap-2">
          {[conflict.left_ref, conflict.right_ref].map((ref) => (
            <button key={ref} type="button" onClick={() => onOpenRef?.(ref)}
              className="rounded border border-border bg-surface-alt px-2 py-1 font-mono text-xs text-primary hover:bg-primary-soft">
              <Link2 size={11} className="mr-1 inline" aria-hidden />{ref}
            </button>
          ))}
        </div>
        {footer && <div className="mt-3">{footer}</div>}
      </div>
    </article>
  );
}

export { SectionTitle, EmptyState };
