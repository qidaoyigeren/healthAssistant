# Harness P1 实施报告

> 2026-09-07 补充：本文保留首次实施记录。真实 OTLP/HTTP 本地传输、恢复入口兼容检查、评测失败门禁和最终回归已在 [统一验收报告](../final-acceptance/README.md) 中补齐；当前通过状态以其中 JSON 为准。

日期：2026-09-06。作者：编码 Agent（Claude Code / GLM）。范围：Harness P1（A/B/C），按 P1 prompt 与共同执行约束执行。

## 0. 基线（本轮实测，非引用旧报告）

- 前置 Harness P0 工作已在工作区（未提交），`stage0/test_harness_p0.py` 实测 **25/25 OK**。
- 全量回归基线（本轮开工时实测，13 套件）：memory p0/p1/p2、reliability p0/p1/p2、
  stage8/stage10/frontend、harness p0、stage3/5/6 **全部 OK（~170 测试）**。
- 环境实测：Python 3.13（.venv），langgraph 1.2.11、langgraph-checkpoint-sqlite 3.1.1、
  openai 3.3.1、pydantic 2.13.4、langchain-core 1.6.2；未安装 pytest / opentelemetry /
  phoenix / agentevals（测试用 stdlib unittest，评测用本地实现的轨迹匹配器）。
- 工作区含他人前端轮未提交改动（read_models.py 等），本轮未触碰其语义。

## 1. 修改清单与理由

### P1-A 统一运行时与强类型工具契约

| 变更 | 位置 | 理由 |
| --- | --- | --- |
| 新增 `harness/` 包（runtime/schema/errors/tools/default_tools） | stage0/harness/ | 运行上下文、唯一 schema 校验、错误分类学、ToolExecutor/ToolSpec/ToolResult、观测 hooks |
| `AgentState.ctx`（RunContext）+ `Observation.error_kind/recoverable/evidence_refs` | agent.py | 两个 runner 共享同一运行上下文；错误稳定分类 |
| `_act` 全部改走 `ToolExecutor.execute`；rag 的 condition-warnings 富集移入 executor handler | agent.py / default_tools.py | 执行约束单点化；两个 runner 天然一致 |
| `PLANNER_ARGUMENT_SCHEMAS` 由 ToolSpec 派生；`schema_errors` 迁至 harness.schema | agent.py | prompt/guard/executor 同源，防漂移 |
| **两级 schema**：`ToolSpec.argument_schema`（执行层）+ `proposal_schema`（模型目录） | tools.py / default_tools.py | hydration 字段（warnings/context_refs/冲突链接）不能被 unknown-key 剥离误删；模型目录仍最小化 |
| executor 纵深校验：record_warnings / create_clinical_conflict 参数必须等于 guard 同一 hydration 实现的产物 | default_tools.py `_verify_hydrated_write` | 即使绕过 guard 也不能持久化模型编造的 warning 正文/冲突字段（同一规则只有一个实现） |
| hooks：before/after_model（planner/composer）、before/after_tool、before_publish | agent.py / tools.py | 观测性钩子两个 runner 共用；hook 异常被吞、不可否决，安全边界仍是直接代码 |
| graph：ctx 进出 checkpoint（`wf["ctx"]`），runner 均写 RunManifest | graph_runner.py | checkpoint 只带 JSON 安全字段；版本随 run 记录 |

### P1-B 证据卸载与语义上下文

