# 结果：自主调查闭环与就诊准备报告 · 2026-09-12

**一句话结论**：工程改造全部落地并有测试锁定；**模型路径任务质量 5/12，未优于离线脚本替身（12/12），也尚未证明对固定流程（0/12）的增益是"规划"带来的而不只是"多给了材料工具"**。真实瓶颈是 **7B 模型在 60% 的任务上根本没去读材料索引**——它把预算耗在重复 `memory_read` 上。

**不达标，如实记录。**

---

## 一、修改内容与关键代码位置

| # | 改动 | 位置 |
|---|---|---|
| 1 | 归因五分类：`llm` / `llm_post_correction` / `system_forced` / `fallback` / `deterministic`（+`rejected`），并单独记录 `hydrated_arguments`、`dropped_calls` | `stage0/agent.py` `HybridPlanner._trace`、`MedicationCoordinatorAgent._system_forced_action`、`_decide` |
| 2 | **堵住纠错上下文泄露**：`correction_for()` 只返回约束（允许工具、open gap、终止状态、拒绝原因），删除 `next_expected_action_hint` | `stage0/agent.py` `HybridPlanner.correction_for` |
| 3 | `next_action()` 职责四分：`forced_stop()` / 模型策略 / `degraded_next_action()` / `observe·sync_authority` | `stage0/investigation.py` |
| 4 | **子问题归模型**：`subquestions` 缺口 + `plan_questions` 工具 + `accept_questions` 校验（实体须在范围内、必须覆盖权威药单、不得含诊断/处方措辞） | `stage0/investigation.py`、`stage0/harness/default_tools.py`、`stage0/harness/tools.py` |
| 5 | **材料可见**：`MaterialIndex` 只读适配器 + `list_materials` / `read_material_item` 两个只读工具，材料差异进入报告 | `stage0/product.py`、`stage0/harness/default_tools.py`、`stage0/agent.py:attach_material_index`、`stage0/care_tasks.py:_execute_evidence_review` |
| 6 | **报告五问 + 证据支持检查**：`verify_statements()`，未回读的引用不构成引用；未核实的解释降为待确认 | `stage0/investigation.py:verify_statements/report_text/_render_statement` |
| 7 | 12 个成对任务 + 三臂评测器 | `stage0/agent_evals/visitprep_dev.json`、`stage0/agent_evals/run_visitprep.py` |
| 8 | 本轮回归与归因测试（45 条） | `stage0/test_agent_visit_prep.py` |

**顺带修掉的两个真实缺陷**（不是重构，是 bug）：

- **过早宣布完成**：`forced_stop()` 只在**每条已捕获证据都回读过**时才允许 `checks_completed`。改造前 `material_conflict` 族会用"读到第 1 条就支持"结束，把第 2 条**相反**证据留在未读状态——一个反对来源就能被"已完成"挡住。
- **一个措辞不当的子问题会终止整轮核查**：改为记录可修订的缺口（最多 3 次），模型可改后重提。

**持久化形状只增不改**：`VERSION`/`CONTRACT` 未 bump，旧 `evidence_review` 待办可直接恢复（新字段取默认值）；新增测试锁定。

## 二、可操作的演示入口

产品入口是**已默认开启**的 `/tasks` 页面（不是评测脚本）：

1. `python -m uvicorn stage0.server:app --host 127.0.0.1 --port 8000` + `cd frontend && npm run dev`
2. `/materials` 上传一份用药 CSV（模板见 `product.py:FIELDS`）
3. `/tasks` → 「发起一个开放证据核查」，goal 填「我下周去看心内科，帮我把这份材料和现在的用药对一下」
4. 系统自动挂载 `MaterialIndex` 并走 `run_open_review`；报告落 `investigation_report` 产物，`GET /v1/investigation-reports/{id}` 可取

助手页自由问答的 `AGENT_INVESTIGATION_ENABLED` **保持关闭**（既有产品决定，本轮未改）。演示只走 `/tasks`。

## 三、验收命令与产物

```bash
# 离线三臂（零远程调用）
.venv/Scripts/python.exe -m stage0.agent_evals.run_visitprep --arm fixed    --out output/visit-prep-2026-09-12/offline-fixed.json
.venv/Scripts/python.exe -m stage0.agent_evals.run_visitprep --arm det      --out output/visit-prep-2026-09-12/offline-det.json
.venv/Scripts/python.exe -m stage0.agent_evals.run_visitprep --arm scripted --out output/visit-prep-2026-09-12/offline-scripted.json

# 真实模型（受 --call-cap 400 硬上限；端点由 assert_live_authorized 白名单把关）
.venv/Scripts/python.exe -m stage0.agent_evals.run_visitprep --arm model --live --call-cap 400 \
    --out output/visit-prep-2026-09-12/live-model-2.json

# 本轮回归
.venv/Scripts/python.exe -m unittest stage0.test_agent_visit_prep -v
```

