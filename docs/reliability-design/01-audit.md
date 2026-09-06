# 现状审计（2026-09-05）

审计范围：`stage0/agent.py`、`server.py`、`api_client.py`、`app.py`、`memory.py`、`response_safety.py`、`rag.py`、`ddi_engine.py`、`test_stage8_agent.py`、`test_stage10_server.py`。尊重工作区未提交修改（HEAD `528b3a5`）。

## 探针记录（本轮实测，临时 SQLite，未触真实 memory.db）

探针脚本执行于 2026-09-05，使用 `create_app(db_path=tempdir, worker_thread=False)` + `TestClient`，与既有测试 `_App` 同构：

```json
{
  "prefix_collision": {
    "http": [202, 202],
    "turn_ids": ["api-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "api-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"],
    "distinct_turn_ids": 1,
    "interactions_after_drain": 1
  },
  "stale_worker": {
    "new_lease_issued": true,
    "status_after_old_complete": "done",
    "result": {"writer": "expired_worker"}
  },
  "committed_replay_shape": {
    "post_keys": ["event_key", "status", "status_url"],
    "get_keys": ["event_key", "response", "status"],
    "post_has_response": false,
    "get_has_response": true
  },
  "consolidation_event_keys": [
    {"event_key": "s1:api-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "turn_id": "api-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}
  ],
  "api_client_generates_key_per_call": true
}
```

前缀碰撞探针的附加证据：两个前 32 位相同、第 33 位起不同的合法 key（长度 40，均在 `IDEMPOTENCY_KEY_PATTERN` 允许范围）均获 202，任务 payload 中 `turn_id` 完全相同；drain 后 `interactions` 表只有 1 条记录——第二个事件没有独立进入 consolidation，被同 turn 去重逻辑吞掉。这不是理论碰撞，是可复现的事件丢失。

## 审计表：Prompt A 指定的 10 项风险复核

