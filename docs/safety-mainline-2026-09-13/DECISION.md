# 长期用药安全主线重构：决策记录（2026-09-13）

本轮把产品重新确立为**长期用药安全跟进**，并把已经长成产品中心的材料核对降回支持能力。
本文件只记录决策与边界；实施结果、验收数字与遗留问题在 `RESULT.md`。

---

## 1. 主线：一次变化要走完的八步

```
用药或背景变化
 → ① 接受并持久化有依据的事件          （已有：episodic_memory / medications / semantic_memory，版本化）
 → ② 程序执行必要安全检查              （本轮改：不再依赖模型是否选中工具）
 → ③ 建立或更新持久化安全事项 SafetyCase（本轮新增）
 → ④ Agent 调查与补问                   （复用：run_open_review + investigation@1）
 → ⑤ 用户或专业人员提供补充             （已有：care_task.input_requests + record_input）
 → ⑥ 恢复同一事项并重新核对             （已有：care_task 持久 investigation + input_versions）
 → ⑦ 更新有依据的处置状态               （本轮新增：disposition 契约）
 → ⑧ 后续变化时重新打开受影响事项        （已有：conclusion_dependencies + dependency_tasks 失效重查）
```

## 2. 核心能力（保留，不动其真相源）

| 能力 | 现有实现 | 处置 |
|---|---|---|
| 用药与事实的版本化记录 | `medications`(predecessor_id 版本链)、`semantic_memory`(version+status)、`episodic_memory` | 保留原样 |
| 当前状态与历史时间线 | `current_medications()`、`timeline()`、`query_state()`、`read_models.history_events` | 保留 |
| 必要安全检查 | `ddi_engine.detect()`（确定性编排）＋ `_recheck_condition`（标签×事实确定性推导） | **移出 agent，成为独立代码路径** |
| 风险结论、依据与依赖 | `conclusions` + `conclusion_dependencies` + `source_refs` | 保留原样 |
| 结论失效与持久化重查 | `_invalidate_conclusions_tx` → `dependency_tasks` → `recheck_pending` | 保留，新增必要检查入口 |
| 冲突、补问、任务恢复 | `conflicts`/`conflict_actions`、`care_task` 状态机、`review_cases` | 保留 |
| 权限、来源完整性、幂等、预算 | `PERMISSION_ROLES`、`EvidenceStore`、`idempotency_keys`+`operation_receipts`、`TurnBudget` | 保留 |
| 取消、异常交付、请求审计 | `run_cancel_requests`、`run_progress_events`、`audit_log`、`workflow_runs` | 保留 |

## 3. 支持能力（保留，但不再是产品中心）

- 材料导入与确定性核对（`product.py` 的 `case`/`item`/`recompute`/`validate`）；
- 材料来源与字段定位（`candidate.locations`、`MaterialIndex`）；
- 参考资料检索与授权原文读取（`rag.py`、`harness/evidence.py` 的 `read_evidence`）；
- 调查对象、补问、证据关联（`investigation.py`、`review/state.py` 的 question/input_request）；
- 报告版本与增量更新（`review/report.py`、`review/incremental.py`）；
- 可复用的工具对话历史与传输适配（`tool_history.py`、`api_client.py`）。

处置：**不删除**，但材料页面从主入口降为支持入口；材料核对不再是进入核心流程的前置。

## 4. 需要合并的重复实现

| 重复 | 决定 |
|---|---|
| `agent._recheck_ddi` / `_recheck_condition` 与「必要安全检查」 | 提取为 `stage0/safety_checks.py` 的模块级函数；agent 方法改为委托，两条路径只有一份实现 |
| 每轮一套的浏览器验收/回放/打分脚本（见 §5） | 只保留默认验证入口 `scripts/verify-agent-closeout.py` 真正调用的；其余删除 |
| 多套「材料专属推进逻辑」 | 保留 `review/advance.ReviewRunner.advance` 这一个核心，删除只服务历史轮次的编排脚本 |
| `eval_memory.py` 与 `test_memory_p0/p1/p2` 的场景重叠 | 场景不变量归入 unittest 套件；`eval_memory` 的独立 CLI 入口删除 |

