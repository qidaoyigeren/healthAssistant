# Harness P0 实施报告

日期：2026-09-06。范围：仅 Harness P0。未进入 Harness P1，未启用付费 provider、真实审核、外部通知或生产迁移。

## 1. 本轮实测基线与失败复现

已检查父目录和仓库中的 AGENTS.md：未发现适用文件。已读取 README、相关代码和 `docs/reliability-implementation/` 三份历史报告。开始时只有用户已有的 `Agent Harness分级改造Prompt.md`、`docs/harness-upgrade-prompts/` 未跟踪文件，均保留。实验、迁移和故障注入全部使用 TemporaryDirectory 中的合成数据库；没有修改正式 memory.db、原始语料或历史评测工件。

指定基线命令原样执行：

```powershell
.\.venv\Scripts\python.exe -m unittest stage0.test_stage8_agent stage0.test_reliability_p1 stage0.test_reliability_p2 -q
```

实际输出尾部为 `Ran 33 tests in 41.230s / OK`，但期间出现：

```text
resume-task pass failed
AttributeError: 'LangGraphAgentRunner' object has no attribute '_audit'
During task with name 'apply_review'
```

这次“33 通过”不是可靠性通过。原审核测试只验证旧案取消、新案出现，未验证 checkpoint、恢复任务消费和第二轮决定。

先新增 [test_harness_p0.py](../../../stage0/test_harness_p0.py)，并加强原 `test_review_stale_opens_new_round_and_keeps_run_waiting`，然后才修改实现。修复前运行：

```powershell
.\.venv\Scripts\python.exe -m unittest stage0.test_harness_p0 stage0.test_reliability_p2.FullReviewCycleTests.test_review_stale_opens_new_round_and_keeps_run_waiting -q
```

当时仅有新预算探针和审核探针，共 2 个测试方法，输出 `Ran 2 tests in 11.642s / FAILED (failures=2, errors=1)`：

| 探针 | 修复前本轮观察 | 修复后 |
| --- | --- | --- |
| 同一持续 memory_read 假 provider，token budget=200，max_cycles=4 | legacy 一次调用，但 workflow_runs 没有 tokens_estimated；graph 四次调用 | 两种 runner 都一次调用，终止原因 tokens，持久化非零累计 |
| graph 累计所有权 | `_node_plan` 无 token gate；`_finish` 用旧 budget 覆盖新累计 | 共用 BudgetSession；checkpoint 是投影，run 账本拥有累计和原始限额 |
| 审核过期 | `_audit` AttributeError；旧 resume_task 仍 pending | 旧任务 consumed；checkpoint 和 interrupt 指向 round 2；第二轮合法决定完成 |
| 旧摘要绑定新事实 | 新单复制旧 summary，却采新事实哈希 | 新事实快照、新 DDI 候选证据、明确未完成的患者条件检查 |
| 任意恢复异常 | catch-all 后重新从 START 运行 | 保留异常，交由 worker 分类；只续跑持久化节点 |

最终同条件小预算补充探针（合成文本 `query`）：

```text
LegacyAgentRunner:    planner_calls=1, reason=tokens, tokens_estimated=1551,
                     tokens_charged=1551, calls_attempted=1, usage_quality=estimate
LangGraphAgentRunner: planner_calls=1, reason=tokens, tokens_estimated=1551,
                     tokens_charged=1551, calls_attempted=1, usage_quality=estimate
```

**1551 是估算单位，不是实际 provider usage。** 当前 token 门在调用前检查剩余额度，并限制请求中的输出上限；成功返回后结算完整输入/输出。一次返回可越过 200，再阻止后续调用。这是明确的软 token 边界，不是硬账单限额；不能从这个假 provider 探针推导模型质量或费用降低。

## 2. 修改与理由

