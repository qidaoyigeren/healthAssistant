# P0 基线核实报告（产品能力升级）

日期：2026-09-07。范围：以当前工作区代码（含未提交改动）为准的现场核实；历史文档只作线索。所有行号与结论均为本次实际确认，未沿用旧报告。

## 1. 能力表（已实现 / 部分实现 / 缺失 / 未验证）

| 能力 | 状态 | 依据（现场核实） |
|---|---|---|
| 不可变证据存储 + Agent 内受控回读（scope/哈希/有界分页） | 已实现 | [evidence.py:103](/d:/py/HealthAssistant/stage0/harness/evidence.py#L103) `EvidenceStore.read`（`MAX_READ_LIMIT=2000`、缺失/跨 scope 同错、读取时哈希复验）；捕获入口 [default_tools.py](/d:/py/HealthAssistant/stage0/harness/default_tools.py#L269) |
| **服务端证据原文 HTTP API（前端可核查）** | 本轮前缺失 → 本轮 P1 实现 | 本轮前 `evidence_refs` 只出现在 progress/trace，不出现在最终响应（server.py `_execute` 序列化字段清单核实）；现已新增 `GET /v1/evidence/{id}` |
| 结论依赖索引、选择性失效、重查任务（租约/恢复/保守不升级） | 已实现 | memory.py `conclusion_dependencies`(:413)、`_invalidate_medication_dependents_tx`(:1492)、`recheck_pending`(:3537)（无 hook 时结论绝不悄悄升回 current） |
| 结论→证据 record 级关联（evidence_id 可回读） | 本轮前缺失 → 本轮 P1 实现 | conclusions.source_refs 只有 uri/quote/retrieval，无 evidence_id（memory.py:3010-3019 核实）；现 capture 时写入 |
| 变更影响摘要（变更事实/受影响结论/重查状态同口径） | 缺失 → 本轮 P1 实现 | 现新增 `GET /v1/change-impact`，统计=明细同一查询 |
| AnswerBundle（结构化结果） | 缺失 → 本轮 P1 最小实现 | AgentResponse 原无此字段（agent.py:157 核实）；现 `answer-bundle@1` 附加于事件响应 |
| 只读批处理（batch_read） | 已实现，默认关闭 | default_tools.py:316（`STAGE0_READ_BATCH`） |
| 只读委派（delegate_task） | 已实现（确定性 worker），默认关闭 | harness/delegation.py:193；`STAGE0_DELEGATED_WORKERS` 默认关 |
| 人工审核（graph interrupt/resume、事实变化后旧审核失效） | 已实现 | graph_runner.py:628/672（review_stale 检测 :683） |
| 前端证据展示（引用列表/quote/抽屉） | 部分实现 → 本轮 P1 补齐 | evidence.tsx 已有 quote 内联 + MemoryRefDrawer；但无证据原文读取、无影响卡片（本轮补） |
| 前端刷新/断网恢复、取消、幂等 | 已实现（有上一轮验收工件，本轮未重跑该专项） | submissions.tsx 状态机 + docs/harness-upgrade/final-acceptance/browser-acceptance.json |
| 结构化材料导入/差异核对（P2）、CareTask（P3）、缓存语义修复（P4）、OCR（P5） | 缺失（后续阶段） | 仓库内无 reconciliation/care_tasks/documents 模块（glob 核实） |

## 2. P3 实验一致性核对

- **早期报告**（docs/harness-upgrade/P3/experiment_report.md，2026-09-06）：回读量不等价（delegate 回读 9 页 vs 其他 3 页），`adopt_delegation=否`（payload 优势仅 4.0% < 20% 阈值），`adopt_batching=是（候选）`。
- **等量回读 rerun**（docs/harness-upgrade/final-acceptance/p3-eval.json，dataset `p3-1.1-equivalent-coverage`，2026-09-07）：三方案等量完整回读后，长标签场景 planner 决策 single/batch/delegate = 12/6/4，`adopt_delegation=true`、`adopt_batching=true`。
- **当前证据支持的结论**：两者不矛盾——结论变化由"等量工作负载"修正引起；`provider_note` 明确全部为脚本化假 provider、worker 为确定性只读流水线（worker_model=None）、延迟为合成模型。因此只能证明**流程与资源行为**达到离线候选门槛，不能宣称真实多 Agent 质量收益；两个开关仍默认关闭（default_tools.py 现场核实）。

## 3. 基线测试（本轮实际执行）

```powershell
.venv/Scripts/python.exe -m unittest stage0.test_memory_p0 …（全部 19 个 stage0/test_*.py 模块）
```

- 实施前基线：**281 项测试，失败 0、错误 0、跳过 0，退出码 0**（100.4s），与历史最终验收记录一致，无既有失败。
- P1 实施后全量回归：**291 项（281 基线 + 10 新增 P1 测试），失败 0，退出码 0**（71.3s）。
- 未覆盖：真实模型实验、独立 held-out 数据、生产部署条件（见 §5）。

## 4. 最小对象契约与评测骨架

- 契约：[contracts.md](contracts.md)（DocumentArtifact / ExtractionCandidate / ReconciliationCase / CareTask / AnswerBundle；只约定关联/scope/revision/状态/引用，未建空服务）。
- 评测协议：[eval-protocol.md](eval-protocol.md)（任务/结果格式、6 类失败分类、held-out 隔离规则）。
- 评测入口：`stage0/product_evals/`（12 个首轮开发任务 + run_eval 执行器，驱动真实服务栈）；自检测试 `stage0/test_product_evals.py`（必败样例→退出码 1；缺数据→unavailable，退出码 2，绝不输出 pass）。

## 5. 限制与环境

- **held-out = unavailable**：无独立采集数据；`stage0/product_evals/tasks/held_out/` 仅含隔离规则，不做盲测声明。
- **真实模型 = 未验证**：默认本地模式；本轮所有实验为脚本 provider（未动用外呼授权）。
- **人工/领域质量评价 = unavailable**：无评审资源；指标限定工程标注口径。
- 开发评测首轮 12 个任务中 1 个（未确认候选隔离，属 P2 候选模型）如实输出 unavailable——P2 实现后转正。
