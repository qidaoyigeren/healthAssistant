/**
 * 用药记录:当前药单 + 三种真实记录动作(新增/剂量变更/停用)+
 * 完整版本历史(含 stopped/superseded,来自 /v1/medication-records)。
 * 没有记录的剂量/频次如实显示「未记录」;不提供剂量计算或推荐。
 */
import React, { useMemo, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { zodResolver } from '@hookform/resolvers/zod';
import { useForm } from 'react-hook-form';
import { z } from 'zod';
import { History } from 'lucide-react';
import { api } from '../../api/client';
import { qk } from '../../api/queryKeys';
import { submissions } from '../../api/submissions';
import { useSessionId, useSubmissions } from '../../hooks/useSubmissions';
import type { MedicationDto, MedicationRecordDto } from '../../api/types';
import {
  Badge, Card, EmptyState, ErrorState, SectionTitle, SkeletonList,
} from '../../components/ui';
import { Field } from '../../components/evidence';
import { SubmissionResultView, TaskTray } from '../shared/submission';

type TabKind = 'add' | 'dose_change' | 'remove';

const TAB_LABELS: Record<TabKind, string> = {
  add: '新增已报告用药',
  dose_change: '记录剂量/用法变更',
  remove: '记录停用',
};

const medSchema = z.object({
  medication: z.string().min(1, '请填写药名').max(120),
  dose: z.string().max(120),
  route: z.string().max(60),
  schedule: z.string().max(120),
  note: z.string().max(500),
});
type MedForm = z.infer<typeof medSchema>;

export function MedicationsPage(): React.ReactElement {
  const stateQuery = useQuery({
    queryKey: qk.memoryState(),
    queryFn: ({ signal }) => api.memoryState({}, signal),
  });
  const recordsQuery = useQuery({
    queryKey: qk.medicationRecords('all'),
    queryFn: ({ signal }) => api.medicationRecords({ status: 'all', limit: 50 }, signal),
  });

  return (
    <div className="space-y-4">
      <header>
        <h1 className="text-xl font-semibold">用药记录</h1>
        <p className="mt-1 text-sm text-ink-secondary">
          记录的是照护中实际报告/发生的变化;名称识别由后端药名映射完成。
        </p>
      </header>

      <Card>
        <SectionTitle>当前药单</SectionTitle>
        {stateQuery.isPending && <SkeletonList rows={3} />}
        {stateQuery.isError && <div className="p-4"><ErrorState error={stateQuery.error} /></div>}
        {stateQuery.data && (
          <MedicationTable medications={stateQuery.data.medications}
            onHistory={recordsQuery.refetch} />
        )}
      </Card>

      {stateQuery.data && (
        <MedicationForms medications={stateQuery.data.medications} />
      )}

      <Card>
        <SectionTitle actions={
          <span className="text-xs text-ink-muted">
            {recordsQuery.data ? `共 ${recordsQuery.data.total} 条版本记录` : ''}
          </span>
        }>
          完整用药历史(含已停用/被替代)
        </SectionTitle>
        {recordsQuery.isPending && <SkeletonList rows={3} />}
        {recordsQuery.isError && <div className="p-4"><ErrorState error={recordsQuery.error} /></div>}
        {recordsQuery.data && recordsQuery.data.items.length === 0 && (
          <div className="p-4">
            <EmptyState title="还没有任何用药版本记录" hint="提交第一条用药记录后,这里会出现完整版本链。" />
          </div>
        )}
        {recordsQuery.data && recordsQuery.data.items.length > 0 && (
          <MedicationHistoryList records={recordsQuery.data.items} />
        )}
      </Card>

      <TaskTrayWrapper />
    </div>
  );
}

function TaskTrayWrapper(): React.ReactElement {
  const tasks = useSubmissions();
  return <TaskTray tasks={tasks} />;
}

function MedicationTable({ medications }: {
  medications: MedicationDto[]; onHistory: () => void;
}): React.ReactElement {
  if (medications.length === 0) {
    return (
      <div className="p-4">
        <EmptyState title="当前没有在用药记录" hint="用下方「新增已报告用药」记录第一条。" />
      </div>
    );
  }
  return (
    <ul className="divide-y divide-border px-4 pb-3">
      {medications.map((medication) => (
        <li key={medication.ref} className="py-3">
          <div className="flex flex-wrap items-center gap-2">
            <span className="font-medium">{medication.display_name}</span>
            <Badge tone="primary">在用</Badge>
            {medication.ingredients.length > 0 && (
              <span className="text-xs text-ink-muted">成分:{medication.ingredients.map((ingredient) =>
                typeof ingredient === 'string' ? ingredient
                  : ingredient.name_cn || ingredient.name_en || '未记录名称').join(' / ')}</span>
            )}
          </div>
          <div className="mt-1 grid grid-cols-2 gap-x-4 gap-y-1 text-sm md:grid-cols-4">
            <span className="text-ink-secondary">剂量:{medication.dose ?? '未记录'}</span>
            <span className="text-ink-secondary">途径:{medication.route ?? '未记录'}</span>
            <span className="text-ink-secondary">频次:{medication.schedule ?? '未记录'}</span>
            <span className="text-ink-secondary">开始:{medication.start_at?.slice(0, 10) ?? '未记录'}</span>
          </div>
        </li>
      ))}
    </ul>
  );
}

function MedicationForms({ medications }: {
  medications: MedicationDto[];
}): React.ReactElement {
  const [tab, setTab] = useState<TabKind>('add');
  return (
    <Card>
      <SectionTitle>记录用药变化</SectionTitle>
      <div role="tablist" aria-label="用药记录动作" className="flex flex-wrap gap-2 px-4 pt-3">
        {(Object.keys(TAB_LABELS) as TabKind[]).map((kind) => (
          <button key={kind} type="button" role="tab" aria-selected={tab === kind}
            onClick={() => setTab(kind)}
            className={`rounded-lg border px-3 py-1.5 text-sm ${
              tab === kind ? 'border-primary bg-primary-soft font-medium text-primary-strong' : 'border-border hover:bg-surface-alt'
            }`}>
            {TAB_LABELS[kind]}
          </button>
        ))}
      </div>
      <div className="p-4">
        <MedicationForm key={tab} kind={tab} medications={medications} />
      </div>
    </Card>
  );
}

function MedicationForm({ kind, medications }: {
  kind: TabKind; medications: MedicationDto[];
}): React.ReactElement {
  const [sessionId] = useSessionId();
  const tasks = useSubmissions();
  const activeList = useMemo(
    () => medications.filter((m) => m.status === 'active'), [medications]);
  const {
    register, handleSubmit, watch, reset, formState: { errors },
  } = useForm<MedForm>({
    resolver: zodResolver(medSchema),
    defaultValues: { medication: '', dose: '', route: '', schedule: '', note: '' },
  });
  const [resultKey, setResultKey] = useState<string | null>(null);
  const [live, setLive] = useState<string | null>(null);
  const [selectedExisting, setSelectedExisting] = useState<string>('');

  const pickExisting = (ref: string) => {
    setSelectedExisting(ref);
    const found = activeList.find((m) => m.ref === ref);
    if (!found) return;
    if (kind === 'dose_change') {
      // 回填旧值,预期完整新值,避免未改字段被置空
      reset({
        medication: found.display_name,
        dose: found.dose ?? '',
        route: found.route ?? '',
        schedule: found.schedule ?? '',
        note: '',
      });
    } else if (kind === 'remove') {
      reset({ medication: found.display_name, dose: '', route: '', schedule: '', note: '' });
    }
  };

  const onSubmit = handleSubmit((values) => {
    const payload: Record<string, unknown> = {
      action: kind,
      medication: values.medication.trim(),
    };
    if (kind !== 'remove') {
      if (values.dose.trim()) payload.dose = values.dose.trim();
      if (values.route.trim()) payload.route = values.route.trim();
      if (values.schedule.trim()) payload.schedule = values.schedule.trim();
    }
    const textByKind: Record<TabKind, string> = {
      add: `新增${values.medication.trim()}。`,
      dose_change: `${values.medication.trim()} 剂量/用法变更为 ${[values.dose, values.route, values.schedule].filter(Boolean).join(' / ') || '(见结构化字段)'}。${values.note.trim()}`,
      remove: `记录停用${values.medication.trim()}。${values.note.trim()}`,
    };
    const key = submissions.submit({
      event_type: 'medication_change',
      text: textByKind[kind],
      payload,
      source: 'caregiver',
      occurred_at: null,
      session_id: sessionId,
    });
    setResultKey(key);
    setLive('提交已受理,正在处理。');
  });

  const resultTask = resultKey ? submissions.task(resultKey) : undefined;

  return (
    <form onSubmit={onSubmit} className="space-y-3" noValidate>
      {kind !== 'add' && (
        <label className="block text-sm">
          <span className="mb-1 block font-medium">从当前药单选择(避免重名/别名歧义)</span>
          <select value={selectedExisting} onChange={(e) => pickExisting(e.target.value)}
            className={inputClass}>
            <option value="">— 选择在用药 —</option>
            {activeList.map((m) => (
              <option key={m.ref} value={m.ref}>
                {m.display_name}{m.dose ? `(剂量 ${m.dose})` : ''}
              </option>
            ))}
          </select>
        </label>
      )}

      <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
        <Field2 label="药名(商品名或通用名)" error={errors.medication?.message}>
          <input type="text" {...register('medication')} className={inputClass}
            placeholder={kind === 'add' ? '如:氨氯地平 / 络活喜' : '从上方选择或输入'} />
        </Field2>
        {kind !== 'remove' && (
          <>
            <Field2 label="剂量" hint="没有就留空,显示「未记录」,不填推荐值">
              <input type="text" {...register('dose')} className={inputClass} placeholder="如:5mg" />
            </Field2>
            <Field2 label="给药途径">
              <input type="text" {...register('route')} className={inputClass} placeholder="如:口服" />
            </Field2>
            <Field2 label="频次">
              <input type="text" {...register('schedule')} className={inputClass} placeholder="如:每日一次" />
            </Field2>
          </>
        )}
      </div>

      <Field2 label="报告说明(可选)" hint="说明保留在事件的原始报告文字中">
        <input type="text" {...register('note')} className={inputClass}
          placeholder="如:出院后开始服用" />
      </Field2>

      <p className="rounded-lg bg-surface-alt px-3 py-2 text-xs text-ink-secondary">
        提交内容预览:将{kind === 'add' ? '新增' : kind === 'dose_change' ? '记录剂量/用法变更' : '记录停用'}
        「{watch('medication') || '…'}」
        {kind === 'dose_change' && ` 为 ${[watch('dose'), watch('route'), watch('schedule')].filter(Boolean).join(' / ') || '未改动任何字段'}`}。
        同名重复新增时,后端可能去重或视为变更,以实际操作结果为准。
      </p>

      <button type="submit" className="rounded-lg bg-primary px-4 py-2 font-medium text-white hover:bg-primary-strong">
        {kind === 'add' ? '记录新增用药' : kind === 'dose_change' ? '记录剂量变更' : '记录停用'}
      </button>

      <div aria-live="polite" role="status" className="sr-only">{live}</div>
      {resultTask && <SubmissionResultView task={resultTask} onClose={() => setResultKey(null)} />}
      {tasks.some((t) => ['failed', 'rejected'].includes(t.status)) && (
        <p className="text-xs text-danger">有提交失败,详见上方任务列表;您填写的内容仍保留在表单中。</p>
      )}
    </form>
  );
}

function MedicationHistoryList({ records }: {
  records: MedicationRecordDto[];
}): React.ReactElement {
  const [detailId, setDetailId] = useState<number | null>(null);
  const detailQuery = useQuery({
    queryKey: qk.medicationRecord(detailId ?? ''),
    queryFn: ({ signal }) => api.medicationRecord(detailId!, signal),
    enabled: detailId != null,
  });
  const statusTone: Record<string, 'primary' | 'neutral' | 'caution'> = {
    active: 'primary', stopped: 'neutral', superseded: 'neutral', disputed: 'caution',
  };
  return (
    <>
      <ul className="divide-y divide-border px-4 pb-3">
        {records.map((record) => (
          <li key={record.ref} className="flex flex-wrap items-center gap-2 py-2.5 text-sm">
            <span className="font-medium">{record.display_name}</span>
            <Badge tone={statusTone[record.status] ?? 'neutral'}>{record.status}</Badge>
            {record.dose && <span className="text-ink-secondary">{record.dose}</span>}
            {record.route && <span className="text-ink-secondary">{record.route}</span>}
            {record.schedule && <span className="text-ink-secondary">{record.schedule}</span>}
            <span className="text-xs text-ink-muted">
              v{record.version} · {record.start_at?.slice(0, 10)}
              {record.end_at ? ` → ${record.end_at.slice(0, 10)}` : ''}
            </span>
            <button type="button" onClick={() => setDetailId(record.id)}
              className="ml-auto inline-flex items-center gap-1 rounded-lg border border-border px-2 py-1 text-xs hover:bg-surface-alt">
              <History size={12} aria-hidden /> 版本链
            </button>
          </li>
        ))}
      </ul>
      {detailId != null && (
        <Card className="m-4 mt-0 border-primary/40">
          <div className="px-4 pt-3">
            <h3 className="text-sm font-medium">版本链(旧 → 新)</h3>
          </div>
          <div className="p-4 text-sm">
            {detailQuery.isPending && <SkeletonList rows={2} />}
            {detailQuery.isError && <ErrorState error={detailQuery.error} />}
            {detailQuery.data && (
              <ol className="space-y-2">
                {detailQuery.data.versions?.map((version, index) => (
                  <li key={version.ref} className="rounded-lg border border-border bg-surface-alt p-3">
                    <p className="flex flex-wrap items-center gap-2">
                      <span className="font-medium">v{version.version}</span>
                      <Badge tone={statusTone[version.status] ?? 'neutral'}>{version.status}</Badge>
                      {index > 0 && (
                        <span className="text-xs text-ink-muted">
                          ← 前一版本 v{(detailQuery.data.versions?.[index - 1])?.version}
                        </span>
                      )}
                    </p>
                    <dl className="mt-1.5">
                      <Field label="剂量">{version.dose ?? '未记录'}</Field>
                      <Field label="途径">{version.route ?? '未记录'}</Field>
                      <Field label="频次">{version.schedule ?? '未记录'}</Field>
                      <Field label="期间">{version.start_at?.slice(0, 10) ?? '?'} → {version.end_at?.slice(0, 10) ?? '至今'}</Field>
                    </dl>
                  </li>
                ))}
              </ol>
            )}
          </div>
        </Card>
      )}
    </>
  );
}

function Field2({ label, hint, error, children }: {
  label: string; hint?: string; error?: string; children: React.ReactNode;
}): React.ReactElement {
  return (
    <label className="block text-sm">
      <span className="mb-1 block font-medium">{label}</span>
      {children}
      {hint && <span className="mt-1 block text-xs text-ink-muted">{hint}</span>}
      {error && <span className="mt-1 block text-xs text-danger">{error}</span>}
    </label>
  );
}

const inputClass = 'w-full rounded-lg border border-border bg-surface px-3 py-2 text-base focus:border-primary focus:outline-none focus:ring-2 focus:ring-primary/30';