| 文件 | 实施内容 |
| --- | --- |
| [turn_budget.py](../../../stage0/turn_budget.py) | 最小共享 TurnBudget/BudgetSession；活动时间、调用次数、token 和 cycle 门；持久化前置预留、结算、未知调用恢复、I/O timeout 下传、迟到拒绝、租约检查。兼容 package/script 两种导入，共享同一 ContextVar |
| [agent.py](../../../stage0/agent.py) | legacy 使用共享预算；Planner 每次解析重试分别计费，guard 拒绝不跳过记账；composer/verifier 都接共享 I/O 门；预算不足走说明未完成工作的确定性文本；实际文本仍校验；恢复检查有稳定 run 身份 |
| [extract_ddi.py](../../../stage0/extract_ddi.py)、[crosscheck_eval.py](../../../stage0/crosscheck_eval.py) | 工具内部 DDI LLM 和可选 KEGG HTTP 都进入同一活动时间/尝试账本；预算中止不继续抽取重试；已有独立数据采集/离线评测 CLI 不新增 Agent run |
| [graph_runner.py](../../../stage0/graph_runner.py) | 初始限额完整；删去复制的预算分支和覆盖合并；逐节点同步；原 checkpoint 继续执行；显式 waiting_review；新旧审核轮路由对账；审核实际交付文本再次校验 |
| [memory.py](../../../stage0/memory.py) | 增量资源账本、恢复任务调度字段、独立排队/人工等待计时；累计单调合并；复用真正审计接口；冲突 tx 入口复用；审核效果、outcome、case 关闭、receipt 单事务；关键领域提交前验证租约；普通 trace 仅持久化过程元数据 |
| [server.py](../../../stage0/server.py) | 恢复任务逐条领取与分类；有界重试、退避、失败隔离；不因 applied 已落库提前消费未完成 checkpoint 的任务；先完成图和结果对账，最后确认任务 |
| [test_reliability_p0.py](../../../stage0/test_reliability_p0.py)、[test_reliability_p2.py](../../../stage0/test_reliability_p2.py) | 更新任意内部 RuntimeError 的 permanent 契约；审核测试验证无意外后台 ERROR、任务消费、checkpoint/interrupt 和第二轮完成 |

没有引入第二套领域主循环，没有重构工具协议、增加新临床决策能力或推进 P1。

## 3. 预算与恢复契约

### 3.1 限额、等号和字段所有权

| 字段 | 契约 |
| --- | --- |
| wall_clock_seconds | `AGENT_TURN_BUDGET_SECONDS`，默认 120；现在约束活动执行时间，人工等待不计入 |
| token_budget | `AGENT_TURN_TOKEN_BUDGET`，默认 150000；有 usage 用实际值，无 usage 用输入/输出字符估算单位 |
| call_budget | 新开关 `AGENT_TURN_CALL_BUDGET`，默认 32；所有已接线 LLM、KEGG 外呼尝试共享，失败和重试也占用 |
| max_cycles | 原 agent max_cycles；0 合法；cycle 在规划前持久化占用，崩溃不返还 |
| consumed_seconds | 当前 invocation 起点基线 + monotonic 活动时间；每个节点/调用同步，不能被旧 checkpoint 回写变小 |
| tokens_estimated | 已结算调用的估算累计，包括有实际 usage 的调用；用于可比观察 |
| tokens_actual | **取得实际 usage 的那部分调用**之和，不是未知调用的假总用量 |
| tokens_charged | 每次调用有实际 usage 时用实际值，否则用估算；用于 token 门 |
| usage_quality / usage_unknown | actual / estimate / mixed 描述已结算计量；未结算或无法还原的历史消耗由 usage_unknown 标记，并阻止新增外呼 |
| llm_attempts | 每个 attempt 有 UUID、run_id、operation_id、cycle、kind、预留 token/time、状态及 nullable usage；kind 也覆盖 kegg_http，HTTP 本身没有 LLM token |

`None` 表示缺失，合法 0 不替换成默认限额。**等于限额即耗尽**，第一轮也检查。恢复沿用 run 首次记录的限额；修改环境变量不提高在途 run 限额。checkpoint 的旧累计只可推进 run 的累计最大值，不能清零；usage_unknown 为单向保守标记。预算终止原因统一为 `budget_exhausted:tokens|calls|wall_clock|cycles|usage_unknown`。

### 3.2 外呼过程

```text
读原限额与累计 → 检查剩余量/租约 → 持久化 attempt 与预留
→ 再扣除持久化耗时，计算允许 I/O timeout → 外呼（事务外）
→ 结算 usage 或 estimate → 拒绝迟到/失去租约的结果
```

预留后尚未发出就耗尽时间时记 `not_dispatched`，该保守预留尝试不返还次数。SDK 隐式重试设为 0；解析修复、composer 参数兼容重试分别创建 attempt。传给 I/O 的 timeout 取客户端/当前调用配置与剩余活动时间的较小值，并保留最多 50ms（小限额按 5%）供本地安全收尾。composer/verifier 不享有额外赠送预算；不足时只使用确定性模板和规则校验。

进程退出发生在 reservation 之后时，行保持 `reserved`，usage 保持 NULL。传输超时/断连保留 `unknown` 和预留。恢复禁止该 run 新外呼，仍可对账已提交操作、执行固定安全收尾。不会把“没有 usage”写成精确 0。实际 usage 是非负整数才接受。

