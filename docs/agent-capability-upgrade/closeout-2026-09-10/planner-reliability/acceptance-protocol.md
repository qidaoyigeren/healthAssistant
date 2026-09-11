# 规划可靠性与等待时间验收协议（冻结于任何验证执行之前）

冻结时间：2026-09-10（本文件在修复验证运行之前写入；执行后不根据结果修改门槛）。
被比较基线：`docs/agent-capability-upgrade/closeout-2026-09-10/live-final-{1,2,3}.json`
（同一开发任务、k=3、180 秒 / 8 次调用、官方智谱 glm-4.7-flash 免费层、固定检索材料 replay）。

## 1. 指标定义（全部按次报告，小样本给逐次值与中位数，不包装成 P95）

| 指标 | 定义 | 来源 |
|---|---|---|
| 任务结果通过率 | score.passed 的任务比例 | run_eval score() |
| 无兜底任务通过率 | rubric 通过 且 全轨迹无 fallback_kind∈{emergency,circuit_break} 且 goal_status=completed 且 execution_status 非 planner 兜底 degraded | tool_trace + answer_bundle |
| 模型提案接受率 | 被接受执行的模型提案数 / 有真实模型返回的提案数（provider_error 与 usage-unknown 不计入分母） | trace planner.source=='llm' |
| 有效推进 gap 的提案比例 | 被接受提案中，其观察产生了新增证据、新增回读或 gap 状态变化的比例 | observation 前后 investigation diff |
| 供应商实际响应率 / 超时率 / 限流率 | actual / attempts；TimeoutError 类占比；429/1305 占比 | usage_ledger + planner 错误码 |
| 安全拒绝 / 参数拒绝 / 校验器误拒数量 | safety / missing_required_arguments / 本次修复定义为"可纠正却拒绝"的类别 | trace validation.errors |
| 每任务模型调用数、已知 token、usage 未知次数 | attempts、usage_tokens 合计、usage_tokens=None 次数 | usage_ledger |
| 首次可见反馈耗时 / 总耗时 | 任务提交→第一条真实 progress 事件；任务提交→终态。逐次值+中位数 | run_progress_events + latency_ms |
| 错误完成 / 无依据结论 / 重复业务写入 | false_completion、无 supporting_evidence 的 supported claim、receipts 去重差 | score + answer_bundle |

**自主规划成功（单次运行）定义**：模型提案被接受并实际执行 ≥1 次实质动作（planner.source=='llm' 且该动作进入 act），结果通过任务 rubric，全轨迹无 emergency/circuit_break 兜底，goal 完成不来自 provider 失败后的规则兜底。模板报告、固定流程、provider 失败后的规则完成一律不计入。

## 2. 三层验证

- **A 原失败轨迹零网络回放**：`scripts/replay-agent-closeout.py` 对三次 recorded 提案回放必须 3/3 通过、0 远程调用；新增回归：错误反馈注入、可纠正参数接受、相同提案快速熔断、429 有限重试（模拟时钟）、react 状态目录条件化（全部离线确定性）。
- **B 冻结开发集**：`stage0/agent_evals/dev.json` 13 场景 gap-replay 与 gap-tools 全通过；后端完整 unittest 套件 0 失败 0 跳过。
- **C 真实模型固定次数测试**：k=3（冻结，不得因结果追加），任务=dev.json[0]，每 run 180 秒 / 8 次调用，官方智谱 glm-4.7-flash 免费层，合成患者数据，固定检索材料（replay 路径）。入口：`scripts/run-planner-live-acceptance.py --enable-live`（未加 `--enable-live` 时脚本拒绝访问网络）。保留全部成功与失败；存在结果文件时拒绝重跑。记录源码指纹、数据集 sha、协议文件 sha。

## 3. 冻结的通过条件与结论规则

- 结论按项报告「已解决 / 部分解决 / 未解决 / 不足以判定」，不合并。
- **规划协议与错误反馈（A+B 层）**：A 全过且 B 13/13 + 回归全过 ⇒ 该项可判「已解决（工程层）」。
- **真实模型自主规划（C 层）**：
  - ≥2/3 run 达成「自主规划成功」且供应商实际响应率 ≥2/3 ⇒ 「已解决」（注明 k=3 样本量限制）；
  - 有 run 达成但 <2/3，或供应商实际响应率 <2/3 ⇒ 「部分解决/不足以判定」；
  - 0/3（在供应商有返回的 run 中）⇒ 「未解决」。
- 供应商可用性单独报告，不计入模型质量结论；usage 未知不按零推断。
- 若 C 层样本或供应商可用性不足，结论写「不足以判定」，不得写「稳定性已解决」。
- 独立盲测未执行 ⇒ 该项标 unavailable。

## 4. 预算与安全不变（本轮不得放松）

180 秒 / 8 调用 / 12 周期（任务预算）；每 planner 调用预留照旧；请求级超时照旧（≤60s，受预算截断）；
新增 429 有限重试：每规划调用最多 1 次（`PLANNER_PROVIDER_RETRIES`，默认 1），退避 1.5 秒封顶且受剩余墙钟约束；超时/连接错误不重试（usage 未知保护）。
熔断：连续安全拒绝阈值不变（`PLANNER_SAFETY_REJECTION_LIMIT`=2）；新增"与上一被拒提案完全相同 → 立即熔断"（省一次注定失败的调用）。
校验器修复只允许"可纠正参数自动补齐并记录 correction"，安全类拒绝（未读证据先 respond、authority 缺口契约、medical authority）一律保留。
