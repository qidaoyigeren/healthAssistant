# 结果：自主调查闭环与就诊准备报告 · 2026-09-12

> **本文件已按后续测量更正。** 初版把瓶颈写成"7B 模型不探索工具集"。加上错误类型采集后，证据指向**网关 429 限流**才是绑定性约束，那条结论已降级。更正过程与证据保留在第九节。

**一句话结论**：工程改造全部落地并有测试锁定。任务质量上，**材料可见性是决定性的且与其他因子是"合取"关系**——六个臂里，凡是没挂材料的（无论谁规划）一律 **0/12**；挂了材料的，脚本规划器 **12/12**、真实模型 **2/12–5/12**。真实模型那一档的分差**主要由网关限流造成**（单批 78/101 次调用被 429 拒绝），不是规划能力。

**仍未达标，如实记录。**

---

## 一、修改内容与关键代码位置

| # | 改动 | 位置 |
|---|---|---|
| 1 | 归因五分类：`llm` / `llm_post_correction` / `system_forced` / `fallback` / `deterministic`（+`rejected`），另记 `hydrated_arguments`、`dropped_calls` | `stage0/agent.py` `HybridPlanner._trace`、`_system_forced_action`、`_decide` |
| 2 | **堵住纠错上下文泄露**：`correction_for()` 只给约束，删除 `next_expected_action_hint` | `stage0/agent.py` `HybridPlanner.correction_for` |
| 3 | `next_action()` 职责四分：`forced_stop()` / 模型策略 / `degraded_next_action()` / `observe·sync_authority` | `stage0/investigation.py` |
| 4 | **子问题归模型**：`subquestions` 缺口 + `plan_questions` + `accept_questions` 校验（实体在范围内、覆盖权威药单、禁诊断/处方措辞） | `stage0/investigation.py`、`stage0/harness/default_tools.py`、`stage0/harness/tools.py` |
| 5 | **材料可见**：`MaterialIndex` + `list_materials` / `read_material_item`，差异进入报告 | `stage0/product.py`、`stage0/harness/default_tools.py`、`agent.attach_material_index`、`care_tasks._execute_evidence_review` |
| 6 | **报告五问 + 证据支持检查**（`verify_statements`） | `stage0/investigation.py` |
| 7 | 12 个成对任务 + **六臂**评测器（2×2 因子网格 + 固定流程） | `stage0/agent_evals/visitprep_dev.json`、`run_visitprep.py` |
| 8 | 逐次 provider 账本：异常类型、outcome、**每次调用**延迟 | `run_visitprep._provider_failures` |
| 9 | 本轮回归（48 条） | `stage0/test_agent_visit_prep.py` |

**顺带修掉的两个真缺陷**（不是重构，是 bug）：

- **过早宣布完成**：`forced_stop()` 只在**每条已捕获证据都回读过**时才允许 `checks_completed`。改造前 `material_conflict` 族读到第 1 条就收工，把第 2 条**相反**证据留在未读状态——一个反对来源被"已完成"挡住。
- **一个措辞不当的子问题终止整轮核查**：改为记录可修订的缺口（上限 3 次）。

**持久化形状只增不改**：`VERSION`/`CONTRACT` 未 bump，旧 `evidence_review` 待办可直接恢复。

## 二、可操作的演示入口

产品入口是**已默认开启**的 `/tasks`（不是评测脚本）：

1. `python -m uvicorn stage0.server:app --host 127.0.0.1 --port 8000` + `cd frontend && npm run dev`
2. `/materials` 上传一份用药 CSV
3. `/tasks` →「发起一个开放证据核查」，goal 填「我下周去看心内科，帮我把这份材料和现在的用药对一下」
4. 报告落 `investigation_report` 产物，`GET /v1/investigation-reports/{id}` 可取

助手页自由问答的 `AGENT_INVESTIGATION_ENABLED` **保持关闭**（既有产品决定）。演示只走 `/tasks`。

## 三、验收命令与产物

```bash
# 离线六臂（零远程调用，可直接复现）
foreach ($a in @('fixed','det-nomat','det','scripted-nomat','scripted')) {
  .venv/Scripts/python.exe -m stage0.agent_evals.run_visitprep --arm $a --out output/visit-prep-2026-09-12/grid-$a.json }

# 真实模型（--call-cap 400 硬上限；端点由 assert_live_authorized 白名单把关）
.venv/Scripts/python.exe -m stage0.agent_evals.run_visitprep --arm model --live --call-cap 400 \
    --out output/visit-prep-2026-09-12/live-model-3.json

.venv/Scripts/python.exe -m unittest stage0.test_agent_visit_prep -v
```

端点（产物 `planner_endpoint`，不含密钥）：`siliconflow` / `Qwen/Qwen2.5-7B-Instruct` / `https://api.siliconflow.cn/v1`。

## 四、任务质量对照（逐条，不用百分位）

### 因子网格（每格 12 个任务）

