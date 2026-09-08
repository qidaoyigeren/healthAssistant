# Harness P2 实施报告

> 2026-09-07 补充：本文保留首次实施记录。浏览器验收补出了快照稳定性、启动恢复、取消终态与 graph 操作明细问题，已修复；OTel 本地导出也已验证。当前结果见 [统一验收报告](../final-acceptance/README.md)。

日期：2026-09-06。作者：编码 Agent（Claude Code / GLM）。范围：Harness P2（无进展检测、受控结果复用、进度事件与可恢复取消），按 P2 prompt 与共同执行约束执行。

## 0. 基线（本轮实测，非引用旧报告）

- P1 工作已在工作区（未提交）。本轮开工时实测：`test_harness_p0` 25/25、
  `test_harness_p1_a` 15/15、`test_harness_p1_b` 16/16、`test_harness_p1_c` 11/11 全部 OK。
- P1 评测基线：`docs/harness-upgrade/P1/eval_report.json`（8/8 场景）。本轮改动后重跑
  P1 评测（`docs/harness-upgrade/P2/p1_eval_rerun.json`）仍 8/8 场景 PASS，全部不变量保持。
- 环境不变：Python 3.13（.venv）、langgraph 1.2.11；无新增依赖。
- 重点核查了 P1 报告"下一阶段接口"：`ToolResult.cacheable`/`ctx.cancel_event`/
  `CallSpanStore.dedup_report`/`EvidenceStore.prune` 均实际可用，P2 直接在其上构建。

## 1. 修改清单与理由

### 一、无进展检测（P2 §一）

| 变更 | 位置 | 理由 |
| --- | --- | --- |
| `read_signature`：工具 + 规范化参数 + scope + **患者 revision** + **语料版本** + 工具契约版本 + 结果摘要 | harness/progress.py | 约束 1 的规范化签名；revision/语料变化自动成为新签名，事实变化后的同参数重查不会被误杀 |
| `NoProgressTracker`（表 `run_progress_state`：last_signature/repeats/stopped_reason） | harness/progress.py | 持久化进展状态：进程重启不重置循环检测；图节点重放（"stopped" 判定）幂等 |
| `_progress_verdict`：成功 domain write 一律 progress 并重置；重复读取只标注 `Observation.no_progress` + 结构化反馈（"已有证据继续有效，第 n/limit 次重复，不因重复提高置信度"）；达到 `AGENT_NO_PROGRESS_LIMIT` 才安全收尾 | agent.py | 约束 2：单次重复合法读取**不是**安全违规；先反馈后停止 |
| legacy 循环与图 `_node_plan`/`_node_execute` 接入停止判定 | agent.py / graph_runner.py | 两个 runner 一致；图的 `execute→plan` 是无条件边，停止状态经 tracker 持久层传递，重放安全 |
| 安全收尾输出未完成项：`_unfinished_items`（guard 的 unmet_requirements + 失败工具），写入 trace 与响应 | agent.py | 约束 4：最终说明未完成项；不因多次相同结果提高置信度 |
| 区分真实推进：写成功/revision 变化 → 新签名；审核等待是停靠不是循环；`refresh` 主动刷新通道；故障恢复（error→ok）产生新签名 | agent.py / tools.py | 约束 3 不误杀 |

### 二、受控结果复用与上下文成本（P2 §二）

| 变更 | 位置 | 理由 |
| --- | --- | --- |
| `ReuseCoordinator` + `ToolCacheStore`（表 `tool_cache`），两级：同 run 内存复用 + 跨 run SQLite 缓存 | harness/reuse.py | 约束 5：优先同 run 复用；跨 run 键 = 全签名（scope+revision+语料+工具版本+参数），患者特定结果**绝无**仅 query 的共享键 |
| executor 接入点：仅 `kind='read' ∧ idempotency='pure' ∧ spec.cacheable`；命中返回带 `cache_hit` 标注的结果；成功才入库 | harness/tools.py / default_tools.py | 约束 6：失败/unknown/空检索**永不**缓存（表 CHECK 只允许 status='ok'）；写入、审核动作、权限检查始终真实执行 |
| `refresh: true` 执行器通道：schema 校验前剥离，强制绕过缓存读取但仍存新结果 | tools.py / reuse.py | 约束 6"主动刷新"；模型无法借此绕过任何权限（纯读） |
| 缓存 ≠ 回执：`operation_receipt` 路径未动；写工具 `receipt_keyed` 语义不变；新真实事件不被缓存/去重吞掉 | tools.py / default_tools.py | 约束 7 |
| 并行只读：本轮**未启用**——注册表中无 ToolSpec 声明 `parallelizable=true`，planner 每周期单动作，无双写者风险；机制位（ToolSpec.parallelizable）沿用 P1 字段 | default_tools.py | 约束 8 保守执行：当前工具面没有可安全并行声明，不引入未经必要性的调度复杂度 |
| 工具目录：保留简单注册表（当前 6 个工具，目录规模无负担）；可见性≠执行时权限（executor 门不变） | — | 约束 9 |
| 开关默认全关：`STAGE0_RUN_REUSE` / `STAGE0_READ_CACHE`；评测双开关两侧安全结论一致（测试） | reuse.py | 约束 10 / 19 |
| 模型/provider 切换：未做任何自动切换；缓存键含工具契约版本（`schema_version`），不向任何未授权服务发送患者内容 | tools.py | 约束 10 |

