# 延迟优化 L1 · 按输出 token 纪律选型 · 2026-09-12

**结论：找到并验证了一个达标端点 —— SiliconFlow `Qwen/Qwen2.5-7B-Instruct`。** 真实批次路径上单次规划 **3.05 s**（原 21.9 s，**7.2×**）、每回合 **9.8 s**（原 72.0 s，**7.3×**），输出 token 中位数 **89**（原真实载荷 1162），`reasoning_tokens` **6/6 次调用全为 0**，参数拒绝 / 解析失败 / 代码代填**全部为 0**。

**同时报告一条不达标项**：k=3 里有 1 个回合因网关 60 s 超时而失败（后面第三节）。加速是真的，但不是无条件的。

**本轮最重要的单个测量是控制组**（第五节）：把同一个模型放在两个网关上跑，**推理量完全一样，速度差 2.3×**。这直接回答了本文档留下的悬念——"`thinking:disabled` 是被网关无视的"——答案是：**不是网关，是模型本身就在推理；换网关治不好，换模型才行。**

---

## 一、阈值（先定，后测）

```python
DISCIPLINE_THRESHOLD_TOKENS = 200
```

依据来自**本轮之前**就存在的验收工件，不是事后挑的：

| 边界 | 数值 | 来源 |
|---|---|---|
| 参考：有纪律的回答 | **49** completion tokens | Zhipu 真实载荷实测 |
| 已记录的最长**契约合规**参数串 | 209 字符 ≈ 130 tokens | TokenDance 探针原始 `raw_arguments` |
| 已观测的**推理型**回答 | **823 – 2440** tokens | TokenDance 未设 `thinking:disabled` |

200 落在**空档里**：约为最长正确回答的 1.5 倍、Zhipu 参考值的 4 倍，同时比推理下限低一个数量级。

**该阈值只衡量输出纪律，不代表质量。** 一个模型可以通过它而仍然答错。

---

## 二、候选与结果

同一份 v3 契约、同一个探针、每个候选 6 次调用（合成内容，无患者数据）。

| 候选 | 契约完整 | completion tokens | `reasoning_tokens` | 延迟 | 429 | 判定 |
|---|---|---|---|---|---|---|
| `siliconflow/Qwen/Qwen2.5-7B-Instruct` | **6/6** | **75 – 80** | **0 ×6** | **1929 – 2067** ms | 0 | **pass** |
| `siliconflow/Qwen/Qwen3.5-9B` | 6/6 | 208 – 223 | 未上报 | 2337 – 4713 | 0 | fail_discipline |
| `tokendance/glm-5.3-flash` | 6/6 | 300 – 402 | 190 – 263 | 10813 – 15560 | 0 | fail_discipline |
| `siliconflow/zai-org/GLM-5.3`（控制组） | 6/6 | 257 – 374 | 180 – 279 | 4601 – 6763 | 0 | fail_discipline |
| `siliconflow/zai-org/GLM-4.5-Air` | **0/6** | 245 – 349 | 158 – 261 | — | 0 | fail_reasoning_exhausted_the_response |
| `zhipu/glm-4.7-flash` | 1/6 | 44 | 0 | 45726（n=1） | **5** | inconclusive_provider_errors |

原始样本见 [qualification-2/qualification.json](qualification-2/qualification.json)（每次调用的 token 与延迟逐条列出，不只给中位数）。

**逐条说明：**

- **`Qwen2.5-7B-Instruct` 达标。** 唯一同时满足契约合规与输出纪律的候选，且 6/6 无 429。首次调用 6272 ms 是冷启动，其后 1929–2067 ms。
- **`GLM-4.5-Air` 6/6 返回 `choices: []`**，HTTP 200。usage 里写着 `completion_tokens` 245–349、`reasoning_tokens` 158–261——**推理把整个 completion 预算吃光，答案从未产出**。这不是"慢"，是不可用。
- **`Qwen3.5-9B` 只超了一点**（208–223 vs 阈值 200），延迟 2.3–4.7 s。按**预先登记的阈值**判为不达标；事后放宽阈值去接纳它就是本文档禁止的"事后挑选好看的阈值"。
- **Zhipu 仍然受可用性限制**：6 次里 5 次 429、1 次超时，唯一成功那次 44 tokens（纪律确实好）、`reasoning_tokens` 0，但**耗时 45.7 s**。

**探针值低估真实路径**（本文档自己的教训：探针中位数 10.8 s vs 真实批次 21.9 s）。所以探针只用于**筛选**，达标候选必须回到真实批次路径复验 —— 见第四节。探针的 completion（75–80）与真实路径（50/89/106）同量级，但它不是批次结论。

---

