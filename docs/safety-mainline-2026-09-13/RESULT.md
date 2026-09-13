# 长期用药安全主线重构：验收结果（2026-09-13）

决策与边界见 [DECISION.md](DECISION.md)；被删除实验的结论索引见 [HISTORY-INDEX.md](HISTORY-INDEX.md)。

**三类结果分开报告，不合并成一个通过率**：程序安全性、产品流程、真实模型作用。

---

## 1. 本轮实际改了什么

**新增三个模块、一个契约、一条队列、一个页面。**

| 落点 | 作用 |
|---|---|
| `stage0/safety_cases.py` | 安全事项：引用层身份（`dedup_key` + 用药分期锚点）、七态生命周期、处置依据校验、主线读模型 |
| `stage0/safety_checks.py` | 必要检查队列与执行器：从 agent 搬出的确定性检测/推导、`findings_of` 口径归一、无 agent 也可运行的重查 |
| `stage0/test_safety_cases.py` / `test_safety_checks.py` / `test_safety_mainline_e2e.py` | 事项契约、检查路径、三个端到端场景 |
| `care_task` 的 `safety-case@1` 契约 | 从**一件具体事项**出发的有界调查，复用既有预算/租约/取消/产物发布 |
| `necessary_checks` 表（memory 层，纯 additive） | "记录改了"与"要重新检查"在同一次提交里落地 |
| `frontend/src/features/safety/` | 主线页（`/` 与 `/safety`）与事项详情页 |

**关键去重**：`agent._recheck_ddi` / `_recheck_condition` / `_condition_warnings` / `_warning_text`
不再是第二份实现，全部委托给 `safety_checks`——"看提示的时候"和"程序自己检查的时候"
不会得到两套结论。

## 2. 程序安全性（不变量）

| 不变量 | 由什么锁住 | 结果 |
|---|---|---|
| 必要检查**不依赖模型** | `test_the_check_runs_without_any_agent_or_provider`（全程未构造 agent） | 通过 |
| 药单一变即登记检查，且在**同一事务** | `test_a_medication_change_enqueues_a_check_in_the_same_transaction` | 通过 |
| 第一次出现的药物组合会被查到 | `test_a_first_time_pair_is_found_by_the_deterministic_path` | 通过 |
| 去重不吞掉新提示 | `test_a_changed_medication_set_is_not_swallowed_by_dedup` | 通过 |
| 检查失败保持可见，不读成"检查过了" | `test_a_failed_check_stays_visible_instead_of_reading_as_checked`、`test_a_failed_check_never_reads_as_no_risk` | 通过 |
| 检测器返回列表时**不**被静默当成"无检出" | `test_a_bare_list_detector_is_understood_not_silently_empty` | 通过（本轮修） |
| 事项不复制权威事实 | `test_a_case_references_facts_and_never_copies_them` | 通过 |
| 同一风险复检**不产生新卡片** | `test_the_same_risk_on_a_later_recheck_updates_one_case` | 通过 |
| 剂量变更同一分期；停药重启新分期 | `test_a_dose_change_stays_the_same_episode_but_a_restart_does_not` | 通过 |
| 「已读」不关闭、不降级 | `test_marking_seen_never_closes_or_downgrades` | 通过 |
| 旧版本的检查不能批准新状态 | `test_an_old_check_cannot_approve_a_new_medication_state`、`test_a_stale_conclusion_cannot_close_the_case` | 通过 |
| 用户转述医生意见不能关闭事项 | `test_a_user_relayed_doctor_opinion_is_not_a_closing_basis` | 通过 |
| 专业依据必须是已生效的复核决定 | `test_professional_basis_requires_an_applied_decision` | 通过 |
| 补充只关闭它实际回答的请求 | `test_input_closes_only_the_request_it_answers`、`test_an_answer_that_matches_nothing_closes_nothing` | 通过 |
| 多条回答不被张冠李戴 | `test_each_answer_lands_on_the_request_it_belongs_to` | 通过（本轮修） |
| 依据变化重新打开**同一件事** | `test_a_resolved_case_reopens_when_the_risk_returns` | 通过 |
| 运行失败 ≠ 事项解决 | `test_a_failed_investigation_leaves_the_case_unresolved` | 通过 |
| 补问身份不含时间/序号，恢复后不重问 | `test_the_question_identity_is_stable_and_order_independent`、`test_re_asking_the_same_gap_does_not_add_a_second_request` | 通过 |
| 迁移是 additive，用户数据不受影响 | 移除 `necessary_checks` 后重开：只补回该表，用药行与当前药单原样保留 | 通过 |

