# Reliability P0 实施报告（框架集成与生产可靠性升级）

实施日期：2026-09-06。范围：**仅 P0**（可靠性与权限入口），依据 `docs/reliability-design/`（设计于 2026-09-05，本轮同批完成 Prompt A 审计与探针）。不进入 P1/P2；未部署、未连接真实人工、未发送外部通知；未触碰真实 `memory.db` 与既有评测工件。

## 一、修改文件与原因

| 文件 | 变更 | 对应 P0 项 |
| --- | --- | --- |
| [stage0/memory.py](../../stage0/memory.py) | ① `idempotency_keys` 增 `event_id`/`run_id` 列、`outbox_tasks` 增 `next_attempt_at`/`heartbeat_at`/`last_error_class`/`max_attempts`/`deadline_at`（增量迁移 `_ensure_r0_schema`，历史行不改写）；② 新表 `operation_receipts`（`UNIQUE(scope_id, operation_id)`）；③ `complete/fail_outbox_task` 租约 fencing（`status='running' AND lease_token=? AND lease_expires_at>now`，rowcount 校验，失败抛 `LeaseRejected`），新增 `heartbeat_outbox_task`；④ `fail_outbox_task` 错误分类 + 指数退避+jitter（base 5s / cap 300s）+ 尝试上限；`reopen_failed_outbox_task`（`effect_unknown` 拒绝重开）；⑤ `publish_event_result`：任务 done + 幂等键 committed + 完整结果**单事务**发布；⑥ `execute_with_receipt`（同 id 异输入抛 `OperationConflict`）与 `record_warnings_batch`（警告 episode+conclusion+回执**单事务**，崩溃不再重放出重复警告） | #4 回执、#5 租约、#6 统一发布、#7 错误分类 |
| [stage0/server.py](../../stage0/server.py) | ① 受理时服务端生成 `event_id`/`run_id`（uuid4 hex），`turn_id=run_id`——前 32 位截断碰撞消除；payload 贯通 `event_key`/`trace_id`；② committed 重放 POST 返回与 GET 相同形状的完整 `response`（单事务发布写入同一 body）；③ 409 引导改为显式 `POST /v1/events/{key}/retry`（ops 角色），不再提示换 key；④ Worker：心跳线程续租（TTL/3 间隔）、租约丢失结果丢弃不误判、失败分类（`classify_error`）+ 脱敏日志（只记异常类型/类别/trace_id/run_id，不记异常消息）、worker 循环异常不再吞掉；⑤ `Principal`（`STAGE0_AUTH_MODE=local-demo`（默认，显式告警）/`deployment`（无 `STAGE0_AUTH_TOKEN` 拒绝启动））；对象级 scope 校验覆盖读端点/冲突操作/retry/rechecks；`conflict_action.actor` 改为认证主体，请求体 actor 降为自述；⑥ 简单限流（默认 60 req/min/principal，429）；⑦ trace 中间件（`X-Trace-Id` 贯穿请求→任务→错误响应） | #1 身份、#2 形状、#6、#7、#8 授权、#9 日志 |
| [stage0/agent.py](../../stage0/agent.py) | ① `handle(client_event_id=...)` + `AgentState.client_event_id`；`MemoryWriteTool` 把 `api:{key}` 作为 `consolidate_interaction(client_event_id=...)` 传入——领域事件身份与请求层一致；② `consolidate_event` 全命令（整合+药物变更）置于 `execute_with_receipt` 回执下；`record_warnings` 走 `record_warnings_batch`；③ 墙钟预算预留 `PLANNER_RESERVE_SECONDS=15s`：预算>预留时提前降级，避免 60s 同步 planner 调用跨过截止线（预算<预留时保持原阈值，Stage 8 测试语义不变） | #1 领域贯通、#4 回执、（P0 限定的）#7 预算 |
| [stage0/api_client.py](../../stage0/api_client.py) | ① `SubmitKeyStore`：一次业务提交一个稳定键；指纹相同且未 resolved 的重试复用原键；committed 后 resolved，再次提交=新事件；② 结果归一化 `{"event_key","status","response"}`，committed 重放与 GET 一致；③ `retry_event()` 显式恢复入口；超时错误信息明确提示"同键重试" | #2 UI 稳定键、#3 形状 |
| [stage0/app.py](../../stage0/app.py) | `_handle_event` API 模式提交路径接入稳定键语义（键逻辑在 `SubmitKeyStore`，由 client 持有）；默认直连路径零改动 | #2 |
| [stage0/test_reliability_p0.py](../../stage0/test_reliability_p0.py)（新） | 14 个 P0 回归测试（见下） | 全部 |
| [stage0/test_stage10_server.py](../../stage0/test_stage10_server.py) | 永久失败构造改为新签名（`lease_token` + `error_class="permanent"`） | 适配 |

设计文档中"deadline_at/max_attempts"均已实装：deadline 在受理时设 30 分钟，超期任务领取时判 `deadline_exceeded` 进失败队列；`max_attempts` 参与回收/失败判定（口径=含首次）。

## 二、幂等/一致性语义（本轮实际承诺）

