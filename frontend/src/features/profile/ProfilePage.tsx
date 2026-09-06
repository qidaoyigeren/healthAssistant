/**
 * 患者档案:真实事实、来源、版本与核实操作。
 * 区分「未记录」与「没有」;空值不默认填 0;肝肾功不默认「正常」;
 * 修改只提交实际变更字段;撤回走显式动作并保留历史。
 */
import React, { useMemo, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { useForm, useFieldArray } from 'react-hook-form';
import { zodResolver } from '@hookform/resolvers/zod';
import { z } from 'zod';
import { Link2, Plus, Search, X } from 'lucide-react';
import { api } from '../../api/client';
import { qk } from '../../api/queryKeys';
import { submissions } from '../../api/submissions';
import { useSessionId, useSubmissions } from '../../hooks/useSubmissions';
import { formatTime, usePreferences } from '../../hooks/usePreferences';
import type { SemanticFactDto } from '../../api/types';
import {
  Card, ConfirmDialog, EmptyState, ErrorState,
  FactStatusBadge, SectionTitle, SkeletonList,
} from '../../components/ui';
import { MemoryRefDrawer } from '../../components/evidence';
import { SubmissionResultView, TaskTray } from '../shared/submission';

const profileSchema = z.object({
  age: z.string().max(4),
  sex: z.union([z.literal(''), z.enum(['男', '女'])]),
  weight_kg: z.string().max(7),
  renal_function: z.string().max(200),
  hepatic_function: z.string().max(200),
  allergies: z.array(z.object({ value: z.string().min(1).max(60) })),
  chronic_diseases: z.array(z.object({ value: z.string().min(1).max(60) })),
  care_notes: z.string().max(1000),
});
type ProfileForm = z.infer<typeof profileSchema>;

const NAMESPACE_ORDER = [
  'age', 'sex', 'weight', 'renal_function', 'hepatic_function',
  'allergy', 'chronic_disease', 'preference',
] as const;
const NAMESPACE_LABELS: Record<string, string> = {
  age: '年龄', sex: '性别', weight: '体重',
  renal_function: '肾功能', hepatic_function: '肝功能',
  allergy: '过敏', chronic_disease: '慢性病', preference: '照护偏好',
};

export function ProfilePage(): React.ReactElement {
  const stateQuery = useQuery({
    queryKey: qk.memoryState(),
    queryFn: ({ signal }) => api.memoryState({}, signal),
  });

  if (stateQuery.isPending) return <SkeletonList rows={5} />;
  if (stateQuery.isError) return <ErrorState error={stateQuery.error} />;

  const state = stateQuery.data;
  const hasProfile = state.facts.length > 0 || state.medications.length > 0;

  return (
    <div className="space-y-4">
      <header>
        <h1 className="text-xl font-semibold">患者档案</h1>
        <p className="mt-1 text-sm text-ink-secondary">
          记录以「按报告记录」为起点;核实需要依据,撤回会保留历史。
        </p>
      </header>

      <ProfileFormCard state={state} hasProfile={hasProfile} />

      <Card>
        <SectionTitle>已记录的事实</SectionTitle>
        {state.facts.length === 0 && state.uncertainties.length === 0 ? (
          <div className="p-4">
            <EmptyState title="还没有记录任何患者情况"
              hint="先用上方表单登记年龄、过敏、慢病等信息;未填写不等于「没有过敏」或「肝肾功能正常」。" />
          </div>
        ) : (
          <FactList facts={state.facts} uncertainties={state.uncertainties} />
        )}
      </Card>

      <TasksAndResults />
    </div>
  );
}

// ---- 当前档案派生 ------------------------------------------------------------

interface CurrentProfile {
  age?: number;
  sex?: string;
  weight_kg?: number;
  renal_function?: string;
  hepatic_function?: string;
  allergies: string[];
  chronic_diseases: string[];
  care_notes?: string;
}

export function deriveProfile(facts: SemanticFactDto[]): CurrentProfile {
  const profile: CurrentProfile = { allergies: [], chronic_diseases: [] };
  for (const fact of facts) {
    if (fact.status !== 'active') continue;
    switch (fact.namespace) {
      case 'age': profile.age = Number(fact.value); break;
      case 'sex': profile.sex = String(fact.value); break;
      case 'weight': profile.weight_kg = Number(fact.value); break;
      case 'renal_function': profile.renal_function = String(fact.value); break;
      case 'hepatic_function': profile.hepatic_function = String(fact.value); break;
      case 'allergy': {
        const value = fact.value as { allergen?: string } | null;
        if (value?.allergen) profile.allergies.push(value.allergen);
        break;
      }
      case 'chronic_disease': {
        const value = fact.value as { name?: string } | null;
        if (value?.name) profile.chronic_diseases.push(value.name);
        break;
      }
      case 'preference': {
        if (fact.fact_key === 'care_notes') profile.care_notes = String(fact.value);
        break;
      }
      default:
        break;
    }
  }
  return profile;
}

// ---- 登记 / 更新表单 -----------------------------------------------------------

function ProfileFormCard({ state, hasProfile }: {
  state: Awaited<ReturnType<typeof api.memoryState>>;
  hasProfile: boolean;
}): React.ReactElement {
  const [sessionId] = useSessionId();
  const profile = useMemo(() => deriveProfile(state.facts), [state.facts]);

  const {
    register, control, handleSubmit, watch, reset, formState: { errors },
  } = useForm<ProfileForm>({
    resolver: zodResolver(profileSchema),
    defaultValues: {
      age: '', sex: '', weight_kg: '', renal_function: '', hepatic_function: '',
      allergies: [], chronic_diseases: [], care_notes: '',
    },
  });
  const allergiesArray = useFieldArray({ control, name: 'allergies' });
  const chronicArray = useFieldArray({ control, name: 'chronic_diseases' });

  // 「补充患者情况」时预填当前值(仅作为对照,提交仍按 delta)
  const [prefilled, setPrefilled] = useState(false);
  React.useEffect(() => {
    if (!prefilled && hasProfile && state.facts.length > 0) {
      reset({
        age: profile.age != null ? String(profile.age) : '',
        sex: (profile.sex ?? '') as ProfileForm['sex'],
        weight_kg: profile.weight_kg != null ? String(profile.weight_kg) : '',
        renal_function: profile.renal_function ?? '',
        hepatic_function: profile.hepatic_function ?? '',
        allergies: [],
        chronic_diseases: [],
        care_notes: profile.care_notes ?? '',
      });
      setPrefilled(true);
    }
  }, [prefilled, hasProfile, state.facts.length, profile, reset]);

  const [resultKey, setResultKey] = useState<string | null>(null);
  const [liveMessage, setLiveMessage] = useState<string | null>(null);

  const onSubmit = handleSubmit((values) => {
    const delta = buildDelta(values, profile);
    if (delta === null) {
      setLiveMessage('没有需要记录的变更。');
      return;
    }
    const eventType = hasProfile ? 'profile_update' : 'register_profile';
    const textParts: string[] = [];
    if (delta.profile.age !== undefined) textParts.push(`年龄 ${delta.profile.age}`);
    if (delta.profile.sex !== undefined) textParts.push(`性别 ${delta.profile.sex}`);
    if (delta.profile.weight_kg !== undefined) textParts.push(`体重 ${delta.profile.weight_kg} 公斤`);
    if (delta.profile.renal_function !== undefined) textParts.push(`肾功能:${delta.profile.renal_function}`);
    if (delta.profile.hepatic_function !== undefined) textParts.push(`肝功能:${delta.profile.hepatic_function}`);
    if (delta.profile.allergies?.length) textParts.push(`过敏:${delta.profile.allergies.join('、')}`);
    if (delta.profile.chronic_diseases?.length) textParts.push(`慢性病:${delta.profile.chronic_diseases.join('、')}`);
    if (delta.profile.preferences !== undefined) textParts.push('照护说明已更新');
    const key = submissions.submit({
      event_type: eventType,
      text: `${eventType === 'register_profile' ? '登记患者情况' : '补充患者情况'}:${textParts.join(';')}。`,
      payload: delta as unknown as Record<string, unknown>,
      source: 'caregiver',
      occurred_at: null,
      session_id: sessionId,
    });
    setResultKey(key);
    setLiveMessage('提交已受理,正在处理。');
  });

  const resultTask = resultKey ? submissions.task(resultKey) : undefined;
  const values = watch();

  return (
    <Card>
      <SectionTitle>{hasProfile ? '补充患者情况' : '登记患者情况'}</SectionTitle>
      <form onSubmit={onSubmit} className="space-y-4 p-4" noValidate>
        <div className="grid grid-cols-1 gap-3 md:grid-cols-3">
          <LabeledField label="年龄" error={errors.age?.message}>
            <input type="number" inputMode="numeric" placeholder="未记录"
              {...register('age')} className={inputClass} />
          </LabeledField>
          <LabeledField label="性别" error={errors.sex?.message}>
            <select {...register('sex')} className={inputClass}>
              <option value="">未记录</option>
              <option value="男">男</option>
              <option value="女">女</option>
            </select>
          </LabeledField>
          <LabeledField label="体重(kg)" error={errors.weight_kg?.message}>
            <input type="number" step="0.1" inputMode="decimal" placeholder="未记录"
              {...register('weight_kg')} className={inputClass} />
          </LabeledField>
          <LabeledField label="肾功能" hint="未填写不等于「正常」">
            <input type="text" placeholder="未记录" {...register('renal_function')} className={inputClass} />
          </LabeledField>
          <LabeledField label="肝功能" hint="未填写不等于「正常」">
            <input type="text" placeholder="未记录" {...register('hepatic_function')} className={inputClass} />
          </LabeledField>
        </div>

        <LabeledField label="过敏(逐个添加)">
          <div className="flex flex-wrap items-center gap-2">
            {profile.allergies.map((allergen) => (
              <span key={`recorded-${allergen}`} className="rounded-full bg-surface-alt px-3 py-1 text-sm">
                {allergen}
                <span className="ml-1 text-xs text-ink-muted">已记录</span>
              </span>
            ))}
            {allergiesArray.fields.map((field, index) => (
              <span key={field.id} className="inline-flex items-center gap-1 rounded-full bg-primary-soft px-3 py-1 text-sm">
                {values.allergies[index]?.value}
                <button type="button" aria-label={`移除待提交的过敏项 ${values.allergies[index]?.value ?? ''}`}
                  onClick={() => allergiesArray.remove(index)}
                  className="rounded-full p-0.5 hover:bg-surface">
                  <X size={12} aria-hidden />
                </button>
              </span>
            ))}
            <AllergenAdder onAdd={(value) => allergiesArray.append({ value })} placeholder="如:青霉素" />
          </div>
        </LabeledField>
        <p className="text-xs text-ink-muted">
          移除上方待提交标签只是不提交;已记录的过敏请用下方「撤回该记录」,会保留理由和历史。
        </p>

        <LabeledField label="慢性病(逐个添加)">
          <div className="flex flex-wrap items-center gap-2">
            {profile.chronic_diseases.map((disease) => (
              <span key={`recorded-${disease}`} className="rounded-full bg-surface-alt px-3 py-1 text-sm">
                {disease}
                <span className="ml-1 text-xs text-ink-muted">已记录</span>
              </span>
            ))}
            {chronicArray.fields.map((field, index) => (
              <span key={field.id} className="inline-flex items-center gap-1 rounded-full bg-primary-soft px-3 py-1 text-sm">
                {values.chronic_diseases[index]?.value}
                <button type="button" aria-label={`移除待提交的慢病项 ${values.chronic_diseases[index]?.value ?? ''}`}
                  onClick={() => chronicArray.remove(index)}
                  className="rounded-full p-0.5 hover:bg-surface">
                  <X size={12} aria-hidden />
                </button>
              </span>
            ))}
            <AllergenAdder onAdd={(value) => chronicArray.append({ value })} placeholder="如:高血压" />
          </div>
        </LabeledField>

        <LabeledField label="照护说明">
          <textarea rows={2} {...register('care_notes')} className={inputClass} placeholder="未记录" />
        </LabeledField>

        <div className="flex flex-wrap items-center gap-3">
          <button type="submit"
            className="rounded-lg bg-primary px-4 py-2 font-medium text-white hover:bg-primary-strong">
            {hasProfile ? '提交变更(仅提交改动项)' : '登记患者情况'}
          </button>
          <span className="text-xs text-ink-secondary">
            只提交有改动的字段;未变字段不会重复产生事实确认。
          </span>
        </div>
      </form>
      <div aria-live="polite" role="status" className="sr-only">{liveMessage}</div>
      {resultTask && <div className="px-4 pb-4"><SubmissionResultView task={resultTask} onClose={() => setResultKey(null)} /></div>}
    </Card>
  );
}

const inputClass = 'w-full rounded-lg border border-border bg-surface px-3 py-2 text-base focus:border-primary focus:outline-none focus:ring-2 focus:ring-primary/30';

function LabeledField({ label, hint, error, children }: {
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

function AllergenAdder({ onAdd, placeholder }: {
  onAdd: (value: string) => void; placeholder: string;
}): React.ReactElement {
  const [value, setValue] = useState('');
  return (
    <span className="inline-flex items-center gap-1">
      <input type="text" value={value} placeholder={placeholder}
        onChange={(e) => setValue(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === 'Enter') {
            e.preventDefault();
            if (value.trim()) { onAdd(value.trim()); setValue(''); }
          }
        }}
        className="w-36 rounded-lg border border-border px-2 py-1 text-sm" />
      <button type="button" aria-label="添加"
        onClick={() => { if (value.trim()) { onAdd(value.trim()); setValue(''); } }}
        className="rounded-lg border border-border p-1.5 hover:bg-surface-alt">
        <Plus size={13} aria-hidden />
      </button>
    </span>
  );
}

interface ProfileDelta {
  profile: Partial<{
    age: number; sex: string; weight_kg: number;
    renal_function: string; hepatic_function: string;
    allergies: string[]; chronic_diseases: string[];
    preferences: Record<string, string>;
  }>;
}

function buildDelta(values: ProfileForm, profile: CurrentProfile): ProfileDelta | null {
  const delta: ProfileDelta['profile'] = {};
  const age = Number(values.age);
  if (values.age.trim() !== '' && Number.isInteger(age) && age >= 0 && age <= 130 && age !== profile.age) delta.age = age;
  if (values.sex !== '' && values.sex !== profile.sex) delta.sex = values.sex;
  const weight = Number(values.weight_kg);
  if (values.weight_kg.trim() !== '' && weight >= 1 && weight <= 400 && weight !== profile.weight_kg) delta.weight_kg = weight;
  const renal = values.renal_function.trim();
  if (renal && renal !== (profile.renal_function ?? '')) delta.renal_function = renal;
  const hepatic = values.hepatic_function.trim();
  if (hepatic && hepatic !== (profile.hepatic_function ?? '')) delta.hepatic_function = hepatic;
  const newAllergies = values.allergies.map((a) => a.value.trim()).filter(Boolean);
  if (newAllergies.length > 0) delta.allergies = newAllergies;
  const newDiseases = values.chronic_diseases.map((d) => d.value.trim()).filter(Boolean);
  if (newDiseases.length > 0) delta.chronic_diseases = newDiseases;
  const notes = values.care_notes.trim();
  if (notes && notes !== (profile.care_notes ?? '')) delta.preferences = { care_notes: notes };
  if (Object.keys(delta).length === 0) return null;
  return { profile: delta };
}

// ---- 事实列表(核实 / 历史 / 撤回)-----------------------------------------------

function FactList({ facts, uncertainties }: {
  facts: SemanticFactDto[]; uncertainties: SemanticFactDto[];
}): React.ReactElement {
  const grouped = useMemo(() => {
    const groups = new Map<string, SemanticFactDto[]>();
    for (const fact of facts) {
      const list = groups.get(fact.namespace) ?? [];
      list.push(fact);
      groups.set(fact.namespace, list);
    }
    return groups;
  }, [facts]);
  const namespaces = [...grouped.keys()].sort((a, b) => {
    const ia = NAMESPACE_ORDER.indexOf(a as typeof NAMESPACE_ORDER[number]);
    const ib = NAMESPACE_ORDER.indexOf(b as typeof NAMESPACE_ORDER[number]);
    return (ia === -1 ? 99 : ia) - (ib === -1 ? 99 : ib);
  });

  return (
    <div className="p-4">
      {namespaces.map((namespace) => (
        <div key={namespace} className="mb-4 last:mb-0">
          <h3 className="mb-1.5 text-sm font-medium text-ink-secondary">
            {NAMESPACE_LABELS[namespace] ?? namespace}
          </h3>
          <ul className="space-y-1.5">
            {(grouped.get(namespace) ?? []).map((fact) => <FactRow key={fact.ref} fact={fact} />)}
          </ul>
        </div>
      ))}
      {uncertainties.length > 0 && (
        <div className="mt-4 rounded-card border border-caution/30 bg-caution-soft/40 p-3">
          <h3 className="mb-1.5 flex items-center gap-1.5 text-sm font-medium text-caution">
            生效时间不明
          </h3>
          <ul className="space-y-1.5">
            {uncertainties.map((fact) => <FactRow key={fact.ref} fact={fact} />)}
          </ul>
        </div>
      )}
    </div>
  );
}

function describeFact(fact: SemanticFactDto | null): string {
  if (!fact) return '';
  return `${NAMESPACE_LABELS[fact.namespace] ?? fact.namespace}:${typeof fact.value === 'object' ? JSON.stringify(fact.value) : String(fact.value)}(v${fact.version})`;
}

function FactRow({ fact }: { fact: SemanticFactDto }): React.ReactElement {
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [verifyOpen, setVerifyOpen] = useState(false);
  const [retractOpen, setRetractOpen] = useState(false);
  const [basis, setBasis] = useState('');
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);

  const runAction = (action: 'verify' | 'retract') => {
    if (!basis.trim()) return;
    setBusy(true);
    api.factActions({ ref: fact.ref, action, actor: 'caregiver', basis })
      .then((result) => {
        if (result.outcome === 'blocked_by_conflict') {
          setMessage('核实被待核实冲突阻塞:先到「待核实」处理冲突,再核实这条记录。');
        } else if (result.outcome === 'verified') {
          setMessage('已记录核实(见处理记录)。');
        } else {
          setMessage('记录已撤回。');
        }
      })
      .catch((err) => setMessage(err instanceof Error ? err.message : '操作失败,请重试。'))
      .finally(() => {
        setBusy(false);
        setBasis('');
      });
  };

  return (
    <li className="flex flex-wrap items-center gap-2 rounded-lg border border-border bg-surface-alt px-3 py-2">
      <span className="min-w-0 flex-1">
        <span className="block break-words text-sm">
          {typeof fact.value === 'object' ? JSON.stringify(fact.value) : String(fact.value)}
        </span>
        <span className="mt-0.5 flex flex-wrap items-center gap-2 text-xs text-ink-muted">
          <FactStatusBadge fact={fact} />
          <span>v{fact.version}</span>
          <span>记录于 <TimeInline iso={fact.created_at} /></span>
          {fact.valid_from
            ? <span>生效 <TimeInline iso={fact.valid_from} /></span>
            : <span className="text-caution">生效时间不明</span>}
          {fact.status === 'superseded' && <span>已被更新替代</span>}
        </span>
        {message && <span className="mt-1 block text-xs text-caution">{message}</span>}
      </span>
      <span className="flex gap-1.5">
        <ActionButton icon={<Search size={13} aria-hidden />} label="历史与来源"
          onClick={() => setDrawerOpen(true)} />
        {fact.status === 'active' && (
          <>
            <ActionButton label="记录核实"
              disabled={fact.verification_status === 'verified'}
              onClick={() => setVerifyOpen(true)} />
            <ActionButton label="撤回记录" onClick={() => setRetractOpen(true)} />
          </>
        )}
      </span>

      <MemoryRefDrawer memoryRef={drawerOpen ? fact.ref : null} onClose={() => setDrawerOpen(false)} />

      <ConfirmDialog
        open={verifyOpen}
        onOpenChange={(open) => { if (!open) { setVerifyOpen(false); setBasis(''); } }}
        title="记录核实结果?"
        description={<>将记录:您已核实「{describeFact(fact)}」。请填写核实依据。</>}
        confirmLabel="记录核实"
        busy={busy}
        onConfirm={() => { runAction('verify'); setVerifyOpen(false); }}
      >
        <BasisInput value={basis} onChange={setBasis} />
      </ConfirmDialog>
      <ConfirmDialog
        open={retractOpen}
        onOpenChange={(open) => { if (!open) { setRetractOpen(false); setBasis(''); } }}
        title="撤回这条记录?"
        description={<>将把「{describeFact(fact)}」标记为撤回,历史与理由保留。</>}
        confirmLabel="撤回记录"
        danger
        busy={busy}
        onConfirm={() => { runAction('retract'); setRetractOpen(false); }}
      >
        <BasisInput value={basis} onChange={setBasis} />
      </ConfirmDialog>
      {fact.source_uri && (
        <button type="button" onClick={() => setDrawerOpen(true)}
          className="inline-flex items-center gap-1 text-xs text-primary">
          <Link2 size={11} aria-hidden /> 来源
        </button>
      )}
    </li>
  );
}

function BasisInput({ value, onChange }: {
  value: string; onChange: (value: string) => void;
}): React.ReactElement {
  return (
    <label className="w-full text-sm">
      <span className="mb-1 block font-medium">依据 / 理由(必填)</span>
      <input type="text" value={value} onChange={(e) => onChange(e.target.value)}
        className={inputClass}
        placeholder="如:对照出院小结 / 药盒标注 / 与医生当面确认" />
    </label>
  );
}

function ActionButton({ label, icon, onClick, disabled }: {
  label: string; icon?: React.ReactNode; onClick: () => void; disabled?: boolean;
}): React.ReactElement {
  return (
    <button type="button" onClick={onClick} disabled={disabled}
      className="inline-flex items-center gap-1 rounded-lg border border-border bg-surface px-2 py-1 text-xs hover:bg-surface-alt disabled:opacity-40">
      {icon}
      {label}
    </button>
  );
}

function TimeInline({ iso }: { iso: string | null }): string {
  const prefs = usePreferences();
  return formatTime(iso, prefs);
}

function TasksAndResults(): React.ReactElement {
  const tasks = useSubmissions();
  return <TaskTray tasks={tasks} />;
}
