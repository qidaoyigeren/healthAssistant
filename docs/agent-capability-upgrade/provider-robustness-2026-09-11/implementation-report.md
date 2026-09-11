# 供应商抖动下的产品健壮性 · 2026-09-11

本轮按"先修产品健壮性，测量其次"执行：让产品在模型供应商不可用时**不静默退化**——
确定性拒绝不吃预算、退避够得着供应商的真实恢复时间、不可用状态对用户可见且可重试。

**未做在线采样。** 本轮的结论全部来自离线回归；下一次验收批次需要另行冻结配置。

---

## 一、触发证据

上一批（协议 v3，k=3）剩余失败**全部是 429**，我原先把它归因为"供应商可用性"。重新读代码后发现其中**有一部分是我们自己造成的**：

| 证据 | 位置 | 含义 |
|---|---|---|
| `min(5.0, max(0.1, env))` | [agent.py](../../../stage0/agent.py) 退避 | 退避**硬上限 5 秒**，即便把 `PLANNER_PROVIDER_RETRY_BACKOFF_SECONDS` 设成 60 也只睡 5 秒 |
| 冒烟实测时间戳 | `planner-closeout-v3-2026-09-11/live-smoke-3/protocol-smoke.json` | 间隔 21 s、40 s 仍 429，**~61 s 才成功**——重试窗口比恢复时间短约 12 倍 |
| `calls_attempted += 1` 调用前自增、异常不回滚 | [turn_budget.py](../../../stage0/turn_budget.py) `call()` | 一次 429 **永久吃掉 8 次调用预算中的一次**，尽管根本没有模型运行过 |
| 样本 2 四个周期全 429 | `live-final-2.json` | 4/8 调用预算被从未执行的调用消耗 |

第三行是关键：上一批里"模型一次都没被问到"的那次采样，**部分原因是预算被拒绝本身吃掉了**。

另有一处同源问题：被退还的调用此前仍会计入 `tokens_charged`（估算值），即一个从未运行的调用会消耗 token 预算。

---

## 二、改动

### 2.1 确定性拒绝不再计费（[turn_budget.py](../../../stage0/turn_budget.py)）

- `call(..., refund_if=None)`：调用方给出"这是确定性拒绝"的判定（agent 传 `_is_rate_limit_error`，分类逻辑仍只有一处）。
- 新增单调计数器 `refused_calls`，计入 `COUNTERS`。
- `exhausted()` 以 `calls_attempted - min(refused_calls, call_budget)` 判定。
- **不做递减**：`merge_budget` 对计数器取 `max()`，递减会被 checkpoint 还原抹掉，破坏"重启后累计预算"。改为**偏移量**表达退还，单调性得以保留。
- 退还上限为**一个 call_budget**，因此总尝试数 ≤ 2×call_budget——供应商一直拒绝也会耗尽，不会死循环。
- 被退还的拒绝**不计入 `tokens_charged`**（`tokens_estimated` 照旧记录，那是我们尝试发送的观测值）。
- **账本行照旧写入**（`status='refused'`）：限流历史是证据，不能丢。
- 超时/连接错误**仍然计费**——远端结果未知，保持既有语义。

### 2.2 退避封套（[agent.py](../../../stage0/agent.py)）

`_rate_limit_backoff(attempt)` 改为指数退避，上限由 `PLANNER_PROVIDER_RETRY_BACKOFF_MAX_SECONDS` 决定（默认 20 s，上限 300 s），并继续受"剩余墙钟的 1/5"约束——**重试不能吃掉它正在抢救的那个回合**。

| 场景 | 基数 | 上限 | 重试数 |
|---|---|---|---|
| 交互（默认） | 1.5 s | 20 s | 1 |
| 验收/后台批次（显式配置） | 5 s | 60 s | 2 |

交互默认重试数由 **0 改为 1**：当初设为 0 的理由是"重试耗尽有限调用额度且未证明收益"，而该成本正是 2.1 修掉的东西。`PLANNER_PROVIDER_RETRIES=0` 仍可显式关闭。

### 2.3 供应商不可用对用户可见、可重试（[care_tasks.py](../../../stage0/care_tasks.py)）

先纠正我上一轮的一处**失实表述**：我当时说"任务显示已完成"。那只适用于评测/助手路径；**care-task 路径的降级终态本来就是 `failed`** 并带诚实说明。真正的缺口是另外两点：

1. **标记未落到用户可见处**：`degraded_reason` 只存在于内部，前端从不渲染（`coverage.degraded_reason` 在 `types.ts` 里定义了却无人使用）。
2. **没有重试入口**：`failed` 是终态，`resume` 直接拒绝。

