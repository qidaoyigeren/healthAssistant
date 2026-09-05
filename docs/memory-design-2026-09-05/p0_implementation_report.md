# P0+P1+P2 实施报告：受控写入、双时间、依赖重查、上下文与评测

实施日期：2026-09-05。依据：`记忆模块升级设计.md` §5、§9.1，以及「设计完成后的实现指令」模板（P0 部分先行实施，同日完成 P1/P2 主体）。

## 1. 范围与边界

- **只实施 P0**。未做双时间查询、依赖失效/重查、ContextBuilder、摘要或检索（P1/P2）。
- 全部测试与探针使用临时合成数据库；**未打开、未修改** `stage0/memory.db`。
- 保留工作区既有改动（Stage 4/5 的 agent.py / app.py / REPORT.md 等未回退）。
- 默认离线路径与显式 LLM 开关（`MEMORY_ENABLE_LLM` / `--llm`）保留；PlannerPolicyGuard / SafetyBoundary 未改动。

## 2. 变更清单（对照设计验收项）

均位于 `stage0/memory.py`：

| 验收项 | 实现 | 位置（约） |
| --- | --- | --- |
| 否定/假设/第三人解析，unresolved 保留 | `_deterministic_extract` 重写：`_mention_context` 判定 negated/hypothetical/other_subject/uncertain；命中者不写患者语义事实，经 `_deferred_report` 保留为 `caregiver_message` 情景记录（`needs_verification=1`）；过敏原贪婪捕获剥离前导说话人/不确定词后再定位 | memory.py `_mention_context` / `_deferred_report` / `_deterministic_extract` |
| 存储层策略强制（绕过工具直写同样生效） | `_write_semantic_fact_tx`：安全关键 namespace 请求 `conflict_policy='update'` 且已有不同值记录时，强制转 `conflict`，审计记录 `policy_forced_to_conflict`；旧记录进入 `disputed` 而非被静默 supersede | `_write_semantic_fact_tx` |
| 事件幂等（稳定 event key + payload 一致性） | `consolidate_interaction` 新增 `client_event_id`（默认 `session:turn`）；`(event_key)` 唯一索引；同 key 同 payload → 重放已提交结果（`replayed=True`）；同 key 不同 payload / 同 turn 被他人 event 占用 → `IdempotencyKeyReused`；v3 旧行按 `session:turn` 收养 | T0 段 |
| T0/T1 事务边界 | T0 独立小事务先存原话（`process_status='pending'`），抽取在写锁外；T1 单事务提交全部语义/情景/工作事实、冲突、审计与 committed 回执；异常整体回滚后用独立事务 CAS 标记 `failed`，原文可重试 | `consolidate_interaction` |
| 消除嵌套提交 | `write_semantic_fact` / `record_event` / `write_working` / `create_conflict` / `apply_medication_change` 拆为「公共入口持有唯一事务 + `_tx` 内部函数不提交」；此前 consolidate 每条事实各自提交、嵌套 `record_event` 中途 commit 的半提交路径已消除 | 各 `_tx` 方法 |
| 严格 ref | 新增 `resolve_ref`：校验格式、存在性与**精确版本**；`@v999`、不存在 id、非法格式均报错。`audit_for` 改为经由它 | `resolve_ref` / `audit_for` |
| 基础候选/来源/核实状态 | 附加列迁移 `_ensure_p0_columns`（additive ALTER，不动旧数据）：`interactions(event_key,payload_hash,process_status,result_json)`、`semantic_memory(extraction_mode,verification_status)`、`episodic_memory(needs_verification)`；迁移后 `schema_version='3-p0'` | 模块级 `P0_COLUMNS` / `_ensure_p0_columns` |
| 裸 MemoryStore 默认离线 | `StructuredFactExtractor` 默认值由「环境变量缺省即启用」改为「未显式设置 `MEMORY_ENABLE_LLM` 即离线」；显式参数优先于环境变量 | `StructuredFactExtractor.__init__` |
| 药名未匹配显式返回 | 既有 `unresolved` 路径保留并纳入同一事务 | `apply_medication_change` |

## 3. 测试证据

新增 `stage0/test_memory_p0.py`（16 项，全部临时库）：

- 离线语义 5 项：「妈妈没有糖尿病」「邻居有糖尿病」「如果妈妈有糖尿病」「我记得她可能对青霉素过敏」均不产生患者确诊事实，分别保留 negated / other_subject / hypothetical / uncertain 待核实记录；「妈妈有糖尿病」正常记录。
- 策略守卫 2 项：直连存储 API 对关键事实用 `conflict_policy='update'` → 强制 conflict、旧记录 disputed、冲突建档；低风险 namespace 仍可 update。
- 严格引用 2 项：`@v999` 报 version mismatch；不存在 id / 非法格式报错；结论 ref 可解析。
- 幂等 3 项：同 event key 重放返回相同 refs 不重复写；同 key 异 payload 拒绝；同 turn 被他人 event 占用拒绝。
- 事务原子性 1 项：注入「第二条事实写失败」→ 两条事实均不生效、事件标记 failed、原文保留；去掉故障重试后整体提交。
- 默认离线 2 项 + 迁移 1 项（列追加幂等、旧数据保留、`schema_version='3-p0'`）。

回归：`python -m unittest stage0.test_memory_p0 stage0.test_stage3 stage0.test_stage5` → **37 项全部 OK**（2026-09-05，约 0.47s）。设计场景探针（4 条否定/主体/假设/不确定输入，临时库）输出与设计 §二 期望一致。

## 3b. P1 实施（同日追加）

新增/修改：`memory.py`（P1 schema + 新接口）、`memory_context.py`（新）、`memory_search.py`（新）、`agent.py`（接入）、`app.py`（可信度面板）。