| 变更 | 位置 | 理由 |
| --- | --- | --- |
| `EvidenceStore`（SQLite 附加表 `evidence_records`，内容哈希、语料版本、检索参数、patient_revision、access_class） | harness/evidence.py | 不可变证据；通用说明书与患者特定数据分别建模 |
| rag_search / ddi_check 执行后捕获证据：`evidence` 视图（id + 相关段落摘录）+ `evidence_refs`；**原文 results 不修改** | default_tools.py | 原始证据与给模型的摘要分离；引用核验仍读原文 |
| `read_evidence` 工具（scope 来自 store；无 path/URL 参数；分页 ≤2000 字符；哈希复验；缺失与跨 scope 同错误） | evidence.py / default_tools.py | 受授权回读，模型不能指定路径/URL，无跨患者存在性 oracle |
| `select_excerpt`：按段落与查询词重叠选最相关段落 | evidence.py | 替代"统一截前 200 字"；相关原文在长段末尾可被选中 |
| `bounded_patient_snapshot`：关键集合（在用药/关键事实/未决冲突）高上限 + 显式 `__omitted__` 标记；替代 `value[:12]` 静默裁剪；planner payload 增 `context_omissions` | harness/context.py / agent.py | 遗漏明确可见，不能被当作已完成检查；13+ 用药、多过敏/冲突有测试 |
| `build_run_summary` / `summarize_observation`：结构化摘要替代哈希式旧观察（含 evidence: "not_recorded" 显式缺失标记） | harness/summary.py / agent.py | 旧观察缺 evidence_id 时明确缺失，不回补 |
| 证据保留：`prune` + `run_pending_rechecks` 顺带回收（活跃 run/未决审核/结论引用受保护） | evidence.py / agent.py | 孤儿回收规则；回收失败不阻断重查 |

### P1-C 轨迹评测、观测与 RunManifest

| 变更 | 位置 | 理由 |
| --- | --- | --- |
| `call_spans` 表 + `SpanRecorder`（call_id/span_id、parent trace、cycle、耗时、回执命中、guard 拒绝、降级原因；usage 权威仍在 llm_attempts） | harness/observability.py | 调用级观测；持久账本单 owner 不变 |
| 重放去重：`dedup_key=kind:source:cycle:args_hash`，同签名折叠 `replay_count`，异签名新 attempt 行；`dedup_report()` 展示去重前后 | observability.py | 同一逻辑重放不重复计量，真实重试保留独立 attempt |
| turn_traces 幂等：`entry_id`（内容哈希）+ `INSERT OR IGNORE`；graph 状态新增 `trace_flushed` 贯通 | memory.py / graph_runner.py | 图节点重建 AgentState 不再把旧 trace 重复落库（实测修复项） |
| `RunManifest`：run 启动即写、不可变、无凭证；`manifest_diff` / `check_restore_compatibility`（缺字段=unknown；graph/limits/policy 差异=需显式迁移） | harness/manifest.py | 版本变化可定位；恢复语义显式 |
| OTel 适配层：`STAGE0_OTEL_EXPORT=1` 且安装 opentelemetry 才启用；有界队列、导出故障仅日志 | observability.py | 可选、默认关；遥测不阻断业务 |
| 评测入口 `stage0/harness_eval.py`：数据集 p1-1.0（回归集 8 场景）+ 指标 + 显式不变量 + JSON/可读报告 + `--baseline` 对比 | harness_eval.py | 在既有场景/故障注入/trace 格式之上构建；LLM judge 不负责权限/预算/幂等 |

### 实施中发现并修复的真实缺陷

1. **图节点重放重复持久化 trace**：graph 每次 `_agent_state` 重建把
   `trace_flushed` 归零，`_flush_traces` 会把全部旧条目重新写入 turn_traces。
   本轮以 trace_flushed 贯通 + entry_id 幂等双重修复（测试
   `test_graph_node_replay_does_not_duplicate_persisted_trace`）。
2. **评测外呼风险**：本机存在已配置 LLM key 时，`llm_planner_enabled=True` 且
   无 provider 会真实外呼（既有 opt-in 行为）。评测入口强制
   "无脚本 provider ⇒ 确定性规划"，堵死该路径。

## 2. 兼容性

- 既有 API/行为不变：确定性 fallback、中文原文引用、冲突显式保留、consolidate
  回执、预算语义（zero-limit、token 边界、租约丢失等 harness P0 全套测试通过）。
- `Observation` 新字段带默认值；旧 checkpoint/旧行兼容（P0 的
  pre-harness/legacy 检查点测试通过）。
- 附加表：evidence_records / call_spans / run_manifests（CREATE TABLE IF NOT
  EXISTS，均由 harness 模块在打开时建表）；turn_traces 新增可空 entry_id 列。
- Stage 8 测试 `test_old_observations_summarized_recent_full` 按新契约更新：
  旧观察摘要由哈希式改为结构式（P1-B 明确要求替代），断言等价增强。

## 3. 迁移 / 回滚