同步 provider、HTTP 客户端和远端服务不能由这里强制杀死。调用可能实际阻塞超过 timeout；本实现记录耗时并拒绝迟到结果，**不承诺线程已停止、远端已停止计费或本地模板严格在 50ms 完成**。没有用 wait_for/to_thread 冒充物理取消。保留现有单 writer 部署限制。

### 3.3 人工等待与旧 run

`waiting_since` / `human_wait_seconds` 与活动预算分离；恢复时只把人工等待差值写入人工指标。`queue_wait_seconds` 另记排队到领取的时间；审核 `due_at` 仍只控制 SLA，不自动批准。测试将等待起点移到数月前，确认活动时间未增加数月。

新 `state_schema_version='2'`、`accounting_version=2`；graph_version 仍为原拓扑家族 1，runner 路由仍遵循原 run。旧 run 的版本/账本不足以重建消耗时进入持久化 usage_unknown，不授予新的 LLM 额度；旧 checkpoint 路由继续可恢复。旧 applied 决定没有新原子回执时进入 effect_unknown，不能猜测重复执行是否安全。

## 4. 审核闭环与崩溃窗口

1. 恢复值仅定位决定；action、payload、actor 从数据库权威记录读取，核验 case/run 关联。
2. 事实过期：旧 outcome → review_stale；旧 case → cancelled；新键固定为原事件 + 原原因码 + 原 round+1。取消/新建/审计分步可恢复，重试不会自行推进轮次。
3. 新单不复制旧 warning summary。读取当前事实和对应 ref；在预算允许时执行当前全药单 DDI 检查，结果作为当前候选证据。患者个体条件检查仍明确列为 incomplete，不把候选当完整安全结论。开单事务再验证采样 revision/hash，变化则 retryable，不绑定不相干快照。
4. incomplete 新证据只允许 close_with_safe_guidance、reject_candidate、request_more_info；不能 confirm_reported_fact 或 resolve_conflict。后者在完整旧证据场景也必须属于该 case 的冲突引用范围。
5. `audit_review_stale` 复用 MemoryStore `_audit`，和该转换回执一起事务提交，不使用空审计方法或吞异常。
6. graph checkpoint 更新 review_case/review_logic_key，并产生对应新 case 的 interrupt；旧任务在图/结果对账后才 consumed。若确认前崩溃，重试识别当前 checkpoint 已指向新轮，不会把旧决定注入新 interrupt。
7. 正常决定通过 `apply_review_effect` 原子提交领域事件/冲突动作、outcome=applied、case 状态和 operation_receipt。checkpoint 前崩溃时，重放读原回执并重建相同结果；不能仅见 applied 就提前消费任务。
8. 审核最终文本（含固定审核后缀）重新通过已有安全检查。旧医学警告不因为新事实哈希而重新获得认可。

跨 domain SQLite 与 checkpoint SQLite 仍不是同一事务，不声称全链路 exactly-once。新审核效果事务关闭了本轮指定的重复领域效果窗口；原普通领域操作继续使用既有 receipts 和领域事件去重契约。

恢复任务保持旧 `status=pending|consumed` 以兼容表 CHECK；另以 `execution_state=ready|running|retryable|failed|done` 表达调度。领取带租约、尝试计数；失败记录明确 `last_error_class`：

| 分类 | 行为 |
| --- | --- |
| retryable | 连接/超时、事实在提交前再次变化；5/10 秒退避，最多 3 次尝试，过期租约恢复也计入上限 |
| permanent | AttributeError/TypeError 等内部错误、不可应用的决定；失败隔离，不自动从头运行 |
| safety | 最终安全检查失败；任务失败，不自动重试 |
| effect_unknown | 历史已应用效果无法由原子回执证明；失败隔离，需运维核查，不能重放猜测 |

失败任务不阻塞其他 ready 任务。process cancellation/interrupt/SystemExit 不被当作普通工具失败吞掉。

## 5. 验证与实际输出

新增测试文件目前 **25 个测试方法**，其中参数化循环覆盖 8 个零限额 runner/limit 组合、8 个取消/建单/审计/确认前后崩溃边界、2 个真实 checkpoint 前后崩溃边界。还覆盖：

- 两种 runner 的 token 终止、持久化和重启后的原始限额；零预算不调用模型、不声称已保存事件。
- guard 拒绝、空响应/JSON 修复、composer 兼容重试、verifier、事实抽取、实际 detector 的 script-style DDI 入口、KEGG HTTP 统一计账。
- 精确 token 等号、尝试上限、未知预留重启、HTTP 超时未知、同步迟到结果、旧 checkpoint 无完整账本的连续恢复。
- 人工等待冻结、旧任务 consumed、新 checkpoint/interrupt 的 case_id、第二轮合法决定真正完成。
- 领域事务内 receipt 失败回滚、效果已提交但 checkpoint 未完成时重放不重复 clinical_review。
- retryable 上限、permanent 不阻塞其他 run、safety 和 effect_unknown 分流、未知内部错误不重新从 START 开始。
- 租约在事实抽取期间失效时拒绝投影；旧 schema 增量重开；普通 trace 不导出正文/模型 rationale。

