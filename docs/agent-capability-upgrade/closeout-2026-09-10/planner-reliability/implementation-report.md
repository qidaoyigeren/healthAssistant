> Historical report. Layer C was executed and additional fixes completed on 2026-09-11. See [current acceptance](../../planner-closeout-2026-09-11/implementation-report.md). Original observations and unavailable status below describe the earlier session only.

# 规划可靠性与等待时间修复报告（2026-09-10）

本轮解决开放式 Agent 的三个问题：依赖规则兜底、真实模型规划成功率低、响应等待过长。
验收协议冻结于任何验证执行之前：[acceptance-protocol.md](acceptance-protocol.md)（sha256 前 16 位 `5caf74a8d8515d76`）。
本报告不覆盖 [上一轮结项报告](../implementation-report.md)；A0–A5 的历史结论以该文件为准。

## 一、根因与轨迹证据

三次真实模型测试（live-final-1/2/3，任务 rubric 3/3 通过、自主规划 0/3）的逐周期归因。阶段耗时实测：**LLM 规划调用占墙钟 89%（83.6s/93.8s），工具与本地开销仅 4.9s**——等待瓶颈不在工具，在模型往返次数与单次延迟。

| 归因类别 | 判定 | 轨迹证据 |
|---|---|---|
| 1. 供应商不可用/超时 | **成立，主因之一** | 13 次规划尝试中 8 次返回 429/1305「该模型当前访问量过大」（run1 c1-c3、run2 c1、run3 c1/c3/c4/c5）；实际响应率 38.5%（5/13）。实际响应单次延迟 5.0s/13.3s/13.9s/15.4s/35.9s（run3 c2 一条调用 35.9s，usage_ledger created 13:23:28 → settled 13:24:03），为供应商侧排队，非本地开销 |
| 2. 模型未理解工具契约 | **成立** | run1 c4/c5：rag_search 找到证据后直接提案 `respond`，跳过 `read_evidence` 回读——"搜到"与"已读取并验证"的区分未传达到位。run2 c2：authority 缺口期间提案 `ddi_check`（契约只允许 memory_read snapshot）。run2 c2/c3、run3 c2：`ddi_check` 缺 `medications`、`memory_read` 缺 `query` 参数 |
| 3. 输入上下文/错误反馈问题 | **成立，且是拒绝后重复失败的直接原因** | run1 c4 被拒后，c5 提案与 c4 **逐字节相同**（temperature=0）：错误信息虽存在于 recent_trace 的 planner JSON 内，但不是可执行反馈——被拒原因 `investigation_not_terminal` 只说"proposal must address a current gap and observable outcome"，未说明还差 read_evidence。run_open_review 路径更糟：拒绝完全不进 trace，下一轮模型根本看不到 |
| 4. 校验器误拒绝 | **部分成立** | `materialize()` 本就从权威记忆水合 `ddi_check.medications`（agent.py 旧 1045-1054 行），但 `validate()` 先以 `missing_required_arguments` 拒绝——与"只拒绝不可纠正提案"的设计自相矛盾。run2 c3 的 `memory_read` 缺 `query` 同理：authority 缺口下 snapshot 是唯一合法值，可自动补齐。安全类拒绝（未读证据先 respond、authority 契约、医疗权威边界）复核后全部正确，**无安全规则被误判** |
| 5. 不必要的串行/重复 | **部分成立** | payload 每周期重复调用两次 `_patient_snapshot()`（快照 JSON 在 payload 内重复）；观测/轨迹每周期有界增长（实测 payload 8k→14.5k 字符）。串行本身符合证据契约（authority→检索→回读），非主要问题；batch_read 并行只读已存在（默认关闭），本轮实测瓶颈不在并行度 |

熔断行为复核：`PLANNER_SAFETY_REJECTION_LIMIT=2` 使 run1/run2 各只获得 1 次反馈机会，而反馈又不可执行（类别 3）——三个类别叠加导致 5 次真实提案全部被拒、0 次自主规划成功。

