# 前端 API 契约(2026-09-06 前端轮)

基线:2026-09-05 工作区代码。本文区分「原有接口」与「本轮新增接口」;字段名以
`stage0/memory.py` 实际序列化为准。前端实现见 `frontend/src/api/types.ts`。

## 一、原有接口(未改动;`memory/state` 有增量归一化)

| 方法 | 路径 | 契约 |
| --- | --- | --- |
| POST | `/v1/events` | 需 `Idempotency-Key`(`[A-Za-z0-9_-]{1,128}`);异步受理 202 `{event_key,event_id,run_id,status:"queued",status_url}`;重放 202 + `Idempotent-Replay: true`(committed 重放携带完整受理体);同键异载荷 422 `idempotency_key_reused`;失败键 409 `previous_attempt_failed` |
| GET | `/v1/events/{key}` | 202 queued/processing;200 `{event_key,status:"committed",response:{text,warnings,conflicts,audit_trail,safety_status,operation_outcomes*,event_id,run_id}}`;500 `{event_key,status:"failed",error_class,error}`(无 trace_id,不保证) |
| POST | `/v1/events/{key}/retry` | 恢复失败任务(保留原事件身份);需 ops/support 角色(local-demo principal 具备);effect_unknown 时 409 `retry_not_available` |
| GET | `/v1/memory/state` | 可带 `valid_at`/`known_at`;返回 facts/medications/open_conflicts/uncertainties/meta。**本轮增量**:open_conflicts 行追加解析后的 `resolution` 与 `ref`(原 `resolution_json` 字段保留,旧客户端不受影响) |
| GET | `/v1/memory/timeline` | `limit` 默认 100 上限 500;**语义不变**:按 occurred_at,id 升序取前 N(前端不使用它做时间线,见 `/v1/history/events`) |
| GET | `/v1/memory/conflicts` | 仅 open 冲突(带 resolution 解析 + ref) |
| GET | `/v1/alerts` | 旧版精简预警(ref/memory_refs/source_refs/text);前端不使用,保留兼容 |
| POST | `/v1/conflicts/{id}/actions` | `{action: resolved\|dismissed\|reopened\|undo, basis, actor, chosen_ref?}`;返回更新后的冲突行 |
| POST | `/v1/rechecks` | `{max_jobs}`(1–20);执行已排队复查;返回 `{status: ok\|no_hook, pending, completed}` |
| GET | `/v1/health` | status/db/schema_version/pending_outbox_tasks/pending_rechecks/llm_planner_enabled/worker_thread/graph_runner_enabled/auth_mode/uptime_seconds |

## 二、本轮新增接口

分页统一 envelope:`{"items": [...], "next_cursor": string|null, "total": number}`;
cursor 为 `(排序键, id)` 的 base64(`{"k":...,"id":...}`),无效 cursor → 422 `invalid_cursor`。
`limit` 默认 50,上限 200。

