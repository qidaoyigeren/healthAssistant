# 取证循环诊断与修复（2026-09-12）

上一轮真实批次 0/8，模型只调用 `memory_read` / `list_materials`，从不进入取证。
本轮只针对这一个故障：先定位，再改代码，再做 A/B 与产品路径验收。

**结论先行**：根因在代码，不在模型能力。改完之后模型**确实开始取证**（0/8 → 2/8
任务发起检索），无效重复被明确计数并停止，但**任务达成率没有改善（0/8 → 0/8）**。
按任务书的口径，这不是达标：更快停止但仍未解决的，单独报告，不记作成功。

---

## 一、根因证据

### 1.1 现场

`output/verification-2026-09-12/live/model-four-tasks.json`：8 个任务，63/80 调用，
**0 次 429**，动作序列里只有三种工具，取证工具 8 × 0 次。

### 1.2 逐请求回放：把真正发给模型的东西取出来

看 `allowed_tools` 或本地拼的 schema 都不算证据。`scripts/diag-evidence-loop.py` 在
**与线上同一条接缝**（`client.chat.completions.create`）装了一个假客户端，把记录下来的
提案逐条重放，原样留下 provider 会收到的 kwargs——messages、tools、tool_choice、
以及每步的观察。重放复现了原轨迹的每一步（来源与状态逐条一致）。

工件：`output/diag-evidence-loop/vp-conflict-003a.json`（修复前），
`output/evidence-loop-fix/replay/*.json`（修复后）。

**逐项核对**（任务书第一部分）：

| 要确认的事 | 结果 |
|---|---|
| 取证工具确实出现在发送请求中 | ✅ 成立。`plan_questions` 被接受后的请求里 `rag_search`、`ddi_check` 同时在 `tools[]` 与 `tool_catalog` 中；检索到证据后 `read_evidence` 出现 |
| 材料条目 ID / 证据 ID 可见 | ✅ 条目 ID、`kind`、`issues`、`current` 全在观察里（1426 字符）；证据 ID、`total_chars`/`truncated`/`offset` 也在 |
| 分页与截断信息可见 | ❌ **不成立**。`_truncate_result_strings` 把长字符串切到 600 字符**不留任何痕迹**，模型分不清"这是全文"与"这是被切过的一半" |
| 模型知道哪些是索引、哪些已读取 | ⚠️ 状态里有（`material_refs` vs `material_read_refs`、`evidence_unread`），但没有"这条材料还没读回原文"的逐条可见性 |
| 未解决问题与可用工具的用途清晰 | ❌ **不成立**。`open_gaps` 只给 `{gap_id, kind, description}`，没有任何东西把"这个问题"与"哪些工具能推进它"连起来；而 `rag_search` 的说明只有一句英文 "Hybrid retrieval over the local drug-label corpus with a deterministic fallback."，对比之下 `plan_questions`/`list_materials` 各有整段中文职责说明 |
| 工具选择没被解析/筛选/去重丢掉 | ⚠️ **发现真缺陷**。见 1.4 |

### 1.3 循环为什么停不下来（直接证据）

- 请求 3→9 的 payload：`allowed_tools`、`open_gaps`、`claims`、`evidence_unread`
  **逐字相同**，只有观察列表在变长；每一步的 `completion_tokens` 恒为 132。
  同一个决策，被喂了七遍。
- 每一条观察上的 `no_progress` **全是 false**——因为
  `AGENT_NO_PROGRESS_LIMIT` 默认 `0`（`agent.py`），而评测与产品路径都没设它，
  `_progress_verdict` 第一行就 `return "continue"`。**唯一的重复纠正机制是关着的。**

### 1.4 反事实：那个状态本来是走得通的

在模型卡住的那个状态上，只把某一步换成 `rag_search`（其余全部照原样重放）：

- `rag_search` 被接受，返回两条**互相矛盾**的说明书片段与两个 evidence_id；
- `read_evidence` 随即出现在下一请求的工具表里；
- 再回读一次，claim 从 `insufficient` 变成 `supported`，`open_gaps` 清空。

工件：`output/diag-evidence-loop/cf-conflict-003a-full.json`。

**所以"工具不可用 / 语料是空的 / 契约不可满足"都被排除。** 剩下两件事：
循环不会告诉模型"这一步没有新增"，接口没把"问题 ↔ 工具"讲清楚。

### 1.5 修复前查出的代码缺陷（全部是代码）

1. **重复检测默认关闭**，且评测路径从不设它（`run_visitprep._budget_env` 只设预算）。
2. **只与上一步比较**：`NoProgressTracker` 存的是 `last_signature`，所以
   A,B,A,B 交替读取旧信息永远不算重复——而这正是真实批次的形状（memory_read ×9、
   list_materials ×5）。
