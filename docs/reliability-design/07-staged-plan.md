# 分阶段实施（P0–P4）、验收与回滚

粗估以"熟悉本仓库的开发者"为单位，需以 P0 实测校正；本文数字是计划，不是已完成事实。

## P0：可靠性与权限入口（4–7 天）

| # | 工作 | 文件 | 验收 |
| --- | --- | --- | --- |
| 1 | 服务端生成 `event_id`/`run_id`；`turn_id=run_id`，消除前 32 位碰撞 | `server.py`、`memory.py`（idempotency_keys 增列） | 两个长 key 前缀相同仍是不同 event；同 key 并发只受理一次 |
| 2 | `handle(client_event_id=...)` 贯通至 `consolidate_interaction`；库中断言 `interactions.event_key == 'api:{key}'` | `agent.py`、`server.py` | 领域层事件身份与请求层一致；历史事件身份不改写 |
| 3 | UI 保存（幂等键、event 身份、status_url）于 session_state；超时/重复操作复用；明确新提交才新建 | `app.py`、`api_client.py` | UI 超时重试不新建事件 |
| 4 | committed POST/GET/client 返回形状统一；v1 202/422 语义保留 | `server.py`、`api_client.py`、`app.py` | 成功重放返回与 GET 一致的完整结果；失败原事件走显式恢复操作，不提示换 key |
| 5 | `operation_receipts` 表 + 三个写入口（consolidation、显式药物变更、警告/结论）接入短事务② | `memory.py` | 同 operation id 不同输入拒绝；真实新事件/新版本检测仍产新记录；"领域写成功后 complete 前崩溃"恢复无重复效果 |
| 6 | claim/heartbeat/complete/fail/领域写全部校验 lease_token + 状态 + 有效期；heartbeat 续租 | `memory.py`、`server.py` | 租约过期重领后旧 worker 的 complete/fail/heartbeat/write 全被拒 |
| 7 | 事务④：任务 done + 幂等键 committed + 结果引用单事务；reconciler 入口 | `memory.py`、`server.py` | 事务间崩溃后状态收敛 |
| 8 | 错误分类 `retryable/permanent/safety/effect_unknown`；`next_attempt_at` + 退避 + jitter；deadline；失败队列 | `memory.py`、`server.py` | 永久错误不自动重试；429/网络按策略退避；次数与时间预算有上限 |
| 9 | `Principal` 依赖、对象级授权、角色边界；actor 从身份派生；local-demo profile 显式；部署 profile 无鉴权拒启 | `server.py`、`app.py` | 未授权读取/修改/恢复被拦截；匿名部署 profile 拒绝启动 |
| 10 | 脱敏结构化日志 + trace_id 贯穿；后台循环异常不再吞；交付正文=检查正文（复核） | `server.py`、`agent.py`、`api_client.py` | 日志无密钥/患者正文；队列停摆有日志可查 |

**回滚**：全部增量迁移（ADD COLUMN / IF NOT EXISTS），新表可保留不使用；行为开关 `STAGE0_RELIABILITY_V2=0` 回到 v1 路径（保留一个发布周期后移除）。不设 `STAGE0_API_URL` 时直连路径行为不变。

**边界声明**：P0 不声称已集成人工审核服务（接口可测试但不接真实人员）；不依赖框架名或单个幂等键宣称全链路 exactly-once——承诺范围是"至少一次执行 + 业务效果回执去重"。

## P1：LangGraph 最小迁移（4–7 天）

- `AgentRunner`/`LegacyAgentRunner`/`LangGraphAgentRunner`；按 [02-architecture.md](02-architecture.md) 节点映射拆 StateGraph；保留 LLM 决策语义与 guard。
- 持久化 saver（本地 SqliteSaver 起步）、event/run/thread 映射、状态/图/政策版本、累计预算持久化、恢复算法；checkpoint 只放安全可序列化状态/引用。
- 通过 operation receipt 处理"领域 commit 成功但 checkpoint 未完成"窗口；interrupt 独立等待不占 Worker/锁。
- 版本以官方资料 + 依赖解析 + 本仓最小冒烟锁定；避免同步节点 timeout 误用与 SDK 重试叠加。
- 新 run feature flag 与旧在途 run 版本路由；legacy/graph 对比只用隔离数据库，禁止双写真实领域状态。

**验收**：原回归行为对齐；进程重启可恢复；预算重启不归零。**回滚**：flag 关闭，在途 graph run 按其保存版本继续或明确迁移，不静默新建重复 run。

## P2：人工处理闭环（5–8 天）

- review_cases/review_decisions/resume_task、模拟 reviewer 页面、用户等待状态、咨询摘要导出。
- 受控分流五路由；普通客服无权作临床判断。
- 幂等建单/审核、CAS 接单、对象授权、interrupt/revision 匹配、恢复时重新验权与事实时效核验（`review_stale`）。
- 未接真实人工、逾期、无人接单、补充信息、撤销、重复回调均有明确行为；超时不默认通过。
- 恢复仍过硬规则与引用校验；人工不能提交任意 goto/SQL/工具名/state patch。

**验收**：建单→重启→接单→等待期间版本变化→恢复 的完整演示；审核提交后崩溃只应用一次。

## P3：观测与评测（3–5 天）

- 全链路 trace（`trace_id → request → event → run → node → tool → review`）、脱敏、队列龄/预算/重试放大指标；外部 tracing 默认关闭。
- 故障演练按 [08-test-matrix.md](08-test-matrix.md) 固化为可重复脚本。
- DDI/RAG 隔离评测（含 Stage 9 遗留的 held-out v2 重跑：需 KEGG 网络 + provider key + 清 RAG fallback 缓存）。

## P4：部署扩展（条件触发，单独估算）

- PostgreSQL + SQLAlchemy/Alembic 迁移、独立 Worker 进程、多身份/多患者隔离、备份恢复与运维手册。
- **多进程并发必须用真实 PostgreSQL 验证，SQLite 通过不算替代。**

## 依赖顺序与一个月裁剪

P0 → P1 → P2；P3 埋点随各阶段做、最后汇总。资源有限时优先：前缀碰撞/重试安全、图恢复、一条真实的本地模拟人工闭环。推迟：多 Agent、Kafka、全量 ORM 改写、向量库替换、Temporal。

## 性能与质量口径

- 所有性能目标标为**待测**；旧报告数字（117/117、28/28、held-out 表）是历史工件，与本轮实测分开报告。
- 两条轴分开衡量：DDI/RAG 证据质量（held-out recall、中文引用覆盖、拒答率）与运行可靠性（失败率、重试放大、重复效果数、恢复成功率）。**不宣称 LangGraph 提高医学准确率。**
