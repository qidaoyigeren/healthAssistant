# 选型 ADR 与 API/状态契约

## ADR-001：Agent 编排采用 LangGraph StateGraph（P1）

- **决定**：引入 LangGraph 承接流程状态、条件路由与 interrupt/resume；不引入 LangChain 其余部分，不用预制 Agent 替换现有安全守卫。
- **理由**：官方定位为低层编排框架，允许混合确定性与模型步骤，与"LLM 决策 + 代码强制安全"的现有语义一致；提供持久化 checkpoint 与 interrupt（人工等待）的框架级支持。
- **否决项**：Temporal（当前无跨服务/长周期需求，且会与图争夺重试与人工等待状态的所有权）；自研状态机（需自行实现恢复/interrupt 语义，收益低于成本）。
- **版本策略**：实施时以官方资料、依赖解析和本仓最小冒烟（持久化/重启/interrupt 三项）锁定版本后再写依赖锁；本文不预设未经本仓验证的版本号。已核验的语义边界：节点 timeout 仅 async（langgraph≥1.2）、RetryPolicy max_attempts 含首次、interrupt 恢复重跑整节点、interrupt 不可被 try/except 包裹（访问日期 2026-09-05，见 [09-sources.md](09-sources.md)）。
- **回滚**：feature flag 只路由新 run；legacy 在途 run 走原实现；flag 关闭即回到现状，无数据迁移负担。

## ADR-002：接口层沿用 FastAPI/Pydantic，身份升级走 /v2

- 保留 v1 的 202/422 语义不动；新增能力（event_id/run_id、对象授权、retry 端点、review 接口）在 `/v2` 明示，v1 可加兼容适配但不得悄悄改变行为。
- Pydantic 判别联合按 event_type 区分请求模型（medication_change / patient_fact / procedure_exposure / query），约束字段、枚举、长度、payload 深度。

## ADR-003：持久化——近期 SQLite 单写者，PostgreSQL 条件触发

- 单患者单写者是**近期默认**，不是缺陷；现有 WAL + 短事务 + `accept_api_event` 单事务受理保留。
- 触发 PostgreSQL 迁移的条件（满足其一）：多写者/多进程、多人多患者协作、高可用目标、SQLite 锁等待或队列龄实测超标。SQLite 通过的并发测试**不能**替代真实 PostgreSQL 验证。
- 迁移时引入 SQLAlchemy/Alembic 只管新表与 repository 边界，不重写全部记忆 SQL。

## ADR-004：重试唯一所有者

- 一次外呼的重试策略由**一个**所有者管理：图内节点用 RetryPolicy；图外适配器（KEGG/HTTP）若用 Tenacity，须显式 stop/wait/retry 条件（禁用其无上限默认装饰器）。SDK 自动 retry 一律设 0；outbox 重领只负责进程恢复与延迟调度，恢复同一 run/operation 并读取持久化 attempt 记录，不得重置次数再叠一层重试。LLM 结构修复、备用 provider、依赖重试全部计入同一调用/成本预算。
- 依赖熔断（provider closed/open/half_open）与现有"连续安全拒绝熔断"是两回事，分开实现，不得合并。

## ADR-005：观测采用 OpenTelemetry，默认不上报第三方

- 默认关闭导出或仅用本地/自托管 OTLP 脱敏管道；LangGraph 本地编排不意味着开启 LangSmith 云 tracing。普通 trace 不记录患者正文与隐藏思维链。trace 关联链：`trace_id → request → event → run → node → tool attempt → review`。

## ADR-006：Redis / Celery / Kafka / 向量库替换 / 多 Agent — 后置

引入条件写明：真实负载或业务依赖明确时再评估；不把"多框架"当作质量指标。当前 `outbox_tasks` 表兼具持久化作业队列职能，名称不意味着已接消息中间件。

## API 契约（v2 草案，P0 实现其中身份与授权骨架）

| 接口 | 契约要点 |
| --- | --- |
| `POST /v2/events` | 鉴权 + patient scope + Idempotency-Key；事务受理；返回 `event_id, run_id, status_url` |
| `GET /v2/events/{event_id}` | 对象授权；统一运行状态、完整结果或等待人工信息 |
| `POST /v2/events/{event_id}/retry` | 授权运维动作；保留 event/operation 身份；`effect_unknown` 拒绝盲重试 |
| `GET /v2/review-cases` | 按 reviewer 权限与患者范围查询（P2） |
| `POST /v2/review-cases/{id}/claim` | expected_revision + CAS（P2） |
| `POST /v2/review-cases/{id}/decisions` | 结构化决策 + Idempotency-Key + expected_revision；事务写决策和恢复任务（P2） |
| `/health/live`、`/health/ready` | 区分进程存活与数据库/迁移/队列就绪 |

### 状态模型

受理 202；业务状态：`queued / running / waiting_user / waiting_review / degraded / succeeded / failed / cancelled`。`degraded` 是终态；`waiting_review` 是非终态。状态端点 200 表示"成功读到资源"，业务失败用 body 内 `status=failed` 表达；非法请求/鉴权失败/不存在仍用 4xx。

### 错误模型（在现有四类 validation/safety/provider/internal 之上增加恢复语义分类）

每个错误附带 `retryable: bool`、`reason_code`；错误区分 `retryable` / `user_action_required` / `effect_unknown`。不返回原始 provider 异常或患者正文。恢复原失败事件用显式 retry 操作，**不再提示"换个幂等键重试"**（避免制造第二个业务事实）。

### 统一结果

```json
{
  "result_version": 1,
  "status": "succeeded",
  "event_id": "…", "run_id": "…",
  "safety_status": "enforced",
  "reason_codes": [],
  "result": {"text": "…", "warnings": [], "conflicts": [], "audit_trail": {}},
  "review_case": null,
  "error": null
}
```

### 权限矩阵（P0 骨架，P2 扩展 reviewer 列）

| 操作 | caregiver | reviewer | support | 运维 |
| --- | --- | --- | --- | --- |
| 提交事件 / 读本患者状态 | ✓ | — | — | — |
| 冲突 resolve/reopen | ✓（记录性） | ✓（P2，需 basis） | — | — |
| 触发 recheck | ✓ | — | ✓ | ✓ |
| 失败任务 retry | — | — | ✓ | ✓ |
| 读 trace/审计 | — | ✓（脱敏摘要） | 故障相关 | ✓ |
| 审核决策 / resume | — | ✓（P2，角色限内） | — | — |

身份从可信 Principal 派生；`source` 保存为自述来源，`claimed_actor` 与 `authenticated_actor` 分开记录。部署 profile 无鉴权配置时拒绝匿名启动；local-demo profile 是显式选择并在启动日志声明。