3. **写入无条件重置**：`memory_write` 直接短路成 progress 并清零。
4. **持久任务路径根本没接**：`run_care_task` 的循环从不调用 `_progress_verdict`，
   只有一个"与上一步提案相同"的指纹护栏，看不见交替重复。
5. **rag_search 没有职责说明**，`open_gaps` 没有 `closable_by`。
6. **截断无痕迹**（1.2）。
7. **并行工具调用被静默丢弃**：一个响应里 `[memory_read(snapshot), rag_search(…)]`
   时，`_first_unexecuted` 执行 memory_read、**丢掉 rag_search**，模型永远不知道
   （直接探针：同一函数在"两个都是新调用"时丢弃后者）。本批 trace 里被丢的都是
   同名重复调用，所以它**不是本批的原因**，是仍在的机制缺陷。
8. **`source_invalid` 仍然阻塞完成**：`FINDING_GAPS` 未改动，
   `closable_by` 对它是空列表（"没有工具能关掉它"）。已加测试钉住。

---

## 二、修改位置

| 文件 | 改了什么 |
|---|---|
| `stage0/harness/progress.py` | 新增 `run_progress_signatures` 成员表：重复判定从"等于上一步"改为"本回合是否已经取到过"（顺序无关）；新增 `forget()`（新规划回合）；`record(new_information=)` 接受"换了参数但没新增信息" |
| `stage0/agent.py` | `NO_PROGRESS_LIMIT_DEFAULT = 2`；`_progress_verdict` 不再对写入短路、把"没有新增"计入、产生结构化 `state.no_progress_feedback`；新增 `_no_progress_feedback()`（已取得什么 / 还没解决什么 / **不指定工具与参数**）、`_progress_key()`、`_dropped_calls_note()`；`Observation.added_information`；payload 增加 `no_progress_feedback` 与 `dropped_calls`；截断加标记 `…[已截断，原文共 N 字]`；持久任务循环接上同一个判定 |
| `stage0/investigation.py` | 新增 `gap_closing_tools()`，`open_gaps` 每条带 `closable_by`（与 `allowed_tools` 同一套谓词取交集，不可能推荐一个当前不允许的工具）；`material_unread` 与逐条 `read` 标记；检索"没有新增内容"如实交给循环；**移除**旧的"两次检索即终止"（它抢在反馈之前把回合杀掉） |
| `stage0/harness/default_tools.py` | `rag_search` 说明改为陈述其职责（推进 `evidence_missing` 缺口；返回候选而非已核验支持；引用前必须 `read_evidence` 回读） |
| `stage0/harness/manifest.py` | `no_progress_limit` 记**生效值**，不再记原始 env |
| `stage0/agent_evals/run_visitprep.py` | 批次配置里显式下发 `AGENT_NO_PROGRESS_LIMIT`，让口径随产物可读 |
| `stage0/harness_eval.py` | `_termination_reason` 把"无进展阈值停止"认作一种**有明确边界的终止**（`repeated_legal_reads` 断言的是"有界，不是意外"，这个口径更精确，不是放宽） |
| `stage0/test_evidence_loop.py` | 新增 15 项测试 |
| `scripts/diag-evidence-loop.py` | 逐请求回放探针（含反事实 `--force`） |
| `scripts/evidence-loop-browser-acceptance.js` | 产品路径浏览器验收 |

**回归**：`scripts/verify-agent-closeout.py` **43/43 通过，574 项测试**（修复前 572）。
离线三臂逐格与修复前**完全一致**（scripted 10/12、det 0/12、fixed 0/12），
说明改动没有波及脚本臂与确定性臂。

---

## 三、A/B 对照

冻结见 `output/evidence-loop-fix/AB-FREEZE.json`（**取数前**写定：任务、评分协议、
端点、调用上限）。A 臂用 HEAD 的干净 worktree（只补了 gitignore 的 `.env`），
B 臂用本轮工作区。同一端点 `siliconflow / Qwen2.5-7B-Instruct`，同一批次上限。

> **诚实记录**：B 臂跑了两次。第一次（`B-*.json`）跑完后又查出 §1.5-7 那类问题的
> 一个变体（旧的"两次检索即终止"抢在反馈之前终止回合），修掉重跑得
> `B2-*.json`。**两次都保留**，结论以 B2 为准。

### 3.1 开发样本（材料缺项 / 材料冲突 / 首次搜索无结果）

| | 调用 | 取证工具调用 | 通过 |
|---|---|---|---|
| A | 24/40 | **0** | 0/3 |
| B2 | 15/40 | **1** | 0/3 |

### 3.2 回归集（其余 5 个任务）

| | 调用 | 取证工具调用 | 通过 |
|---|---|---|---|
| A | 34/60 | **0** | 0/5 |
| B2 | 31/60 | **1** | 0/5 |