| 臂 | 调查契约 | 规划器 | 材料可见 | 通过 |
|---|---|---|---|---|
| `fixed` | 关 | 确定性（HEAD 固定流程） | ✗ | **0/12** |
| `det-nomat` | 开 | 确定性 | ✗ | **0/12** |
| `det` | 开 | 确定性 | ✓ | **0/12** |
| `scripted-nomat` | 开 | 脚本替身 | ✗ | **0/12** |
| `scripted` | 开 | 脚本替身 | ✓ | **12/12** |
| `model-nomat` | 开 | **真实模型** | ✗ | **0/12** |

**读法**：材料单独 **0→0**（`det-nomat`→`det`）；规划器单独 **0→0**（`det-nomat`→`scripted-nomat`）；**两者同时**才 **0→12/12**。增益是**合取**的，不是相加的。**六个臂里没有材料的四个全部 0/12**——这是本轮最稳的一条结论。

### 真实模型批次（同一端点、同一模型、同一代码）

| 批次 | 通过 | 规划调用 | 成功响应 | 被 429 拒绝 | 调用延迟中位数 |
|---|---|---|---|---|---|
| `live-model`（第 1 批） | 4/12 | 102 | — | — | — |
| `live-model-2`（第 2 批） | 5/12 | 104 | — | — | — |
| `live-model-3`（加错误类型后） | **2/12** | 101 | **23** | **78 (77%)** | 90 ms |
| `live-model-nomat` | **0/12** | 88 | 81 | 7 (8%) | 2533 ms |
| `live-model-pace`（2 个任务，间隔后重跑） | **2/2** | 22 | 9 | 13 (59%) | 102 ms |

**同一份代码、同一个模型、同样两个任务**（`vp-full-001a`、`vp-diff-004a`）：在 `live-model-3` 里都失败（没读到材料），在 `live-model-pace` 里**都通过**（读到了材料）。唯一变化是拿到响应的比例从 23% 升到 41%。→ **限流是本轮的绑定性变量。**

`live-model-nomat` 的对照价值最高：它**没被限流**（8%），模型有充足预算，仍然 **0/12**——因为材料工具根本没注册。这证明"模型能力"在无材料时不是瓶颈，也证明材料可见性不是可选项。

## 五、Provider 账本（本轮新增的测量）

```
live-model-3   : outcomes {response: 23, rate_limit: 78}   失败类型 {RateLimitError: 78}   中位 90 ms
live-model-nomat: outcomes {response: 81, rate_limit: 7}    失败类型 {RateLimitError: 7}    中位 2533 ms
```

**没有一次超时。** 失败全是 `RateLimitError`，中位延迟 ~90 ms 是**快速拒绝**的特征，不是推理慢。这直接推翻了我初版里"模型把预算烧在重复读取上"的因果方向——那些重复读取是限流之后**只剩少量成功响应**的结果，不是原因。

`PLANNER_PROVIDER_RETRIES` 默认 1，429 会走一次有界退避重试；**在 77% 拒绝率下一次重试远远不够**（重试后仍失败才落成 `provider_error`）。

## 六、归因四类（第 2 批，逐次计数）

| 类别 | 次数 |
|---|---|
| 模型选择（`llm`，未纠正、无代填） | 33 |
| 模型纠错（`llm_post_correction`） | 10 |
| 系统强制操作（`system_forced`） | 0 |
| 策略降级（`fallback`） | 22 |
| 确定性（未启用模型规划器） | 2 |
| 提案被拒（`rejected`） | 10 |
| 代码代填决定性参数 | 0 |

安全校验、权限检查、持久化**不计入**降级。`dropped_calls` 单独记录（如 `['list_materials']`、`['list_materials(repeat)']`）。

### 延迟口径（三行分开）

- **成功回合**：通过任务的墙钟 17.4–25.1 s
- **全部回合**：5.4–40.1 s（12 个任务全部完成，无崩溃、无超时）
- **超时 / 失败**：超时 **0**；429 造成的调用失败按上表逐次记账

样本量 12，**不报百分位、不宣称生产稳定性**。

## 七、"模型调整后改善"的案例

**发现 1 个（有 trace 证据），另 1 个未发现。**

**案例（成立）——拒绝反馈驱动自我修正，12 个任务里出现 10 次。**
模型首轮提 `memory_read(query="current_medications")`，被 `authority_requires_full_memory_read` 拒绝；**下一轮自己改成 `snapshot`**，被接受并关闭 authority 缺口。trace 记为 `llm_post_correction`——不是因为照抄了代码给的动作（那正是本轮删掉的 `next_expected_action_hint`）。固定流程没有这个回路：它的 `memory_read` 由代码写死，不存在"改对"这回事。

**案例（未发现）——纯观察驱动的策略调整。**
期望看到"首轮检索无结果 → 改写查询 → 命中"。离线替身能走通（`vp-noresult-*` 族），**真实模型批次里没有出现**。**如实报告：未发现。**

## 八、已完成 / 失败 / 未验证