- 请求层：同键同载荷重放返回**与 GET 相同**的完整 committed body；同键异载荷 422；失败键 409 + 显式 retry 指引。
- 领域层：`interactions.event_key == "api:{key}"`（探针证实）；consolidation/警告批次由 `operation_receipts` 防重放，同 operation id 异输入拒绝。
- 残余窗口（诚实边界）：`execute_with_receipt` 的 executor 内部事务先提交、回执后落库——若在两者之间崩溃，重放会再次进入 executor，由其领域级去重（consolidation 的 event_key、identical-add 去重）兜底后在重放轮补上回执。警告批次无此窗口（单事务）。**不宣称全链路 exactly-once**：承诺范围是"至少一次执行 + 回执去重"。

## 三、实际测试结果（2026-09-06，Windows 11，Python 3.13.14，.venv）

| 命令 | 结果 |
| --- | --- |
| `python -m unittest stage0.test_stage3 stage0.test_stage5 stage0.test_stage6 stage0.test_memory_p0 stage0.test_memory_p1 stage0.test_memory_p2 stage0.test_stage8_agent stage0.test_stage10_server stage0.test_reliability_p0` | **131/131 OK（10.9s）**（117 既有 + 14 新增） |
| `python -m stage0.eval_memory --ablate` | **passed 28/28**；policy/bitemporal/dependency/selective_invalidation 四机制全部 load-bearing |

### P0 验收场景 → 测试

| 验收项（Prompt B） | 测试 |
| --- | --- |
| 两个长 key 前缀相同仍是不同 event | `test_long_prefix_collision_yields_two_events`（且库中 event_key 各为 `api:{key}`、双药物各一条） |
| 同 key 并发只受理一次 | 既有 `test_concurrent_same_key_same_acceptance`（保留通过） |
| UI 超时重试不新建事件；成功后有意重复=新事件 | `test_timeout_retry_reuses_key_and_resolves` |
| 成功重放返回一致完整结果 | `test_committed_replay_post_matches_get_shape` |
| 领域写成功后、发布前崩溃 → 无重复效果 | `test_recovery_after_publish_crash_has_no_duplicate_effects`、`test_warning_batch_replay_is_atomic` |
| 租约过期重领后旧 worker complete/fail/heartbeat 全被拒 | `test_stale_lease_writes_rejected`（`LeaseRejected`） |
| 永久错误不自动重试；retryable 按退避；预算上限 | `test_retryable_failure_backs_off_and_caps`、`test_permanent_failure_reopened_via_explicit_retry`、`test_worker_classifies_failures`（safety 类） |
| 未授权读取/恢复被拦截 | `test_deployment_mode_refuses_anonymous_start`、`test_deployment_mode_enforces_token_and_roles`（401/403） |
| 日志无患者正文 | `test_worker_failure_log_carries_no_patient_text` |

### 修复前后探针对照

修复前探针（设计轮，`docs/reliability-design/01-audit.md`）：碰撞→同 turn_id、drain 后仅 1 条 interaction；旧租约 complete 覆盖为 done；POST 重放缺 response；event_key 为 `s1:api-aaa…` 截断派生。

修复后探针（`probe_after_p0.json`，本轮）：两事件 turn_id/run_id 全独立；旧租约 complete 抛 `LeaseRejected`、新执行者正常 done；POST 重放与 GET response 逐字段一致；`interactions.event_key` == `api:{key1}`/`api:{key2}`；回执 2 条 consolidate + 1 条 warnings。

## 四、迁移与回滚

- **迁移**：全部增量（`ALTER TABLE ADD COLUMN`（nullable/带默认）+ `CREATE TABLE IF NOT EXISTS`），对已开库幂等；建议先 `python -m stage0.backup --backup`。历史身份不改写；legacy `session:turn` event_key 行继续可读。
- **回滚**：新表/新列可保留不使用；代码回滚 `git revert` 本轮提交即可（无破坏性 schema 变更）。不设 `STAGE0_API_URL` 的 Streamlit 直连默认路径行为不变（本轮未改其逻辑分支）。

## 五、未解决问题与下一阶段接口

1. **领域写入与执行资格（lease）联动为部分实现**：回执短事务在校验回执前不直接校验 worker 租约（租约校验在任务层 complete/fail/heartbeat/发布处完成）；P1 引入 `WorkflowState`/runner 后把 lease 上下文传入领域命令。
2. **对账 reconciler 未建**：`publish_event_result` 已消除主要窗口；"receipt 有 succeeded 但任务未 done"的对账入口留待 P1（配合 workflow_runs 表）。
3. **effect_unknown 仅语义就位**：分类、拒绝盲重试、retry 端点拒绝已实现；真实外部效果路径（通知等）P2 落地。
4. **held-out DDI v2 重跑仍未执行**（Stage 9 遗留，需 KEGG 网络 + provider key）。
5. **性能目标全部待测**；本轮未做任何压测。
6. P1 接口已就位：`operation_receipts`（P1 execute_write 节点复用）、`trace_id` 贯穿（P1 图节点延续）、`run_id`（P1 thread 映射）、`deployment` auth（P2 reviewer 角色扩展）。

## 六、手工配置说明

- 部署 profile：设 `STAGE0_AUTH_MODE=deployment` + `STAGE0_AUTH_TOKEN=<token>`（可选 `STAGE0_AUTH_ROLES=caregiver,ops`）；客户端带 `X-Stage0-Token` 头。缺失 token 时服务**拒绝启动**。
- local-demo profile（默认）：无鉴权，单患者演示；启动日志显式告警，不得暴露到 localhost 之外。
- 人工审核服务：P0 未集成（显式边界）；retry/角色接口为 P2 预留。
