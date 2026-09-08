# Harness P3 实验报告：只读专项 Agent 委派（有条件试验）

> 历史实验：长证据场景三方案回读量不同，不能据此比较采用收益。2026-09-07 已改为等量完整回读，见 [实施收尾报告](implementation_report.md) 和 `../final-acceptance/p3-eval.json`。当前开关仍默认关闭。

日期：2026-09-06。作者：编码 Agent（Claude Code / GLM）。
机器可读工件：`docs/harness-upgrade/P3/experiment_report.json`（dataset `p3-1.0`，`--repeat 3` 取中位）。

## 0. 实验设计（先限定，后运行）

### 0.1 三种方案

| 方案 | 实现 | 说明 |
| --- | --- | --- |
| A 单主 Agent | 每 queryInterface 一次 planner 决策 + 顺序派发（现状生产行为） | scripted provider 按观察逐条提出 rag_search / read_evidence |
| B 普通只读批处理 | `batch_read`（`STAGE0_READ_BATCH`，默认关）：1 次规划决策，执行层 `execute_read_batch` 把纯读批量扇出 | 每个条目仍走同一 `execute` 路径逐项校验 |
| C 只读专项 worker | `delegate_task`（`STAGE0_DELEGATED_WORKERS`，默认关）：1 次规划决策，固定职责 worker（EvidenceRetriever / EvidenceConsistencyChecker）执行 | worker 为**确定性只读流水线**（`worker_model=None`，见 §3 诚实边界） |

三个模式使用**同一工具目录**（三个工具在所有侧都注册），差异只在 planner 决定什么——比较的是规划组织方式，不是注册表不对称。

### 0.2 场景（合成，离线，临时库）

1. **multi_drug_labels（复杂 #1）**：4 种慢性病药 + 新增第 5 种，5 条检索查询（多来源检索）。必要检查：5 条查询全部派发。
2. **long_label_consistency（复杂 #2）**：~4k 字符长标签（相关段落在末尾），需分页回读证据并逐字核对 3 条声明。
3. **simple_current_meds（简单）**：直接查询当前用药。

### 0.3 预注册验收标准（运行前写明）

- **GAIN**：planner 决策调用，多来源场景减少 ≥50%、长标签场景减少 ≥25%；安全/引用结论与基线一致。
- **CONTEXT**：委派 worker 相对批处理的 payload 优势需达**实质性水平（≥20% 削减）**才采用；否则视为"未证实相对批处理的额外收益"。
- **COST**：`tokens_charged` 开销 ≤ +20%。
- **SIMPLE**：简单查询零委派。

### 0.4 延迟口径（诚实声明）

假 provider 单次调用是微秒级。实验注入**合成延迟模型**：每次 planner 决策 300ms、每次检索 50ms（`STAGE0_P3_SIM_PLANNER_MS/_IO_MS` 可调），用于把"规划往返次数"映射为可感知的墙钟差异。所有 wall_seconds 均为**建模值**，不代表真实系统耗时；真实 planner 单次 18–60s（Stage 6 实测），因此"决策调用数"才是生产成本的有效代理。

## 1. 实测结果（repeat=3 中位）

### 1.1 multi_drug_labels（多来源检索）

| 模式 | 决策调用 | payload 均值 chars | rag 派发 | tokens_charged | 建模耗时 s | 委派数 | 安全 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| single_agent | 7 | 9714.1 | 5 | 46753 | 2.555 | 0 | enforced |
| batch | 3 | 11694.3 | 5 | 24477 | 1.273 | 0 | enforced |
| delegate | 3 | 8124.7 | 5 | 19321 | 1.272 | 1 | enforced |

### 1.2 long_label_consistency（长证据核对）

| 模式 | 决策调用 | payload 均值 chars | rag 派发 | 回读页 | tokens_charged | 建模耗时 s | 委派数 | 安全 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| single_agent | 6 | 8028.8 | 1 | 3 | 33464 | 2.000 | 0 | enforced |
| batch | 4 | 8197.5 | 1 | 3 | 22994 | 1.373 | 0 | enforced |
| delegate | 4 | 7868.0 | 1 | 9 | 24234 | 1.344 | 1 | enforced |

（delegate 的回读页 9 = checker 对 3 个 evidence 各读 3 页并做逐字核对；payload 中只回 verdicts + 摘录。）

### 1.3 simple_current_meds（简单查询）

三模式完全一致：3 次决策（consolidate → memory_read → respond）、零委派、零批处理、`enforced`。**验收 SIMPLE 通过。**

### 1.4 预注册判定

| 判定项 | 结果 | 依据 |
| --- | --- | --- |
| GAIN（决策削减） | ✅ | multi 57.1%、long 33.3%（batch 与 delegate 相同） |
| CONTEXT（严格：delegate ≤ batch） | ✅ | 7868 ≤ 8197.5 |
| CONTEXT（实质性：≥20% 削减） | ❌ | 仅 **4.0%**（见 §2 判读） |
| COST（≤+20%） | ✅ | tokens 全部低于基线（委派预留为保守记账） |
| SIMPLE（零委派） | ✅ | 三个模式在简单场景均零委派 |
| 安全/引用一致 | ✅ | 三侧全部 `enforced`、citation_validity=1.0、无医疗权威表述 |
| **adopt_delegation** | **否** | 未证实相对批处理的实质性额外收益 |
| **adopt_batching** | **是（候选）** | 满足 GAIN/COST/SIMPLE/安全一致 |

## 2. 结论判读（为什么委派保持默认关闭）

1. **流程级收益批处理已全部拿走。** 决策调用削减（57% / 33%）与建模耗时下降在 batch 与 delegate 上**完全相同**——收益来源是"把 N 次规划往返折叠为 1 次"，与是否引入子 Agent 无关。
2. **payload 隔离优势只有 4.0%，低于 20% 实质性阈值。** P1-B 的证据卸载 + payload 压缩（字符串截断至 600/200 字符）已经把"长证据涌入规划上下文"的问题在单 Agent 内解决了一大半；委派返回的小结果相对批处理单观察的优势因此很小。
3. **委派的核心卖点（worker 独立 LLM 上下文窗口）在确定性流水线下无法兑现，也无法证伪。** 当前 worker 无模型（`worker_model=None`），"专项 Agent"的隔离收益只能等真实 worker 模型出现后再评估。本实验**不声称**任何真实模型质量收益。
4. **成本侧委派是纯增项**：2000 tokens/任务的保守预留（不退还）+ 状态机/回执/门禁机制面。收益相同时，机制面更小的方案优先。

## 3. 诚实边界（P3 约束 12）

- 本次实验全部使用**脚本化假 provider** 与**确定性只读流水线 worker**。报告中"专项 Agent"指**协议与运行时机制**，不代表发生了真实模型规划/调用。
- 建模耗时（planner 300ms / IO 50ms）是人为参数；生产收益的正确读数是**决策调用数**（每次 18–60s，Stage 6 实测）。
- `adopt_batching=True` 也只是流程结论：`batch_read` 保持默认关闭，真实模型/真实语料负载下的收益需 live 验证后再灰度。

## 4. 可复现命令

```bash
PYTHONPATH=. python stage0/harness_p3_eval.py --out docs/harness-upgrade/P3/experiment_report.json --repeat 3
.venv/Scripts/python.exe -m unittest stage0.test_harness_p3   # 22 项协议/安全/恢复测试
```
