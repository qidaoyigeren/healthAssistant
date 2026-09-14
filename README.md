# 用药协管员：长期用药安全跟进

**记住父母的用药与相关背景，在变化发生时执行必要安全检查，并围绕产生的安全事项持续跟进到有依据的处置。**

## 产品主线

```
用药或背景变化
 → ① 接受并持久化有依据的事件
 → ② 程序执行必要安全检查            （代码，不依赖模型是否选中工具）
 → ③ 建立或更新持久化安全事项 SafetyCase
 → ④ Agent 调查与补问
 → ⑤ 用户或专业人员提供补充
 → ⑥ 恢复同一事项并重新核对
 → ⑦ 更新有依据的处置状态
 → ⑧ 后续变化时重新打开受影响事项
```

Agent 的角色是**持续跟进用药安全事项的调查协调者**。它不决定是否跳过必要检查、
不修改权威药单、不自行诊断或调整用药，也不凭自己的判断静默关闭风险。

### 一件事是怎么被推进的

Agent 从**一件具体事项**开工，拿到的是事项上下文（为什么有这件事、相关记录、当前结论
及它们的**触发条件状态**、未决问题与已收到的回答、上次做到哪、这次新增了什么），
不是一份患者档案复述。每一步决策留下五个结构化字段：针对哪个未决问题、依据哪些已有
结论或证据、做什么动作、预期解决什么、什么结果会改变下一步。

补充回答分三类处理：**有内容地回答**关闭该问题；**明说不知道**结束这一轮追问、
但**不消除不确定性**（转去找替代证据，并且继续阻止关闭）；**空值**什么都不关。
回答记录当时看到的记录版本——事实一变，那条回答即失效、问题重新打开并写明原因。

材料解析、字段比较、原文读取和报告生成都是**支持能力**，服务于这条主线，
不再是独立扩张的产品中心。

## What it is / is not

It is a single-caregiver, single-patient engineering demonstration over a durable SQLite
record. The default path is deterministic and offline-first.

It is **not a medical device, diagnostic system, prescriber, or clinical decision-support
service**. It does not diagnose, prescribe, or tell anyone to start, stop or change a
medicine. Severe, uncertain, uncited and conflicting results are escalated to a doctor or
pharmacist. **No real clinician service is connected** — the professional-review queue is a
clearly-marked local simulation and never auto-approves. All metrics here are
software-engineering measurements, not clinical validation.

## 架构

分工（职责划分，不是同名目录）：

| 职责 | 落在哪 |
|---|---|
| 用药与事实服务 | `memory.py`（`medications` 版本链、`semantic_memory`、`episodic_memory`） |
| 安全检查与结论 | `ddi_engine.py`（确定性检测）、`safety_checks.py`（必要检查队列与执行）、`conclusions` + `conclusion_dependencies` |
| 安全事项与生命周期 | `safety_cases.py` |
| 调查运行器与领域能力 | `agent.py`（`run_open_review`）、`investigation.py`、`review/` |
| 证据、处置与变化说明 | `harness/evidence.py`、`conclusions.source_refs`、`read_models.change_impact` |
| 材料输入适配 | `product.py`、`document_parser.py`、`review/coverage.py` |
| 产品界面与通知 | `frontend/`（`/` 与 `/safety` 是主线页）、应用内待办（无外部通知） |

### 必要安全检查不依赖模型

用药新增/停用/剂量变更，或安全相关的患者事实变化时，`MemoryStore` 在**同一个事务里**
登记一次必要检查（`necessary_checks`）；worker 用确定性检测器执行它，产出结论与依赖行，
再把结论收敛成安全事项。这条路径上**没有 planner、没有 provider**：agent 构造不出来或
供应商不可用时，检查照样跑完、事项照样建立。

去重键包含用药集合哈希，所以"同一个世界状态不重复检查"，而"世界变了"一定会再查一次——
事件去重不会吞掉新的安全提示。检查失败保持可见（重试后标记 failed），**绝不**读成"检查通过"。

### 安全事项：引用，不是第二个真相源

`SafetyCase` 只保存**引用**（`linked_conclusion_refs` / `evidence_refs` / `relevant_fact_refs`）
与过程（`open_questions` / `required_inputs` / `linked_run_ids` / `history` / `resolution_basis`），
不复制任何患者事实、用药值或风险等级。

生命周期状态与运行状态分开：`open` / `investigating` / `awaiting_user` /
`awaiting_professional` / `resolved` / `needs_recheck` / `execution_failed`。
**一次运行失败只把事项推进到 `execution_failed`，绝不关闭它。**

