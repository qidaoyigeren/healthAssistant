# Harness P2 — 进度事件与取消 API 契约

日期：2026-09-06。本契约在既有 `POST /v1/events` + `Idempotency-Key` 提交/轮询契约之上做**纯增量**扩展；所有请求沿用 `X-Stage0-Token` 头鉴权（本地演示模式为固定 demo principal），不把长效 token 放 URL。

## 0. 为什么是“游标轮询”而不是浏览器 EventSource

浏览器原生 `EventSource` 不能携带自定义鉴权头（`X-Stage0-Token`），把 token 放进 URL 查询串会把长效凭证泄漏到日志/代理。因此本轮提供：

* **兼容轮询（本契约，前端已接入）**：`GET /v1/runs/{run_id}/progress?after=<seq>`，带数值游标，断线重连从游标补齐，客户端按 `event_id` 去重；
* SSE/流式实现留作后续（需要 `fetch`-based 流或短期一次性流 token 时再挂 `/stream` 端点），事件存储与游标语义已按可重放设计，切换传输层不改变本契约的数据模型。

## 1. 进度事件模型

一次 run（`run_id`，服务端生成，受理时即持久化 `workflow_runs` 行）产生少量**产品级**事件，写入 `run_progress_events`（与领域库同文件，附加表）：

| kind | 中文含义 | 触发点 |
| --- | --- | --- |
| `accepted` | 已受理 | worker 出队该 run 的任务 |
| `organizing` | 整理记录 | memory_read / memory_write / ask_clarification / read_evidence 完成 |
| `retrieving` | 检索依据 | rag_search 完成 |
| `checking_risks` | 核对风险 | ddi_check 完成 |
| `waiting_review` | 等待人工审核 | run 停靠人工审核 |
| `completed` | 完成 | run 到达终态（worker 侧） |
| `failed` | 失败 | 任务失败（分类错误后） |
| `cancel_requested` | 取消请求已受理 | cancel API 首次受理 |
| `cancelled` | 取消完成 | 任务在排队时被跳过 / 运行中取消落定 / 等待审核 run 被取消 |

事件字段：

```json
{
  "event_id": "pe-<sha1[:20]>",   // 重放稳定：同 run 同 (kind,cycle,tool,detail) 再发 → 同 id（图节点重放安全）
  "seq": 3,                       // run 内单调递增游标（有理间隙，重放不占用）
  "kind": "retrieving",
  "cycle": 1,
  "tool": "rag_search",
  "detail": {"ok": true, "error_kind": null, "evidence_count": 2, "reused": false},
  "created_at": "2026-09-06T10:00:00+00:00"
}
```

**内容红线**：`detail` 只允许粗粒度计数与状态码（ok / error_kind / evidence_count / reused / task_id / run_status / phase / actor / error_class / review_case id）。原始 graph state、隐藏推理、未经安全校验的医学正文**绝不**进入事件流（测试断言了已交付正文的句子不出现在事件 JSON 中）。

## 2. `GET /v1/runs/{run_id}/progress`

* 权限：任意已认证 principal + scope `local-demo`（与其他读端点一致）。
* 查询参数：`after`（int，默认 0）= 客户端已见到的最大 `seq`。
* 响应 200：

```json
{
  "run_id": "…", "run_status": "running",
  "events": [ /* seq > after 的事件，按 seq 升序，至多 200 条 */ ],
  "latest_seq": 7,
  "snapshot": false
}
```

* **断线重连**：客户端带上次 `latest_seq` 作为 `after`，服务端补齐增量；客户端按 `event_id` 去重（幂等重放不产生新事件行）。
* **游标过期**：若游标与保留历史之间出现间隙（事件被保留期清理），`snapshot: true` —— 客户端应重置去重窗口，以当前页为准。
* 404 `unknown_run`：无此 run。
* 进度事件被 `STAGE0_RUN_PROGRESS=0` 关闭时：200 + 空事件 + `snapshot: true` + `note` 说明。

## 3. `POST /v1/runs/{run_id}/cancel`

* 权限：`caregiver` 或 `ops` 角色 + scope `local-demo`。
* 请求体可选：`{"reason": "…"}`（≤200 字符，仅审计用）。
* **幂等**：重复调用返回当前状态，不产生副作用。
* **CAS/终态规则**（与审核提交、结果发布并发时，唯一结果由数据库事务内的状态机决定）：

| 场景 | 响应 `cancel_state` | 行为 |
| --- | --- | --- |
| run 不存在 | 404 `unknown_run` | — |
| 终态 `succeeded` / `degraded` / `failed` | `already_final`（200） | 不改写历史；已提交领域效果不变 |
| 排队（task 未执行） | `cancelled` | run → `cancelled`；worker 认领时发现取消 → 不执行，持久化 `run_status=cancelled` 的终态结果（幂等键照常收敛），事件流记录 `cancel_requested`+`cancelled` |
| 模型/工具调用中 | `cancelled` | 持久行 + 进程内 `threading.Event`；run 在下一调度点（规划/执行门）停止；已提交的领域写入不回滚；迟到模型/工具结果在下一门被拒绝 |
| 等待人工审核 | `cancelled` | run → `cancelled`；未决 review case → `cancelled`；pending resume tasks → `cancelled` |
| 与审核提交并发 | 取决于事务顺序 | 迟到的 reviewer 决定：case 已 `cancelled` → 409 `review_decision_conflict`；已认领的 resume task 执行前复核 run 状态 → 拒绝复活（`_resume_one` 二道防线） |
| 与发布并发 | 唯一终态 | `_node_publish` / legacy 收尾在 `is_cancel_requested` 为真时记录 `cancelled`；否则取消请求对终态 run 返回 `already_final` |

* 响应 200：

```json
{
  "run_id": "…", "cancel_state": "cancelled", "run_status": "cancelled",
  "status_url": "/v1/runs/{run_id}/progress",
  "note": "取消请求已受理；已提交的领域记录不会回滚，未执行的工作不再执行。"
}
```

* `cancel_requested` 与 `cancelled` 是两个独立事件种类：前者是请求受理，后者是 run 实际停止。

## 4. 与既有契约的关系

* `POST /v1/events` + `Idempotency-Key` 语义**不变**；`GET /v1/events/{key}` 的 202 轮询响应新增 `event_id`/`run_id` 字段（增量，旧客户端忽略），使刷新/断线恢复后的客户端仍能接上进度游标与取消。
* 终态**只由后端持久化结果确认**：轮询 `committed` 响应的 `response.run_status`（`cancelled` / `waiting_review` / 空=正常）是权威；前端不做动画推断。
* 客户端关闭轮询/流（“停止等待”）只停止订阅，**不取消**后台工作；取消必须显式调用 cancel API。
* 取消后的修正走既有显式领域事件（再次提交新事件 / review 决策），不存在静默回滚。

## 5. 开关

| 环境变量 | 默认 | 含义 |
| --- | --- | --- |
| `STAGE0_RUN_PROGRESS` | `1` | 进度事件总开关（关闭时 progress 端点返回空快照） |
| `AGENT_NO_PROGRESS_LIMIT` | 未设（关） | ≥1 启用无进展检测；值 = 停止前允许的重复次数 |
| `STAGE0_RUN_REUSE` | `0`（关） | 同 run 只读结果复用 |
| `STAGE0_READ_CACHE` | `0`（关） | 跨 run 受控缓存（TTL 900s，键含 scope/revision/corpus/tool 版本；仅缓存成功纯读） |