## 三、控制组：同一个模型，两个网关

`zai-org/GLM-5.3`（SiliconFlow）与 `glm-5.3-flash`（TokenDance 网关）是**同一个模型**。同契约、同 `thinking: disabled`：

| 网关 | completion tokens | **`reasoning_tokens`** | 延迟 |
|---|---|---|---|
| TokenDance | 300, 328, 319, 361, 387, 402 | 190, 224, 200, 238, 220, 263 | 10813 – 15560 ms |
| SiliconFlow | 257, 374, 257, 374, 374, 374 | **180, 279, 180, 279, 279, 279** | **4601 – 6763 ms** |

**推理量重叠、统计上无法区分（190–263 vs 180–279）；速度差约 2.3×。**

于是两件事同时被分开：

1. **隐藏推理是模型的属性，不是网关的。** 换网关不改变输出规模——本文档"结论 2"（`thinking: disabled` 有效但未被真正遵守）的后半句得到了直接解释：**GLM 这两个端点的模型根本不遵守该参数。**
2. **网关影响的是速度**（2.3×），不是输出。

**这是单变量对照**：模型、契约、参数、采样数全部相同，只有网关不同。

---

## 四、真实批次路径验证（不是探针）

冻结批次入口已按既有模式改为新端点（[run-planner-live-acceptance-v3.py](../../../../scripts/run-planner-live-acceptance-v3.py)），k=3，`effective_config()` 解析后确认为 `siliconflow / Qwen/Qwen2.5-7B-Instruct`。

| run | 目标状态 | 回退 | 参数拒绝 | 解析失败 | 代码代填 | 每次规划延迟 | 回合墙钟 |
|---|---|---|---|---|---|---|---|
| 1 | incomplete，degraded | 是 | 0 | 0 | 0 | 2024, **60016（超时）** | 63081 |
| 2 | **completed** | **无** | **0** | **0** | **0** | 1799, 2934, 3161 | **8886** |
| 3 | **completed** | **无** | **0** | **0** | **0** | 2435, 3403, 3936 | **10731** |

**逐次调用的 token 明细**（L0 的代码改动在此生效，历史批次没有这些字段）：

| run | cycle | prompt | **completion** | **reasoning** |
|---|---|---|---|---|
| 2 | 1 / 2 / 3 | 2335 / 4178 / 5331 | **50 / 89 / 106** | **0 / 0 / 0** |
| 3 | 1 / 2 / 3 | 2335 / 4178 / 5332 | **50 / 89 / 99** | **0 / 0 / 0** |

**验收对照（先注册、不放松）：**

| 项 | 要求 | 实测 |
|---|---|---|
| 参数拒绝 | 0 | **0** |
| 解析失败 | 0 | **0** |
| 代码代填 | 0 | **0** |
| 契约完整 | 探针与批次一致 | 探针 6/6、批次 6/6 次规划调用全部 `accepted` |

**run 1 的失败是可用性，不是正确性 —— 且失败路径按设计工作**：第 2 次调用 `APITimeoutError`（60013 ms），传输结果未知 → 预算置 `usage_unknown` 并 **fail-closed 停止后续模型调用**（`budget_exhausted:usage_unknown`）。这正是可靠性轮设计的行为，没有静默重试，也没有重复计费。

---

## 五、延迟对照（L0 口径，同一条命令）

```bash
python scripts/latency-baseline.py --probe latency-2026-09-11/latency.json \
  --cohort "L1-siliconflow-qwen2.5=latency-2026-09-11/L1/live/live-final-*.json" \
  --out docs/agent-capability-upgrade/latency-2026-09-11/L1/baseline.json
```

| 批次 | 端点 | 可比回合 | 规划调用中位数 | 回合墙钟中位数 | 调用数/回合 | completion 中位数 | `reasoning_tokens` |
|---|---|---|---|---|---|---|---|
| provider-switch-2026-09-11 | tokendance glm-5.3-flash | 3/3 | **21867.5** | **72021.0** | 3 | unavailable | unavailable |
| **L1（本轮）** | **siliconflow Qwen2.5-7B** | **2/3** | **3047.7** | **9808.8** | **3** | **89** | **0 ×7** |

**单次规划 7.2×、每回合 7.3×，调用数不变（3 次）。**

**必须说清楚的两点：**

