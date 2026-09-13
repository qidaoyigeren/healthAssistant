/**
 * **开发期 fixture** —— 用来在 B 的长跟进接口还没有落地时,把界面本身做完整验收。
 *
 * 三条自我约束,缺一条这个文件就不该存在:
 *
 *  1. **只在开发期、且显式打开时生效**。调用方用
 *     `import.meta.env.DEV && import.meta.env.VITE_SAFETY_FIXTURE === '1'`
 *     判断,并通过**动态 import** 加载本模块 —— 生产构建里 `import.meta.env.DEV`
 *     被静态替换成 `false`,整条分支连同这个 chunk 一起消失。
 *  2. **不兜底真实失败**。fixture 生效时它**替换**请求,而不是"请求失败之后
 *     拿它顶上"。真实后端返回 4xx/5xx 时,页面看到的仍然是那个错误。
 *  3. **不改生产请求路径**。本文件不发任何网络请求,也不注册任何全局钩子。
 *
 * 数据全部是合成的:没有真实患者、没有真实药物、没有调用模型。
 */
import { ApiError } from '../../api/http';
import type {
  CareTaskDto, SafetyCaseDto, SafetyClosureEvidenceDto, SafetyFollowUpDto,
} from '../../api/types';

/** 打开 fixture 的条件。生产构建里这是常量 `false`。 */
export const FIXTURE_ENABLED: boolean =
  import.meta.env.DEV && import.meta.env.VITE_SAFETY_FIXTURE === '1';

/** 场景开关:`?fx=legacy|empty|conflict|failure`。默认是"正常"。 */
export function fixtureScenario(): string {
  if (typeof window === 'undefined') return 'normal';
  return new URLSearchParams(window.location.search).get('fx') ?? 'normal';
}

const NOW = '2026-09-13T08:00:00+00:00';
const CASE_ID = 'fixture-case-0001';

// ---- 一条样板事项 -------------------------------------------------------------

function baseCase(): SafetyCaseDto {
  const followUp: SafetyFollowUpDto = {
    kind: 'review_at',
    at: '2026-10-01T00:00:00+00:00',
    condition: null,
    owner: 'caregiver',
    note: '合成数据：下次复诊时把药单给医生看',
    recorded_at: NOW,
    // §4.5：有时间**不等于**已确认。这条是"已安排、尚未确认"。
    confirmed: false,
    confirmed_at: null,
    confirmed_by: null,
    confirmation_ref: null,
    revision: 1,
    schedule_state: 'scheduled',
    last_triggered_at: null,
    last_trigger_reason: null,
    care_task_id: null,
    blocked_reason: null,
  };

  return {
    case_id: CASE_ID,
    case_type: 'interaction_risk',
    status: 'monitoring',
    status_label: '持续跟进中（风险仍在）',
    subject_keys: ['fixture-subject'],
    trigger: { kind: 'necessary_check', trigger_kind: 'medication_set' },
    medications: [{ ref: 'memory:medication:45@2', label: '合成药甲', available: true }],
    facts: [{ ref: 'memory:fact:7@1', label: '合成过敏史', available: true }],
    conclusions: [{
      ref: 'memory:conclusion:123@1',
      available: true,
      kind: 'interaction',
      text: '合成药甲与合成药乙合用可能增加合成风险（合成结论，无临床含义）',
      status: 'current',
      trigger_state: 'risk_present',
      trigger_reasons: ['合成检查：两种药同时在用'],
      sources: ['https://synthetic.invalid/label/a'],
    }],
    evidence_refs: ['https://synthetic.invalid/label/a', 'ev-fixture0000000001'],
    open_questions: [],
    required_inputs: [],
    answered_inputs: [answeredInput()],
    answered_inputs_count: 1,
    linked_run_ids: [],
    next_action_summary: '按约定时间复核一次。',
    responsible_party: 'caregiver',
    resolution_basis: null,
    disposition: 'accepted_monitoring',
    follow_up: followUp,
    linked_review_case_ids: [],
    user_seen_at: null,
    input_versions: { medications: 2, semantic: 1 },
    revision: 3,
    created_at: NOW,
    updated_at: NOW,
    history: [
      { at: NOW, event: 'opened' },
      {
        at: NOW, event: 'disposition', disposition: 'accepted_monitoring',
        basis_kind: 'monitoring_arrangement', actor: 'caregiver', follow_up: followUp,
      },
    ],
  };
}