## 5. 确认废弃的开发资产（已查调用方/路由/导入/配置/持久状态）

判定依据：`rg` 全仓调用方（排除 `node_modules/`、`output/`、`docs/` 与自身），以及是否属于默认验证入口。
仅被历史文档引用的按「文档引用」计，不作为保留理由。

**A. 一次性的取证/回放/评分/验收脚本（无任何非文档调用方）**

`scripts/` 下：`a5-demo-api.py`、`agent-capability-ablation.py`、`analyze-model-tool-controlled.py`、
`analyze-retrieval-feedback.py`、`analyze-tool-history.py`、`create-product-dev-tasks.py`、
`create-product-ocr-samples.py`、`diag-final-block.py`、`evidence-loop-browser-acceptance.js`、
`latency-baseline.py`、`latency-diagnostic.py`、`material-review-browser-acceptance.js`、
`material-review-conditions-browser.js`、`material-review-conditions-live.py`、
`material-review-coverage-live.py`、`material-review-demo.py`、`material-review-live-acceptance.py`、
`model-qualification-probe.py`、`model-tool-controlled.py`、`model-tool-product-acceptance.py`、
`planner-wire-probe.py`、`product-live-api-acceptance.py`、`product-live-browser-acceptance.js`、
`product-live-cold-detector.py`、`product-live-extractor-diagnostic.py`、
`product-live-question-browser.js`、`product-live-question-final-browser.js`、
`retrieval-feedback-controlled.py`、`retrieval-feedback-forensics.py`、`retrieval-feedback-product.py`、
`retrieval-feedback-replay.py`、`retrieval-feedback-ui-fixture.py`、`run-a5-live.py`、
`run-live-layer-c.sh`、`run-material-review-browser.js`、`run-planner-live-acceptance-v3.py`、
`run-planner-live-acceptance.py`、`tool-history-capture.py`、`tool-history-controlled.py`。

**B. 对应的夹具/编排入口（只服务上述脚本）**

`stage0/agent_closeout_fixture.py`、`stage0/material_review_browser_fixture.py`、
`stage0/product_p1_browser_fixture.py`、`stage0/product_live_acceptance.py`、`stage0/run_heldout_v2.py`、
`stage0/run_product_acceptance.py`、`stage0/run_harness_acceptance.py`、`stage0/harness_browser_fixture.py`。

> 例外：`stage0/harness_browser_fixture.py` 与 `product_p1_browser_fixture.py` 由 `scripts/harness-browser-acceptance.js`
> 等启动；这些脚本一并删除，故夹具同时删除。

**C. 已结束的实验开关与分支（P3 委派 / 批量读取）**

`stage0/harness/delegation.py`（`STAGE0_DELEGATED_WORKERS`，默认关）、
`stage0/harness_p3_eval.py`、`stage0/test_harness_p3.py`、
`harness/default_tools.py` 中的 `BATCH_READ_SPEC` / `DELEGATE_TASK_SPEC` 与对应注册分支、
`agent.py` 中的注册与 `plan` 权限随之收敛。

**D. 无效或重复的测试**

> **重要更正。** 起初怀疑"测试很多但很弱"，逐个体检后的结论相反：**这套测试是强的**。
> 48 个文件、约 707 个用例，全部离线确定性，且以负对照驱动（`test_visitprep_scoring.py`
> 的文档字符串就是"一个从不失败的检查不是证据"）。各套件之间**刻意不重复**锁同一边界
> （`test_material_review_coverage.py` 开头明写这一点）。
> 因此**没有删除任何测试套件**，只改写少数弱断言：

- `test_multi_agent_review.py`：`assertGreater(calls, 0)`（"至少发生一次模型调用"）→
  改写为该文件姊妹用例已经锁定的确切断言；
- `test_harness_p2.py`：`assertGreaterEqual(len(feedback), 1)` → 改写为真正检查注释所
  声称的结构/证据命名；