**本轮修掉的两个真缺陷**（都不是设计问题，是实现缺陷，且都偏向"更不安全"的方向）：

1. **静默空结果**：`current_findings` 只认 `{'warnings': [...]}`。一个返回**列表**的检测器
   会被当成"没有检出"——把"没检查"读成"没问题"，是这条主线上最危险的失败方向。
   现在两种口径都认，认不出的形态**抛异常**而不是降级为空。
2. **张冠李戴**：`record_input` 的 safety-case 分支把 `answers[0].value` 安到批内**每一个**
   `request_id` 上。用户答了 A，事项会记成他答了 B。现在按各自的 `request_id` 归档，
   只有"一条回答对一条请求"时才允许省略 id。

另外修掉一个**集成漏洞**：worker 的 `CARE_TASK_REVIEW_OPERATIONS` 前缀表没带新契约，
safety-case 任务会掉进通用事件分支并以 `KeyError` 失败——就是那行注释警告过的
"漏改一处让任务永远排队中"。已加入前缀并加入契约。

## 3. 产品流程（主线验收）

默认验证入口 `python scripts/verify-agent-closeout.py --out output/<新目录>` 现在会跑
`stage0/test_safety_mainline_e2e.py` 并**按类别**输出：

| 类别 | 覆盖 | 结果 |
|---|---|---|
| 必要检查是否完成 | 4 条用例 | 通过 |
| 安全事项是否正确建立及更新 | 3 条 | 通过 |
| 调查是否取得有效进展 | 3 条 | 通过 |
| 等待或处置状态是否正确 | 4 条 | 通过 |
| 模型和程序分别做了什么 | 3 条 | 通过 |
| 请求、故障与成本是否可追溯 | 2 条 | 通过 |

三个端到端场景（隔离合成数据，全部走产品路径：事件受理 → worker → 检查 → 事项 → 查询）：

- **场景 1**：长期用药 → 经事件受理路径提交变化 → 必要检查执行并留下**带来源的结论**
  → 建立事项（类型/对象/触发/下一步/责任方齐全）→ 用户能看到原因、依据与下一步。
- **场景 2**：事项进入 `awaiting_user` → **关掉进程重开** → 同一 `case_id`、同一条
  `request_id` 仍在 → 补充到达只关闭它回答的那条 → 补充**不等于**解决。
- **场景 3**：带依据的处置记录 → 相关事实变化 → 同一事项被打上 `needs_recheck`、旧依据作废、
  历史里留下 `reopened` → 「已读」与旧版复核都**不能**批准新状态（409）。

**离线全量**（冻结树，`source_unchanged: True`）：**723 次测试 / 49 个套件 / 0 个失败套件**，
4 项冻结评测按预期通过（含负对照必须失败）。见上面的权威结果。

### 冻结树上的权威结果

```
python scripts/verify-agent-closeout.py --out output/closeout-20260913
→ status pass | source_unchanged True | 723 tests / 49 suites / 0 failed
→ evals: baseline-replay ✓  gap-replay ✓  gap-tools ✓  gap-replay-negative ✓（负对照按预期失败）
→ mainline: 20 tests，六个类别全部通过
```

`source_unchanged: True` 是本轮的关键一条：源码指纹在运行前后一致，说明这份"全过"
确实对应现在这棵树，而不是跑动中被改过的某一版。

## 4. 真实模型的作用（有限验收，如实报告）

预先限定：**1 个任务**、`--max-calls 6`、`--wall-seconds 180`、`--max-cycles 6`，
停止条件为预算用尽 / 到达终止状态 / 时间到 / 异常。命令与产物：

```powershell
python scripts/safety-mainline-live-acceptance.py --out output/live-safety-20260913/live.json --max-calls 6 --wall-seconds 180
```

| 项 | 值 |
|---|---|
| 事项由代码建立（**未花任何模型调用**） | 是 |
| 规划来源 | `llm`（真实模型，不是脚本规划器） |
| provider 调用 / token | 6 次 / 33,400；被拒 0 次 |
| 墙钟 | 24.5 秒 |
| 终止原因 | `no_progress` |
| 事项最终状态 | `execution_failed`（**未解决**） |
| 提出的补问 | 0 条 |
| 模型是否自行关闭事项 | **否** |
| 契约版本 | `safety-case@1` 校验通过 |

**读法**：必要检查、结论、事项建立与状态推进**全部由代码完成且可复现**；
真实模型在这次调查里**没有产出有效进展**，调查被 `no_progress` 有界停止，
事项留在"本次执行未完成、仍未解决"——这正是这条路径该有的诚实行为，
而不是失败被掩盖成成功。

