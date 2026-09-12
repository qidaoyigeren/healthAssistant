# 延迟优化 L0 · 建立度量与基线 · 2026-09-12

**本轮不改任何规划行为。** 只做三件事：把"延迟"变成一条命令能重算的度量、把缺的字段补上、把既有三个批次按同一口径重算。

**结论先行：**

1. **本文（优化 Prompt）表格里的 4 行数字全部逐字复现**，一条命令、离线、可重复。
2. **但"延迟主要由输出 token 决定"这个结论，此前从来没有在批次路径上被测量过** —— 三个批次的 `llm_attempts` 只记了 `usage_tokens` 总量，没有 prompt/completion 拆分。本轮的代码改动使**未来的批次**带拆分；历史批次**不可回填**，一律记 `unavailable`。
3. **重算暴露了一个会误导人的分母问题**：三个批次里 **4/9 个回合根本不可用于延迟比较**（模型调用丢了、回落了）。其中 `planner-closeout-2026-09-11` **3/3 全部不可用**，它此前被引用的 31.1 s/回合中位数是在故障回合上算的。基线改用"可比回合"作分母，并把未过滤的数字并排列出以便审计。

---

## 一、口径（后续阶段必须复用这一套，不要另建）

产物：[scripts/latency-baseline.py](../../../../scripts/latency-baseline.py) → [baseline.json](baseline.json) 的 `metric` 块（机器可读）。

**单位一：一次模型调用**

| 字段 | 含义 |
|---|---|
| `prompt_tokens` / `completion_tokens` | 供应商报什么记什么；**没报就是 `unavailable`，不估算** |
| `wall_ms` | 调用方看到的墙钟 |
| `outcome` | `response` / `rate_limit` / `timeout` / … |

**单位二：一个回合（一个 run 产物）**

| 字段 | 含义 |
|---|---|
| `model_call_count` | 模型调用数 |
| `turn_wall_ms` | 整个回合 |
| `first_progress_ms` | 首个后端进度事件。**这是"首次可见反馈"，与回合墙钟是两个测量，永不平均在一起** |
| `remote_ms` | 各次调用墙钟之和 —— 回合里有多少是在等别人的 GPU |

**派生量**

```
ms_per_output_token = wall_ms / completion_tokens
可评分（scorable） = outcome == 'response' 且 completion_tokens > 0
```

拒绝、超时、未报拆分的调用**留在计数里**（它们是延迟证据），**不进比率**，且排除数被显式报出，不静默丢弃。

**分母与不可评分样例（本轮的实质修正）** —— 一个回合只有在**每一次模型调用都报了 usage** 时才算"延迟可比"。理由：丢了调用的回合墙钟短，是因为它**崩得快**，不是因为模型快；把它混进中位数会低估延迟，看起来像一次没发生的改善。基线同时输出过滤后与未过滤两个数字。

`planner_latency_ms_each` **不在这里重算** —— 直接 `importlib` 加载 [analyze-planner-metrics.py](../../../../scripts/analyze-planner-metrics.py) 复用，确保本基线与冻结的验收指标不会漂移。

---

## 二、复现本文数字

```bash
python scripts/latency-baseline.py --out docs/agent-capability-upgrade/latency-2026-09-11/L0/baseline.json
```

`probe.doc_reconciliation` 段逐行比对，**4/4 `reproduced`，零差异**：

| 端点 / 场景 | 输入 | 输出 | 延迟 | 状态 |
|---|---|---|---|---|
| Zhipu · 真实载荷 | 4224 | 49 | 1.5 s | reproduced |
| TokenDance · 极小提示 | 437 | 1451 | 40.4 s | reproduced |
| TokenDance · 真实载荷 | 4219 | 1162 | 31.6 s | reproduced |
| TokenDance · 不设 `thinking:disabled` | 4219 | 2440 | 55.9 s | reproduced |

**两点口径说明**（不是差异，是本文没写清楚的地方）：

- **本文引用的是单次调用**（极小提示那行引的是第 2 次），而 `latency.json` 自带的 `median_*` 字段在 n=2 时取的是**上中位数，也就是最大值**。两者此处恰好一致，但后续不要把这些 `median_*` 当中心值读。
- **"≈27 ms/token" 是混合值**：6 次可评分调用的实际比率是 **22.9 – 37.3 ms/token**；回归出来的**边际**成本是 **19.27 ms/token**（R² = 0.956），另有一个约 **9.7 s** 的常数项。27 落在区间内、作为中心值没问题，但它把边际成本和固定成本混在了一起。见下节。

