# 规划协议 v3 真实模型验证 · 2026-09-11

**两个问题的直接回答：**

1. **最终 schema 与默认不重试配置，是否已经获得真实模型验证？——是。**
   供应商接受了 v3 请求形状（具名函数 + `tool_choice="required"`，无降级、无 400），
   真实模型在 v3 schema 下**自己填写了 `query`**（此前 8/8 次全部遗漏），
   四次模型响应全部通过校验并被接受，参数拒绝 0、解析失败 0、代码代填 0。
   本批 `PLANNER_PROVIDER_RETRIES=0` 为实际生效值，已在派发前用真实解析器回读核对，无环境变量覆盖。

2. **本批是否达到无兜底自主规划门槛？——否，1/3，未达 2/3。**
   同时供应商实际响应率 4/11 = 36.4%，也未达 2/3。
   但**剩余失败全部是 429/1305 限流，不是协议或参数问题**。
   这一批不足以证明生产稳定性，也不足以否定协议修复。

---

## 一、请求协议检查与根因证据

### 1.1 根因

此前对外发送的不是"具名函数 + 明确参数"，而是**一个名为 `propose_next_action` 的函数，其 `arguments` 是一个没有属性、没有说明、也不在必填列表里的空对象**。模型能看见的具名属性只有 `decision`/`tool`/`gap_id`/`expected_observation`，而它**精确地填了这几个**，跳过了那个不透明的 `arguments`。

证据链：

| 证据 | 内容 |
|---|---|
| 采样期 schema（git 差分） | `"arguments": {"type": "object"}`，`"required": ["decision"]` |
| 在线失败记录 | 3 次采样共 8 个有响应的提案，**全部**没有 `arguments`；6 次因 `arguments.query is required` 被拒，2 次 memory_read 由代码补 `snapshot` |
| 载荷自述 | `payload.protocol.tool` 的示例直接写着 `"arguments": {}` |

### 1.2 排除"解析丢参数"（用户点名的一节）

**结论：解析没有丢参数，是模型真的没给。**

- `LLMPlanner._parse_response` → `_parse_json` 是 `json.loads` 直通，**不做任何字段过滤**；
- trace 里记录的 `proposal` 就是 `propose()` 的返回值本身（`agent.py` 中 `proposal = self.llm_planner.propose(state)` 后原样进入 `_trace`）；
- 新增回归 `test_parsing_preserves_every_argument_the_provider_returned` 固定这一点：含未知键的投喂参数必须逐键保留。

### 1.3 上一轮最后那次修改为什么仍然不够

上一轮把 `arguments` 改成必填，并把所有允许工具的字段**并成一个扁平并集**。实测（`wire-audit/flat-union-protocol-v2.json`）表明：

```
rag_search 必填 = 无（arguments.required = None）
arguments.properties = [drug_name, focus_medication, medications, operation, query, section, top_k]
description = "...各工具必填字段：memory_write: operation; memory_read: query"
```

即：**"哪个工具需要哪些参数"仍然只写在中文散文里**——正是已经失败 8/8 的那种信号。模型会填 schema 里具名的属性，不会从散文里推断必填项。

（顺带修掉一个真实漂移：无 executor 的回退目录 `PLANNER_ARGUMENT_SCHEMAS` 用的是 `argument_schema`（执行器接口），会把 `warnings`/`context_refs` 这类"只能由代码水合、提示词明令模型不得提供"的字段暴露给模型。已改为 `model_schema`，与 `ToolExecutor.catalog()` 一致。）

---

## 二、实际修改与针对性验证

### 2.1 修改

**协议 v3：每个当前允许的工具暴露为自己的具名函数。**

- `LLMPlanner.tool_definitions(state)`：逐个工具生成函数，`parameters` 直接取该工具的 `model_schema`（与校验器同源），并附上调查元数据 `gap_id`/`expected_observation` 作为**具名必填属性**；
- `LLMPlanner._proposal_from_call()`：把具名调用**转回既有内部提案形状** `{decision, tool, arguments}`，元数据从 `arguments` 中提回顶层。**校验器、执行器、权限、证据、预算一律未改**；
- `tool_choice="required"`；若供应商以 400 拒绝该值，**一次性降级为 `auto`**（400 在远端执行前被拒，不计费、不消耗限流重试）；
- 旧 `propose_next_action` 契约仍可解析（缓存或异构供应商不会打断回合），`function_schema()` 保留给默认关闭的 review_worker；
- 未自动补造检索 query；具名调用缺必填参数**照旧拒绝**（`test_named_call_missing_query_is_still_rejected_not_filled_in`）。