**不声称**：这不是模型能力的度量（n=1，单次，一个合成事项）。
它与既往轮次的结论一致：*开放式的模型自主调查在真实批次上尚未被验收。*
本轮因此**不把任何安全保证建立在模型之上**。

## 5. 当前能力边界

**已由代码可靠执行**（不依赖模型、离线可复现）：
版本化用药与事实；必要安全检查及其去重/重试/可见失败；结论与依赖索引；
依据失效与持久化重查；安全事项的建立、更新、重开与处置依据校验；
权限、作用域、来源完整性、幂等、预算、取消与请求审计。

**仍依赖模型**（不作为安全保证）：开放式调查中"还缺什么信息"的判断、材料差异的解释、
跨来源矛盾的分析。模型不可用时这些**不会**被自动化替代——事项停在待调查/等待补充，
界面如实说明本次未经模型调查。

**必须由用户或专业人员确认**：任何用药调整；风险在临床上是否成立；用户转述的医生意见。

**可复用的既有路线**：本轮的 `safety-case@1` 与既有 `evidence_review@1` / `material-review@2`
共用同一个执行核心（`run_open_review` / `ReviewRunner.advance`）、同一套预算、租约、
取消与产物发布；没有第二套模型循环。

## 6. 实际删除清单（脚本 / 夹具 / 文档）

判定标准：**先查调用方**（`rg`，排除 `node_modules/`、`output/`、`docs/`、自身），
只有"非文档代码零调用方 + 属于已结束实验或被当前实现取代"才删。
**没有删除任何用户数据、患者数据库、上传材料、有效证据、用户报告或凭据。**
**没有删除任何测试套件**（见 DECISION.md §5D 的更正：这套测试经逐个体检是强的）。

**脚本（43 个，`scripts/` 从 47 → 6，其中 2 个是本轮新增）**
`a5-demo-api.py`、`agent-capability-ablation.py`、`agent-capability-demo.js`、
`create-product-ocr-samples.py`、`diag-evidence-loop.py`、`harness-browser-acceptance.js`、
`latency-baseline.py`、`latency-diagnostic.py`、`material-review-browser-acceptance.js`、
`material-review-conditions-browser.js`、`material-review-conditions-demo.py`、
`material-review-conditions-live.py`、`material-review-coverage-live.py`、
`material-review-demo.py`、`material-review-live-acceptance.py`、`model-qualification-probe.py`、
`model-tool-controlled.py`、`model-tool-product-acceptance.py`、`planner-wire-probe.py`、
`product-full-browser-acceptance.js`、`product-live-api-acceptance.py`、
`product-live-browser-acceptance.js`、`product-live-cold-detector.py`、
`product-live-extractor-diagnostic.py`、`product-live-question-browser.js`、
`product-live-question-final-browser.js`、`product-p1-browser-acceptance.js`、
`retrieval-feedback-*.py`（4 个）、`run-a5-live.py`、`run-live-layer-c.sh`、
`run-material-review-browser.js`、`run-planner-live-acceptance{,-v3}.py`、
`tool-history-{capture,controlled}.py`、`analyze-{model-tool-controlled,retrieval-feedback,tool-history}.py`

**`stage0/` 入口与夹具（15 个）**
`agent_closeout_fixture.py`、`eval_negative.py`、`harness/delegation.py`、
`harness_browser_fixture.py`、`harness_p2_eval.py`、`harness_p3_eval.py`、
`make_negatives.py`、`material_review_browser_fixture.py`、`product_live_acceptance.py`、
`product_p1_browser_fixture.py`、`product_quality_eval.py`、`run_harness_acceptance.py`、
`run_heldout_v2.py`、`run_product_acceptance.py`、`test_harness_p3.py`

**死脚手架与过期文档（9 个）**
`main.py`（与 `stage0/server.py` 无关的 FastAPI Hello World，无人 import）；
`框架集成与生产可靠性升级方案{,Prompt}.md`、`生产化与亮点升级设计{,Prompt}.md`、
`记忆模块升级设计{,Prompt}.md`、`真实产品前端生成Prompt.md`、`Agent Harness分级改造Prompt.md`。

**为什么不影响核心功能或用户数据**