- `test_stage3.py` 里靠下标算出的"`ddi_check` 在 `record_ddi_warnings` 之前"是**真实的安全
  顺序不变量**（先有证据再落盘），保留其语义。

**保留**：为旧实验手写固定 `run_id` / `task_id` 的写法到处可见，但它们是**夹具身份**而非
预言值，且总是与真实行为断言配对——不做删除。

**E. 文档**

根目录相互矛盾的路线图与提示词文档（`生产化与亮点升级设计*.md`、`记忆模块升级设计*.md`、
`框架集成与生产可靠性升级方案*.md`、`Agent Harness分级改造Prompt.md`、`真实产品前端生成Prompt.md`）
与死脚手架 `main.py` 删除；`README.md` 重写为**唯一**当前有效的产品定位与架构说明入口；
`docs/agent-capability-upgrade/ENTRY-POINTS.md` 从"22 个脚本"的陈旧清单重写为当前入口。
`RESUME.md` 属于个人叙述、不是开发资产，不动。
`CONTEXT.md`、`DEMO.md` 为当前文档，保留。

**F. 明确保留为非默认工具的历史实验**

`stage0/eval_memory.py`（28 个记忆场景 + `--ablate`）。它**仍然服务当前设计决策**：
`selective_invalidation` 的默认值与其口径由它的消融结果支撑。它不在默认验证入口里，
也不影响产品路径。

**不删除**：患者数据库 `stage0/memory.db*`、`stage0/data/` 下的语料与结构化产物、用户上传材料
（`product_objects` 的 `document`）、有效证据（`evidence_records`）、用户报告与摘要、`.env` 凭据、
用户任务行（`care_task` / `case`）。删除实验脚本不清除任何用户任务。

## 6. 用户状态与旧任务兼容

- **旧任务数据一律保留**：`product_objects` 的 `care_task` / `case` / `summary` /
  `investigation_report` / `material_review_report` 行不作迁移、不作删除。
- **旧 goal_type 保留最小适配器**：`reconcile_material@2`、`current_medications@1`、`visit_summary@1`
  继续按其契约版本执行；新代码不写入它们，但读得到、恢复得了。
- **契约版本不冒充**：`care_task.contract_version` 与 `material_review` 的
  `review_contract` 各自独立校验；新契约（`safety_case@1`）使用自己的版本号，恢复时校验不一致即拒绝继续。
- **用户「已读」不是状态**：`user_seen_at` 只是一个时间戳，任何生命周期迁移都不读它。

## 7. 安全事项生命周期（SafetyCase）

### 7.1 与既有概念的关系（不建平行真相源）

```
conclusion   ── 安全检查的版本化产物（权威，已有）
evidence     ── 结论与调查的依据（权威，已有：EvidenceStore + source_refs）
SafetyCase   ── 围绕同一安全问题持续跟进的业务事项（本轮新增，**只引用，不复制**）
task/run     ── 某次实际调查执行（已有：care_task + workflow_runs）
input_request── 等待补充的信息（已有：review state + care_task.missing_inputs）
review/disposition ── 有依据的复核与处置记录（已有 review_decisions；本轮新增 case 上的 disposition）
```

SafetyCase **不保存**任何患者事实、用药值或风险等级副本。它只保存引用
（`linked_conclusion_refs` / `evidence_refs` / `relevant_fact_refs`）与过程
（`open_questions` / `required_inputs` / `linked_run_ids` / `history` / `resolution_basis`）。

### 7.2 字段

`case_id, patient_scope, case_type, dedup_key, related_medication_refs, trigger_event_refs,
linked_conclusion_refs, relevant_fact_refs, evidence_refs, current_status, input_versions,
open_questions, required_inputs, next_action_summary, responsible_party, linked_run_ids,
resolution_basis, disposition, user_seen_at, history, created_at, updated_at`

### 7.3 状态（与运行状态分开）