1. **改善来自"模型少说"，不是"token 更快"。** 本批次的 `ms_per_output_token` 聚合值是 **32.2 / 41.1**，比 TokenDance 真实载荷的 22.9–37.3 **更高**。输出变短后，每次调用的固定开销占比上升。把这两个数字混在一起说"变快了"会掩盖真正的机制。
2. **对照不完全可控 —— 声明在案。** 换了端点、换了模型、换了网关，三项同时变（与 09-11 那次同类问题）。可以归因的是"这个端点整体快 7×"；**不能**把 7× 拆成"其中多少来自网关、多少来自模型"——除了控制组那条已单独测出的 2.3× 网关分量。若要把七倍拆干净，需要在同一网关上再跑一个同为 `Qwen2.5-7B-Instruct` 但输出量相当的对照，本轮未做。

---

## 六、代码改动

| 位置 | 改动 |
|---|---|
| [extract_ddi.py](../../../../stage0/extract_ddi.py) | 新增 `siliconflow` provider（**仅** `LLM_PROVIDER` 可达，不进自动探测顺序）；白名单新增 4 个显式三元组 |
| [model-qualification-probe.py](../../../../scripts/model-qualification-probe.py) | 候选改为候选级结构；记录 `completion_tokens` / `reasoning_tokens` / `ms_per_output_token`；**新增 `assert_live_authorized()`（此前探针绕过白名单）**；`--thinking product\|disabled\|enabled`；预先登记的阈值与判定 |
| [run-planner-live-acceptance-v3.py](../../../../scripts/run-planner-live-acceptance-v3.py) | 冻结端点改为 `siliconflow/Qwen/Qwen2.5-7B-Instruct`；模型 pin 改用该 provider 真正读取的变量 |
| [turn_budget.py](../../../../stage0/turn_budget.py) / [memory.py](../../../../stage0/memory.py) / [agent.py](../../../../stage0/agent.py) | 台账与 `provider_attempts` 增加 `reasoning_tokens`（L0 的同类扩展） |
| [test_live_regressions.py](../../../../stage0/test_live_regressions.py) | 新增：SiliconFlow 只能被显式选择，不能因 key 存在而被自动选中 |

**探针此前绕过白名单**是一个真实缺口：`probe()` 直接 `create_llm_client(config)`，从未调用 `assert_live_authorized()`。白名单对验收路径有效、对探针无效，而探针正是新增端点的入口。本轮补上，探针现在与产品同样 fail-closed。

**为什么 `siliconflow` 不进自动探测**：自动探测的既有设计原则是"处理患者衍生数据的端点必须是刻意决定，不能是'谁的 key 恰好在 .env 里'的副作用"。把它加进探测顺序会让上述原则失效。回归测试锁住这一点。

**`reasoning_tokens` 的意义**：本文档此前把"`thinking:disabled` 未被遵守"作为**推断**（从 completion 膨胀反推）。网关上报该字段后，它变成**直接测量**——`reasoning_tokens = 0 ×7` 是"没有推理"，`180–279` 是"在推理"，而 `NULL`（Qwen3.5-9B）是"网关没说"。三者必须保持可区分，合并会读成"干净"。

---

## 七、未解决 / unavailable

1. **可用性未解决。** 3 个回合里 1 个因网关 60 s 超时失败（33%）。加速与稳定在目前是**两个不同端点上的两件事**：TokenDance 稳但慢，SiliconFlow 快但有一次超时。**L3 之外的下一步应是可用性，而不是继续压延迟。** n=3，这个失败率的置信区间很宽，不足以断言端点本身不可靠。
2. **归因不完全可控**（第五节第 2 点）：7× 未拆分。
3. **历史批次仍无 token 拆分**，不可回填；本轮的 `recorded` 只适用于此后记录的批次。
4. **`Qwen3.5-9B` 与阈值只差 8–23 tokens**，延迟 2.3–4.7 s。它按预注册阈值被判不达标，但若后续把阈值论证改成别的依据，它值得重新评估——**本轮不这么做**，因为事后放宽阈值正是被禁止的操作。
5. **探针与批次仍有口径差**（探针 completion 75–80 vs 批次 50/89/106，探针延迟 1.9–2.1 s vs 批次 1.8–3.9 s）。本轮两者方向一致，但探针数字**不得**当作批次结论引用。
6. **未测**：本轮没有对 `Qwen2.5-7B-Instruct` 做独立 held-out 质量评估。它通过了契约与输出纪律，**这不等同于任务质量达标** —— 见下一节。

---

## 八、必须与"延迟改善"分开报告的一条

本轮的验收范围是**契约合规 + 输出纪律 + 延迟**，**不是任务质量**。批次里 run 2/3 达到 `autonomous_success`，但这与 09-11 的 3/3 门槛不是同一件事，样本也不同（n=2 达标 vs n=3 达标），**不能据此宣布规划能力达标维持不变**。要断言端点切换没有损害任务质量，需要按既有协议重跑独立 held-out —— 该工件在本轮开始时即为 `unavailable`，本轮未解决。
