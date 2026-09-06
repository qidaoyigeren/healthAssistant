# Reliability P1 实施报告（LangGraph 最小迁移）

实施日期：2026-09-06。范围：**仅 P1**（在 P0 基础上），依据 `docs/reliability-design/02-architecture.md`（节点映射）与 `07-staged-plan.md`（P1 验收）。不进入 P2；未部署；未触碰真实 `memory.db` 与既有评测工件；对比/影子全部使用隔离临时数据库。

## 一、修改文件与原因

| 文件 | 变更 | 对应 P1 项 |
| --- | --- | --- |
| [stage0/graph_runner.py](../../stage0/graph_runner.py)（新） | `AgentRunner` 协议；`LegacyAgentRunner`（原 `handle()` 直通 + workflow_runs 记账）；`LangGraphAgentRunner`（StateGraph：`load_context → plan → execute → compose → publish`，条件路由：拒绝→重规划循环、完成→compose、有动作→execute）；`RunnerRouter` 版本路由；`make_runner` 工厂 | Runner 抽象、按职责拆图 |
| [stage0/memory.py](../../stage0/memory.py) | 新表 `workflow_runs`（run_id 主键、event/thread 映射、graph/state 版本快照、status、budget_json、result_json）+ `workflow_run_start/get/update`（预算**合并**语义） | run 持久化、预算恢复 |
| [stage0/server.py](../../stage0/server.py) | `OutboxWorker` 改用 routed runner（`run()` 接口）；`create_app(runner_factory=, checkpoint_path=)`；`/v1/health` 增 `graph_runner_enabled`；worker stop 释放 checkpointer 连接 | flag 接入服务层 |
| [stage0/test_reliability_p1.py](../../stage0/test_reliability_p1.py)（新） | 9 个 P1 回归测试（见下） | 验收 |
| [stage0/requirements-graph.txt](../../stage0/requirements-graph.txt)（新） | 版本冻结（`pip freeze` 实测，非猜测） | 版本锁定 |
| [stage0/test_memory_p1.py](../../stage0/test_memory_p1.py)、[stage0/eval_memory.py](../../stage0/eval_memory.py) | 修复两处**日期定时炸弹**（与本轮功能无关，见"计划外修复"） | 回归门禁恢复 |

## 二、设计要点与实现证据

### 2.1 节点级拆分（不是把 handle 包进一个大节点）

| 图节点 | 承接的原循环逻辑 | 恢复语义 |
| --- | --- | --- |
| `load_context` | `expire_working` + 初始状态 | 只读 |
| `plan` | 预算门（cycle>0 + 15s 预留）→ cycle++ → 熔断/decide/PlanningRejected 处理 → 路由 | 每 plan 步 checkpoint；拒绝循环回 `plan`，与 legacy `continue` 逐语义对齐（每次拒绝同样消耗一个 cycle） |
| `execute` | `_act` + observe trace + `_reflect` + `_flush_traces` | 领域写在 P0 operation receipts 下——重放命中回执返回原结果 |
| `compose` | `_respond` + `_finalize`（含预算降级通知前置、最终安全检查） | 纯函数 |
| `publish` | workflow_runs 终态 + 结果引用 | 幂等 |

LLM 决策语义、guard、熔断（≥2 次连续拒绝切确定性规划）、确定性 fallback 全部是 agent 自己的组件；图只管路由与恢复，不承载医学政策。

### 2.2 双写窗口（官方文档核验过的边界）

checkpoint（SqliteSaver，独立文件 `memory.db.checkpoints`，`setup()` 自管 schema）与领域库**不是同一事务**。窗口关闭方式：恢复路径先尝试 `graph.invoke(None, config)` 从最后完成步续跑；框架无法续跑时兜底整体重跑——两种路径都靠 P0 回执保证无重复副作用（测试 `test_crash_after_domain_write_before_checkpoint` 实证：崩溃在领域写之后、checkpoint 之前，恢复后药物版本与 consolidate 回执各恰一条）。

### 2.3 预算持久化（重启不归零）

`workflow_runs.budget_json` 记录 `consumed_seconds`（逐节点累计活动时间）与 `tokens_estimated`（chars/1.5 口径，沿用 Stage 8 校准）；resume 时合并式加载进初始状态（`workflow_run_update` 的 merge 语义 + `build_workflow_state(budget=...)`）。

### 2.4 版本路由

`RunnerRouter.run`：已有 workflow_runs 行 → 按其 `graph_version` 路由（legacy run 永远 legacy、graph run 永远当前 graph 版本）；新 run → `AGENT_GRAPH_RUNNER` flag（**默认关闭**，直连与 API 默认路径行为不变）。测试实证：flag 开启时在途 legacy run 不改道、新 run 走图。

### 2.5 重试所有权（ADR-004）

图内**不设** RetryPolicy；任务级恢复归 P0 outbox 租约循环（分类退避）；planner SDK `max_retries=0`（既有）；无任何叠加层。