- 每一批删除后都验证了导入、构建与相关回归；最终冻结树上 723 测试 / 0 失败。
- 删除的是**执行入口**，不是能力：确定性引擎、记忆与依赖索引、证据库、任务与预算、
  材料核对核心全部保留；被删实验的**结论**留在 `docs/` 的 `RESULT.md` 里，
  并由 [HISTORY-INDEX.md](HISTORY-INDEX.md) 建了索引。
- 唯一受影响的测试是被删入口**自己**的套件（`test_harness_p3.py` 属于 P3 实验本体）；
  其他测试只移除了对已删入口的引用，断言一条没少。

**一处真实的覆盖变化，如实记录**：`test_harness_acceptance.py` 里的
`test_p2_aggregation_is_median_and_safety_checks_every_repeat` 随 `harness_p2_eval.py`
一并移除。它测的是那次 P2 优化实验的**度量入口**（中位数聚合与采纳门槛），
不是运行时机制——运行时的 P2 机制（受控复用、无进展检测）在 `harness/reuse.py` 与
`harness/progress.py` 里，仍由 `test_harness_p2.py` 覆盖。所以失去的是"对一次已结束实验的
度量能力"，不是"对现有行为的安全覆盖"。**如果不认可这个判断，恢复方式是从 git 历史取回
`harness_p2_eval.py` 与该用例。**

**`stage0/REPORT.md` 的陈旧命令**：该文件是 2026-08-27 的历史报告，正文里仍写着
`python stage0/make_negatives.py` 与 `python stage0/eval_negative.py`。已在其开头加历史标注，
指出现行入口在哪；正文数字保持原样，不改写历史记录。

**一处需要你知道的情况**：清理过程中出现过一个文件
`scripts/material-review-conditions-demo.py`，**不是本会话创建的**（它的 mtime 落在本会话
进行期间，但我和清理子代理都没有写过它）。它的名字与 `material-review-demo.py` 高度相似、
属于同一批"功能重叠的 demo"，且 `rg` 显示除它自己以外没有任何引用，因此按既定类别删除。
它当时**未被 git 跟踪**，所以无法从版本历史恢复。如果那是另一个并行会话的工作，
这是一个真实的损失——本项目此前也出现过并行会话互相覆盖工作区的事故，建议合并前确认。

## 7. 合并与保留清单

**被复用的既有设施（没有另建一套）**

| 设施 | 谁在用 |
|---|---|
| `product_objects` 事务与回执（`ProductStore.command`） | 安全事项的建立/更新/处置——重放不会加倍 |
| `conclusions` + `conclusion_dependencies` | 事项的**唯一**真相源；事项只存 ref |
| `dependency_tasks` + `recheck_pending` + 租约 | 失效重查；本轮把它的 hook 从"必须由 agent 安装"改成"没有 agent 也有确定性默认" |
| `care_task` 生命周期（预算/租约/取消/产物发布） | `safety-case@1` 与 `evidence_review@1`、`material-review@2` 共用同一套 |
| `run_open_review` / `ReviewRunner.advance` | 两个调查引擎，没有第三个模型循环 |
| `outbox_tasks` / `workflow_runs` / `run_cancel_requests` / `resume_tasks` | 事件执行、进展、取消、复核恢复 |
| `EvidenceStore` + `read_evidence` | 授权原文读取，事务与作用域不变 |

**只为持久任务兼容而保留的旧路径**

- `reconcile_material@2`、`current_medications@1`、`visit_summary@1` 三个旧契约原样保留并
  继续可执行；新代码不写入它们，但读得到、恢复得了。
- 旧 `care_task` / `case` / `summary` / `report` 行**不迁移、不删除**；
  各自的 `contract_version` / `review_contract` 独立校验，版本不符即拒绝继续，
  不让一个契约的版本号冒充另一个。
- `stage0/eval_memory.py` 保留为**非默认工具**：`selective_invalidation` 的当前默认值
  由它的消融结果支撑。

## 8. 最重要的剩余问题

1. **开放式调查里模型产出为零**（本次 n=1：0 条补问、`no_progress`）。
   **不阻断主线**：必要检查、事项与处置依据都由代码保证，模型不可用时主线仍然成立。
   但它决定了"Agent 能替用户查清多少"的上限。
2. **材料核对的交付恒为 `partial`，且真实模型从不主动外部取证**（沿用上轮结论，本轮未改）。
   **不阻断主线**：材料已是支持入口；但"材料能不能自动核对完"仍未解决。
3. **没有后台 worker 时一切都不会自动推进**（`necessary_checks`、`dependency_tasks`、`outbox_tasks`）。
   **不阻断**（界面明说，且用户主动操作时同步执行），但"长期跟进"目前仍依赖有人把服务跑起来。