/** 一条问题 + 七种答案依据,把五个视觉状态和三种来源属性都摆出来。 */
function answeredInput(): SafetyCaseDto['answered_inputs'][number] {
  return {
    request_id: 'fixture-request-1',
    question: '合成药乙目前的服用频次是什么？',
    fields: ['frequency'],
    for_professional: false,
    status: 'answered',
    answered_at: NOW,
    answered_parts: [
      {
        value: '每日一次', field: 'frequency',
        source: 'patient_record', provenance: 'authoritative_record',
        quote: null, source_ref: 'memory:medication:45@2', at: NOW,
        version: { medications: 2, semantic: 1 }, still_uncertain: [],
        assessment: {
          status: 'verified',
          reason: '答案取自当前有效的用药记录，核对了写下时的记录版本仍是当前版本',
          source_ref: 'memory:medication:45@2',
          locator: 'medications[0].schedule',
          dependency_refs: ['memory:medication:45@2'],
        },
      },
      {
        value: '随餐服用', field: 'with_food',
        source: 'evidence', provenance: 'reference_evidence',
        quote: '合成药乙应随餐服用', source_ref: 'ev-fixture0000000001', at: NOW,
        version: { medications: 2, semantic: 1 }, still_uncertain: [],
        assessment: {
          status: 'verified',
          reason: '引用片段与已回读的证据原文逐字一致',
          source_ref: 'ev-fixture0000000001',
          locator: '第 3 段第 2 行',
          dependency_refs: [],
        },
      },
      {
        value: '晚上吃', field: 'time_of_day',
        source: 'user', provenance: 'user_reported',
        quote: null, source_ref: null, at: NOW,
        still_uncertain: ['具体时刻'],
        assessment: {
          status: 'candidate',
          reason: '依据是用户陈述，尚未与任何权威记录或原文核对',
          source_ref: null, locator: null, dependency_refs: [],
        },
      },
      {
        value: '可能与合成药甲有轻微相互作用', field: 'interaction',
        source: 'model', provenance: null,
        quote: null, source_ref: null, at: NOW, still_uncertain: [],
        assessment: {
          status: 'candidate',
          reason: '模型提出的解释，尚未找到可核对的依据',
          source_ref: null, locator: null, dependency_refs: [],
        },
      },
      {
        value: '每日两次', field: 'frequency_old',
        source: 'patient_record', provenance: 'authoritative_record',
        quote: null, source_ref: 'memory:medication:45@1', at: NOW,
        version: { medications: 1, semantic: 1 }, still_uncertain: [],
        assessment: {
          status: 'stale',
          reason: '这条答案依据的是用药记录 v1，当前记录已经是 v2',
          source_ref: 'memory:medication:45@1',
          locator: 'medications[0].schedule',
          dependency_refs: ['memory:medication:45@1'],
        },
      },
      {
        value: '饭前还是饭后没有依据', field: 'timing',
        source: 'material', provenance: 'material_record',
        quote: null, source_ref: null, at: NOW, still_uncertain: [],
        assessment: {
          status: 'unsupported',
          reason: '材料里没有任何一句话能支撑这个说法',
          source_ref: null, locator: null, dependency_refs: [],
        },
      },
      {
        // 没有 assessment —— 必须显示成「未核实」，不得按 verified 处理。
        value: '合成药乙由家属代买',
        field: 'purchaser', source: 'user', provenance: 'user_reported',
        quote: null, source_ref: null, at: NOW, still_uncertain: [],
      },
      {
        // 一种**没有授权读取入口**的引用形态（契约文档的示例就长这样）。
        // 界面不得替它拼 /v1/evidence/{...} 之类的路径，也不得静默丢掉。
        value: '说明书里写着随餐服用', field: 'with_food_v2',
        source: 'evidence', provenance: 'reference_evidence',
        quote: '合成药乙应随餐服用', source_ref: 'evidence:8842', at: NOW,
        still_uncertain: [],
        assessment: {
          status: 'candidate',
          reason: '引用形态不是服务端内容寻址的证据 id，无法在应用内回读原文',
          source_ref: 'evidence:8842', locator: '第 3 段第 2 行', dependency_refs: [],
        },
      },
    ],
  };
}

// ---- 场景变体 ----------------------------------------------------------------

function legacyCase(): SafetyCaseDto {
  const view = baseCase();
  const followUp = view.follow_up as SafetyFollowUpDto;
  // 存量记录：自称已确认，但没有确认时间也没有确认记录 → 必须按未确认读。
  view.follow_up = { ...followUp, confirmed: true, confirmed_at: null, confirmation_ref: null };
  // 历史答案连 assessment 字段都没有。
  view.answered_inputs = view.answered_inputs.map((item) => ({
    ...item,
    answered_parts: (item.answered_parts ?? []).map(({ assessment, ...rest }) => {
      void assessment;
      return rest;
    }),
  }));
  return view;
}

function emptyCase(): SafetyCaseDto {
  const view = baseCase();
  return {
    ...view,
    status: 'open',
    status_label: '待调查',
    conclusions: [],
    evidence_refs: [],
    answered_inputs: [],
    answered_inputs_count: 0,
    required_inputs: [],
    open_questions: [],
    follow_up: null,
    disposition: null,
    history: [{ at: NOW, event: 'opened' }],
  };
}