端点（产物内 `planner_endpoint`，不含密钥）：`siliconflow` / `Qwen/Qwen2.5-7B-Instruct` / `https://api.siliconflow.cn/v1`。
实际消耗 **104 次规划调用**（上限 400，未触顶）。全部 12 个任务都被采样，无 `not_sampled`。

## 四、任务质量对照（逐条，不用百分位）

| 臂 | 通过 | 规划调用 | 单任务墙钟 | 读到材料索引 | 读到且通过 |
|---|---|---|---|---|---|
| **A `fixed`** HEAD 固定流程 | **0/12** | 0 | 0.1 s | 0 | 0 |
| **A′ `det`** 确定性规划 + 挂材料 | **0/12** | 0 | 0.1 s | 0 | 0 |
| **B′ `scripted`** 模型路径的离线替身 | **12/12** | 0 | 0.1 s | 12 | 12 |
| **B `model`** 真实模型（第 1 批） | **4/12** | 102 | 6.3–30.4 s | 6 | 4 |
| **B `model`** 真实模型（第 2 批，修多调用截断后） | **5/12** | 104 | 6.6–40.1 s | 6 | 5 |

逐条（第 2 批）：

| 任务 | 结果 | 墙钟 | 终止原因 | 读了材料 |
|---|---|---|---|---|
| vp-full-001a | PASS | 17.5 s | budget_insufficient | ✓ |
| vp-full-001b | PASS | 19.8 s | budget_insufficient | ✓ |
| vp-missing-002a | fail `material_issue_not_reported` | 20.3 s | budget_insufficient | ✓ |
| vp-missing-002b | fail `material_index_never_read` | 11.5 s | no_progress | ✗ |
| vp-conflict-003a | fail `material_index_never_read` | 9.7 s | no_progress | ✗ |
| vp-conflict-003b | fail `material_index_never_read` | 20.0 s | no_progress | ✗ |
| vp-diff-004a | PASS | 17.4 s | checks_completed | ✓ |
| vp-diff-004b | PASS | 23.5 s | checks_completed | ✓ |
| vp-noresult-005a | fail `material_index_never_read` | 12.3 s | no_progress | ✗ |
| vp-noresult-005b | fail `material_index_never_read` | 12.3 s | no_progress | ✗ |
| vp-distract-006a | fail `material_index_never_read` | 6.6 s | no_progress | ✗ |
| vp-distract-006b | PASS | 40.1 s | budget_insufficient | ✓ |

**跨两批稳定成立的唯一强相关：读到材料索引的任务 6/6 全通过；没读到的 6 个全部失败。**

### 归因四类（第 2 批，逐次计数）

| 类别 | 次数 |
|---|---|
| 模型选择（`llm`，未经纠正、无参数代填） | 33 |
| 模型纠错（`llm_post_correction`） | 10 |
| 系统强制操作（`system_forced`） | 0 |
| 策略降级（`fallback`） | 22 |
| 确定性（未启用模型规划器） | 2 |
| 提案被拒（`rejected`） | 10 |
| 代码代填决定性参数（`hydrated_arguments`） | 0 |

安全校验、权限检查、持久化**不计入**降级。`dropped_calls` 单独记录：一个响应里多出的工具调用被丢弃时逐条可见（如 `['list_materials']`、`['list_materials(repeat)']`）。

### 延迟口径（三行分开）

- **成功回合**：4 个通过任务的墙钟 17.4 / 17.5 / 19.8 / 23.5 s（`checks_completed` 者 17.4–23.5 s）
- **全部回合**：6.6–40.1 s（12 个任务全部完成，无崩溃、无超时）
- **超时 / 失败**：超时 **0**；网关错误 0；任务级失败 7 个（均为契约未达成，非执行错误）

样本量 12，**不报百分位、不宣称生产稳定性**。

## 五、"模型调整后改善"的案例

**发现 1 个（有 trace 证据），另 1 个未发现。**

**案例（成立）——拒绝反馈驱动自我修正，12 个任务里出现 10 次。**
模型首轮提出 `memory_read(query="current_medications")`，被 `authority_requires_full_memory_read` 拒绝；**下一轮它把 query 改成了 `snapshot`**，被接受并关闭 authority 缺口。trace 里这一条记为 `llm_post_correction`（不是因为照抄了代码给的动作——那正是本轮删掉的 `next_expected_action_hint`；纠错上下文现在只给约束）。固定流程没有这个回路：它的 `memory_read` 由代码写死，不存在"改对"这回事。

