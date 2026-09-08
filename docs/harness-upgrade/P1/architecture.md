# Harness P1 架构 — 统一运行时、证据上下文、观测与版本记录

> 2026-09-07：下方记录首次实施架构。本轮已补接真实本地 OTel 导出与 manifest 恢复门，最新兼容性和验证边界见 [最终验收](../final-acceptance/README.md)。

日期：2026-09-06。范围：Harness P1（A/B/C 三批）。前置：Harness P0（工作区已验证，25/25）。

## 1. 总览

```
            ┌────────────────────────────────────────────────────────┐
            │                    服务层 (server.py)                   │
            │   202 受理 / outbox / 租约 / 回执 / 人工审核（既有）      │
            └───────────────────────────┬────────────────────────────┘
                                        │ run(event, run_id, event_id, …)
                    ┌───────────────────▼────────────────────┐
                    │            RunnerRouter                │
                    │   LegacyAgentRunner │ LangGraphAgentRunner
                    │  （只负责路由/循环/检查点/恢复，无第二套执行规则）
                    └───────────────────┬────────────────────┘
                                        │ 共享（两个 runner 走同一套）
      ┌─────────────────────────────────▼──────────────────────────────────┐
      │                     MedicationCoordinatorAgent                     │
      │  HybridPlanner(PlannerPolicyGuard) ── RunContext（可信主体/身份）    │
      │  LLMPlanner / Composer / Verifier（来源维度计入 turn_budget）        │
      │  before_model / after_model / before_publish（观测性 hooks）        │
      │                              │                                     │
      │                    ┌──────────▼──────────┐                          │
      │                    │    ToolExecutor     │  stage0/harness/tools.py │
      │                    │ ToolSpec + handler  │  一个工具一个契约          │
      │                    └──────────┬──────────┘                          │
      │  执行顺序（P1-A #5）：                                               │
      │  1 身份/范围/权限 → 2 参数校验（schema.py，唯一实现）→               │
      │  3 代码派生安全上下文（event_key/operation_id）→                     │
      │  4 证据/事实时效捕获（patient_revision）→ 5 预算/租约门 →            │
      │  6 回执核查/执行 → 7 结果归一化 → 8 计量 + 审计                      │
      └───────┬──────────────┬───────────────┬───────────────┬────────────┘
              │              │               │               │
      EvidenceStore   turn_budget      call_spans      run_manifests
      （不可变证据，   （持久预算账本，   （调用 span，    （每次 run 一份，
       read_evidence    唯一 owner）      重放去重 +       不可变，无凭证）
       受权回读）                        可选 OTel）
```

设计原则：

1. **一个执行现实**：legacy 循环与 LangGraph 图都调用 agent 的同一条
   `_act → ToolExecutor.execute` 路径与同一个 `planner.decide`。新增工具
   = 注册 `ToolSpec` + handler + `bind_tools(executor.catalog())`，两个
   runner 不需要任何条件分支改动（测试 `test_new_tool_executes_on_both_runners_without_loop_changes`）。
2. **权限/安全字段不由模型设置**：主体来自代码解析的 `Principal`；warning
   正文、引用、conflict 链接仍由 guard 的 hydration 从真实观察注入；
   executor 端再做一次纵深校验（复用**同一个** hydration 实现，不是第二套规则）。
3. **错误分类稳定**（P1-A #8）：`ToolErrorKind` 区分
   unknown_tool / invalid_arguments（可恢复）/ permission_denied /
   evidence_unavailable / retrieval_empty / policy_violation / internal_error /
   budget_exhausted / cancelled。BudgetExceeded、LeaseRejected、图中断是
   **透传异常**，永不变成"可重试工具失败"。
4. **原始证据与模型视图分离**（P1-B）：检索结果原文不动，摘要/摘录只是派生
   视图；最终引用核验仍读原始观察。
5. **可靠账本与观测分离**（P1-C）：`llm_attempts`/`workflow_runs.budget`/
   `audit_log` 仍是权威；`call_spans` 是可折叠的观测元数据，采样/导出故障
   不影响业务。

## 2. 模块地图（stage0/harness/）