| 状态 | 含义 |
|---|---|
| `open` | 待调查：事项已建立，尚无调查运行 |
| `investigating` | 调查中：有活跃 run |
| `awaiting_user` | 等待用户补充（`required_inputs` 非空且用户可答） |
| `awaiting_professional` | 等待专业复核（需人工意见，界面明确标注未连接真实医生服务） |
| `resolved` | 已完成有依据的处置 |
| `needs_recheck` | 因新信息需要重新复核 |
| `execution_failed` | 当前执行失败，但事项**仍未解决**（绝不等于已处理） |

运行状态（`care_task.status` / `workflow_runs.status`）不写入 `current_status`；一次运行失败只把
事项推进到 `execution_failed`，不关闭它。

### 7.4 身份与去重

`dedup_key = sha256(scope, case_type, subject_keys, episode_anchor)`：

- `case_type`：`interaction_risk` / `condition_risk` / `evidence_gap` / `discrepancy` / `source_invalidated`；
- `subject_keys`：相互作用按两条**归一化成分键**排序；个体风险按（药，事实键）；其余按事项对象 ref；
- `episode_anchor`：**用药分期**。对每条相关用药沿 `predecessor_id` 回溯到最近一次
  `status='stopped'` 边界之后的第一个版本，取其 `id` 作为「本次生效链」标识。

由此：剂量变更（`superseded`）→ 同一分期 → **更新既有事项**，不产生新卡片；
停药后重新启用 → 新分期 → 新事项；同名药的不同原因事项不合并。

### 7.5 关闭与降级（代码强制，记录依据/操作者/版本）

允许关闭为 `resolved` 的**唯一**依据集合：

1. `deterministic_check_completed`：该事项 subject 上的必要检查已完成，且所引结论为 `current`，
   且结论记录的 `input_revision` 等于当前 scope 版本；
2. `professional_review_applied`：引用一条 `review_decisions` 记录，其 `outcome='applied'`
   （`review_stale` 一律拒绝）。

**明确禁止**（代码拒绝，返回 409 与原因）：

- 用户点「已读」→ 关闭（`mark_seen` 只写 `user_seen_at`）；
- 模型说「应该没问题」→ 关闭（模型输出不是 `resolution_basis` 的合法来源）；
- provider 失败后把事项当作已处理（只能进 `execution_failed`）；
- 用旧版本上的复核结果批准新状态（`input_revision` 不匹配即拒绝）；
- 删除未解决问题使事项更容易完成（没有任何删除路径）。

用户转述医生意见：只写 `resolution_basis.kind='user_reported'`，事项进入 `awaiting_professional`，
**不**自动标记为已验证的医生记录。

## 8. 必要安全检查先执行

- 触发：用药新增/停用/剂量变更；相关患者背景事实变化；结论依赖的来源或事实失效；
  新材料经确认后改变当前记录；未决事项收到新的补充信息。
- 流程（代码，非模型）：受理并持久化事件（事务内）→ 同一事务内登记必要检查
  （`necessary_checks`，按 `(trigger_kind, trigger_ref)` 去重）→ worker 领取带租约执行
  → 运行确定性检测器 → 记录结论与依赖 → 建立/更新 SafetyCase → 交给 Agent 做进一步判断。
- Agent 不可用时：必要检查、结论与已知提示**照常产生**；Agent 调查不启动，事项停在
  `open`/`awaiting_user`，界面明确说明「本次未经模型调查」。
- 严重程度与处置规则来自 `ddi_engine` 的既有确定性口径；模型不能编造或降低既有警告等级。

## 9. 跟进由持久任务驱动

复用既有 `outbox_tasks`（事件执行）、`dependency_tasks`（失效重查）、`care_task.due_at`（预记复核时间）、
`resume_tasks`（复核恢复）。**不新增调度框架**。本轮只做应用内待办与进展展示，不自动发送邮件或消息。
明确部署要求：**后台 worker 未运行时**，`necessary_checks`、`dependency_tasks`、
`outbox_tasks` 都不会自动推进——只有用户主动操作时才同步执行。进程内演示不构成离线持续服务能力。