身份 `dedup_key = hash(scope, case_type, subject_keys, episode_anchor)`，其中
`episode_anchor` 沿用药版本链回溯到最近一次停用边界：剂量变更（`superseded`）仍是同一次
用药 → **更新既有事项**；停药后重新启用 → 新分期 → 新事项。所以同一风险每天复检不会
变成每天一张新卡片，而不同用药阶段不会因药名相同被错误合并。

### 处置依据由代码强制

**"检查做过了"不是关闭理由。** 一条 `current` 的风险结论只证明"检查跑了、风险还在"。
关闭必须现场证明**本事项的触发条件已经消除**——由依赖索引 × 当前权威记录确定性判定
（例如药物对的一方已停药），而不是对结论文本做关键词匹配。`closure_evidence` 同时要求：
结论版本适用于当前状态、关联结论里没有一条"触发条件仍成立"、没有阻塞性未决问题。

三种动作语义分开：

- `resolved_with_basis`——**关闭**，需上述证明，或一条**适用且允许完成**的
  `professional_review_applied` 决定；
- `escalated_to_professional`——交给专业人员（用户转述医生意见走这条，**不**关闭）；
- `accepted_monitoring`——风险仍在但已有安排，**持续跟进**，不是"等待专业人员"，
  也不是风险消失。没有可信时间/条件时记为**待确认的安排**，模型不能自己编一个复查周期。

明确拒绝：用户点"已读"就关闭（`mark_seen` 只写时间戳）、模型判断作为依据、
provider 失败后当作已处理、拿旧版本的复核批准新状态、用**别的**事项的复核决定关闭本事项、
把本地模拟工作台的复核包装成专业医疗确认。操作者身份与角色取自**认证上下文**，
不读请求体自报的 `actor`。

### 长期跟进由持久任务驱动

复用既有的 `outbox_tasks`（事件执行）、`dependency_tasks`（依据失效重查）、
`care_task`（跨会话调查，含累计预算与租约）、`resume_tasks`（复核恢复），
以及 `follow_up_runs`（长期跟进：到期或相关记录变化时触发一次调查）。
跟进队列挂在**同一个** `OutboxWorker` 周期里，排在 `necessary_checks` **之后**——
确定性检查总是先跑完，才轮到消费这一版模型调查。**没有新增调度框架。**

> **「已安排」与「已确认」是两件事。** 给一个复查时间**不等于**有人确认过：确认只能
> 由确认端点产生，并且留下 `confirmed_at` / `confirmation_ref`。系统里没有任何一条路径
> 会因为你填了时间就把一条安排记成"已确认"。
>
> 安排按 `schedule_state` 推进（`scheduled → due → triggered`，失败进 `blocked` 并带原因）；
> 取消**不清空**时间与条件，只把状态改成 `cancelled`。

> **部署要求**：后台 worker 未运行时，`necessary_checks`、`dependency_tasks`、
> `outbox_tasks` 与 `follow_up_runs` 都**不会自动推进**——只有用户主动操作时才同步执行。
> 进程内演示不构成离线持续服务能力。应用内会常驻显示检查队列的真实状态，
> 「读不到」与「没有问题」是两件事。

### 回访：回来跟进时，接着上次的事情继续

用户再次打开一件事项时，看到的是**一次回访**（页面入口「继续本次跟进 / 开始回访」，
子路由 `/safety/:caseId/visit`），按五步推进：这次为什么需要跟进 → 上次之后已记录
的变化 → 当前最需要回答的问题 → 回答之后的实际进展 → 本次结果及下一次安排。

- **接着上次走，不新开一次**。上一访没结束就继续它——否则用户已经答过的问题会变成
  上一访的遗留，他回来看到的第一题又是原来那道。
- **本次为什么跟进由服务端按事实判定**：到期 > 相关记录变化 > 收到新补充 > 用户主动。
  调用方不能把到期说成"用户主动发起"——那会让真正该跟进的那件事永远不算数。
- **"上次之后"用持久化游标**，不是"最后几条历史"。没有新记录时**只说**「系统尚未
  收到新记录；这不等于情况没有变化，也不表示风险已经解除」。
- **已有信息足够时不硬造问题**；问就只问最关键的 1～3 条，不要求复述整个用药情况。
- **五种回答含义不同，不能合并成「已解决」**：已完成 / 尚未完成 / 不清楚 / 情况有变化 /
  暂不回答。只有「已完成」且问题确实是跟进行动，才可能把那条问题答上；「暂不回答」与
  「不知道」不是一回事。