`EXECUTOR_ONLY_ARGUMENTS` 保证 `warnings`/`context_refs`/`reported_event_ref`/`warning_ref` 等永不进入对外 schema。

### 2.2 离线验证

- **新增 11 个协议回归**（`NamedFunctionProtocolTests`），含一个走 `HybridPlanner.decide()` 的端到端用例——`handle()`、LangGraph runner、持久待办 worker 共用这条路径；
- **完整离线收尾：`status=pass`，34 个模块、405 项测试全部通过（0 失败、0 跳过）**，源码前后一致（上一轮为 394，+11 即本轮新增协议回归）；
  开发集 `gap-replay` 13/13、`gap-tools` 13/13，冻结基线 0/13 与负对照 0/1 为预期失败。见 [final-code/engineering.json](final-code/engineering.json) 与同目录日志。
  幂等与事务原子性、重启后累计预算、患者事实版本、证据范围与哈希、取消优先于迟到结果等既有回归均包含在 405 项内。

  **覆盖边界（不要过度解读）**：具名函数的**解析**只有一条实现（`LLMPlanner._parse_response`），
  普通会话、LangGraph runner、持久待办 worker 都经 `HybridPlanner.decide()` 走它，本轮用一个端到端用例固定了这条路径；
  但 **LangGraph 与持久待办自己的模块用例用的是脚本化 `proposal_provider`，会绕过解析**——它们证明的是这两条路径的
  预算/取消/重启/终态契约没被破坏，**不等于**具名函数响应在这两条路径上各自被独立验证过。
  在线抽样走的是普通会话评测路径。
- 状态扫描（`wire-audit/named-function-protocol-v3.json`）确认每个状态下必填要求一致：

| 状态 | 允许工具 | 各工具必填 |
|---|---|---|
| authority 未读 | memory_write, memory_read | operation / query |
| claim gap 打开 | + rag_search, ddi_check | + query / medications |
| 已有未读证据 | + read_evidence | + evidence_id |
| 代码终止后 | respond | （无） |

`memory_read.query` 保留其枚举，`rag_search.query` 不被施加该枚举——共享参数合并的枚举污染已随扁平并集一并消失。

---

### 2.3 在线协议冒烟（先于批量，避免浪费一次性批次）

三次冒烟共 9 次尝试、8 次 429，第 4 次（宽退避）拿到响应：

```
returned_functions = ["rag_search"]
raw_arguments = {"query":"阿司匹林 布洛芬 相互作用 药物相互作用",
                 "gap_id":"interaction_check",
                 "expected_observation":"找到关于阿司匹林和布洛芬相互作用的证据"}
```

三点结论：供应商**接受** `tool_choice="required"`（无 400、批量中 `tool_choice_degraded_attempts=0`）；
接受 `respond` 的空 `required` 数组；**真实模型在 v3 schema 下自己填写了 `query`**——正是统一 schema 下 8/8 遗漏的那个字段。
该冒烟用最小双函数集合（rag_search + respond）验证请求形状，**不代表**完整契约；完整契约由批量检验。

## 三、新批次协议、实际配置与复现入口

**入口**：`scripts/run-planner-live-acceptance-v3.py`（默认拒绝远程；`--enable-live` 才生效）。
旧 `run-planner-live-acceptance.py` 固定 `retries=1`，**不能**代表当前默认配置，保留仅作历史复现。

**冻结配置**（写入 `live/effective-config.json`，并在派发前用真实 `LLMPlanner._provider_retry_limit()` 回读校验）：

```
provider=official_zhipu  model=glm-4.7-flash
base_url=https://open.bigmodel.cn/api/paas/v4
planner_provider_retries=0      <- 产品默认，实测生效
planner_arg_autocorrect=1   planner_tool_choice=required
planned_k=3  seconds_per_run=180  calls_per_run=8  max_cycles=12
ambient_overridden={}           <- 无环境变量覆盖
```