- 迁移：纯附加，无数据改写；旧库首次打开自动建表/加列。
- 回滚：删除 harness 包引用即可回到 P0 行为；但**不可**借回滚重新开启已修复
  的重复 trace 持久化（entry_id 幂等保留）。read_evidence/证据捕获随 executor
  一起消失，不产生孤运行为。
- 影响运行语义的版本变化：`RunManifest.graph.state_schema_version` 记录在案，
  恢复用 `check_restore_compatibility` 判定，不做静默迁移。

## 4. 实际测试命令与结果（2026-09-06 实测）

```
.venv/Scripts/python.exe -m unittest stage0.test_harness_p1_a     # 15/15 OK
.venv/Scripts/python.exe -m unittest stage0.test_harness_p1_b     # 16/16 OK
.venv/Scripts/python.exe -m unittest stage0.test_harness_p1_c     # 11/11 OK
PYTHONPATH=. python stage0/harness_eval.py --out docs/harness-upgrade/P1/eval_report.json
                                                                  # 8/8 场景 PASS
# 全量门禁（16 套件，含既有 13 套回归）：
for t in test_memory_p0 test_memory_p1 test_memory_p2 test_reliability_p0 \
         test_reliability_p1 test_reliability_p2 test_stage8_agent test_stage10_server \
         test_frontend_read_models test_harness_p0 test_harness_p1_a test_harness_p1_b \
         test_harness_p1_c test_stage3 test_stage5 test_stage6; do \
  .venv/Scripts/python.exe -m unittest stage0.$t; done           # 全部 OK，GATE_FAIL=0
```

## 5. 验收对照

| 必须验收项 | 证据 |
| --- | --- |
| 两 runner 预算/拒绝/错误类型/授权语义一致；新增测试工具不需要给两套循环补分支 | test_harness_p1_a `SharedExecutorParityTests`（legacy+graph 同一 agent 注册新工具） |
| 用药列表 13 项及更长、多关键过敏/冲突、相关原文在长段末尾、分页越界、跨 scope 读取、哈希不符 | test_harness_p1_b `SemanticContextTests`/`EvidenceStoreTests` |
| 长轮次原始证据不变、规划输入受预算控制、遗漏明确可见 | test_harness_p1_b `test_capture_keeps_raw_results_untouched…`/`test_planner_payload_surfaces_omissions…` |
| checkpoint/审计/观测无凭证；观测导出故障不影响业务；预算账本可靠 | test_harness_p1_c `test_manifest_is_saved_immutable_and_credential_free`、OTel 测试、harness_p0 账本套件 |
| 同一逻辑重放不重复计量，真实重试独立 attempt，持久化 trace 不被图节点重复追加 | test_harness_p1_c `SpanDedupTests` |
| 固定数据集生成 JSON+可读报告；后台故障使场景失败（unexpected_error_logs 计入）；版本变化定位到 manifest 差异 | harness_eval 8 场景 + `test_manifest_diff_locates_version_changes…` |

P0 基线对比（诚实口径）：Harness P0 没有轨迹评测/manifest 基线文件，因此
本轮不伪造"前后对比数字"；对比能力已实装（`--baseline`），本轮报告
`eval_report.json` 即为后续阶段的可复测基线。P0 的安全/恢复断言（25 项）在
P1 改动后**全部保持通过**，作为发布门禁。

## 6. 未验证范围

- OpenTelemetry/Phoenix 真实导出（依赖未安装，仅适配层 + 关闭路径已测）。
- read_evidence 的前端/服务端读模型暴露（模型侧可用，API 未挂）。
- 真实 LLM 模式下的评测指标（无外呼授权；全部为 fixture replay，不声称真实
  模型质量）。
- held-out 评测集：本轮仅标注框架与回归集，未构建 held-out 场景。

## 7. 下一阶段接口（供 P2 参考）

- `ToolResult` 已含 `cacheable`/`receipt_replayed` 字段：P2 受控缓存可直接
  按 `dedup_key`（kind:source:cycle:args_hash → 运行间应改为
  content-derived）挂接。
- `ctx.cancel_event` 已贯通 executor 取消门：P2 执行进度/取消可复用。
- `CallSpanStore.spans_for_run/dedup_report`：P2 重复调用削减的量化基线。
- `EvidenceStore.prune` 与 read_evidence：P2 进度视图可引用证据分页读取。