// ---- 可变状态(让三个动作在浏览器里真的改变界面)-----------------------------

let current: SafetyCaseDto = baseCase();
let loadedScenario: string | null = null;

/**
 * 按场景装载一次。**只在场景变化时重置** —— 页面每 15 秒轮询一次,
 * 每次调用都重置的话,刚做的安排会被下一次轮询抹掉。
 */
function loadScenario() {
  const scenario = fixtureScenario();
  if (scenario === loadedScenario) return;
  loadedScenario = scenario;
  current = scenario === 'legacy' ? legacyCase()
    : scenario === 'empty' ? emptyCase()
      : baseCase();
}

function fail(code: number, message: string): never {
  throw new ApiError('http', code,
    { code: 'product_validation', category: 'validation', message, trace_id: 'fixture-trace' },
    message);
}

/** 三个写动作共用的前置检查。冲突/失败场景在这里一次性生效。 */
function guard(expectedRevision: number | undefined) {
  if (fixtureScenario() === 'conflict') {
    fail(409, '事项已被其他操作更新，请刷新');
  }
  if (fixtureScenario() === 'failure') {
    fail(500, '合成故障：fixture 场景 failure 下所有写入都会失败');
  }
  if (expectedRevision !== undefined && expectedRevision !== current.revision) {
    fail(409, '事项已被其他操作更新，请刷新');
  }
}

// ---- 对外的 fixture 接口 -----------------------------------------------------

export function fixtureCase(caseId: string): SafetyCaseDto {
  loadScenario();
  if (caseId !== CASE_ID && !caseId.startsWith('fixture-')) {
    fail(404, 'fixture 只提供 fixture-case-0001 这一条合成事项');
  }
  return current;
}

export function fixtureClosureEvidence(): SafetyClosureEvidenceDto {
  return {
    ok: false,
    reason: '合成数据：触发条件仍显示风险成立，且没有已生效的关闭依据。',
    refs: ['memory:conclusion:123@1'],
    checked: [{ ref: 'memory:conclusion:123@1', state: 'risk_present' }],
    eliminated: [], still_present: ['memory:conclusion:123@1'], blocking_inputs: [],
  };
}

export function fixtureCareTasks(): { items: CareTaskDto[] } {
  return { items: [] };
}

export function fixtureSchedule(body: {
  expected_revision?: number; kind: string; at?: string;
  condition?: unknown; owner?: string; note?: string;
}): SafetyCaseDto {
  guard(body.expected_revision);
  current = {
    ...current,
    revision: current.revision + 1,
    updated_at: NOW,
    follow_up: {
      kind: body.kind, at: body.at ?? null,
      condition: (body.condition ?? null) as SafetyFollowUpDto['condition'],
      owner: body.owner ?? 'caregiver', note: body.note ?? null, recorded_at: NOW,
      // **排期不产生确认** —— §4.5。填了时间仍然是未确认。
      confirmed: false, confirmed_at: null, confirmed_by: null, confirmation_ref: null,
      revision: (current.follow_up?.revision ?? 0) + 1,
      schedule_state: body.kind === 'arrangement' ? 'unscheduled' : 'scheduled',
      last_triggered_at: null, last_trigger_reason: null, care_task_id: null,
      blocked_reason: null,
    },
  };
  return current;
}

export function fixtureCancel(body: { expected_revision?: number; reason?: string }): SafetyCaseDto {
  guard(body.expected_revision);
  const followUp = current.follow_up;
  if (!followUp) fail(409, '现在没有可以取消的安排。');
  current = {
    ...current,
    revision: current.revision + 1,
    // 取消**不清空**内容和条件:历史留着,靠状态表达"已取消"。
    follow_up: { ...followUp, schedule_state: 'cancelled', blocked_reason: null },
  };
  return current;
}

export function fixtureConfirmation(body: { expected_revision?: number; note?: string }): SafetyCaseDto {
  guard(body.expected_revision);
  const followUp = current.follow_up;
  if (!followUp) fail(409, '没有已安排的跟进，没有可确认的东西。');
  if (followUp.schedule_state !== 'scheduled' && followUp.schedule_state !== 'due') {
    fail(409, '没有处于已安排状态的跟进，没有可确认的东西。');
  }
  current = {
    ...current,
    revision: current.revision + 1,
    follow_up: {
      ...followUp,
      confirmed: true,
      confirmed_at: NOW,
      confirmed_by: 'caregiver',
      confirmation_ref: `confirmation:fixture:${body.note ? 'noted' : 'plain'}`,
    },
  };
  return current;
}

export const FIXTURE_CASE_ID = CASE_ID;