**案例（未发现）——纯观察驱动的策略调整。**
我期望看到"首轮检索无结果 → 模型改写查询 → 命中"或"发现材料差异 → 决定回读该条 → 报告引用它"。离线替身能走通这条路径（`vp-noresult-*`、`vp-missing-*` 族 12/12），**但真实模型批次里没有出现**：`vp-noresult-005a/b` 两个任务模型都没读到材料，`no_progress` 结束。**如实报告：未发现**。

## 六、已完成 / 失败 / 未验证

**已完成并验证**
- 归因五分类 + `hydrated_arguments` + `dropped_calls`，纠错上下文不再泄露动作与参数（`test_agent_visit_prep` 45 条 + 既有 354 条回归全绿）
- `forced_stop` / 模型策略 / 降级策略 / 状态同步四分；持久化只增不改，旧状态可恢复
- 子问题归模型：`plan_questions` 校验拒绝"造药名""漏覆盖""处方措辞"
- 材料索引与材料读取工具接入产品路径 `/tasks`
- 报告五问 + `verify_statements()` 证据支持检查
- 12 个成对任务（6 族 × 2，成对任务的 goal 与患者事实逐字一致、只改证据）+ 三臂评测器，评分只读 `expected`，**无按 task_id/族名分支**（有测试锁定）
- 真实模型批次在 400 次调用上限内完成，端点已记录

**失败（未达成）**
- **模型路径 5/12，未达"用任务质量验收"的通过线，也未优于固定流程到可证明的程度**——真实瓶颈是模型不探索工具集
- `vp-missing-002a`：读到了 `unresolved` 差异，却没把"缺少剂量单位"写进报告
- `vp-conflict-003*`：模型完全没读材料，两个冲突任务都失败（固定流程的离线对照也失败，但原因不同）
- 第 1→2 批的改进只有 +1 个任务，**不能称为稳定增益**

**未验证**
- **独立 held-out 仍然缺失**（`product_evals/tasks/held_out/` 为空）。本轮 12 个任务是我自己写的开发集，参与过调试，**不构成泛化主张**
- `frontend/` 一轮未动：报告新分节的界面渲染未做浏览器验收
- 离线替身 12/12 **只证明契约可满足**（状态机与执行约束），**不证明模型规划能力**；其中 15 次 `policy_fallback` 说明部分周期是确定性策略走完的
- 成本：只统计了调用次数（104），未取到 token 计量；无 p50/p95

## 七、是否产生可证明的 Agent 增益

**部分产生，但不能归因到"自主规划"。**

可证明的：
1. **固定流程在 12/12 个任务上读到材料索引 0 次**——即使把材料工具挂上（`det` 臂），确定性规划器也从不选择它们。这是一个**能力增益**，不是规划增益：模型路径至少能（6/12 次）看到材料差异。
2. **纠错回路真实工作**：10 次拒绝 → 10 次修正后通过，且归因与"一次就对的规划"分开计数。

不能说成立的：
3. 模型路径 5/12 vs 固定流程 0/12，差额主要来自**新增的材料可见性**。`det` 臂（有材料可见性、无模型规划）也是 0/12，说明"看得到"还不够，"选择去看"才是分水岭——而 7B 模型在 6/12 的任务上没做出这个选择。

**因此：保留已验证的工程改进（归因、去泄露、过早完成修复、材料可见性、证据支持检查），明确剩余瓶颈是模型的选择能力，不通过扩大兜底、修改成功定义或挑选样本宣布完成。**

## 八、剩余瓶颈与下一步建议（不本轮实施）

1. **模型不探索工具集**是主要瓶颈。可选：更强的模型（端点已换成 Qwen2.5-7B，回退是一行配置）；或在提示中把 `list_materials` 提为"用户问材料时必须先调用"——但那是把策略写回提示，需用同一套成对任务验证它是否真的改善，不能默认有效。
2. **重复 `memory_read` 浪费预算**：`_first_unexecuted` 只处理"一个响应内多调用"，跨周期重复读取仍靠 no-progress 兜底。可考虑把重复读取的反馈更早送进下一轮 payload。
3. **未发现纯观察驱动的策略调整**，需要更大样本和更强的模型才有机会观察到；12 个任务在 7B 上不足以支撑泛化结论。

## 九、口径声明

- 12 个任务是**合成开发集**，作者自写、参与调试，**不是独立 held-out**。
- 成对任务的 goal 与 `initial_state` **逐字一致**，只改证据；有测试锁定。
- 评分规则先定后跑，写在任务 JSON 的 `expected` 里；评测器不识别任何具体任务。
- 工具调用次数与路径相似度**不计入**成功指标；合法的不同顺序不判失败。
- 所有尝试（含失败、崩溃、`no_progress`）都进入统计，没有剔除任何一轮。