| 方法 | 路径 | 说明 | 数据来源 |
| --- | --- | --- | --- |
| GET | `/v1/alert-records`?status=current\|stale\|all&kind&limit&cursor | 完整预警读模型。字段同 conclusions 行 + 解析的 memory_refs/source_refs + `severity:null`、`confidence:null`(**无结构化严重度列,如实为 null,不从文字推断**)+ `evidence_available` | conclusions 表 |
| GET | `/v1/alert-records/{id}` | 单条 + `chain`(结论替代链) | conclusions + `conclusion_chain` |
| GET | `/v1/conclusions/{id}/history` | `{versions(旧→新), current_head, status, stale_reason}` | `conclusion_chain` |
| GET | `/v1/history/events`?limit&cursor&event_type&q&occurred_from&occurred_to | 事件历史,**倒序**(新在前),服务端 cursor 分页 | episodic_memory |
| GET | `/v1/history/event-types` | 类型与计数(筛选器数据) | episodic 聚合 |
| GET | `/v1/history/search`?q&limit | 历史候选搜索;返回 `{mode: no_query\|fts5_trigram\|like_fallback, results, tokens}`。**有写库副作用**(FTS 索引同步,单写者进程内安全);结果只是历史候选 | `memory_search.search_history` |
| GET | `/v1/medication-records`?status=active\|stopped\|superseded\|disputed\|all&limit&cursor | 全部药物版本(含 stopped/superseded),非当前药单过滤 | medications 表 |
| GET | `/v1/medication-records/{id}` | 单条 + `versions`(同 medication_key 全版本,旧→新,含 predecessor_id) | medications 表 |
| GET | `/v1/conflict-records`?status=open\|resolved\|dismissed\|all&limit&cursor | **全状态**冲突;行含解析后的 resolution + ref | conflicts 表 |
| GET | `/v1/conflict-records/{id}` | 单条 + `sides`(left/right 引用的真实记录、layer、audit_log;解析失败如实标 error)+ `actions`(动作序列) | conflicts + resolve_ref + audit_for + conflict_actions_for |
| GET | `/v1/conflicts/{id}/history` | 动作历史(action/basis/actor/chosen_ref/previous_status/created_at/undone_by) | conflict_actions_for |
| GET | `/v1/memory/item`?ref= | 解析 `memory:{layer}:{id}@v{version}` 引用 → `{layer,item_id,version,item,audit_log}`;版本精确匹配;非 memory: 引用/未知层 → 422(**不接受文件路径或外部 URI**) | `resolve_ref` + `audit_for` |
| POST | `/v1/memory/fact-actions` | `{ref, action: verify\|retract, actor, basis}`;basis 必填(空 → 422)。返回真实 outcome:`verified` / **`blocked_by_conflict`(HTTP 200,不是错误)** / `retracted`;未知引用或不可操作状态 → 422 `invalid_fact_ref`。**无幂等保护:客户端重试 = 新的业务动作**(契约要求如此,前端对同一动作不做自动重试) | `verify_semantic_fact` / `retract_semantic_fact` |
| GET | `/v1/recheck-tasks` | `{pending, history, pending_count}`;每项附 `target_conclusion`(引用不存在时为 null) | dependency_tasks + conclusions |
| GET | `/v1/sessions` | 会话列表(session_id/event_count/first_at/last_at) | interactions 聚合 |
| GET | `/v1/sessions/{sid}/events`?limit&cursor | 会话提交记录,倒序;每项含 idempotency_key/process_status/request(从 outbox payload 恢复)/response(**outbox 持久化结果,没有就是 null,不补造**)/outbox_status/outbox_error | interactions + outbox_tasks |
| GET | `/v1/sessions/{sid}/turns/{turn_id}/trace` | 已落库执行审计(`traces_for_turn`);只展示已有记录 | turn_traces |
| GET | `/v1/overview` | 服务端聚合计数(口径见响应内注释):medications_active / medications_records / facts_active / facts_uncertain_time / conclusions_current / conclusions_stale / conflicts_open / rechecks_pending / outbox_pending + last_recorded | 各表 COUNT |
| POST | `/v1/data/exports` | 全库 JSON 交换导出(.json.gz);**先做在线备份快照再导出**(快照一致性);返回报告 + artifact_id | `export_database` |
| POST | `/v1/data/backups` | SQLite 在线备份(.db);返回报告 + artifact_id | `backup_database` |
| GET | `/v1/data/artifacts` | 产物列表(artifact_id/kind/size_bytes/created_at) | exports/ + backups/ |
| GET | `/v1/data/artifacts/{id}/verify` | 产物校验(sha256/integrity/counts,来自服务端) | `verify_artifact` |
| GET | `/v1/data/artifacts/{id}/download` | 受控下载;artifact_id 白名单格式(`memory-\d{8}-\d{6}.(db\|json.gz)`),拒绝路径穿越 | FileResponse |

## 三、事件结果的结构化 `operation_outcomes`(本轮新增字段)

`AgentResponse` 新增 `operation_outcomes`,来自真实工具结果,不解析自然语言:

```ts
interface OperationOutcomeDto =
  | { kind: "medication_change"; outcome: "add"|"dose_change"|"remove"|"deduplicated"|"unresolved";
      ref?: string; display_name?: string; event_ref?: string; replayed?: boolean }
  | { kind: "semantic_fact"; outcome: "inserted"|"update"|"conflict"|"deduplicated";
      ref?: string; namespace?: string; key?: string };
```

实现位置:`agent.py`(`AgentResponse._operation_outcomes`,取最近一次
`consolidate_event` 观察结果)、`memory.py`(`ConsolidationResult.semantic_outcomes` 记录
每次事实写入的真实 outcome)。clarification 路径同样携带。事件无写入时为空数组。

## 四、错误模型

- 受理层错误(统一):`{"error":{code,category,message,trace_id,details}}`,category ∈
  validation/safety/provider/internal。
- 任务失败轮询:`{"event_key",status:"failed",error_class,error}` —— **无 trace_id**,前端
  不声称每个失败都有追踪号。
- 网络错误/非 JSON 响应:前端 `ApiError(kind: network|http|parse)` 单独建模。

## 五、幂等与重试契约(前端已实现)

1. 每次新提交 = 新 UUID key;请求重发复用同 key + 完全相同内容(含 session_id/occurred_at/source/text)。
2. POST 响应丢失 → 状态 unknown → 继续轮询同 key,不自动重发 POST。
3. `Idempotent-Replay: true` 视为同一次事件,不重复插入助手消息。
4. 422 `idempotency_key_reused` 明确提示「需要新的提交操作」,用户输入保留。
5. 409 `previous_attempt_failed` → 提供服务端重试(retry 端点,保留原身份)或用户明确的新提交。
6. 冲突动作 / fact-actions / rechecks / 导出备份**没有**幂等键保护;前端不做自动重试,
   重复点击有 busy 状态防抖。
7. 写事件(档案/用药/暴露)同会话串行;查询类(user_message/query_current_medications)不受限。