### 三、进度事件与可恢复取消（P2 §三）

| 变更 | 位置 | 理由 |
| --- | --- | --- |
| `ProgressEventStore`（表 `run_progress_events`）：9 种产品级事件、重放稳定 `event_id`、`seq` 游标、`events_since` 补齐 + **间隙检测**（`snapshot: true`）、保留期 prune | harness/progress.py | 约束 11/12；事件 detail 仅计数/状态码，无正文（测试断言） |
| 事件接线：worker 出队 `accepted`、终态 `completed`/`failed`、停靠 `waiting_review`、取消 `cancel_requested`/`cancelled`；工具级事件经 `after_tool` hook（两个 runner 共用）；`STAGE0_RUN_PROGRESS`（默认开）总开关 | server.py / agent.py / graph_runner.py | 约束 11；observability 失败不影响业务 |
| 游标 API `GET /v1/runs/{run_id}/progress?after=N`（404/快照语义/关闭语义），**轮询而非 EventSource**：原生 EventSource 无法携带 `X-Stage0-Token`，不把长效 token 放 URL（详见 api_contract.md §0） | server.py / docs/P2/api_contract.md | 约束 12/14 |
| 受理即建 run 行（`status='queued'`，`graph_version='accepted'` 占位），runner 首次启动原子升格为真实版本 + `running`，不触碰已取消 run | memory.py / graph_runner.py | 取消与进度必须能寻址排队中的 run；"in-flight run 不被静默改道"语义保留（版本只盖一次章） |
| 幂等取消 `POST /v1/runs/{run_id}/cancel`：CAS 状态机（queued/running/waiting_review → cancelled；终态 → `already_final`）；角色+scope+归属校验；`cancel_requested` ≠ `cancelled` | harness/progress.py / server.py | 约束 15 |
| 取消传播：持久请求行 + 进程内 `threading.Event` 注册表接入 `RunContext.cancel_event`（legacy/graph 每节点重绑）；取消在下一模型/工具调度点生效，规划器已产出但未执行的方案被丢弃 | agent.py / graph_runner.py | 约束 15/18；同步调用物理限制保留（await 取消≠杀线程），迟到结果由门拒绝 |
| 已提交领域效果不回滚：取消收尾文案与结果如实区分"已完成记录保留/未执行检查不再执行"；取消后修正走既有显式领域事件 | server.py / agent.py | 约束 16 |
| 等待审核取消：run → cancelled、未决 case → cancelled（复用 `close_review_case`）、pending resume tasks → cancelled；迟到 reviewer 决定被 case 状态机 409 拒绝；已认领 resume task 执行前复核 run 状态（二道防线）；取消/审核/发布并发由事务内 CAS 决定唯一终态 | progress.py / memory.py / server.py | 约束 17 |
| 客户端关闭仅停止订阅：前端"停止等待"语义不变，取消是显式独立按钮 | frontend submission.tsx | 约束 18 |

### 实施中发现并修复的真实缺陷

1. **无进展判定自我清除**：`_progress_verdict` 在 progress 分支调用 `reset()`，把
   `last_signature` 每步清空，重复永远无法累计。修复：`record()` 已正确推进状态，
   不再额外 reset（这正是"评测先行"抓到的 bug）。
