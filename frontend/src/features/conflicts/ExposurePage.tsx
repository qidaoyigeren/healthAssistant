/**
 * 记录造影剂暴露(procedure_exposure):明确输入暴露物与医生参与,
 * 时间不明确时保存原始描述并如实标注;不支持任意手术/药械风险判定。
 */
import React, { useState } from 'react';
import { Link } from 'react-router-dom';
import { zodResolver } from '@hookform/resolvers/zod';
import { useForm } from 'react-hook-form';
import { z } from 'zod';
import { submissions } from '../../api/submissions';
import { useSessionId, useSubmissions } from '../../hooks/useSubmissions';
import {
  Badge, Card, EmptyState, SectionTitle,
} from '../../components/ui';
import { SubmissionResultView, TaskTray } from '../shared/submission';

const exposureSchema = z.object({
  agent: z.string().min(1, '请填写暴露物').max(80),
  doctor_involved: z.enum(['yes', 'no']),
  occurred_at: z.string().max(40),
  note: z.string().min(1, '请简单描述这次暴露(将按原文保存)').max(2000),
});
type ExposureForm = z.infer<typeof exposureSchema>;

export function ExposurePage(): React.ReactElement {
  const [sessionId] = useSessionId();
  const tasks = useSubmissions();
  const {
    register, handleSubmit, formState: { errors },
  } = useForm<ExposureForm>({
    resolver: zodResolver(exposureSchema),
    defaultValues: { agent: '含碘造影剂', doctor_involved: undefined as unknown as 'yes' | 'no', occurred_at: '', note: '' },
  });
  const [resultKey, setResultKey] = useState<string | null>(null);
  const [live, setLive] = useState<string | null>(null);

  const onSubmit = handleSubmit((values) => {
    // 发生时间:仅当用户给出明确时间(yyyy-MM-dd 及以上)才作为 occurred_at;
    // 否则保留在原始描述中,不擅自换算成具体日期。
    const explicit = /^\d{4}-\d{2}-\d{2}/.test(values.occurred_at.trim());
    const occurredAt = explicit ? new Date(values.occurred_at.trim()).toISOString() : null;
    const text = values.note.trim()
      + (explicit ? '' : values.occurred_at.trim() ? `(时间描述:${values.occurred_at.trim()})` : '(发生时间未明确)');
    const key = submissions.submit({
      event_type: 'procedure_exposure',
      text,
      payload: {
        agent: values.agent.trim(),
        doctor_involved: values.doctor_involved === 'yes',
      },
      source: 'caregiver',
      occurred_at: occurredAt,
      session_id: sessionId,
    });
    setResultKey(key);
    setLive('提交已受理,正在处理。');
  });

  const resultTask = resultKey ? submissions.task(resultKey) : undefined;

  return (
    <div className="space-y-4">
      <header>
        <Link to="/" className="text-sm text-primary underline">← 返回总览</Link>
        <h1 className="mt-2 text-xl font-semibold">记录造影剂暴露</h1>
        <p className="mt-1 text-sm text-ink-secondary">
          当前支持的重点是含碘造影剂暴露;不声称已实现全种类手术/药械风险判定。
        </p>
      </header>

      <Card>
        <SectionTitle>暴露记录</SectionTitle>
        <form onSubmit={onSubmit} className="space-y-3 p-4" noValidate>
          <label className="block text-sm">
            <span className="mb-1 block font-medium">暴露物</span>
            <input type="text" {...register('agent')} className={inputClass} />
          </label>
          <fieldset className="text-sm">
            <legend className="mb-1 font-medium">是否有医生参与安排(明确选择,不猜测)</legend>
            <div className="flex gap-4">
              <label className="inline-flex items-center gap-1.5">
                <input type="radio" value="yes" {...register('doctor_involved')} /> 是
              </label>
              <label className="inline-flex items-center gap-1.5">
                <input type="radio" value="no" {...register('doctor_involved')} /> 否
              </label>
            </div>
            {errors.doctor_involved && (
              <p className="mt-1 text-xs text-danger">请明确选择是或否。</p>
            )}
          </fieldset>
          <label className="block text-sm">
            <span className="mb-1 block font-medium">实际发生时间</span>
            <input type="datetime-local" {...register('occurred_at')} className={inputClass} />
            <span className="mt-1 block text-xs text-ink-muted">
              时间不明确可留空或填原始描述(如「上个月」);系统会保留原描述并标注
              「发生时间未明确」,不会换算成具体日期。
            </span>
          </label>
          <label className="block text-sm">
            <span className="mb-1 block font-medium">原始描述(按原文保存)</span>
            <textarea rows={3} {...register('note')} className={inputClass}
              placeholder="如:医生上个月让做的 CT 用了造影剂。" />
            {errors.note && <p className="mt-1 text-xs text-danger">{errors.note.message}</p>}
          </label>
          <button type="submit"
            className="rounded-lg bg-primary px-4 py-2 font-medium text-white hover:bg-primary-strong">
            记录暴露
          </button>
          <div aria-live="polite" role="status" className="sr-only">{live}</div>
        </form>
      </Card>

      {resultTask && resultTask.status !== 'committed' && (
        <Card className="p-4">
          <Badge tone="caution">已受理,等待处理结果</Badge>
        </Card>
      )}
      {resultTask && <SubmissionResultView task={resultTask} onClose={() => setResultKey(null)} />}
      {!resultTask && tasks.length === 0 && (
        <EmptyState title="还没有提交过暴露记录" hint="提交后这里会显示检查结果与两侧记录(医疗行为/说明书佐证)。" />
      )}
      <TaskTray tasks={tasks} />
    </div>
  );
}

const inputClass = 'w-full rounded-lg border border-border bg-surface px-3 py-2 text-base focus:border-primary focus:outline-none focus:ring-2 focus:ring-primary/30';