### 2.6 interrupt 接线（P2 预留）

`LangGraphAgentRunner.resume(run_id, value)` 封装 `Command(resume=...)`；P1 无节点抛 `interrupt()`。框架级冒烟（独立小图 + SqliteSaver）实证：interrupt 表面化、resume 值成为 `interrupt()` 返回值、**包含节点从头重跑**（计数 1→2）——这正是副作用必须拆独立节点的原因，已写入 P2 设计依据。

## 三、实际测试结果（2026-09-06，Windows 11，Python 3.13.14）

| 命令 | 结果 |
| --- | --- |
| `python -m pip install langgraph langgraph-checkpoint-sqlite` | langgraph **1.2.11** / langgraph-checkpoint-sqlite **3.1.1**（langchain-core 1.6.2 等，见 requirements-graph.txt） |
| `python -m unittest stage0.test_reliability_p1` | **9/9 OK** |
| `python -m unittest <10 套件全量>`（stage3/5/6、memory p0–p2、stage8、stage10、p0、p1） | **140/140 OK（16.6s）** |
| `python -m stage0.eval_memory --ablate` | **passed 28/28**；四机制全部 load-bearing |

冒烟（官方资料要求的三项）均由测试承载：持久化（SqliteSaver setup + checkpoint 落盘）、重启恢复（`invoke(None)` 续跑 + 兜底重跑）、interrupt 接线。

### P1 验收对照

| 验收项（Prompt B P1） | 证据 |
| --- | --- |
| legacy/graph 行为对齐 | `test_legacy_and_graph_produce_identical_outcomes`：两个隔离库输出（文本/警告/投影/审计键）逐字段相等 |
| 进程（节点间）崩溃可恢复 | `test_crash_between_nodes_resumes_from_checkpoint`、`test_crash_after_domain_write_before_checkpoint` |
| 领域 commit 成功但 checkpoint 未完成的窗口 | 上二者 + 回执计数断言（无重复效果） |
| 预算重启不归零 | `test_restart_does_not_reset_budget` |
| checkpoint 只放安全可序列化状态 | `test_checkpoint_state_is_json_safe`（逐 channel `json.dumps`，含 metadata） |
| flag 路由 + 在途 run 版本保持 | `test_inflight_legacy_run_keeps_legacy_version`、`test_flag_off_routes_new_runs_to_legacy`、`test_graph_flag_routes_service_events`（服务层 202→committed 全链路） |
| interrupt 不占 Worker/锁（接线就绪） | 冒烟测试；P1 生产图无 interrupt 点（诚实边界） |

## 四、计划外修复：两处日期定时炸弹（与本轮功能无关）

日期翻到 2026-09-06 后，`test_memory_p1.test_out_of_order_stop_keeps_valid_interval_semantics` 与 `eval_memory.S12` 突然失败：二者把 `known_at` 硬编码为写测试当日（09-05）的"次日零点"（`2026-09-06T00:00:00Z`），而记录的 `created_at` 是运行时刻——运行日一旦 ≥ 该零点，记录被 known-at 截止隐藏。修复方式与其兄弟场景（`eval_memory.py:202` 既有做法）一致：显式回填 `created_at` 到查询窗口内。修复后 140/140 + 28/28 恢复。此修复不改变被测语义，只消除日历敏感性。

## 五、迁移与回滚

- **依赖**：`pip install -r stage0/requirements-graph.txt`（新依赖仅 graph 路径 import，flag 关闭时不加载 langgraph——`make_runner` 只在 flag 开启时构造 `LangGraphAgentRunner`）。
- **schema**：`workflow_runs` 纯增量表；checkpointer 自管其表（独立文件）。
- **回滚**：`AGENT_GRAPH_RUNNER` 未设/设 0 即回到 P0 行为；`graph_runner.py` 可整体删除；workflow_runs/checkpoint 文件可保留不动。

## 六、未解决问题与 P2 接口

1. **`invoke(None)` 恢复语义依赖框架版本行为**：当前 1.2.11 下按"从最后完成步续跑"工作（测试锁定）；升级 LangGraph 时需重跑 `test_reliability_p1.CrashRecoveryTests`。
2. **checkpointer 连接生命周期**：worker `stop()` 释放；uvicorn 热重载场景未测。
3. **shadow 对比是离线 fixture 级**：LLM 模式下 legacy/graph 的 planner 调用次数一致性未测（无 provider 窗口）；flag 开启 + LLM planner 的组合留待有 provider 时验证。
4. **P2 接口已就位**：`resume()`（Command(resume) 接线）、`workflow_runs`（review_case 关联 run）、`deployment` auth 角色（reviewer 扩展位）、interrupt 副作用拆节点的设计依据（冒烟实证的"节点重跑"语义）。
5. held-out DDI v2 重跑仍未执行（Stage 9 遗留）；性能目标全部待测。