2. **图路由吞掉停止判定**：`execute→plan` 是无条件边，`_node_execute` 设置的
   `route="compose"` 会被忽略，重复读取跑满 16 周期。修复：`_node_plan` 读取
   tracker 的 `stopped_reason`（持久层），停止跨节点生效且重放安全。
3. **`workflow_runs.status` CHECK 不含 `queued`**：受理即建 run 行被静默
   `INSERT OR IGNORE` 吞掉。修复：真实的表重建迁移
   `_ensure_p2harness_schema`（逐列原样搬迁，幂等）。
4. **图 runner 跳过版本盖章**：run 行预创建后 `existing is None` 为假，
   `workflow_run_start` 不再被调用，`graph_version` 停留在占位值（reliability_p1
   测试抓到）。修复：占位行（'accepted'）在 runner 启动时补盖章 + 写 manifest。
5. **取消 API 先于 runner 构造时缺表**：`run_cancel_requests` 等 DDL 由
   agent/worker 惰性创建，`create_app` 现在启动时显式初始化 progress 存储。

## 2. 兼容性

- 既有 API/行为不变：`POST /v1/events` + `Idempotency-Key` 契约原样；确定性 fallback、
  中文原文引用、冲突显式保留、预算语义、单写者约束全部保持（17 套件回归 OK）。
- 增量字段：`GET /v1/events/{key}` 202 响应新增 `event_id`/`run_id`；`Observation`
  新增 `no_progress`（默认 False）；旧 checkpoint/旧行兼容（`_observation_from_dict`
  容缺省）。
- 附加表：`run_progress_events` / `run_cancel_requests` / `run_progress_state` /
  `tool_cache`（IF NOT EXISTS）；唯一非附加迁移是 `workflow_runs` 的 CHECK 约束重建
  （列与数据逐字保留）。
- 前端仅增量：进度步骤文案、取消按钮、"已取消"徽标；未做整站重设计。

## 3. 迁移 / 回滚

- 迁移：旧库首次打开自动执行 `_ensure_p2harness_schema`（幂等：检测到 CHECK 已含
  `'queued'` 即跳过）；在途 run 无状态改写。
- 回滚：删除 harness.progress/reuse 引用与两个新端点即回到 P1 行为；progress 事件
  行为只读、无业务语义。**不可**借回滚重新开启的已修复缺陷：无进展判定的
  self-clear、图路由吞停止判定（它们是本轮修复的正确性行为，默认开关关闭时
  这两条路径本就不触发，回滚到"关闭"即是回滚到正确基线）。
- 影响运行语义的版本变化：run 行新增 `queued` 状态与 `accepted` 占位版本——在途
  run（旧格式）无此形态，不受影响；恢复兼容性仍由 `check_restore_compatibility`
  判定。

## 4. 实际测试命令与结果（2026-09-06 实测）

```
.venv/Scripts/python.exe -m unittest stage0.test_harness_p2      # 16/16 OK
# 全量门禁（17 套件）：
for t in test_memory_p0 test_memory_p1 test_memory_p2 test_reliability_p0 \
         test_reliability_p1 test_reliability_p2 test_stage8_agent test_stage10_server \
         test_frontend_read_models test_harness_p0 test_harness_p1_a test_harness_p1_b \
         test_harness_p1_c test_harness_p2 test_stage3 test_stage5 test_stage6; do \
  .venv/Scripts/python.exe -m unittest stage0.$t; done           # 全部 OK，GATE_FAIL=0
PYTHONPATH=. python stage0/harness_eval.py --out docs/harness-upgrade/P2/p1_eval_rerun.json
                                                                 # 8/8 场景 PASS
PYTHONPATH=. python stage0/harness_p2_eval.py --out docs/harness-upgrade/P2/optimization_report.json
cd frontend && npm run build                                     # tsc -b + vite 通过
PYTHONPATH=. python docs/harness-upgrade/P2/demo_p2.py           # 演示复现通过
```

## 5. 优化对比（同工作负载、假 provider，`optimization_report.json`）

诚实口径：假 provider 单次调用是微秒级，wall_seconds 不作为收益口径；以下仅证明
流程与资源行为，不声称真实模型质量或实际费用变化。

