# 规划模型端点切换 · 2026-09-11

**换端点，不换协议。** 从官方智谱 `glm-4.7-flash` 切到 TokenDance 网关 `glm-5.3-flash`。

**理由只写"可用性"，不写"更快"** —— 因为资格探针证明它**不会更快**。这个区分很重要：如果理由写"更快"，记录就是错的。

---

## 一、资格验证（先做，通过才换）

换端点不是改配置：协议 v3 依赖供应商遵守 `tool_choice="required"`、并把**具名函数**的必填嵌套参数填对。这些行为是在官方 Zhipu `glm-4.7-flash` 上验证的，**不能假定搬到网关还成立**。

探针（`scripts/model-qualification-probe.py`，合成内容，两边同契约）：

| | 接受 | 契约完整 | 限流 | 每次调用延迟 |
|---|---|---|---|---|
| Zhipu `glm-4.7-flash` | 4/5 | 4/4 | 1 | 11788, 15179, 11684, **1242** ms |
| TokenDance `glm-5.3-flash` | **5/5** | **5/5** | 0 | 10880, 9653, 10824, 10342, 14058 ms |

**结论：**

- **协议安全** —— 网关忠实透传 `tools` 与 `tool_choice="required"`，模型每次都填对 `query`/`gap_id`/`expected_observation`。原始参数串见 [qualification.json](../model-qualification-2026-09-11b/qualification.json)。
- **不会更快** —— 中位数 10.8 s vs 11.8 s，n=4–5 完全在噪声内。Zhipu 方差更大（1.2–15.2 s，12 倍跨度），新端点更集中（9.7–14.1 s）。
- **延迟主要是时段，不是模型** —— 同一批的 Zhipu 调用是 1.2–2.0 s，探针里却是 6–15 s。同模型同契约差一个数量级。

**探针踩过的坑（记录在案）**：第一版漏了 `thinking: disabled`，两边都测出 6–20 s。该选项产品每次调用都设（`llm_completion_options` → `extra_body.thinking`），**探针不设就是在测另一个模型**。

**行为差异（需留意）**：新端点的 `expected_observation` 明显更长、还主动填 `section`/`top_k` 等可选字段；Zhipu 的参数更精简。契约都合规，但**新模型的参数载荷大得多**。

---

## 二、改动

| 位置 | 改动 |
|---|---|
| [extract_ddi.py](../../../stage0/extract_ddi.py) | 新增**显式** `LLM_PROVIDER` 选择器；新增 `AUTHORIZED_LIVE_TARGETS` 白名单与 `assert_live_authorized()` |
| [run_eval.py](../../../stage0/agent_evals/run_eval.py) | 授权 guard 改为白名单校验；`--live` 的 thinking/max_tokens 改为**跟随实际解析出的 provider** |
| [run-planner-live-acceptance-v3.py](../../../scripts/run-planner-live-acceptance-v3.py) | 冻结目标改为 tokendance/glm-5.3-flash；`CHILD_ENV` 固定 `LLM_PROVIDER`；`effective_config()` 改为**解析**端点并在不符时派发前报错 |
| `stage0/.env` | `LLM_PROVIDER=tokendance`（该文件已 gitignore） |
| [test_live_regressions.py](../../../stage0/test_live_regressions.py) | 新增 3 项回归 |

**为什么用显式选择器而不是重排优先级**：原优先级（Zhipu 优先）是为了"防止 .env 里的旧 key 意外选中旧供应商"。**处理患者衍生数据的端点必须是刻意决定，不能是"谁的 key 恰好在 .env 里"的副作用。** 因此新增显式开关，未设置时行为完全不变（原优先级测试仍然通过）。

**白名单是 fail-closed**：`assert_live_authorized()` 只放行显式列出的 (provider, model, base_url) 三元组，任何未列出的组合直接报错——包括"用白名单里的 provider 但改了 base_url"这种偷换。授权 guard 于是从"硬编码一个目标"变成"可审计的清单"，新增目标是一次刻意、可复核的动作。

### 验证

- **完整离线收尾：`status=pass`，34 个模块、417 项测试全部通过（0 失败）**（414 + 本轮新增 3），源码前后一致，
  开发集 `gap-replay` / `gap-tools` 13/13，基线 0/13 与负对照为预期失败。见 [final-code/engineering.json](final-code/engineering.json)。
- **入口冒烟：一次通过，无限流。** 走真实批次入口（`--smoke`），`effective_config` 报 `LLM_PROVIDER=tokendance`、`ambient_overridden={}`，
  模型返回完整参数。见 [live-smoke/](live-smoke/)。

---

## 三、未验证的边界（不要过度解读）

- **探针用的是手工构造的 2–3 函数契约**，不是 agent 真实的 `tool_definitions(state)`（最多 5–6 个函数）。**真实全量契约尚未在新端点跑过。**
- **没有在新端点上跑过验收批次**，可用性是否真的改善**未测量**。探针的 0/5 vs 1/5 样本太小，说明不了任何事。
- **切换是全局的**：`resolve_llm_config` 被 planner、composer、verifier、fact_extractor、ddi_extractor 共用。**只有规划路径做了契约资格验证**，其余路径（尤其 DDI 结构化抽取）在新模型上的行为未验证。
- 没有对比过新端点的**成本/计费**；`.env` 里的 key 是否含额度由使用者自行确认。

---

## 四、非可比性声明

**本轮之后的任何在线结果，与之前官方 Zhipu 批次不可比较。** 不同端点、不同模型、不同容量池。早先的 `planner-closeout-v3-2026-09-11` 批次**保持为它自己的记录**，不因本次切换而重述或覆盖。

要评价切换效果，必须**新开一个冻结批次**（新目录、新指纹），并把它与 Zhipu 批次的差异归因到"端点"，而不是"协议变好了"。