---

## 三、四个耗时分量

| 分量 | 能否测量 | 本轮结论 |
|---|---|---|
| 供应商排队 / 退避 | **是** | TokenDance 批次 `remote_ms − Σ(attempt latency)` = **2.8 – 3.0 ms**，可忽略。Zhipu 的 `planner-closeout`（retries=1）是 **3.0 – 6.0 s**，重试/退避可见 |
| 模型 decode | **否**，仅推断 | 见下 |
| 模型 prefill | **否**，仅推断 | 见下 |
| 本地工具执行 | **上界** | `turn_wall − remote_ms`；含响应组装、序列化、检查点写入，这几项**没有埋点**，所以是上界不是测量 |

**TokenDance 批次（provider-switch，3/3 可比）的分解：**

| run | 回合墙钟 | 远程 | **远程占比** | HTTP attempt | 排队+退避 | 本地+未埋点 | 首次可见反馈 |
|---|---|---|---|---|---|---|---|
| 1 | 67928 | 66861 | **98.4%** | 66859 | 2.8 | 1067 | 17023 |
| 2 | 72021 | 71051 | **98.7%** | 71048 | 2.9 | 970 | 20768 |
| 3 | 88840 | 87858 | **98.9%** | 87855 | 3.0 | 982 | 22319 |

**≈98–99% 的回合在规划调用内部，本地部分约 1.0 s。** 这从另一个方向独立确认了"载荷瘦身不是有效方向"：本地没有可优化空间，输入规模也已证明与延迟无关。

**prefill vs decode：无法分离（推断，非测量）。**  非流式调用只返回一个数，里面同时装着排队、prefill 和 decode；没有任何产物记录 time-to-first-token。只能回归推断（`probe.prefill_vs_decode`）：

| 自变量 | 斜率 | R² | n |
|---|---|---|---|
| `completion_tokens`（TokenDance） | **19.27 ms/token** | **0.956** | 5 |
| `prompt_tokens`（TokenDance） | 0.876 ms/token | **0.017** | 5 |
| Zhipu | — | — | **1**，样本不足 |

**延迟由输出 token 解释（R²=0.96），输入 token 解释不了（R²=0.02）。** 这是对"结论 1"的独立复现，只是换了个角度。

**该推断的弱点，记录在案**：n=5、非同质采样、网关行为随时段波动、**没有流式测量**。截距（9.7 s）里混着排队与 prefill，所以**不要**把截距读成 prefill。要真正拆开两个分量需要一次流式调用测 time-to-first-token —— 本轮没做。

---

## 四、三个批次的可比基线

| 批次 | 端点 | 可比回合 | 规划调用中位数 | 回合墙钟中位数 | 未过滤的回合中位数 | 调用数/回合 | token 拆分 |
|---|---|---|---|---|---|---|---|
| planner-closeout-2026-09-11 | zhipu glm-4.7-flash，retries=1 | **0 / 3** | — | — | 31147 | — | unavailable |
| planner-closeout-v3-2026-09-11 | zhipu glm-4.7-flash，retries=0 | **1 / 3** | 1536.6 | 6876.4 | 5109.9 | 3 | unavailable |
| provider-switch-2026-09-11 | tokendance glm-5.3-flash | **3 / 3** | **21867.5** | **72021.0** | 同左 | 3 | unavailable |

**`provider-switch` 的数字与本文一致**：单次规划 21.9 s、每回合 72.0 s、每回合 3 次调用。这是三个批次里**唯一** 3/3 可比的，所以它作为基线是可靠的。

**被排除的回合及原因**（`batches[].excluded_runs`，可审计）：

| 批次 | 文件 | 排除原因 |
|---|---|---|
| planner-closeout | live-final-1/2/3 | 8 次中 6 次、6 次中 3 次、6 次中 3 次调用 `failed_estimate`（无 usage） |
| planner-closeout-v3 | live-final-2 | **4/4** 调用无 usage |
| planner-closeout-v3 | live-final-3 | 4 次中 3 次无 usage |

**必须说明的限制**：本文"同一协议在 Zhipu 上 1.5 s/调用"这一条，在**批次路径**上的证据只有 `planner-closeout-v3` 的**唯一一个健康回合**（3 次调用，1250–2028 ms）。探针那边的证据是另一套口径。所以端点对比目前的强度是：**TokenDance 9 次干净调用 vs Zhipu 3 次干净调用**，方向和量级都很清楚（14 倍），但 Zhipu 侧的批次样本是 n=1，不是 n=3。