批次指纹 `19618726cc18156acb78cc652720bdeae2474bcf2a330b240fbef21d34b4267a`，采样期间 `source_unchanged=true`。

**入口契约**：默认禁网；新目录必须不存在（含部分执行批次）；每次派发前落盘 manifest；不追加样本；保留全部成功/失败/限流/预算耗尽记录。

```powershell
# 协议冒烟（1 次真实调用，验证供应商接受 v3 请求形状）
.venv/Scripts/python.exe scripts/run-planner-live-acceptance-v3.py --enable-live --smoke --out <新目录>

# 固定 k=3 批量
.venv/Scripts/python.exe scripts/run-planner-live-acceptance-v3.py --enable-live --out <新目录>

# 指标
.venv/Scripts/python.exe scripts/analyze-planner-metrics.py '<live>/live-final-*.json' --out <新文件>
```

未购买额度、未启用付费模型、未发送真实患者数据；仅使用既有智谱免费额度与既有合成开发任务。

---

## 四、三次原始在线结果与逐次指标

原始件：[live-final-1.json](live/live-final-1.json)、[live-final-2.json](live/live-final-2.json)、[live-final-3.json](live/live-final-3.json)，逐次指标 [metrics.json](live/metrics.json)。

| | 样本 1 | 样本 2 | 样本 3 |
|---|---|---|---|
| 任务结果 | 通过 | 通过 | 通过 |
| 无兜底 | **是** | 否（provider_error） | 否（provider_error） |
| 自主规划成功 | **是** | 否 | 否 |
| 尝试 / 响应 / 用量未知 | 3 / 3 / 0 | 4 / 0 / 4 | 4 / 1 / 3 |
| 提案接受 / 拒绝 | 3 / 0 | — | 1 / 0 |
| 缺参数 / 解析失败 / 安全拒绝 | 0 / 0 / 0 | 0 / 0 / 0 | 0 / 0 / 0 |
| 新证据 / 新回读 / gap 变化 | 1 / 1 / 3 | 1 / 1 / 3 | 1 / 1 / 3 |
| 限流 / 超时 | 0 / 0 | 4 / 0 | 3 / 0 |
| 已知 token | 11,507 | 0 | 5,293 |
| 首次后端进度 | 4,027 ms | 3,581 ms | 3,637 ms |
| 总耗时 | 6,876 ms | 4,075 ms | 5,110 ms |
| 重复业务写入 | 0 | 0 | 0 |
| 错误完成 | 无 | 无 | 无 |

> 注：样本 2、3 的"新证据/新回读/gap 变化"来自**规则兜底**执行的工具，不是模型决策——
> 样本 2 模型一次都没被真正问到。该行只描述状态变化，不代表自主性。

**样本 1 是整轮系列中第一次真正的无兜底自主核查**，全部由模型自选并执行：

```
memory_read(query="snapshot")                      ← 模型自填，corrections=[]
rag_search(query="合成药甲 合成药乙 药物相互作用 证据", top_k=5, gap_id=..., expected_observation=...)
read_evidence(evidence_id="ev-6cd864426aa9d41485ac") ← 回读上一步检索到的证据
→ 代码置 termination_reason=checks_completed
```

三个提案的 `argument_corrections` 均为空——**没有代码代填任何参数**（整批 `autocorrected_proposals=0`）。这不是"读一次药单就收工"：它完成了检索、证据回读与 gap 推进。

**合并口径**（3 次）：任务通过 3/3，无兜底 1/3，自主规划 1/3，供应商实际响应 4/11 = 36.4%，
提案接受 4/4 = 100%，缺参数 0，解析失败 0，安全拒绝 0，代码代填 0，重复写入 0，错误完成 0，
已知 token 16,800，7 次用量未知（不推断为零），总耗时中位数 5,110 ms。

### 4.1 供应商可用性

协议冒烟 9 次尝试中 8 次 429，第 4 次（宽退避）才拿到响应；批量 11 次尝试中 7 次 429。
**本批未达门槛的原因可归因于供应商可用性，而非模型协议**：

- 有响应的 4 个提案 **100% 被接受**，参数拒绝与解析失败均为 0；
- 样本 2 的 4 个周期**全部**是 429 → `provider_error` → 应急兜底，模型一次都没被真正问到；
- 样本 3 前 3 个周期 429，第 4 周期响应后即被接受。