## 二、代码修改及作用

规划协议（`stage0/investigation.py`、`stage0/agent.py`，协议版本 `propose-next-action@2`）：

1. **状态条件工具目录**：`investigation.allowed_tools()` 按当前状态（authority 未读 / 缺事实 / 证据缺口 / 已终止）只暴露合法工具；payload `tool_catalog` 与 `correction_task.allowed_tools_now` 同步收窄。展示层收窄，校验器仍为唯一权威且是收窄集合的超集。
2. **明确"搜到"≠"已读取并验证"**：planner view 新增 `evidence_unread`、`evidence_searched_count/read_count`、`open_gaps`；协议注释明确"引用前必须 read_evidence 回读"。
3. **可执行错误反馈**：被拒提案注入下一轮 payload 顶层 `correction_task`（被拒提案、逐条原因、当前允许工具、代码侧下一步建议）；`investigation_not_terminal` 等七个拒绝码的提示改为具体动作（含"证据已检索但未回读: [...]，先 read_evidence"）。两条执行循环（`_handle`、`run_open_review`）都被接线；后者此前拒绝完全不可见。
4. **修正次数受限 + 相同提案快速熔断**：同一提案逐字节重复被拒 → 立即熔断（不再消耗注定失败的模型调用）；连续拒绝阈值不变（默认 2）。
5. **respond 终止条件保持代码控制**：`termination_reason` 未设置时 respond 一律拒绝（规则未放松，反馈变具体）。
6. **版本化统一契约**：`PLANNER_PROTOCOL_VERSION='propose-next-action@2'` 写入 payload 与 investigation 视图；删除 `registered_planner_tool_schemas` 重复定义（executor 目录路径曾被第二个定义遮蔽成死代码）。

减少等待（`stage0/agent.py`）：

7. **429 有限重试**：限流类失败（429/1305，结果确定被拒、不会重复计费）每规划调用最多重试 1 次（`PLANNER_PROVIDER_RETRIES`），退避 1.5s 且受剩余墙钟约束；超时/连接错误**不重试**（usage 未知保护，维持既有账本语义）。每次尝试独立入账。
8. **payload 瘦身**：快照单次计算；有 investigation 时 `patient_memory_snapshot` 改为指向 `investigation.facts` 的紧凑指针（权威事实/版本/冲突仍完整保留在 investigation 视图内，无静默丢失）；`context_omissions` 复用同一快照。
9. **拒绝不再烧周期**：run_open_review 中相同提案重复即停，不再烧完剩余调用预算。

校验器误拒修复（安全规则零放松）：

10. **可纠正缺参自动补齐**：`ddi_check.medications`（权威药单水合）、authority 缺口下 `memory_read.query`（水合为 snapshot）——纯缺键时接受并记录审计 correction；任何非缺键错误照旧拒绝；`PLANNER_ARG_AUTOCORRECT=0` 一键恢复严格拒绝。

前端与进度（`stage0/server.py`、`frontend/src/features/tasks/CareTasksPage.tsx`）：

11. **care-task worker 里程碑事件**：受理/完成/失败写入真实进度账本（此前该分支只有工具级事件）。
12. **照护待办页接通进度事件**：running 时每 2s 轮询 `GET /v1/runs/{id}/progress` 渲染最新真实事件（已受理/整理记录/检索依据/核对风险/等待人工审核）；run 状态 degraded 显示"部分步骤使用兜底完成，报告如实标注"。无伪造进度条。

## 三、离线验证（全部通过）