| 主线 | 实现 | 位置 |
| --- | --- | --- |
| **双时间查询** | `query_state(valid_at, known_at)`：行级条件 `created_at<=known_at AND valid_from<=valid_at AND (valid_to IS NULL OR valid_to>valid_at)`；后录入的回溯更正不会泄漏进更早的 known_at 视图；未知 valid_from 进入 `uncertainties` 不冒充确定成员；同时返回药单、未决冲突与 scope revision。**已声明的近似**：conflict 的 status 是当前状态，历史视图中双方并列，不精确重建状态时间线 | `MemoryStore.query_state` |
| **冲突闭环** | `resolve_conflict`：resolved/dismissed/reopened/undo，动作+依据+操作者写入 `conflict_actions` 追溯表（含 undone_by 撤销链）；reopen 同事务将引用冲突双方的当前结论标 stale 并建重查任务；记录核实≠临床裁决在 UI 文案中体现 | `MemoryStore.resolve_conflict` |
| **依赖失效** | `scope_revisions`（medications/semantic 集合 revision，空集合有 revision=0）；结论记录 input_revision；药单变化同事务失效所有消费旧 revision 的结论（覆盖"新增药物不在旧引用中"）；语义事实变化按 (namespace,key) 引用失效；`retract_semantic_fact` 受控撤回（保留历史）同事务失效依赖 | `MemoryStore._after_fact_change_tx` 等 |
| **持久重查** | `dependency_tasks` 表（open/running/done/failed/cancelled，attempts、lease_token、open 部分唯一索引去重）；`recheck_pending` 由 agent hook 用**真实 detector** 按当前药单重查；无 hook 时任务保持 open，绝不静默当作风险解除；"未检出"写入显式的新结论版本（含"不代表风险解除"文案）；新旧结论以 predecessor_id/superseded_by 互链，`conclusion_chain` 可追溯 | `MemoryStore.recheck_pending`、`agent._recheck_hook` |
| **ContextBuilder** | `memory_context.build_context` → ContextPacket：固定优先级 open_conflicts→critical_facts→current_medications→pending_verification→preferences→relevant_history；预算超限时关键节标记 `complete=False` 并记录 excluded refs 与原因，不静默丢弃；snapshot 携带 revision；已接入 `MemoryReadTool("context_packet")` | `memory_context.py` |
| **Agent 接入** | agent 注册 recheck hook；`run_pending_rechecks` 供 app 每轮调用；MemoryReadTool 新增 context_packet / pending_rechecks 查询（deterministic planner 白名单未改动，不影响既有规划） | `agent.py` |
| **UI** | app.py 末尾"记忆可信度面板"：待核实/未决冲突/待重查/任务四指标、待核实原文、stale 结论版本链、一键重查、冲突处理（四动作+必填依据） | `app.py` |

## 3c. P2 实施（同日追加）

- **memory_search.py**：FTS5 trigram 索引（SQLite 3.53 环境；无 FTS5 时自动降级 LIKE 子串匹配）；`sync_history_index` 增量同步；搜索结果只作召回辅助，不决定当前事实真假。
- **eval_memory.py**：22 个确定性场景（否定/第三人/假设/不确定/肯定记录/策略守卫/错主体更正/幂等重放/幂等冲突/事务原子性/晚到无泄漏/乱序停药/冲突生命周期/新药 scope 失效/事实更正失效/重查失败不冒充成功/严格 ref/空状态拒答/提示词注入/历史检索/预算不完整/跨会话）。另含 **3 项消融**（policy/bitemporal/dependency），消融运行验证了三机制均"承重"——移除后受保护场景确实失败。
- **未实施**（如实列出）：P2 的可重建摘要编译器（summary ablation 因此无对应场景）、性能/延迟基准、向量检索。52 场景完整矩阵完成 22 条起步集。

## 3d. 测试证据（最终）

- `python -m unittest stage0.test_memory_p0 stage0.test_memory_p1 stage0.test_stage3 stage0.test_stage5` → **51 项全部 OK**（P0 16 + P1 14 + 既有 21）。
- `python -m stage0.eval_memory --ablate` → **22/22 通过**；消融报告：policy / bitemporal / dependency 均 load-bearing。结果存 `docs/memory-design-2026-09-05/eval_report.json`。
- 所有测试/评测使用临时合成库；`stage0/memory.db` 未被打开或修改。

## 4. 仍存在的限制

1. **默认 event key 是 `session:turn`**：覆盖 Streamlit 同轮 rerun / 重试；跨进程重启后由 UI 重新生成的 pending 事件 ID 尚未做。
2. **双时间的已声明近似**：conflict/结论的 status 是当前状态，历史视图按 created_at 过滤但不重建状态时间线；完整 state_slices（设计 §4.4）未实现。
3. **否定/主体规则是高精度小覆盖**：只收敛演示所需的封闭模式，不声称中文开放域召回；LLM 抽取路径的候选按同一存储守卫校验，但 LLM 本身可能漏抽。
4. **重查消费是同步内联的**（app 每轮调用/测试直调），没有后台 worker；进程关闭期间任务持久保留但不自动执行——符合设计 §七 的离线 MVP 口径。
5. **`streamlit` 不在当前 `.venv`**（既有环境状况），app.py 仅通过语法解析与单元回归验证；其面板逻辑建议下次运行 UI 时人工过一遍。
6. **未实施**：可重建摘要编译器、性能基准、向量检索、52 场景完整矩阵（完成 22 条起步集）、多患者。
7. 审计日志仍是普通 SQLite 行，可追踪、**不可称为防篡改**（设计 §六 原文口径）。
8. 所有收益表述限于上述测试与评测实测；无生产流量、无临床效果声明。