改动：
- 新增 `provider_outage()` 分类：只有**瞬时**原因（`provider_error` / `usage_unknown`）才算不可用；
  契约、安全、预算、schema 失败**不**给重试——重复一次结果不会变。
- 任务新增 `degraded_reason`（诊断）、`degraded_label`（照护者可读）、`retry_available`（布尔）。
- `resume` 只对这一标记开一道门，并且**用掉即清除**；`cancelled` 永不重开；任务预算上限照旧约束重试次数。

文案按"说清状态、说清存了什么、说清能做什么"写，不外泄内部 token：

> 本次未经模型核查：模型服务暂时不可用。以下为依据已保存记录整理的部分结果，可稍后重新核查。

### 2.4 前端

沿用该页既有设计语言（`text-caution`、`buttonClass`、`Badge` 的 `caution` 色调），不引入新的视觉体系：

- **CareTasksPage**：渲染 `degraded_label`；当 `retry_available` 时显示「重新核查」按钮（与既有「继续核查」同一套 resume 通路）。
- **submission**：`coverage.degraded_reason` 指示不可用时，徽标由「记录完成」改为**「未经模型核查」**（caution 色调），并在正文上方给出说明。

`GET /v1/care-tasks` 返回的是原始任务字典，新字段自动随行，无需改接口。

---

## 三、测试

新增 **9 项**离线回归（`stage0/test_planner_reliability.py`），全部先写失败用例再实现：

**ProviderRefusalTests**
- 429 不消耗调用预算；账本行仍在；`refused_calls` 如实计数
- **因果对照**：同一供应商、同一预算，仅关闭退还 → 恢复"被饿死"的旧行为（抛 `BudgetExceeded`）
- 超时**仍然**计费（守住既有语义）
- 一直拒绝的供应商在 `2×call_budget` 内终止，不会死循环
- 被退还的拒绝**不计 token**（与关闭退还的同场景对比）

**BackoffEnvelopeTests**
- 退避按 2→4→8→16 增长并在 20 s 封顶
- 剩余墙钟不足时被夹到 1/5，不会吃掉回合

**ProviderOutageRetryTests**
- 供应商不可用 → 出现照护者可读标记（不含内部 token）、`retry_available` 为真、部分报告保留
- 终态任务只对**带标记**的任务开重试门，且用掉即清除；预算耗尽这类非瞬时失败仍然关闭

**完整离线收尾：`status=pass`，34 个模块、414 项测试全部通过（0 失败）**（405 + 本轮新增 9），
源码前后一致，见 [final-code/engineering.json](final-code/engineering.json) 与同目录日志，
开发集 `gap-replay` / `gap-tools` 13/13，冻结基线与负对照为预期失败。
其中**重启后累计预算、幂等与事务原子性、证据范围与哈希校验、取消优先于迟到结果**的既有回归均在套件内通过——
单调计数器不可递减这一约束是本轮设计的硬前提。

前端：`tsc --noEmit` 通过，生产构建通过（主包 ~518 KB 提示保留）。

---

## 四、验证边界（不要过度解读）

- **没有在线采样。** 退避封套与退还逻辑只在离线与模拟传输层验证；它们对真实响应率的实际提升**尚未测量**。
- **未做浏览器复验。** 前端改动经类型检查与构建，但「未经模型核查」徽标与「重新核查」按钮没有在真实浏览器里点过。
- 交互默认 `PLANNER_PROVIDER_RETRIES` 由 0 改为 1 是**产品默认值变更**，没有伴随的在线对照实验。
- 传输层仍然**不重试超时/连接错误**（远端结果未知，保留既有语义）——供应商超时高发时本回合仍会降级。

---

## 五、仍未解决

1. **盲测任务集不存在。** `dev.json` 的 13 个任务全部用于开发；`run_heldout_v2.py` 是 DDI 语料归因（需 KEGG 网络与付费 key）。规划任务的独立 held-out 要新建任务集，属独立项目。
2. **在线检索全链路未做。** `--path tools` 走 `LocalOnlyRAG`，其 `_get_retriever` 明确抛"embeddings disabled"，刻意不是完整 hybrid+BGE 链路。
3. **默认关闭的 `review_worker` 仍用旧统一 `propose_next_action` 契约**（`harness/model_review.py`）。本轮决定不动：它默认关闭、契约本就不同、且改动没有配套的活路径覆盖，风险大于收益。
4. **退避"最长一次"与"总时长"只各取了一个观测点**（~61 s）。真实的退避分布需要更多采样才能定标。