需要说明的配置影响：产品默认 `PLANNER_PROVIDER_RETRIES=0` 取消了上一协议中那次有界的 429 重试。
在本轮观测到的限流强度下，这会把单次 429 直接变成该周期的应急兜底，**降低无兜底达成率**。
本轮按要求以默认 0 采样，未额外跑 `retries=1` 对照，因此**不能**给出两者差值的受控估计。

历史批次（09-10、上一轮）源码与协议均不同，**只作诊断参考，不是同源受控 A/B**。

---

## 五、最终源码与在线采样源码一致性

**一致（字节级）。** 见 [source-identity.json](final-code/source-identity.json)。

需要说清楚一个陷阱：本轮涉及的**两个指纹函数覆盖的文件集不同**，因此它们的摘要值**不可互相比较**——
在线入口覆盖 `stage0/**.py + frontend/src/**.{ts,tsx} + scripts/*.py`，
而离线收尾 `verify-agent-closeout.py` **不含 `scripts/`**。直接比这两个数字会得出"不一致"的错误结论（我确实先撞上了这个假警报）。

正确的判据是各自重算并匹配自己的记录值：

| 作用域 | 采样/收尾时记录 | 现在重算 | 匹配 |
|---|---|---|---|
| 并集（含 `scripts/`，**约束性**） | `19618726…4267a` | `19618726…4267a` | 是 |
| 收尾（不含 `scripts/`） | `8076f881…dc7b` | `8076f881…dc7b` | 是 |

并集作用域已覆盖 `scripts/`，其重算值与采样时一致 ⇒ **采样后未改动过任何 `stage0/`、`frontend/src/`、`scripts/` 文件**；
批量自身 `source_unchanged=true`，收尾自身 `source_unchanged=true`。
最终离线收尾（405 项测试、34 个模块、4 项开发集评测）运行在这份未变的源码上。

> **这是时点记录，不是当前状态。** `checked_at = 2026-09-11T11:50Z`。
> 此后开展的产品健壮性轮（429 预算退还、退避封套、供应商不可用标记与重试入口）
> 有意改动了 `stage0/` 与 `scripts/`，**现在重算指纹不会再等于 `19618726…4267a`**——
> 那是预期的。本批在线结果仍然只代表它采样时的那份源码。

---

## 六、仍未解决的问题与下一步必要条件

1. **供应商可用性是当前真正的瓶颈**（本批 36.4% 响应率）。在拿到稳定响应之前，任何"自主规划成功率"的估计都只是可用性噪声。
   下一步必要条件：更稳的配额/时段，或在不改变语义的前提下恢复有界 429 重试并**单独成批**做对照（不得与本批混算）。
2. **本批只有 k=3、且是作者合成开发集的单个已暴露任务**，不能外推到生产稳定性，也不是独立盲测。
3. **默认关闭的 review_worker 仍使用旧的统一 `propose_next_action` 契约**（`harness/model_review.py`，多 Agent 复核默认关）。本轮按"不扩展多 Agent"未改动，其 schema 风险与本次修复前同源。
4. **真实本地检索全链路未复测**（`--path tools` 离线 13/13，但未做在线全链路）。
5. **未执行独立 held-out 归因重跑。**
6. 无兜底门槛需要至少 2/3；在 36.4% 响应率下，即使协议完全正确，达成概率也很低——**先把响应率提到 2/3 以上，本门槛才具备可测性**。

---

## 附：本轮修改文件

- `stage0/agent.py` — 协议 v3（`tool_definitions`/`_proposal_from_call`/`tool_choice` 与降级/`PROPOSAL_META_KEYS`/`EXECUTOR_ONLY_ARGUMENTS`/模型面向 schema 回退修正）
- `stage0/test_planner_reliability.py` — 新增 11 个协议回归
- `stage0/test_stage6.py` — 更新为 v3 载荷契约
- `stage0/agent_evals/run_eval.py` — 产物记录 `planner_protocol_version`
- `scripts/run-planner-live-acceptance-v3.py`、`scripts/planner-wire-probe.py` — 新增
- `scripts/analyze-planner-metrics.py` — 增补逐次指标字段