| # | 风险（Prompt A 原文摘要） | 代码证据 | 状态 | 优先级 | 修复方向 |
| --- | --- | --- | --- | --- | --- |
| 1 | turn_id 取幂等键前 32 位，可碰撞 | [server.py:255](../../stage0/server.py#L255) `turn_id = f"api-{idempotency_key[:32]}"`；探针 1 复现事件被吞 | **已证实** | P0 | 服务端生成 uuid `event_id`/`run_id`；键完整映射到事件，不截断充当身份 |
| 2 | event_key 未贯通 handle → MemoryWriteTool → consolidate_interaction | `handle()`（[agent.py:2131](../../stage0/agent.py#L2131)）签名无 event_key；[agent.py:264-266](../../stage0/agent.py#L264) 只传 `turn_id`；探针 4：interaction 的 event_key 是 `s1:api-aaaa…`（由 turn_id 派生），不是 payload 里的 `api:{key}` | **已证实** | P0 | `handle(client_event_id=...)` 透传；测试断言库中实际 event_key |
| 3 | complete/fail/heartbeat 不校验 lease_token，旧执行者可覆盖 | [memory.py:2441-2459](../../stage0/memory.py#L2441-L2459) 仅按 task_id UPDATE；探针 2：过期执行者 complete 后任务变 `done` 且结果被覆盖 | **已证实** | P0 | 所有任务写入带 `lease_token + running + 未过期` 条件并检查 rowcount；领域写同样校验 |
| 4 | committed 重复 POST 缺 response，client 直接返回 acceptance | [server.py:278-282](../../stage0/server.py#L278-L282) 合并的 stored response 只有 `{event_key,status}`；[api_client.py:44-46](../../stage0/api_client.py#L44-L46) 见 committed 即返回；探针 3 形状不一致 | **已证实** | P0 | POST 统一返回受理资源（含完整 response 或引用 status_url）；客户端统一走状态端点 |
| 5 | UI 超时/重复操作生成新幂等键 | [api_client.py:41](../../stage0/api_client.py#L41) `key = idempotency_key or f"ui-{uuid4().hex}"`；[app.py:376-380](../../stage0/app.py#L376-L380) 未传键、session_state 无保存 | **已证实** | P0 | 提交时生成并保存（key, event_key, status_url）于 `st.session_state`；重试复用 |
| 6 | 任务完成与请求结果分事务 | [server.py:128-133](../../stage0/server.py#L128-L133) `complete_outbox_task` 与 `complete_idempotency_key` 两次独立 `with connection` | **已证实**（代码层） | P0 | 单事务发布：任务 done + key committed + result；checkpoint 窗口用回执+对账收敛 |
| 7 | 崩溃测试只覆盖"领取后未执行" | [test_stage10_server.py:181-210](../../stage0/test_stage10_server.py#L181-L210) 仅过期租约回收重执行；无"领域写成功后 complete 前崩溃"用例 | **已证实** | P0 | 新增故障注入测试：写后崩溃 → 恢复 → operation receipt 唯一 |
| 8 | record_conclusion、药物版本、工单、审核、通知缺稳定操作回执 | [memory.py:1784](../../stage0/memory.py#L1784) `record_conclusion` 直接 INSERT，无回执表；全仓无 `operation_receipts`；审核/通知系统尚不存在（Stage 11 未实施） | **已证实**（部分为"未实现"而非"实现有缺陷"） | P0（回执表 + 三个写入口） | 新增回执表；consolidation/药物变更/警告结论先接；工单/通知随 P2 |
| 9 | 120s 预算对运行中外呼与末尾 composer 无约束；重启重置 | [agent.py:2146-2165](../../stage0/agent.py#L2146-L2165) 循环顶部检查，`perf_counter` 进程内；planner 60s 同步调用可跨过剩余预算；`TurnBudget.from_env` 每次 handle 重建 | **已证实** | P1（P0 只做调用前剩余预算检查） | 调用前传剩余时间，预留 15s 收尾；预算持久化到 run（P1） |
| 10 | actor/source 可伪造；状态查询/memory ref/人工恢复无对象授权 | [server.py:76-81](../../stage0/server.py#L76-L81) `ConflictActionIn.actor` 来自请求体；全服务无鉴权依赖；读端点无 scope 校验 | **已证实** | P0（骨架） | `Principal` 依赖注入；actor 派生；部署 profile 无鉴权拒绝启动；local-demo 显式声明 |

## 其他确认事实（非 Prompt A 清单但影响设计）

- **失败任务无退避**：`fail_outbox_task` 将任务重开为 `open`，worker `poll_interval=0.2s` 立即重领——三次 attempt 在 <1s 内烧完（[memory.py:2448-2459](../../stage0/memory.py#L2448-L2459)、[server.py:111-134](../../stage0/server.py#L111-L134)）。错误一律 `f"{type(exc).__name__}: {exc}"`，未分类。
- **后台吞异常**：worker loop 的 `drain_once` 异常与 `run_pending_rechecks` 异常均 `except: pass`（[server.py:168-170](../../stage0/server.py#L168-L170)、[server.py:136-139](../../stage0/server.py#L136-L139)），队列停摆无日志。
- **`_recover_expired_outbox_tx` 与 rechecks 行为不一致**：outbox 恢复不递增 attempts（依赖 claim 时 +1），rechecks 恢复时 +1（[memory.py:2424-2439](../../stage0/memory.py#L2424-L2439) vs [memory.py:2482-2498](../../stage0/memory.py#L2482-L2498)）。语义需统一并在测试中固定。
- **租约 TTL 300s > 任务无心跳**：任务执行无 heartbeat 续租，长回合（LLM 60s×N + RAG）可能超过 TTL，被回收重执行 → 依赖 event_key 去重兜底；当前该去重恰好又依赖风险 #1/#2 的坏身份。三层叠加是重复副作用的最大来源。
- **保留项**：`extract_facts`/`ddi_live_fallback` 执行器未接入 outbox（Stage 10 报告已声明的裁剪）；TurnBudget chars/1.5 估算是粗兜底（报告偏差 1）。
- **健康项（复用，不重做）**：双时间记忆、依赖失效、FTS、备份、`accept_api_event` 的受理+入队单事务（[memory.py:2334-2369](../../stage0/memory.py#L2334-L2369)）设计正确，保留。
- **已修复项确认**：设计文档曾疑虑的"安全拒绝文本 vs 交付文本不一致"（预算降级通知）已在 Stage 8 修复——`BUDGET_DEGRADED_NOTICE` 在最终安全检查之前追加（[agent.py:2153-2165](../../stage0/agent.py#L2153-L2165) 后接 `_finalize` → `safety.enforce`）。

## 已实现 / 实现不完整 / 设计预留 / 待验证 的边界

- **已实现且测试覆盖**：请求幂等四象限、outbox 崩溃恢复（仅 claim 后未执行形态）、租约领取、四类错误模型、trace 持久化、预算/熔断/verifier。
- **实现不完整**：领域级 event_key 贯通（#2）、租约 fencing（#3）、结果发布事务（#6）、错误分类与退避、身份授权（#10）。
- **设计预留**：outbox 其他任务类型、`/v2` API、review_cases（Stage 11/P2）、PostgreSQL 迁移（D7 触发条件）。
- **待验证**：held-out DDI v2 重跑（需 KEGG 网络 + provider，Stage 9 遗留）；本轮未运行全量回归与线上评测，历史数字（117/117、28/28）不属于本轮证据。
