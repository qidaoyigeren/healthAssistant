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

## 六、长期用药安全事项主线(2026-09-13 前端轮)

产品主线改为 `/`(别名 `/safety`),读 `stage0/safety_cases.py` 的读模型。
事项是一条**引用层**:它保存 ref,真相仍在 medications / semantic_memory / conclusions 里。
所以引用解析不了时字段是 `available:false`,前端如实标注「读取不到」,不补造内容。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/v1/safety-mainline` | 主线六块内容,顺序即阅读顺序:`current_medications` / `recent_medication_changes` / `attention` / `awaiting_user` / `awaiting_professional` / `needs_recheck` / `recently_settled` + `counts` + `necessary_checks` |
| GET | `/v1/safety-cases` | `{items: CaseView[], statuses: Record<string,string>}`(statuses 是服务端给的状态标签表) |
| GET | `/v1/safety-cases/{case_id}` | CaseView |
| GET | `/v1/safety-cases/{case_id}/closure-evidence` | 只读的「能不能关、为什么」现场核对:`{ok, reason, refs, checked:[{ref,state,reasons}], eliminated:[ref], still_present:[ref], blocking_inputs:[request_id]}`;`state` ∈ `trigger_eliminated`/`risk_present`/`unknown`。界面用它决定关闭按钮是否可用,不自己另算一套关闭条件 |
| POST | `/v1/safety-cases/{case_id}/seen` | body `{key}`。**只**写 `user_seen_at` 时间戳:不关闭事项、不清空未决项。界面上不得表述为「已处理」 |
| POST | `/v1/safety-cases/{case_id}/answer` | body `{key, expected_revision, request_id, value, answer_kind?}`;`answer_kind` ∈ `provided`/`unknown`/`empty`,省略则由服务端按内容判定。返回新的 CaseView。空值与「不知道」走**不同**路径:空值什么都不关;「不知道」结束追问但**不消除**不确定性,仍阻止关闭 |
| POST | `/v1/safety-cases/{case_id}/disposition` | body `{key, expected_revision, disposition, basis_kind, note?, decision_id?, follow_up?}`。**不含 `actor`**:身份由服务端从认证上下文取,请求体里的 `actor` 不被读取。`follow_up` = `{kind: review_at\|on_event\|arrangement, at?, condition?, owner?, note?}` |
| POST | `/v1/safety-cases/{case_id}/follow-up` | body `{key, expected_revision, action: "schedule"\|"cancel", kind?, at?, condition?, owner?, note?, reason?}`。`kind` ∈ `review_at`/`on_event`/`arrangement`;`at` **必须带时区**(naive → 422,响应统一规范化成 `+00:00` 秒精度);`condition` 是白名单结构 `{kind, ref, ...}`,未知 kind 或自由文本 → 422(**不静默降级**)。**不接受 `confirmed`**:给了时间或条件不等于有人确认过。返回新的 CaseView |
| POST | `/v1/safety-cases/{case_id}/follow-up/confirmation` | body `{key, expected_revision, note?}`。产生一条确认记录,使 `confirmed: true` 并写入 `confirmed_at`/`confirmed_by`/`confirmation_ref`;`confirmed_by` 取认证主体,**不接受请求体自称**。前置条件是存在 `schedule_state ∈ {scheduled, due}` 的安排,否则 409 |
| POST | `/v1/safety-cases/{case_id}/visits` | body `{key, expected_revision}`。**开始或继续**一次回访:已有未结束的回访就接着它走(不新开——新开会让用户答过的问题变回第一题)。「本次为什么跟进」由**服务端按事实判定**(`due` > `record_change` > `input_arrived` > `user_started`),调用方不能把到期说成主动。返回 CaseView |
| GET | `/v1/safety-cases/{case_id}/visits/{visit_id}` | 读一次回访的**持久结果**:本次为什么跟进、相对上次新增、已完成的动作、仍未解决、下一步、下一次安排及其确认状态。每条陈述带 `basis`(`program_check`/`user_report`/`model_explanation`/`record`) |
| POST | `/v1/safety-cases/{case_id}/visits/{visit_id}/candidates` | body `{key, name, field, value, note?}`。登记一条**待确认**的用药变更候选。`field` ∈ `dose`/`schedule`/`route`/`start_at`;`before` 由服务端从当前权威记录取,不采信调用方自报。**确认之前权威记录一个字节都不动** |
| POST | `/v1/safety-cases/{case_id}/visits/{visit_id}/candidates/{cid}/confirm` | 确认候选 → 沿**既有权威入口**(`record_input(medications=…)`)写入,必要安全检查按既有路径重新排队。只有这一步能改记录 |
| POST | `/v1/safety-cases/{case_id}/visits/{visit_id}/candidates/{cid}/dismiss` | 放弃候选。**什么都不写**——权威记录本来就没被它碰过 |
| POST | `/v1/safety-cases/{case_id}/investigate` | body `{key, budget?, goal?}`;建 `care_task`(goal_type=`safety_case`)并排队,返回任务;进度用 `/v1/runs/{run_id}/progress` 轮询 |
| POST | `/v1/care-tasks/{task_id}/input` | body `{key, revision, review_request_ids: string[], answers?: [{request_id, value, kind?}]}`;kind ∈ `user_report`/`material_note`。只关闭**指名回答**的那条 request_id(安全事项的补充现在直接走上面的 `/answer`) |
| POST | `/v1/care-tasks/{task_id}/resume` | 提交补充后用 `revision+1` 继续;`record_input` 恰好把任务版本 +1 |

CaseView 字段以 `case_view()` 序列化为准(见 `frontend/src/api/types.ts` 的 `SafetyCaseDto`)。
`necessary_checks.note` 是服务端对队列语义的原文说明,必须在界面上可见:
未运行时检查队列不会自动推进,「没有提示」不等于「检查通过」。

`answered_parts` 的每个元素现在还带一个可选的 `assessment`
(`{status: verified|candidate|stale|unsupported, reason, source_ref, locator, dependency_refs}`)。
**没有这个键就是「未核实」**,不得按 `verified` 读,也不得补默认值。
`verified` 只表示"约定范围内的答案依据已核对",不表示整体用药安全、也不是专业医疗判断。

`follow_up` 在既有 6 键之上新增 `confirmed`(只由确认端点置真,存量记录里的
`confirmed: true` 但拿不出 `confirmed_at`/`confirmation_ref` 的一律按 `false` 读)、
`confirmed_at`/`confirmed_by`/`confirmation_ref`/`revision`/`schedule_state`/
`last_triggered_at`/`last_trigger_reason`/`care_task_id`/`blocked_reason`。
`kind`(安排的种类)与 `schedule_state`(走到哪了)是两件事,不要合并。
`schedule_state` 由 worker 周期推进;`worker_thread=False` 时不跑,界面不得
宣称「后台已在执行」。

### 回访(visit)

CaseView 增加一个派生的 `visit` 块(没有回访历史时是 `null`):
`visit_id` / `status` / `is_open` / `reason` / `focus` / `change_candidates` /
`pending_candidates` / `result` / `first_visit` / `care_task_id` / `task_status`。
`status` ∈ `open` / `awaiting_user` / `completed` / `blocked`,**以执行它的任务为准**
(记录里的 `open` 在任务已跑完等用户时是不准确的,照它显示会让用户白等)。

`result.since_last` 在没有新记录时**只会**说「系统尚未收到新记录;这不等于情况没有
变化,也不表示风险已经解除」。**不得**把它渲染成「情况稳定」或「风险已解除」——
前者是系统的信息状态,后者是一句没有人做过的判断。

`change_candidates[].source` ∈ `user_declared` / `model_proposed`,**必须显示**:
确认的人要知道自己在确认什么。

### `/answer` 的回答种类(加法式扩展)

原有 `provided` / `unknown` / `empty` 不变,新增四种**跟进行动表态**:

| 值 | 含义 | 对请求的影响 |
|---|---|---|
| `done` | 已完成 | **仅当**该请求 `question_kind='follow_up_action'` 才置 `answered`;否则保持未决 |
| `not_done` | 尚未完成 | 保持 `open`,记 `deferred_kind`/`deferred_at` |
| `declined` | 暂不回答 | 保持 `open`,记 `deferred_kind`;**本轮不再追问同一条**(≠「不知道」) |
| `changed` | 情况有变化 | 保持 `open`,并置 `expects_change_candidate`——要走候选→确认那条路 |

四者**含义不同,不能合并成「已解决」**。与 `unknown` 的区别:`unknown` 表示
「这位用户答不了」,系统转去找替代证据;`declined` 表示「先别问这条」,还要再等他。

**推迟类的回答不唤醒调查**:未完成/暂不回答没有给出模型可以据以行动的新事实,
「情况有变化」的下一步是候选确认。唤醒会让那一轮既没有合法动作、又终止不了,
最后以熔断收场——用户会看到「这次回访没有跑成」,而他只是说了一句「还没做」。

### 状态与结论语义(前端必须照此显示)

- `monitoring`(持续跟进中(风险仍在)):**风险仍然成立且已有一项安排**,不是「等待专业人员」,
  也不是「风险已消除」。`case.follow_up` 的 `confirmed:false` 表示没有可信的复核时间或触发条件,
  必须显示为「一项待确认的安排」,不得渲染成真实复查周期。
  (当前 `case_view()` 尚未输出 `follow_up`;界面退回历史里那次持续跟进处置登记的同一份安排。)
- `awaiting_professional`:只有专业人员能回答的问题;项目未连接真实医护服务,`NO_CLINICIAN_NOTICE`
  必须始终可见,不得表述为「已送达医生」。
- 结论的 `trigger_state`:`risk_present` = 检查显示风险仍然成立;`trigger_eliminated` = 触发条件已消失;
  `unknown` = 无法判断。三者不能混读。
- `required_inputs[].status`:`open`(等用户)/ `answered` / `unknown`(用户明说不知道 →
  同时 `needs_alternative_evidence:true`)。`unknown` **不再等用户**,但**仍未解决、仍阻止关闭**,
  不得显示为「已回答」。
- 历史里的 `resolution_basis_retired`:此前的处置因事实变化**不再适用**,当前依据已清空;
  该记录保留在历史里(它确实发生过),但不得当作现行依据展示。

### 处置:关闭条件由服务端强制(前端只显示它的原话)

- `disposition` ∈ `resolved_with_basis` / `escalated_to_professional` / `accepted_monitoring`。
- `basis_kind` ∈ `deterministic_check_completed` / `professional_review_applied` / `user_reported`。
- 只有前两种依据能关闭事项;`user_reported`(用户转述医生意见)用于关闭一律 409,
  说明为「用户转述与模型判断不能作为关闭事项的依据;请提交给专业人员复核」。
- `accepted_monitoring` 把事项置为 `monitoring`(**不**转入等待专业复核),同时记录一份
  `resolution_basis.kind = 'monitoring_arrangement'` —— 那是「凭什么说风险还在」,**不是**关闭依据。
- 其余非关闭处置把事项推向 `awaiting_professional`。
- `professional_review_applied` 在当前项目里**必然失败**:没有真实医护服务,也不存在属于本事项的
  已生效复核决定。界面要么不提供它,要么以禁用状态给出并说明原因,不让用户去撞一次 409。
- `deterministic_check_completed` 现场校验(即 `closure-evidence`):被引用的结论仍是 `current`、
  记录的 `input_revision` 等于当前 scope 版本、存在触发条件已消除的结论、没有仍显示风险成立的结论、
  没有阻塞性的未决问题;任一不满足即 409(如「关联结论仍显示风险存在,不能关闭;如已有管理安排,
  请登记为持续跟进」)。
- 409 体走统一错误模型(`error.category = validation`),前端**原样**显示 `error.message`,
  追踪号单独显示,不改写、不吞掉。