**已完成并验证**
- 归因五分类 + `hydrated_arguments` + `dropped_calls`；纠错上下文不再泄露动作与参数（48 条新测试 + 既有 354 条回归全绿）
- 职责四分；持久化只增不改，旧状态可恢复
- 子问题归模型（`plan_questions` 在 11/12 个任务上被调用——**缺口驱动确实有效**）
- 材料索引与读取接入产品路径
- 报告五问 + 证据支持检查
- 12 个成对任务（成对任务 goal 与患者事实逐字一致、只改证据），评分只读 `expected`、**无按 task_id/族名分支**（有测试锁定）
- **六臂因子网格**，把"材料可见性"与"谁来规划"分开
- provider 账本：按异常类型、按**每次调用**记账

**失败（未达成）**
- 真实模型最好一批 5/12，**未达通过线**
- `vp-missing-002a`：读到 `unresolved` 差异，却没把"缺少剂量单位"写进报告
- 未见"纯观察驱动"的策略调整

**未验证**
- **限流构成未定**：无法区分是"按分钟配额（被我连续跑批次耗尽）"还是"账号硬上限"。跨批次方差（8% vs 77%）倾向于前者，但**未验证**
- **独立 held-out 仍然缺失**（`product_evals/tasks/held_out/` 为空）。12 个任务是自写开发集、参与过调试，**不构成泛化主张**
- `frontend/` 未动：报告新分节的界面渲染未做浏览器验收
- 离线替身 12/12 **只证明契约可满足**，不证明模型规划能力
- 成本：只统计调用次数，未取到 token 计量；无 p50/p95

## 九、更正记录

初版结论把瓶颈归为"7B 模型不探索工具集：60% 任务没调 `list_materials`，预算烧在重复 `memory_read` 上"。

更正依据：
1. 初版只采集了错误 **code**（`provider_error`），没采集异常**类型**——我当时的诊断建立在猜上面。
2. 补上类型后：`live-model-3` 的 101 次调用里 **78 次是 429**，且**零超时**；中位延迟 90 ms 是快速拒绝。
3. `live-model-nomat` 几乎没被限流（7/88），模型有充足预算，仍 **0/12**——说明"模型不探索"解释不了这一格。
4. **定案测试**：间隔后重跑同样两个失败任务 → **2/2 通过**，只有成功响应比例变了。

原结论**降级**为一个待验假设（模型在预算紧张时的工具选择偏好），并**不是**当前的主要瓶颈。

## 十、是否产生可证明的 Agent 增益

**可证明的：**
1. **材料可见性是合取性必要条件**：六个臂中无材料的四个一律 0/12（含真实模型、含脚本替身）。这不是"锦上添花"，是"有没有"。
2. **固定流程在本任务族的通过率是 0/12**，且它读材料索引 **0 次**（即使把工具挂上）。
3. **纠错回路真实工作**：10 次拒绝 → 10 次改对，且与"一次就对的规划"分开计数。

**不能说的：**
4. "模型路径优于固定流程"——在限流未排除前，真实模型那一档的数字**不可用于比较**，因为三个批次拿到的成功响应比例差异巨大（23%–92%）。
5. 更不能说成"规划增益"：`det` 臂（有材料、无模型规划）也是 0/12，说明**看到**不够；但 `scripted` 12/12 说明**有规划器去选**就够。真实模型处在这两者之间，而它的分差被限流污染。

**因此：保留已验证的工程改进（归因、去泄露、过早完成修复、材料可见性、证据支持检查、provider 账本），明确剩余瓶颈是限流与模型选择能力，不通过扩大兜底、修改成功定义或挑选样本宣布完成。**

## 十一、剩余瓶颈与下一步

1. **先排除限流，否则任何模型侧结论都不可信**（最高优先）。做法：查端点配额；把批次切成带间隔的节奏跑；把 `PLANNER_PROVIDER_RETRIES` 与退避上限按实测恢复时间调整（既有退避上限 20 s，而本项目记录过的恢复时间 ~61 s）。**不要**改成无限重试。
2. **用缺口驱动材料读取**。数据支持：`plan_questions` 靠缺口驱动，11/12 被调用；`list_materials` 只是可选工具，`det` 臂 0 次选中。同一个杠杆，接线方式不同。
3. **拒绝"不可能推进任何缺口"的重复动作**（按缺口状态判定，不按次数），接进已证明有效的纠错回路。
4. **换更大模型**：端点回退是一行配置，但必须先用同一套成对任务、在**限流已排除**的条件下验证。
5. **扩样本 + 独立 held-out**：12 个自写任务不足以支撑泛化结论。

## 十二、口径声明

- 12 个任务是**合成开发集**，作者自写、参与调试，**不是独立 held-out**。
- 成对任务 goal 与 `initial_state` **逐字一致**，只改证据；有测试锁定。
- 评分规则先定后跑，写在任务 JSON 的 `expected` 里；评测器不识别任何具体任务。
- 工具调用次数与路径相似度**不计入**成功指标；合法的不同顺序不判失败。
- 所有尝试（含失败、崩溃、`no_progress`、429）都进入统计，没有剔除任何一轮。
- 被限流污染批次的数字**与其对照臂并排呈现**，不单独引用。