### 3.3 逐任务对照

| 任务 | A 终态 | A 工具 | B2 终态 | B2 工具 |
|---|---|---|---|---|
| vp-missing-002a | budget_insufficient | memory_read, plan, list×5 | **no_progress** | memory_read, plan, list×3 |
| vp-conflict-003a | budget_insufficient | memory_read×2, plan, list×6 | **no_progress** | memory_read, plan, plan×2 |
| vp-noresult-005a | budget_insufficient | memory_read×3, plan, list | budget_insufficient | memory_read, plan, **rag_search** |
| vp-full-001a | budget_insufficient | memory_read, plan, list×2 | **no_progress** | memory_read, plan, list×3 |
| vp-full-001b | budget_insufficient | memory_read, plan, list×5 | **no_progress** | memory_read, plan, list×3 |
| vp-missing-002b | budget_insufficient | memory_read, plan, list×5 | **no_progress** | memory_read, plan, list×3 |
| vp-conflict-003b | budget_insufficient | （0 步，超时） | **no_progress** | memory_read, plan, list×3 |
| vp-noresult-005b | budget_insufficient | memory_read×7, plan, list | **no_progress** | memory_read, plan, **rag_search**, list×3 |

### 3.4 四项验收判据

| 判据 | 结果 |
|---|---|
| 是否取得与任务相关的原文 | ⚠️ **部分**。真实臂 0/8 → **2/8 任务发起检索**（rag_search 被真的执行、返回真实片段）；但 8 个任务里 **`read_evidence` 一次都没被调用**，所以模型臂**没有回读到任何原文** |
| 是否减少无效重复 | ✅ 是。A 最多连续 7 次同一读取；B2 全部在阈值处停止，重复步被明确标注并反馈 |
| 是否改善报告正确性或任务达成 | ❌ **否**。0/8 → 0/8，`supported_claims` 全为 0 |
| 是否出现根据观察调整行动的真实案例 | ✅ 有，见 3.5 |

### 3.5 一个真实的取证案例

`B-dev` 的 `vp-conflict-003a`（B 臂第一次真实取数）里，读完权威快照、声明子问题
之后，模型**自己**选了 `rag_search`，查询 `"氨氯地平 每日一次 剂量"`
——此前 8/8 任务这一步从未发生。同一轨迹里还有一次**根据观察改写查询**：第一次用
关键词形式，第二次改用子问题原句 `"确认氨氯地平的每日一次剂量是否正确。"`。

B2 复跑时 `vp-noresult-005a` 同样自主发起检索。两次取数的证据都保留在
`output/evidence-loop-fix/live/`。

**但它没有改善结果**：两次检索之后模型没有回读原文，claim 仍是 `insufficient`，
`checks_completed` 依旧不可达。所以这是"行为变了"，不是"任务解决了"——
按任务书要求单独报告，不记作达标。

### 3.6 调用账本核对

批次账本 `planner_calls_spent` 与逐次轨迹的差额，每一笔都能对上：

| 批次 | 账本 | 逐次轨迹 | 差额 | 差额去向 |
|---|---|---|---|---|
| A-dev | 24 | 21 | 3 | 每任务 1 次：**首个被安全拒绝的提案**。`_trace` 只为 `llm`/`llm_post_correction`/`fallback` 记 `provider_attempts`，`rejected` 的那次**确实发了请求**却没有记进去 |
| A-regression | 34 | 29 | 5 | 同上，5 个任务。该批次另有 2 次 `APITimeoutError`（已计入 outcome） |
| B2-dev | 15 | 12 | 3 | 同上 |
| B2-regression | 31 | 26 | 5 | 同上 |

即 `账本 = 成功响应 + 超时 + 被拒提案数`，逐项相等。**这是一个观测缺口**
（被拒的提案花了真实调用却不出现在逐次台账里），本轮未改，记在这里。

---

## 四、产品路径验收（浏览器）

用真实浏览器 + 隔离合成夹具（专用临时库、脚本工具，不碰用户数据库），
经 `/materials` 与 `/assistant`、`/tasks` 走完整流程。

| 项 | 结果 | 证据 |
|---|---|---|
| 上传（`/materials` CSV 导入） | ✅ | `01-upload.png` |
| 调查（`/assistant` 发核查请求 → 证据核查状态卡） | ✅ | `02-investigation.png` |
| 重复停止如实展示 | ✅ | 卡片显示"终止原因：**未获得新证据，已停止重复核查**"（`run.log`，p1 夹具） |
| 部分报告 | ✅ | 五节有界报告渲染；"已核查范围/待补充内容/终止原因"齐全 |
| 引用查看 | ✅ | 点"回读证据"打开原文抽屉：来源类型、地址、版本、哈希校验、长度、原文、不可变证据说明（`notes.evidence_drawer_head`） |
| 取消 | ✅ | 造一个真实待办（缺药名材料）→ `/tasks` 取消 → 状态"已取消"，"已保存的记录与部分报告在取消或失败后仍然保留"（`04-cancelled.png`） |
| 无 React 报错 / 无 5xx | ✅ | `errors: []`, `serverErrors: []` |
| `source_invalid` 不得支持结论 | ✅ 未削弱 | `FINDING_GAPS` 未改动；新增测试断言有失效来源时**不得**判 `checks_completed`，且 `closable_by == []` |