---

## 五、代码改动（只加观测，不动语义）

| 位置 | 改动 |
|---|---|
| [memory.py](../../../../stage0/memory.py) | `llm_attempts` 增 `prompt_tokens` / `completion_tokens` / `reasoning_tokens` 三列（走既有 `_add_column` 迁移）；`settle_llm_attempt()` 接受并写入 |
| [turn_budget.py](../../../../stage0/turn_budget.py) | 新增 `_as_count()` / `_usage_total()` / `usage_split()` / `usage_reasoning_tokens()`；`call()` 从 usage 取拆分并透传 |
| [agent.py](../../../../stage0/agent.py) | `last_provider_attempts` 的 `response` 条目带上 `prompt_tokens` / `completion_tokens` / `reasoning_tokens`，使**具体某一次** HTTP attempt 可归因 |
| [test_harness_p0.py](../../../../stage0/test_harness_p0.py) | 新增 1 项不变量测试 |
| [latency-baseline.py](../../../../scripts/latency-baseline.py) | **新增**入口；`--cohort` 供后续阶段复用同一口径 |
| [ENTRY-POINTS.md](../ENTRY-POINTS.md) | 入口清单登记 |

**`reasoning_tokens` 是 L1 期间追加的（同一类观测扩展，随 L0 的代码一起记录）。** 本文档第三节把"`thinking:disabled` 未被遵守"列为**推断**，因为没有产物含 time-to-first-token。L1 发现网关会上报 `completion_tokens_details.reasoning_tokens`，于是该结论可以改为**直接测量**。三个状态必须保持可区分：`0` = 网关说没有推理、`>0` = 在推理、`NULL` = 网关没说。合并 `NULL` 与 `0` 会把"无从判断"读成"清白"。见 [L1 报告](../L1/implementation-report.md) 第六节。

**两条不可放松的约束，已用测试锁住：**

- **拆分是观测，不是计费。** 预算算术仍然只用总量；`tokens_actual` / `tokens_charged` 在有无拆分的两种供应商下都只跟总量走。
- **供应商没报拆分时必须记 `NULL`，不能记 0。** 记 0 会读成"一个输出极有纪律、只是从不回答的模型"——那是伪造观测。测试 `test_latency_token_split_is_observational_and_never_fabricated` 同时覆盖这两种供应商（现有夹具全部只报总量，天然覆盖 NULL 路径）。

`SELECT *` 的读取方（`run_eval.py`、`product_live_acceptance.py`）自动获得新列，无需改动。

---

## 六、验证

| 项 | 命令 | 结果 |
|---|---|---|
| 基线可重算且确定性 | `python scripts/latency-baseline.py --out …` 跑两次 | **逐字节一致** |
| 一条命令复现本文数字 | 同上 | 4/4 `reproduced` |
| 新不变量测试 | `python -m unittest stage0.test_harness_p0 -v` | 26 passed（含新增 1 项） |
| 全量离线回归 | `python scripts/verify-agent-closeout.py --out output/latency-l0-closeout` | **status pass，420 tests / 34 suites，0 失败**，4 项 eval 全过 |

源码指纹（`source_fingerprint.py`，`repo` 作用域）：`152ed3176cc66559db357827810656f9265d16b872d864418fd04f061fc5964e`

---

## 七、未解决 / unavailable

1. **历史批次的 token 拆分不可恢复。** 总量推不出拆分，一律 `unavailable`，不回填。生效范围是**未来批次**。
2. **首次可见反馈只有后端口径。** `first_backend_progress_ms` 有；**浏览器可见**的首帧没有任何产物记录（`analyze-planner-metrics.py` 里 `first_browser_visible_feedback_ms` 硬编码为 `None`）。本轮未做，在此登记以免与回合墙钟混淆。
3. **prefill / decode 仍未分离**，需要流式测量。
4. **本地耗时是上界**，响应组装与检查点写入未埋点。
5. **Zhipu 批次侧延迟样本 n=1**（见第四节限制）。

**与后续阶段的对比方式**：L1 起，任何延迟结论都必须给出 `completion_tokens` 与 `ms_per_output_token`，且分母声明为"可比调用"；只有回合墙钟而无输出 token 的结论，一律视为未测量。
