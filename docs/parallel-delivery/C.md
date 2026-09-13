# 任务 C — 安全体验（前端）

**分支**：`codex/safety-experience` ｜ **worktree**：`D:\py\HealthAssistant.worktrees\safety-experience`
**接口**：[CONTRACT.md §3.6 / §4.7](./CONTRACT.md) ｜ **所有权**：[OWNERSHIP.md §1](./OWNERSHIP.md)

---

## 目标

把 A 产出的答案可信性和 B 产出的长期跟进状态，变成照护者能读懂、且**读得准确**的界面。

## 你拥有的文件

- `frontend/src/features/safety/`（整目录）
- `frontend/src/api/client.ts`、`frontend/src/api/types.ts`、`frontend/src/api/queryKeys.ts`

其余文件一律只读冻结——**包括 `frontend/vite.config.ts`**（见下）。

## 要做的两件事

### 1. 答案可信性展示（[CONTRACT.md §3](./CONTRACT.md)）

- `types.ts:664-670` 的 `answered_parts` 元素类型目前只声明 5 个键
  （`value/field/source/provenance/still_uncertain`），后端实际下发 11 个。
  **补上 `assessment`。**
- 必须区分 **5 个视觉状态**：4 个 `status`（`verified`/`candidate`/`stale`/`unsupported`）
  **加上"未核实"**（无 `assessment`）。缺 assessment ≠ verified。
- 文案必须体现含义边界：`verified` **只**表示"约定范围内的答案依据已核对"，
  不能渲染成"安全""已确认无误""可以放心"。

### 2. 长期跟进展示与操作（[CONTRACT.md §4](./CONTRACT.md)）

- 展示 `schedule_state`、`confirmed`、`last_triggered_at`/`last_trigger_reason`、`blocked_reason`。
- **要有一个清楚的"已安排但未确认"中间态**——这是本次要建立的关键区分。
- 提供三个动作入口：改期（`schedule`）/ 取消（`cancel`）/ 确认（`confirmation`）。
- `labels.ts:148` 现在硬编码 `（UTC）` 且 `at.slice(0,16)`；服务端规范化 `at` 之后，
  这个渲染要么修正、要么加注释说明为什么仍成立。

## 起点提示

`frontend/src/features/safety/` 下已有 `SafetyPage.tsx`、`SafetyCaseDetailPage.tsx`、
`CaseCard.tsx`、`labels.ts`。`SafetyCaseDetailPage.tsx` 在 `:380-433` 已经在渲染
`information_state` / `answered_parts` / `still_uncertain`，`:917-931` 在构造
`follow_up` 请求体——**从这两处入手**，不要新起一套组件树。

## 端口与后端

不要改 `vite.config.ts`，它已经支持环境变量：

```bash
cd frontend
STAGE0_DEV_API_TARGET="http://127.0.0.1:8103" npx vite --port 5203
```

## 验收标准

- 无 `assessment` 的答案显示为"未核实"，**不是** verified。
- 四种 `status` 与"未核实"在视觉上可区分。
- "已安排未确认"与"已确认"在视觉上可区分。
- 三个跟进动作能发出正确请求并处理 409（revision 冲突）。
- 不修改所有权外文件。

## 需要越界时

走 [CONTRACT.md §7](./CONTRACT.md)：在本文档追加"接口需求"一节，不要直接改别人的文件。
