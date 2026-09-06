# 可靠性升级设计蓝图：总览与一页建议

设计日期：2026-09-05。审计基线：当前工作区（HEAD `528b3a5` + 未提交 Stage 8–10 修改），全部结论以本轮代码核查与临时库探针为准（探针记录见 [01-audit.md](01-audit.md)）。

## 文档地图

| 文件 | 内容 |
| --- | --- |
| [01-audit.md](01-audit.md) | 现状审计表（10 项风险逐项复核，已证实/已修复/待验证区分）+ 探针结果 |
| [02-architecture.md](02-architecture.md) | 总体架构图、Agent 图节点映射、人工审核状态图、WorkflowState |
| [03-adr.md](03-adr.md) | 框架选型 ADR（含 LangGraph 官方文档核验结论，访问日期 2026-09-05） |
| [04-data-and-transactions.md](04-data-and-transactions.md) | 身份模型、operation_receipts、关键事务伪 SQL、崩溃恢复时序、保留策略 |
| [05-retry-and-security.md](05-retry-and-security.md) | 错误分类决策表、重试预算、超时分层、权限矩阵、注入防御 |
| [06-human-handoff.md](06-human-handoff.md) | 分流矩阵、review_cases 状态机、审核决策与恢复、无人接单行为 |
| [07-staged-plan.md](07-staged-plan.md) | P0–P4 分阶段工作、文件映射、验收与回滚 |
| [08-test-matrix.md](08-test-matrix.md) | 故障注入与安全测试矩阵（环境、断言、产物路径） |
| [09-sources.md](09-sources.md) | 已核查官方来源（含访问日期）与剩余假设 |

## 一页建议

**最优先的不是换框架，而是关闭"重复副作用"与"旧执行者覆盖"两类窗口。** 本轮探针复现了五个可稳定触发的缺陷：

1. 幂等键前 32 位截断导致两个合法不同 key 共享同一 `turn_id`，第二个事件在 consolidation 层被卷入同一 turn（探针：drain 后 interactions 仅 1 条）。
2. 过期租约持有者的 `complete` 仍把任务写成 `done` 并覆盖新执行者的结果（探针：`status_after_old_complete=done`）。
3. committed 后重复 POST 返回缺 `response` 的受理体，而 GET 返回完整结果——同一业务请求经不同重试路径得到不同结果形状。
4. `api_client.submit_event` 每次调用生成新幂等键，UI 超时重试会登记为第二个业务事件。
5. API 侧 `event_key`（`api:{key}`）止步于任务 payload，从未传入 `handle()`/`consolidate_interaction`，文档声称的领域层去重键实际不存在。

这五项全部落在 P0，修复方案与验收见 [07-staged-plan.md](07-staged-plan.md)。LangGraph 迁移（P1）在此之前没有意义：流程恢复越可靠，上述窗口被触发得越稳定。

**建议排序**：P0（身份/租约/回执/退避/授权骨架，4–7 天）→ P1（LangGraph 最小迁移，4–7 天）→ P2（人工闭环，5–8 天）→ P3（观测与隔离评测）→ P4（PostgreSQL/多写者，条件触发）。一个月窗口内：P0 + P1 + 一条真实的本地模拟人工闭环。

## 可直接接续执行 P0 的任务清单

1. `server.py`：生成服务端 `event_id`/`run_id`（uuid4 hex），`turn_id` 不再从键截断；payload 携带 `event_id` 并传入 `handle()`。
2. `agent.py`：`handle()` 增加 `client_event_id` 参数，透传至 `consolidate_interaction(client_event_id=...)`；数据库断言 interaction.event_key == `api:{key}`。
3. `memory.py`：`complete/fail/heartbeat_outbox_task` 增加必填 `lease_token` 参数，UPDATE 带 `WHERE status='running' AND lease_token=? AND lease_expires_at>now`，检查 rowcount。
4. `server.py`：committed 重放 POST 返回与 GET 相同形状（含 `response`）；客户端只从状态端点取结果。
5. `app.py`/`api_client.py`：一次提交的幂等键与 event 身份存入 `st.session_state`，超时重试复用；仅明确的新业务提交才生成新键。
6. `memory.py`：新增 `operation_receipts` 表（`(scope_id, operation_id)` 唯一 + `input_hash` 拒绝），保护 consolidation、显式药物变更、警告/结论写入的重放窗口；不重写历史身份。
7. `server.py`/`memory.py`：任务完成、幂等键状态、结果发布收敛为单事务；任务表加 `next_attempt_at`/`last_error_class`/`heartbeat_at`，失败退避 + jitter，不再立即重开。
8. `server.py`：`Principal` 依赖（local-demo profile 显式声明；部署 profile 无鉴权配置拒绝启动）；`actor` 从身份派生，请求体 `actor/source` 降级为自述字段。
9. `server.py`/`api_client.py`：错误分类（`retryable/permanent/safety/effect_unknown`）、脱敏结构化日志贯穿 `trace_id`、后台循环不再吞异常。

红线：默认离线路径行为不变；不重置/不改写现有 `memory.db` 与既有工件；未实测数字一律"待测"。
