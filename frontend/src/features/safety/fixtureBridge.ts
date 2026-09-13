/**
 * 安全事项页的数据入口 —— **唯一的开发期 fixture 开关点**。
 *
 * 每个函数都是同一个形状:
 *
 * ```ts
 * if (import.meta.env.DEV && import.meta.env.VITE_SAFETY_FIXTURE === '1') {
 *   const fixture = await import('./devFixture');
 *   return fixture.<对应动作>();
 * }
 * return api.<真实端点>();
 * ```
 *
 * 为什么这样写能保证**不进入生产请求路径**:
 *
 *  - `import.meta.env.DEV` 在生产构建里被 Vite 静态替换成 `false`,整个条件
 *    折叠成常量假;条件内的 `import()` 因此被 rollup 连同 `devFixture` chunk
 *    一起删掉。产物里不会留下这个模块,也不会留下任何 fixture 数据。
 *  - 判断在**发起请求之前**:fixture 生效时它替换请求,而不是在真实请求失败
 *    之后顶上 —— 所以它不会把一次真实失败伪装成成功。
 *
 * 真实后端返回 4xx/5xx 时,这里什么都不做,错误原样交给页面。
 */
import { api } from '../../api/client';
import type {
  CareTaskDto, SafetyCaseDto, SafetyClosureEvidenceDto,
  SafetyFollowUpConditionDto, SafetyFollowUpConfirmationDto,
} from '../../api/types';

export function fixtureModeOn(): boolean {
  return import.meta.env.DEV && import.meta.env.VITE_SAFETY_FIXTURE === '1';
}

export async function loadCase(caseId: string, signal?: AbortSignal): Promise<SafetyCaseDto> {
  if (import.meta.env.DEV && import.meta.env.VITE_SAFETY_FIXTURE === '1') {
    const fixture = await import('./devFixture');
    return fixture.fixtureCase(caseId);
  }
  return api.safetyCase(caseId, signal);
}

export async function loadClosureEvidence(caseId: string, signal?: AbortSignal):
Promise<SafetyClosureEvidenceDto> {
  if (import.meta.env.DEV && import.meta.env.VITE_SAFETY_FIXTURE === '1') {
    const fixture = await import('./devFixture');
    return fixture.fixtureClosureEvidence();
  }
  return api.safetyCaseClosureEvidence(caseId, signal);
}

export async function loadCareTasks(signal?: AbortSignal): Promise<{ items: CareTaskDto[] }> {
  if (import.meta.env.DEV && import.meta.env.VITE_SAFETY_FIXTURE === '1') {
    const fixture = await import('./devFixture');
    return fixture.fixtureCareTasks();
  }
  return api.careTasks(signal);
}

export async function followUpSchedule(caseId: string, body: {
  key: string; expected_revision: number;
  kind: 'review_at' | 'on_event' | 'arrangement';
  at?: string; condition?: SafetyFollowUpConditionDto;
  owner?: string; note?: string;
}): Promise<SafetyCaseDto> {
  if (import.meta.env.DEV && import.meta.env.VITE_SAFETY_FIXTURE === '1') {
    const fixture = await import('./devFixture');
    return fixture.fixtureSchedule(body);
  }
  return api.safetyCaseFollowUpSchedule(caseId, body);
}

export async function followUpCancel(caseId: string, body: {
  key: string; expected_revision: number; reason?: string;
}): Promise<SafetyCaseDto> {
  if (import.meta.env.DEV && import.meta.env.VITE_SAFETY_FIXTURE === '1') {
    const fixture = await import('./devFixture');
    return fixture.fixtureCancel(body);
  }
  return api.safetyCaseFollowUpCancel(caseId, body);
}

export async function followUpConfirmation(caseId: string,
                                            body: SafetyFollowUpConfirmationDto):
Promise<SafetyCaseDto> {
  if (import.meta.env.DEV && import.meta.env.VITE_SAFETY_FIXTURE === '1') {
    const fixture = await import('./devFixture');
    return fixture.fixtureConfirmation(body);
  }
  return api.safetyCaseFollowUpConfirmation(caseId, body);
}
