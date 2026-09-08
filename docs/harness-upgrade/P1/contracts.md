# Harness P1 契约速查

所有示例均为合成数据。契约实现位置以 `stage0/harness/` 为准。

## 1. RunContext（runtime.py）

```
RunContext(
  run_id, turn_id, session_id, event_id, client_event_id,
  operation_id, attempt_id,          # P0 身份契约延续
  principal: Principal(user_id, roles, scope_id),   # 代码解析，非模型输入
  patient_revision: int | None,       # 写入时由 executor 捕获
  trace_id, parent_span_id,           # 观测关联
  cancel_event: threading.Event | None
)
```

- `ctx.checkpoint_dict()`：仅 CHECKPOINT_FIELDS 白名单（JSON 安全）；principal、
  预算句柄、取消事件一律不进 checkpoint。
- `ctx.budget`：动态读取当前 `BudgetSession`（turn_budget.CURRENT），不持有。
- `ctx.cancelled()`：cancel_event 或租约/预算 guard 失效 → True。
- 恢复时 `RunContext.from_checkpoint(data)` 重新按配置解析 principal，
  checkpoint 永不成为凭证来源。

## 2. ToolSpec / ToolResult（tools.py）

```
ToolSpec(name, description, argument_schema, result_shape, kind: read|write,
         required_permission, timeout_owner, retry_owner='none',
         idempotency: pure|receipt_keyed|none, cacheable, parallelizable,
         proposal_schema?)        # 模型目录子集；缺省 = argument_schema
ToolResult(tool, ok, value, error{error_kind, recoverable, error},
           evidence_refs[], metrics{attempts, active_seconds, corrections},
           receipt_replayed)
```

权限表（PERMISSION_ROLES，代码所有）：`memory:read / memory:write / rag:search /
ddi:detect / evidence:read / clarify` → 允许的角色集合。proposal 无法扩权。

错误种类（ToolErrorKind）与可恢复性：

| kind | 可恢复 | 典型来源 |
| --- | --- | --- |
| unknown_tool | 否 | 未注册工具 |
| invalid_arguments | **是** | schema 校验失败、分页越界 |
| permission_denied | 否 | 主体无所需角色 |
| evidence_unavailable | 否 | 证据缺失/跨 scope/哈希不符（同一错误，无存在性 oracle） |
| retrieval_empty | 是 | 检索为空（作为失败返回给模型，可换查询重试） |
| policy_violation | 否 | MemoryPolicyError / 伪造 warning 正文 / 冲突字段不匹配 |
| internal_error | 否 | 其余异常（类型名 + 安全摘要，≤300 字符） |
| budget_exhausted / cancelled | 否 | 透传或显式终止分类，绝不伪装成功 |

透传异常（不落为工具结果）：`BudgetExceeded`、`LeaseRejected`、langgraph
中断类。

## 3. 内置工具契约（default_tools.py）

| 工具 | kind | 权限 | 幂等 | 备注 |
| --- | --- | --- | --- | --- |
| ddi_check | read | ddi:detect | pure | `medications` 由 guard grounding 用**完整权威药单**覆写 |
| rag_search | read | rag:search | pure | 执行后捕获证据（`evidence` 视图 + `evidence_refs`），原文不动 |
| memory_read | read | memory:read | pure | 六类查询白名单不变 |
| memory_write | write | memory:write | receipt_keyed | proposal 只选 operation；warnings/context_refs/冲突字段为执行层注入，executor 复用同一 hydration 实现做纵深校验 |
| ask_clarification | read | clarify | pure | 拒绝诊断/处方话术（guard） |
| read_evidence | read | evidence:read | pure | `read_evidence(evidence_id, offset≤?, limit≤2000)`；scope 来自 store，无 path/URL 参数 |

新增工具流程（无 runner 改动）：`executor.register(spec, handler)` →
`agent.planner.bind_tools(agent.executor.catalog())`。

## 4. Hooks（观测性，不可否决）