- **用药变更走候选确认**：用户声明的、模型从话里读出的，进的是同一个待确认队列，
  **来源在界面上可见**。确认之前当前药单一个字节都不动；确认后走既有的用药变更入口
  写入，必要安全检查按原有路径重新排队。
- 回访记录**只存引用与渲染后的叙述**（每条陈述标明是程序核对、用户报告、模型的解释
  还是权威记录），不复制患者事实、药单或证据。

## 运行

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r stage0/requirements-stage1.txt

# 后端（单写进程）
python -m uvicorn stage0.server:app --host 127.0.0.1 --port 8000

# 前端
cd frontend; npm install; npm run dev
```

页面入口：`/`（= `/safety`）长期用药安全事项；`/medications` 记录用药变化（变化会触发必要检查）；
`/materials` 材料核对（**支持入口**：导入材料 → 确认信息 → 关联用药与安全事项）；
`/tasks` 照护待办；`/alerts` `/conflicts` `/history` 证据、待核实与时间线。

没有后台 worker 时，页面仍会显示已保存的一切，但不会自动推进。

## 验证

**一个默认入口**：

```powershell
python scripts/verify-agent-closeout.py --out output/<新的目录>
```

它跑全部 `stage0/test_*.py`、冻结的开发集评测，以及**主线产品验收**
（`stage0/test_safety_mainline_e2e.py`）。输出**按类别分开报告**，不合并成一个通过率：

| 类别 | 回答的问题 |
|---|---|
| `necessary_checks_completed` | 必要检查是否执行完毕 |
| `cases_created_and_updated` | 安全事项是否正确建立与更新 |
| `investigation_made_progress` | 调查是否取得有效进展 |
| `waiting_and_disposition_states_correct` | 等待与处置状态是否正确 |
| `model_and_program_separated` | 模型和程序分别做了什么 |
| `requests_failures_and_cost_traceable` | 请求、故障与成本是否可追溯 |

目录不可复用（拒绝覆盖既有验收，防止用新结果顶替失败证据）；`source_unchanged` 比对
运行前后的源码指纹，运行中被改动即判 fail。**默认全部离线**，不需要任何凭据。

浏览器验收（真实 Chromium 走真实页面；后端是隔离合成库、**不调模型**）：

```powershell
node scripts/safety-mainline-browser-acceptance.js --out output/browser-<日期>
```

可操作的主线演示（隔离临时库，打印页面真正读到的东西）：

```powershell
python scripts/safety-mainline-demo.py
```

有限真实模型验收（**默认不跑**，需已配置的供应商凭据，上限在开跑前打印）：

```powershell
python scripts/safety-mainline-live-acceptance.py --out output/<新的目录>.json --max-calls 6 --wall-seconds 180
```

## 当前能力边界

**已经由代码可靠执行**：用药与事实的版本化记录；必要安全检查及其去重、重试与可见失败；
结论与依赖索引；依据失效与持久化重查；安全事项的建立/更新/重开与处置依据校验；
权限、作用域、来源完整性、幂等、预算、取消与请求审计。

**仍依赖模型**（因此不作为安全保证）：开放式调查中"还缺什么信息"的判断、
材料差异的解释、跨来源矛盾的分析。模型不可用时这些**不会**被自动化代替——
事项停在待调查或等待补充，并如实说明本次未经模型调查。

**必须由用户或专业人员确认**：任何用药调整；风险是否在临床上成立；
用户转述的医生意见。系统不代替这些判断，也不把它们记成已验证的事实。

**已知薄弱处**（沿用既往轮次的诚实标注，未因本轮而改变）：KEGG 是佐证而非权威中文说明书
依据；`P` 级相互作用在缺少更强证据时默认 `moderate`；类别推断（例如阿司匹林 × 布洛芬）
是低置信度的、会强制升级而非直接断言；材料核对的交付在覆盖不全时恒为 `partial`，
真实模型也**从不主动外部取证**；开放式的模型自主规划能力在真实批次上**尚未被验收**。

## 历史

已结束实验的脚本与夹具本轮已删除；它们的**结论与索引**保留在
[docs/safety-mainline-2026-09-13/HISTORY-INDEX.md](docs/safety-mainline-2026-09-13/HISTORY-INDEX.md)，
主线决策记录在 [DECISION.md](docs/safety-mainline-2026-09-13/DECISION.md)。
各轮原始报告仍在 `docs/agent-capability-upgrade/`、`docs/product-upgrade/`、
`docs/harness-upgrade/` 等处，作为历史记录阅读。