受影响的离线 Agent、记忆、API 和审核回归命令：

```powershell
.\.venv\Scripts\python.exe -m unittest stage0.test_stage3 stage0.test_stage5 stage0.test_stage6 stage0.test_memory_p0 stage0.test_memory_p1 stage0.test_memory_p2 stage0.test_stage8_agent stage0.test_stage10_server stage0.test_reliability_p0 stage0.test_reliability_p1 stage0.test_reliability_p2 stage0.test_harness_p0 -q
```

实际结果：

```text
Ran 175 tests in 39.719s
OK
```

保留的旧 FastAPI on_event 弃用提示与本轮无关。故障注入测试明确断言预期分类；正常审核闭环显式断言没有意外后台 ERROR。没有再用“退出 0 + 存在某条记录”代替审核恢复验证。

最终又细化了“预留持久化后重新计算剩余 timeout”及“沿用客户端单次 timeout”，针对相关路径追加执行以下组合：

```powershell
.\.venv\Scripts\python.exe -m unittest stage0.test_harness_p0 stage0.test_stage8_agent stage0.test_reliability_p1 stage0.test_reliability_p2 -q
```

实际输出为 `Ran 58 tests in 21.713s / OK`。这是最终代码版本的定向复验；上面的 175 项是此前同轮完整受影响回归，不混用两次耗时。

测试入口未替换；原基线命令仍可使用，新模块追加即可。`git diff --check` 通过；Windows Git 提示 LF/CRLF 转换，无空白错误。

## 6. 依赖与官方接口核查

本轮未安装或升级依赖。当前 Python 3.13.14，实际安装 openai 3.3.1、langgraph 1.2.11、langgraph-checkpoint 4.2.0、langgraph-checkpoint-sqlite 3.1.1，与已阅读的 requirements-graph.txt 固定版本一致。

本地 inspect 已确认 OpenAI Completions.create 接受 timeout/max_tokens/max_completion_tokens，OpenAI.with_options 接受 max_retries。持久化测试实际使用当前版本的 SqliteSaver、Command(resume=...)、interrupt()、get_state/update_state。

参考仅复用设计思想，未新增 Pydantic AI runtime：

- [Pydantic AI UsageLimits](https://pydantic.dev/docs/ai/core-concepts/agent/#usage-limits)：调用前的次数门、累计 usage；费用限制不能当硬账单保证。
- [Pydantic AI Timeouts](https://pydantic.dev/docs/ai/core-concepts/timeouts/)：单调用 timeout 与整回合约束分开；同步线程无法由 await timeout 强制停止。
- [LangGraph interrupts](https://docs.langchain.com/oss/python/langgraph/interrupts)：同 thread 恢复、interrupt 所在节点重新执行、副作用须幂等。

## 7. 迁移、回滚、未验证范围和 P1 接口

迁移由 MemoryStore 开库时执行：新增 llm_attempts 表；resume_tasks/workflow_runs 只加字段，不重建旧状态 CHECK，不删除历史身份或领域数据。迁移测试在临时旧表中保留 pending 任务并验证重开后的原 operation_id。真实数据库未迁移；使用时应沿用现有备份流程。

关闭 `AGENT_GRAPH_RUNNER` 仍可使**新** run 走 legacy，但两种 runner 都使用修复后的预算。关闭审核开关不绕过已有 run 的审核 checkpoint。不能通过回退到旧代码重新启用预算缺口；旧在途 run 保持原 runner，缺账本时安全降级。新增表可保留，无需删除来关闭功能。

未验证真实模型质量、真实账单、远端取消、多进程并发、真实医护判断或生产迁移。本轮故障注入模拟进程退出和 SQLite/checkpoint 边界，不等价于断电存储介质测试。原 trace 中的历史正文未做破坏性清理；新普通 trace 已限定过程元数据，原始证据继续受领域/checkpoint 存储边界控制。

供下一阶段复用的接口是预算上下文、completion_call/provider_call/network_call、attempt 账本、原子审核效果和逐任务恢复状态。新的工具或外部 provider 必须显式接入这些入口；任意第三方注入回调自行创建网络连接时无法由 Python 全局拦截保证。Harness P1 的工具层整合、完整患者条件重核验工作流和更多领域原子化未在本轮自动实施。