| 指标 | 优化关闭 | 优化开启 | 说明 |
| --- | --- | --- | --- |
| 重复读取场景实际执行读取数 | 16（跑满 max_cycles） | **3**（1 新 + 阈值 2） | `AGENT_NO_PROGRESS_LIMIT=2`；终止原因从预算耗尽变为显式 no_progress 安全收尾 |
| 固定 4 事件工作负载 ddi_check 真实派发 | 3 | **2** | 跨 run 缓存命中重复事件的同参数检测 |
| 跨 run 缓存命中（reuse_stats） | — | 3 hits / 7 misses / 7 stores | 同 run 命中为 0：确定性规划器无 run 内重复读取（诚实记录） |
| 两侧安全结论 | enforced | enforced（完全一致） | 警告数量、引用有效性一致；开关两侧安全不变 |
| tokens_charged | 6391 | 6455 | 持久账本，基本持平（±1% 内为计时/估计噪声） |
| 诚实不完整响应（重复读取场景） | 是 | 是 | 两侧都明确"未保存/未完成"并升级，不以重复提高置信度 |

## 6. 验收对照

| 必须验收项 | 证据 |
| --- | --- |
| 相同状态重复读取被限制；事实变化后同参数重查仍可执行 | `NoProgressTests`（阈值停止 16→3、revision 变化=新签名、单次重复非违规、重启不重置） |
| 缓存隔离、失效、unknown/失败不缓存、主动刷新、版本变化正确；开关两侧安全结论一致 | `ReuseTests`（scope/revision 键隔离、refresh 绕过、失败不入库、写入永不复用、开关两侧一致） |
| 进度断线重连、事件去重、鉴权、终态同步正确；未审查内容不先流出 | `ProgressEventTests`（游标补齐、间隙快照、event_id 幂等、404、正文不泄漏断言）+ `api_contract.md` |
| cancel 在排队/调用中/领域提交后/等待审核/发布竞争/进程重启的结果明确可复现 | `CancelTests`（排队跳过且幂等键收敛、终态 already_final、waiting_review 关 case + 迟到决定 409、运行中取消保留已提交效果、重启后仍取消） |
| 固定工作负载对比调用数/重复率/缓存命中/token/延迟与任务/安全/引用指标 | `harness_p2_eval.py` → `optimization_report.json`（§5 表） |
| 测试受影响前端流程并运行构建/类型检查；不额外重设计 | `frontend/src/api/*` + `features/shared/submission.tsx` 增量修改；`npm run build` 通过 |

## 7. 开关与默认状态（约束 19）

| 能力 | 开关 | 默认 | 结论 |
| --- | --- | --- | --- |
| 进度事件与游标 API | `STAGE0_RUN_PROGRESS` | **开** | 纯附加观测，安全/恢复指标零回归；前端已消费 |
| 取消 API | 常开（非优化，是恢复能力） | 开 | 幂等、CAS、审计齐全 |
| 无进展检测 | `AGENT_NO_PROGRESS_LIMIT` | **关** | 目标指标确有改善（16→3）且安全无回归，但改变终止语义；建议灰度后再默认启用，本轮保留关闭 |
| 同 run 复用 | `STAGE0_RUN_REUSE` | **关** | 确定性规划器下无 run 内重复（无收益证明），保留关闭 |
| 跨 run 缓存 | `STAGE0_READ_CACHE` | **关** | 隔离/失效/安全一致性已验证、命中已量化，但真实语料/LLM 负载下的收益未验证，保留关闭 |

## 8. 未验证范围

- 真实 LLM / 真实检索语料下的缓存收益与延迟收益（无外呼授权；假 provider 只证明流程）。
- SSE/流式传输实现（本轮交付兼容轮询契约；事件模型已按可重放设计，见 api_contract.md §0）。
- 并行只读执行（注册表无 `parallelizable=true` 声明，未启用也未计收益）。
- OpenTelemetry 真实导出（沿袭 P1 未验证范围）。
- 多 worker 进程部署形态下的取消（单写者拓扑不变；取消注册表是进程内的，持久行保证
  跨重启语义，但跨进程 `Event` 传播依赖共享存储的轮询门——当前单进程拓扑无此场景）。

## 9. 下一阶段接口（供后续阶段参考）

- `ReuseCoordinator.stats` 提供命中/存储/刷新计数，可直接接入后续成本观测。
- `ProgressEventStore.emit/events_since` 是通用游标事件层，SSE 端点可直接挂接。
- `run_progress_state.stopped_reason` 可扩展更多"安全收尾"原因码。
- 取消状态机（`request_cancel` CAS + resume 复核）是后续暂停/恢复类能力的模板。