| 模块 | 职责 | 关键接口 |
| --- | --- | --- |
| `runtime.py` | RunContext：可信 principal/scope、run/operation/attempt 身份、patient_revision、预算句柄、取消、trace 关联；`checkpoint_dict()` 仅 JSON 安全字段 | `RunContext.from_checkpoint`、`ctx.cancelled()` |
| `schema.py` | 唯一 JSON-schema 校验器（prompt/guard/executor 共用），unknown key 剥离 | `schema_errors`、`strip_unknown_keys` |
| `errors.py` | 错误分类学与透传集合 | `ToolErrorKind`、`ToolExecutionError.to_payload`、`classify_exception` |
| `tools.py` | ToolSpec/ToolResult/ToolExecutor + 观测性 hooks | `execute(ctx, tool, args, state)`、`catalog()` |
| `default_tools.py` | 五个内置工具契约 + read_evidence 注册 + 证据捕获 + 纵深校验 | `build_default_executor(agent, evidence_store=…)` |
| `evidence.py` | 不可变 EvidenceStore、相关段落摘录、证据保留回收 | `put/read/get_meta/prune`、`select_excerpt` |
| `context.py` | 按字段语义的有界视图，显式遗漏标记 | `bounded_patient_snapshot`、`omissions`、`view_is_complete` |
| `summary.py` | 结构化 run 摘要（替代哈希式旧观察） | `build_run_summary`、`summarize_observation` |
| `manifest.py` | 不可变 RunManifest + 差异定位 + 恢复兼容性判定 | `build_manifest`、`manifest_diff`、`check_restore_compatibility` |
| `observability.py` | call span 持久化（重放去重）、可选 OTel 导出 | `SpanRecorder`、`CallSpanStore.dedup_report`、`OTelExporter` |

配套改动：`agent.py`（executor/ctx/hooks 接线、两级 schema、payload 有界化）、
`graph_runner.py`（ctx 进出 checkpoint、trace_flushed 贯通、manifest 写入）、
`memory.py`（turn_traces.entry_id 幂等持久化 + evidence/call_spans/run_manifests
附加表由 harness 模块建表）、`harness_eval.py`（评测入口）。

## 3. 关键决策与理由

### 3.1 两级 schema（模型 proposal vs 执行层参数）
guard 校验"模型提议"（memory_write 只有 `operation`），executor 校验
"materialize 后的动作"（含注入的 warnings/context_refs/冲突字段）。
`ToolSpec.argument_schema` 描述执行层接口，`proposal_schema` 是模型目录子集。
这样 unknown-key 剥离不会把 hydration 字段误删（实施中曾因单级 schema 引发
Stage 6 回归，两级 schema 即为修正）。

### 3.2 重放去重的两层设计（P1-C #21）
- **turn_traces**：条目内容哈希作 `entry_id`，`INSERT OR IGNORE`；graph 状态
  新增 `trace_flushed` 贯通，节点重放不再把旧 trace 重复落库。
- **call_spans**：`dedup_key = kind:source:cycle:args_hash`。同一逻辑事件
  （同 key、同结果签名）→ 折叠进 `replay_count`；结果签名不同（如回执命中
  `receipt_replayed`、错误类别不同）→ 新 `attempt_no` 行 = 真实重试。
  `dedup_report()` 直接给出 去重前事件数 / 去重后行数 / 折叠数。

### 3.3 预算与重试所有权不变（P1-A #7）
ToolSpec 全部 `retry_owner='none'`（ADR-004 延续）：SDK max_retries=0、图无
RetryPolicy、唯一的有界 parse-retry 在 planner。executor 只做单次调用的
attempts/active_seconds 计量（观测口径），不叠加管理同一瞬时重试；
durable 计量仍在 `llm_attempts`（来源维度：planner/composer/verifier/
fact_extractor/ddi_extractor）。

### 3.4 证据保留规则（P1-B #18）
`prune(older_than_days, active_run_ids, protected_evidence_ids)`：
窗口外且 (a) 不属于 running/waiting_review 的 run、(b) 不被 open/waiting_user
审核单 summary、(c) 不被 conclusions source_refs 引用的证据才可删。
`run_pending_rechecks` 每轮顺带执行，且回收失败绝不阻断重查。

### 3.5 评测的诚实边界（P1-C #27/#28）
全部场景合成数据 + 假 provider；`make_agent` 强制"无脚本 provider 即确定性
规划"——实施中发现本机存在已配置 key 时 LLM 模式会发起真实外呼，评测入口
必须堵死该路径（也如实记录：这是既有 opt-in 行为，非本轮引入）。LLM judge
不参与权限/预算/幂等判定；安全先后关系是显式不变量（如
`INV_WARNINGS_GROUNDED` = record_warnings 必须在成功的 ddi/rag 观察之后），
不是工具顺序复制。

## 4. 未验证范围 / 已知限制

- OpenTelemetry 导出仅实现了 opt-in 适配层（本 venv 未安装 opentelemetry，
  `STAGE0_OTEL_EXPORT=1` 时记录日志并保持关闭）；Phoenix 可作为本地后端接入
  该适配层，未实测。
- read_evidence 未接入 server.py 的前端读模型（模型侧已可用；前端展示留待
  下一轮）。
- `manifest_example.json` 中 `code.revision` 是示例生成时的 git HEAD；工作区
  dirty 为真（本轮改动未提交），故 `diff_hash` 非 null。
- 评测 `near_budget` 场景验证的是 LLM 模式下的 token 耗尽路径；确定性模式下
  planner 不消耗 token，同等预算约束由 cycles/call 门承担（由
  test_harness_p0 的 zero-limit 套件覆盖）。