| 验证 | 结果 | 证据 |
|---|---|---|
| A: 原三次失败提案零网络回放 | 3/3 通过，0 远程调用；run2 降级原因由 `circuit_break:safety_rejections` 变为 `provider_error`（缺参提案经自动补齐被接受，证明修复作用于真实记录数据） | [recorded-replay-after-fix.json](../../../../output/planner-reliability-20260910/recorded-replay-after-fix.json) |
| A: 协议 v2 回归（反馈注入/相同提案熔断/可纠正参数/具体消息/状态目录/429 重试/超时不重试） | 10/10 通过 | `stage0/test_planner_reliability.py` |
| B: 冻结开发集 gap-replay / gap-tools | 13/13 与 13/13 | [gap-replay.json](../../../../output/planner-reliability-20260910/gap-replay.json)、[gap-tools.json](../../../../output/planner-reliability-20260910/gap-tools.json) |
| B: 负对照（故意错误必须被检出） | 正确 0/1 | [gap-replay-negative.json](../../../../output/planner-reliability-20260910/gap-replay-negative.json) |
| B: 全量工程套件（33 模块隔离进程） | 388 项执行 0 失败 0 跳过，源码指纹运行前后一致 | [engineering.json](../../../../output/planner-reliability-20260910/engineering/engineering.json) |
| 前端 TypeScript + Vite 构建 | 通过（既有 517KB 主包提示不变） | `npm run build` 输出 |

## 四、复现入口

```powershell
# 一键离线验证（隔离进程全量回归 + 4 项 eval；默认不访问网络）
.venv/Scripts/python.exe scripts/verify-agent-closeout.py --out output/<new-dir>

# 原失败轨迹零网络回放
.venv/Scripts/python.exe scripts/replay-agent-closeout.py --directory docs/agent-capability-upgrade/closeout-2026-09-10 --out output/<new-dir>/replay.json

# 协议指标分析（只读工件，不调用）
.venv/Scripts/python.exe scripts/analyze-planner-metrics.py <live-final-*.json> --out <report.json>
```

## 五、真实调用验收入口（明确启用才执行）

```powershell
# 显式启用：--only-live --live-repeats 3 才发起真实调用；k=3 冻结，结果文件存在即拒绝重跑
.venv/Scripts/python.exe scripts/verify-agent-closeout.py --out output/<new-dir> --only-live --live-repeats 3
```

使用既有授权范围：官方智谱 glm-4.7-flash 免费层、合成患者数据、固定检索材料（replay 路径）、每 run 180 秒/8 调用——与基线完全同源同预算。保留全部成功与失败。

## 六、指标表

### 修复前基线（live-final-1/2/3，2026-09-10 收端口径）

| 指标 | 值 |
|---|---|
| 任务 rubric 通过 / 无兜底通过 / 自主规划成功 | 3/3 · 0/3 · **0/3** |
| 供应商实际响应率 / usage 未知次数 | 38.5%（5/13）· 8 |
| 模型提案接受率 / 有效推进 gap 比例 | **0/5** · — |
| 安全拒绝 / 参数拒绝 / 校验器可纠正误拒 | 5 · 3 · 3 |
| 已知 token / 每任务模型尝试 | 17,707 · 13（4.3/run） |
| 总耗时逐次值（中位） | 25.1 / 29.6 / 39.2 s（29.6 s） |
| 错误完成 / 无依据结论 / 重复业务写入 | 0 · 0 · 0 |

### 修复后（k=3，冻结协议，见第五节入口执行）

结果写入本节：见文末「验证 C 结果」。

## 七、产品默认配置与回退

| 配置 | 默认 | 回退 |
|---|---|---|
| `PLANNER_ARG_AUTOCORRECT` | 1（可纠正缺参自动补齐） | 设 0 = 恢复严格拒绝 |
| `PLANNER_PROVIDER_RETRIES` | 1（仅限流；退避 `PLANNER_PROVIDER_RETRY_BACKOFF_SECONDS`=1.5s） | 设 0 = 恢复失败即兜底 |
| `PLANNER_SAFETY_REJECTION_LIMIT` | 2（不变） | 调整阈值或设 0 走旧路径 |
| `correction_task` / 状态条件目录 / 相同提案快速熔断 | 常开（协议 v2） | 无开关；行为由回归测试锁定，回退需回滚本轮代码 |
| `AGENT_MULTI_REVIEW_*`、batch_read、delegate | 不变（默认关闭） | 不变 |