| 钩子 | 时机 | meta 关键字段 |
| --- | --- | --- |
| before_model / after_model | planner / composer / verifier 调用前后 | kind, cycle, status(accepted/safety_rejected/fallback/…), guard_rejected, payload_chars, latency_ms |
| before_tool / after_tool | executor 派发前后 | tool, cycle, args_hash, ok, error_kind, replayed, evidence_refs |
| before_publish | 最终安全门之前 | degraded_reason, safety_status |

约束：hook 异常被吞（观测不得破坏回合）；安全边界（guard、hydration、
`_check_response`、`SafetyBoundary.enforce`）是直接代码，不经过 hook，
插件无法关闭。

## 5. EvidenceStore（evidence.py）

```
put(content, source_uri, run_id, content_ref, corpus_version,
    retrieval_params, patient_revision, access_class: general_label|
    patient_specific, scope_id) -> EvidenceRecord(evidence_id='ev-'+hash20)
read(evidence_id, scope_id=store scope, offset≥0, 1≤limit≤2000)
  -> {content, offset, returned_chars, total_chars, truncated,
      source_uri, content_hash}
```

- 证据 ID 内容派生 → 同内容重捕幂等。
- read 校验：存在性、scope、（patient_specific 的 scope 限制）、内容哈希、
  分页。缺失与跨 scope 返回**同一条** evidence_unavailable 错误。
- `select_excerpt(text, query)`：按段落与查询词重叠选**最相关段落**（不是
  前 200 字）；摘录是派生视图，无原文引用资格。
- `prune(older_than_days, active_run_ids, protected_evidence_ids)`：
  活跃 run、未决审核单、结论引用保护证据；其余过窗即回收。

## 6. RunManifest（manifest.py）

写于 run 启动（两个 runner 均接），`run_manifests(run_id PK)`，INSERT OR
IGNORE → 不可变。sections：

- `code`：git revision + dirty + diff_hash（dirty=工作区有未提交修改）
- `graph`：graph_version、state_schema_version
- `models`：planner_mode/model、composer/verifier 开关与模型 id（**无 key**）
- `prompts`：planner/response/verifier 系统提示、canonical proposal schema、
  工具目录、executor 规格的 sha256[:16]
- `policy`：安全拒绝阈值、权限表、review 开关
- `corpus`：rag 索引目录（含 DDI_ENGINE_RAG_INDEX_DIR 覆盖）、ddi 配对索引哈希
- `limits`：TurnBudget 全量；`feature_flags`、`dependencies`

恢复语义：`check_restore_compatibility(old, current)` → graph/models/prompts/policy/limits/corpus
有差异 ⇒ `migration_required=true`（必须显式兼容迁移或失败/待处理）；
旧 manifest 缺字段 ⇒ `unknown`，不回填当前值。差异定位用
`manifest_diff(a, b)`。

2026-09-07：检查现已接入 graph run/resume 和已有 legacy run 入口；原预算使用原 run 的限制，改变环境不会补充额度。图恢复缺 manifest 会拒绝并要求显式迁移；旧 legacy 无 manifest 保留 unknown，不用当前配置补填历史。详见最终验收报告。

## 7. 评测数据集（harness_eval.py，DATASET_VERSION=p1-1.0）

回归集 8 场景（全部合成 + 假 provider，离线）：happy_path、错误工具参数、
重复合法读取、证据注入（扩展既有注入语料）、检索为空/低置信升级、临预算、
图重启不重复效果、审核逾期不自动通过。

指标：目标完成、必要检查覆盖（ddi_check）、禁止动作数（正则）、引用有效率、
无进展调用率、预算终止原因、恢复完成、重复领域效果数、阶段延迟（span）、
usage_tokens_charged、unexpected_error_logs。

运行：

```
PYTHONPATH=. python stage0/harness_eval.py --out docs/harness-upgrade/P1/eval_report.json \
    [--baseline <旧 eval_report.json>]
```