> **夹具说明**：调查路径要真的走起来，验收夹具必须开着 `AGENT_INVESTIGATION_ENABLED`
> 并用确定性规划器（离线、零远程调用）。**产品默认值未改动，仍是关闭**——上一轮
> "未达标因此不开放"的结论没有被本轮推翻（见第五节）。

---

## 五、剩余瓶颈归因

**判定：瓶颈现在是模型，而且位置比上一轮前进了一步。**

| 候选 | 判定 | 依据 |
|---|---|---|
| 接口 | **已排除（本轮修的）** | 反事实：同一状态下一次 `rag_search` + 一次 `read_evidence` 就能让 claim `supported`、缺口清空；修复后模型**确实开始选它**（2/8） |
| 执行机制 | **已排除** | 同一批任务、同一语料，scripted 臂 10/12 到达 `checks_completed`；本轮修复未改变 scripted/det 臂的逐格结果 |
| 服务可用性 | **已排除** | A/B 两批 **0 次 429**；A 臂 34 次里 2 次超时，不足以解释 8/8 |
| 模型 | **是** | 模型已经会**发起**检索，但**不会回读**：8 个任务里 `read_evidence` 调用数为 0。检索到的原文未回读 → claim 恒为 `insufficient` → `checks_completed` 不可达 |

**这仍然是能力问题，但换了一个位置**：从"不进取证"变成"取了证不回读"。
按任务书的规则，这属于**未达标**，且**不允许**用"更长的预算"或"换个说法再搜一次"
掩盖——所以本轮没有加预算，也没有放松任何完成条件。

---

## 六、留给下一轮的

1. **模型臂从不回读原文**（新的 P0）。接口侧已把 `evidence_unread` 与"引用前必须
   回读"写在系统提示、协议说明与缺口描述三处，仍然不发生。下一步值得做的是
   **受控对照**：换已授权候选模型跑同一批任务，看这是 Qwen2.5-7B 的规模问题还是
   提示的位置问题。本轮**未做**（见第七节）。
2. **被拒提案的调用不入账**（§3.6）：观测缺口，应在 `_trace` 里为 `rejected` 也
   保留 `provider_attempts`。
3. **并行调用被丢弃**（§1.5-7）：本轮只做到"让模型看见"，没有改变"只执行一个"的
   契约。是否要执行全部（或按优先级选）是需要单独论证的决定。
4. 上一轮遗留、本轮未动：`claim_id = digest(list(entities))` 对顺序敏感。

---

## 七、本轮明确没做的事

- **没有换模型做受控对照**：任务书要求"若同一模型仍无法完成，再用已授权候选做
  受控对照"。本轮把预算花在了 A/B 与产品验收上，**没有**跑候选模型对照，
  因此"是不是模型规模问题"**未验证**，不作主张。
- **没有加预算、没有放松完成条件**：`checks_completed` 的每一条都保持原样。
- **没有把 `source_invalid` 加进非阻塞白名单**。
- **没有新增多 Agent、没有迁移框架、没有扩大产品范围**。
- 真实批次的上限与用度：A 58 次、B1 47 次、B2 46 次；B1 为被取代的版本，
  其产物保留在 `output/evidence-loop-fix/live/B-*.json`，不参与结论。

---

## 附：工件路径

| 内容 | 路径 |
|---|---|
| 逐请求回放探针 | `scripts/diag-evidence-loop.py` |
| 修复前逐请求捕获 | `output/diag-evidence-loop/` |
| 反事实（强制取证） | `output/diag-evidence-loop/cf-conflict-003a-full.json` |
| 修复后逐请求捕获 | `output/evidence-loop-fix/replay/` |
| A/B 冻结 | `output/evidence-loop-fix/AB-FREEZE.json` |
| A/B 真实批次 | `output/evidence-loop-fix/live/{A,B,B2}-*.json` |
| 离线各臂（修复后） | `output/evidence-loop-fix/arms-d/` |
| 门禁 | `scripts/verify-agent-closeout.py --out output/evidence-loop-fix/gate6` |
| 浏览器验收脚本 | `scripts/evidence-loop-browser-acceptance.js` |
| 浏览器证据 | `output/evidence-loop-fix/browser/` |
| 新增测试 | `stage0/test_evidence_loop.py`（15 项） |