可靠性边界未放松：幂等键含业务内容摘要、共享事务补充、派发前预算预留、患者版本复验、证据回读哈希校验、取消优先于迟到结果、模型失败与规则兜底如实标记 degraded。

## 八、逐项结论

见文末「验证 C 结果」后的最终结论表。

## 验证 C 结果

**状态：未执行（unavailable）——被执行环境故障阻塞，非项目原因，非供应商原因。**

本轮会话的命令执行工具（Bash/PowerShell 安全分类器）在离线验证全部完成、代码冻结（工程指纹 `d8630301c84300892eac2039305a6310f74ce33f2946f7d09693f2c53b65d168`，运行前后一致）之后持续不可用，多次重试无效，k=3 真实调用无法发出。按冻结协议第 3 节：样本不足时结论写「不足以判定」，不得以工程测试通过替代真实模型体验通过；不存在任何已执行的在线结果被标记为成功。

补跑入口（一行命令，k=3 冻结、结果文件存在即拒绝重跑；与基线同任务/同预算/同协议/合成数据）：

```powershell
.venv/Scripts/python.exe scripts/verify-agent-closeout.py --out output/planner-reliability-20260910/live --only-live --live-repeats 3
.venv/Scripts/python.exe scripts/analyze-planner-metrics.py output/planner-reliability-20260910/live/live-final-*.json --out output/planner-reliability-20260910/live/after-metrics.json
```

执行完成后，将本报告「六、指标表」的修复后表与「八、逐项结论」按 [acceptance-protocol.md](acceptance-protocol.md) 第 3 节冻结的判定规则回填。

### 最终结论表（按协议逐项）

| 验收项 | 结论 | 依据 |
|---|---|---|
| 拒绝后可执行错误反馈 + 有限修正 | **已解决（工程层）** | A 层 3/3 回放 + 10 项新回归；B 层 13/13×2 + 388 项全过 |
| 校验器误拒绝（可纠正缺参） | **已解决（工程层）** | 回放中 run2 缺参提案由熔断降级变为接受执行（recorded-replay-after-fix.json） |
| 等待过长（本地可优化部分：往返次数/重复上下文/429 快速失败） | **部分解决（工程层）** | 相同提案快速熔断、429 有限重试、payload 去重已实施并有回归；真实端到端耗时变化需 Layer C 数据，当前无在线样本 |
| 真实模型自主规划成功率 | **不足以判定** | Layer C 未执行（环境阻塞）；基线 0/3 不能被工程通过替代 |
| 供应商可用性（429/1305 容量） | **未解决（外部依赖）** | 与代码无关；本轮仅新增有限重试缓解，不改变免费层容量现实 |
| 前端进度真实事件 | **已解决（工程层）** | 里程碑事件 + progress 轮询实现，前端构建通过；浏览器端到端复验未在本轮重跑（沿用上轮浏览器验收路径） |
| 独立盲测 | **unavailable** | 未具备（与上轮一致） |

### 对最终问题的直接回答

**当前还不能宣称已能稳定提供开放式 Agent 体验。** 依据冻结协议：工程与协议层的失败源（不可执行反馈、校验器误拒、拒绝不可见、快速熔断缺失、429 无重试、重复上下文、前端进度断链）已全部修复并通过全部离线验证（3/3 记录回放、13/13×2 开发集、388 项回归、负对照正确）；但「自主规划成功」必须由真实模型的固定次数测试证明，而本轮 Layer C 因执行环境故障未能发出任何在线调用——按协议此项只能记「不足以判定」。剩余阻塞仅一个：运行第五节的一行命令完成 k=3 真实测试；若其结果达到协议阈值（≥2/3 自主规划成功且供应商响应率 ≥2/3），方可升级为「已解决（k=3 样本量限制下）」。供应商免费层容量（38.5% 实际响应率）是独立于代码的外部风险，即使协议修复完全生效，也可能单独导致体验不稳定。
