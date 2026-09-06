# 用药协管员：框架集成与生产可靠性升级方案 Prompt

配套方案：[框架集成与生产可靠性升级方案.md](框架集成与生产可靠性升级方案.md)。

用法：需要进一步形成正式技术设计时复制 Prompt A；已经决定开始代码改造时复制 Prompt B，并设置实施范围。A 只写设计，B 才授权实现，避免开发 Agent 误解本次“先出方案”的要求。

## Prompt A：详细技术设计与实施蓝图

```text
你是负责 HealthAssistant 用药协管员项目的资深 Python 后端与 Agent 系统架构师。

请基于当前仓库的真实代码，设计一次可分阶段实施的框架集成和生产可靠性升级。重点是 LangGraph、端到端幂等、可恢复执行、重试/超时/熔断、数据与 Agent 安全，以及失败后的人工交接闭环。

本次只进行代码审计、必要的临时库探针和设计文档交付，不修改业务代码、不安装新依赖、不启用线上 provider、不部署服务、不触达真实患者或审阅员。不要把已有功能重复列为待从零实现；不要把方案或旧报告的测试数字说成当前已实现、已验证。

一、先建立真实基线

读取 AGENTS.md（如有）、README.md、CONTEXT.md、生产化与亮点升级设计.md、框架集成与生产可靠性升级方案.md，以及 docs/production-upgrade-2026-09-05/ 下的实施报告；随后直接核查以下代码：
- stage0/agent.py：CareEvent/AgentState、MemoryWriteTool、Planner/Guard、TurnBudget、handle、_act、_respond、_finalize。
- stage0/response_safety.py、stage0/memory.py、stage0/server.py、stage0/api_client.py、stage0/app.py。
- stage0/rag.py、stage0/ddi_engine.py、stage0/extract_ddi.py，以及现有测试/评测入口和依赖文件。

尊重工作区已有未提交修改。以代码为准，区分已实现、实现不完整、设计预留和待验证。框架 API/最佳实践仅引用查证过的官方资料，注明访问日期；不要照抄与当前安装版本不兼容的示例。

特别复核以下已发现风险；如果当前代码已修复，改成说明修复证据而不是继续宣称存在：
1. API 从 Idempotency-Key 前 32 位生成 turn_id，两个合法不同 key 可能碰撞。
2. API event_key 是否贯通 handle → MemoryWriteTool → consolidate_interaction(client_event_id)，还是只保存在任务 payload。
3. complete/fail/heartbeat 和领域副作用是否校验当前有效 lease_token，旧执行者能否覆盖新状态。
4. 已完成事件重复 POST 是否缺完整 response，而 API client 直接返回 acceptance。
5. UI 超时或重复操作是否保持原幂等键，而不是每次 submit 随机新键。
6. 任务完成与请求结果是否分事务；写成功但 checkpoint/任务状态未提交时是否重复副作用。
7. 现有崩溃测试是否只覆盖“领取后未执行”，而没有覆盖“领域写成功后崩溃”。
8. record_conclusion、药物版本、工单、审核、通知是否各有稳定操作回执。
9. 120s 预算能否约束正在运行的外呼与末尾 composer，重启是否重置预算。
10. actor/source 是否能从请求体伪造；状态查询、memory ref 和人工恢复是否有对象级授权。

输出一张现状审计表：代码位置、事实、风险、优先级、修复、验证方法；高风险推断用临时数据库/假 provider 探针验证，明确探针覆盖边界，不读写正式 memory.db。

二、总体原则

保留现有离线确定性默认路径、DDI/RAG 工具、双时间记忆、依赖失效、引用校验、冲突显式保存和医学边界。系统可记录用户报告的用药变化，不自行诊断、开药或指挥改剂量。

采用模块化单体、单患者单写者作为近期默认；仅为真实并发/部署需求增加基础设施。当前没有真实审阅员团队的证据，人工流程先用明确标记的本地模拟 reviewer 演示，不能伪称医生已接单。

给出框架选型矩阵：LangGraph、FastAPI/Pydantic、SQLite/PostgreSQL、checkpoint saver、SQLAlchemy/Alembic、RetryPolicy/Tenacity、OpenTelemetry；解释 Temporal/Celery/Redis/Kafka/多 Agent 为什么现在引入或后置。每个组件有一个明确职责，避免两套系统管理同一重试、状态或人工等待。

三、LangGraph 迁移设计

给出 AgentRunner 接口与 Legacy/LangGraph 两种实现，以及原函数到图节点的映射。使用显式 StateGraph 拆出 load_context、triage、plan、guard、execute_read/write、observe_reflect、compose、final_safety、open_review、await_review、apply_review、publish；允许按代码实际调整命名。

普通路径仍由 LLM 在授权工具范围内决定下一步；代码负责硬性安全、权限、预算和证据。不要把整个 handle 放进一个节点后声称获得了节点级恢复。

定义 WorkflowState 字段及其所有者，区分领域事实、运行状态和给模型看的摘要。至少涵盖 event/run/thread 映射、患者 revision、evidence refs、pending operation、图/状态/规则/提示词/模型/语料版本、累计 token/成本/活动时间、审核和结果引用。原始依据可回溯，压缩摘要不能替代证据。

说明 checkpoint 和领域事务的双写窗口，以及如何用操作回执与对账恢复。说明 interrupt 恢复会重跑包含它的节点；将非幂等副作用拆到其他节点，等待期间释放 Worker/锁，禁止吞掉 Graph 的中断信号。

给出本地持久化 saver 和 Postgres saver 配置边界；框架序列化不携带凭证、连接或任意可执行对象。定义版本升级与在途 run 的恢复/回滚方式。影子验证使用只读快照或隔离库，禁止双写真实患者库。

四、幂等、事务与任务一致性

区分 request_key、event_id、run_id、operation_id、attempt_id。请求唯一键带可信 scope/principal/operation 范围；哈希只包含校验后的业务载荷。相同 key/payload 重放原事件，同 key/不同 payload 保持 422 兼容；不以内容相似度误删真实的新事件。

业务效果采用稳定 operation id + input hash + 唯一约束 + 成功回执。操作意图先持久化再执行；恢复沿用 id，不得每次重试生成 UUID，也不得用 node name 去掉合法的后续循环操作。

给出事件受理事务、领域写事务、审核决策/恢复任务事务、结果发布事务的步骤或伪 SQL。LLM/外呼必须在事务外。任务完成、run 终态、结果引用尽量原子提交；与 checkpoint 的剩余窗口用回执和 reconciler 收敛。

任务字段必须有 lease_token/有效期、heartbeat、next_attempt_at、attempts/deadline、错误类别。旧 lease 的 complete/fail/heartbeat/业务写入都被拒绝。解释患者级顺序/CAS、人工等待期间版本变化和多 Worker 下的领取方式。

外部效果超时不明时先对账，不能盲重试；没有下游幂等/查询支持就明确不能保证 exactly-once。失败重试保留原 event，不能指导用户换 key 造成第二个业务事实。

给出幂等键、操作回执、checkpoint、审核数据的保留与清理规则，活动工作不能清理。

五、错误、重试、超时、熔断与背压

输出分类决策表：网络/429/5xx、400/401/403、schema/JSON、权限与政策拒绝、证据不足、数据库锁、外部效果未知、永久内部错误。

每类说明是否重试、次数是否包含首次、backoff/jitter、Retry-After、deadline、成本、重试所有者和最终路由。图重试、SDK retry、Tenacity、outbox 重领不得相乘放大；结构修复和备用 provider 同样计入上限。

区分节点 timeout、整回合活动预算、队列等待、人工 SLA；重启不能重置预算，人工等待不消耗执行预算。保留安全收尾时间，限制实际网络 I/O；解释取消 await 不代表同步线程或远端请求停止，迟到结果必须隔离。

将现有连续安全拒绝熔断与 provider closed/open/half_open 熔断分开；定义请求限流、患者/依赖并发上限、队列容量、失败队列和运维重试。降级不能减弱安全策略或擅自换数据接收方。

六、安全与人工交接

按入口、对象授权、工具、证据、输出、数据、观测分层设计。身份由可信 Principal 派生，前端传入 actor/source 不能成为权限；RAG、记忆、用户文本、审核自由文本均是不可信数据。

包括强类型事件校验、工具 allowlist、禁止任意 SQL/shell/URL、出站请求限制、来源版本/引用核验、UI 渲染限制、最小化 LLM 数据、checkpoint/备份保护与日志脱敏。guard 模型只能辅助，不替代代码授权或硬性规则。

证据不足/检索为空/依赖断开/药名未识别必须不同于“无风险”；LLM 自报概率不作为医学可信度。对最终实际交付文本执行检查，人工决定也不能解除诊断/处方等产品边界。

给出人工分流矩阵：用户澄清、临床专业审阅、技术支持、政策拒绝、经专业审核的紧急指引。紧急指引先响应，不排普通人工队列等待。

设计 review_cases 状态机、唯一建单规则、摘要与证据包、角色分工、CAS 接单、决策 schema、SLA/无人接单/逾期/撤销。没有真实服务时应明确显示“尚未接入人工服务”，并支持导出咨询摘要。

审核 API 必须鉴权、限定对象、验证 case/interrupt/revision、决策幂等，并在同一事务写审核记录和 resume_task。后端才可生成 Command(resume=...)，前端不能直接提供任意 state patch/goto。恢复前重新验权、核验最新患者事实；旧审批不能授权新状态。恢复后仍经过安全与引用门槛。

七、输出要求

请将结果写入 docs/reliability-design/，至少包含：
1. 现状审计及已证实/待验证区分。
2. 总体架构图、Agent 图、人工状态图。
3. 选型 ADR、状态/错误/API 契约。
4. 数据模型/唯一约束/迁移草案、关键事务与崩溃恢复时序。
5. 重试预算、权限矩阵、人工交接与无人接单行为。
6. P0–P4 分阶段工作、文件映射、风险、验收和回滚。
7. 故障注入与安全测试矩阵，明确测试环境、断言和产物路径。
8. 已核查的官方来源及剩余假设。

验收必须覆盖：长 key 前缀碰撞、同 key 并发、UI 超时重试、重复 POST 结果、业务写后 checkpoint 前崩溃、旧租约写入、429 重试上限、effect_unknown、审核提交后崩溃、等待人工后重启、过期审批、越权读/恢复、RAG 注入、无证据、无人接单和版本回滚。

性能目标标为待测；旧报告数字与本轮实测分开。衡量 DDI/RAG 质量和运行可靠性两条轴，不宣称 LangGraph 直接提高医学准确率。普通 trace 不记录患者正文或隐藏思维链。

最后给出优先级明确的一页建议，以及可以接续执行 P0 的任务清单。常规设计选择自行决策并说明假设；只在真正无法推断且会改变方案的约束上集中提出问题，不因一般可逆选择停止工作。
```

## Prompt B：按阶段实施

默认仅授权 P0。想实施后续阶段时，明确将下面的“本轮范围”修改为 P1、P2 或相应连续阶段；不要仅因为计划中存在后续阶段就一口气改造全仓。

```text
请依据当前仓库的《框架集成与生产可靠性升级方案.md》和 docs/reliability-design/ 下已有设计（如存在），实际完成本轮范围内的代码、迁移、必要测试与文档。

本轮范围：P0——可靠性与权限入口。完成后报告，不自动进入 P1/P2，不部署、不连接真实人工或发送外部通知。

先核查 AGENTS.md、git status、当前实现和现有测试。尊重所有未提交修改，不重置/删除业务数据库或评测工件，不把未验证能力写成已完成。若设计与代码矛盾，以实际代码和可复现行为为准，说明调整后继续完成授权范围。

共同红线：
- 保留现有默认离线、确定性 fallback、中文引用、双时间记忆、冲突与医学边界。
- 测试使用临时数据库、假 provider 和模拟身份，不读取/打印密钥，不擅自对外发送患者数据。
- 所有业务写入都在可信身份、患者范围、有效租约和 operation receipt 下执行；网络/LLM 不在事务内。
- 不依赖框架名称或单个幂等键宣称全链路 exactly-once。

执行 P0 时必须完成：
1. 复现并修复前 32 位 turn_id 碰撞。独立保存 event/run 身份，让 API 事件身份实际进入 consolidation；保持已有历史事件可读，不改写旧身份。
2. UI 保存一次提交的稳定幂等键、事件身份和状态 URL。HTTP/轮询超时复用原事件，明确新业务提交才新建。
3. 修复 committed POST/GET/client 返回形状。保留 v1 既有 202/422 语义，新增契约通过兼容适配或 v2 明示；失败的原事件恢复使用显式操作，不提示换 key。
4. 增加领域操作回执和必要唯一约束，保护 consolidation、显式药物变更、警告/结论写入的重放窗口；同 operation id 不同输入拒绝。真实新事件与新版本检测仍可产生新记录。
5. claim/heartbeat/complete/fail/领域写入校验 lease_token、状态、有效期与必要 revision。旧 worker 的所有写入均被拒绝，不能只改完成函数。
6. 结果、任务与请求状态统一事务发布；保留对账/恢复入口。对未实现的外部副作用明确返回不支持，不伪装可用。
7. 类型化 retryable/permanent/safety/effect_unknown 错误；增加 next_attempt_at、退避+jitter、次数/时间预算和失败队列。去除无条件快速重试，审计 SDK/LLM 修复/worker 的重试放大。
8. 部署入口建立可信 Principal、对象级授权和角色边界；actor 从身份派生。保留显式 local-demo profile；部署 profile 无鉴权配置时不能匿名启动。为后续人工审核提供可测试授权接口，不在 P0 声称已经集成人工服务。
9. 将后台异常改为脱敏结构化日志并贯穿 trace id；避免吞异常使队列悄悄停摆。实际交付正文与安全检查正文一致。

当本轮范围被明确修改为 P1 时，在 P0 基础上完成：
- AgentRunner/LegacyAgentRunner/LangGraphAgentRunner 与按职责拆分的 StateGraph；保留原 LLM 决策语义和 guard。
- 持久化 saver、event/run/thread 映射、状态/图/政策版本、累计预算和恢复算法；checkpoint 内只放安全可序列化状态/引用。
- 通过原 operation receipt 处理“领域 commit 成功但 checkpoint 未完成”窗口；interrupt 独立等待，不能占 Worker/锁。
- 所需 LangGraph 版本以官方资料、依赖解析和本仓最小冒烟为依据锁定；避免同步节点 timeout 误用和 SDK 重试叠加。
- 新 run feature flag 与旧在途 run 版本路由；对比使用隔离数据库，禁止双写真实领域状态。

当本轮范围被明确修改为 P2 时，在前序基础上完成：
- review_cases/review_decisions/resume_task、模拟 reviewer 页面、用户等待状态与咨询摘要。
- 受控分流：澄清、专业审核、技术支持、拒绝、紧急固定提示；普通客服无权作临床判断。
- 幂等建单/审核、CAS 接单、对象授权、interrupt/revision 匹配、恢复时重新验权和事实时效核验。
- 未接真实人工、逾期、无人接单、补充信息、撤销、重复回调均有明确行为；超时不得默认通过。
- 恢复仍过硬规则和引用校验；人工不能提交任意 graph goto、SQL、工具名或 state patch。

当本轮范围被明确修改为 P3/P4 时，只实施设计中对应阶段；多进程并发使用真实 PostgreSQL 验证，SQLite 通过不算替代。外部 tracing 默认关闭或只用本地/自托管脱敏管道。

实现顺序：先补针对实际失败场景的回归 → 最小增量迁移和核心修复 → 接通 UI/API → 跑本阶段及受影响的既有测试 → 文档与可复现演示。阶段内常规可逆选择自行推进，不重复索要已经授权的许可；确有外部账号/真实人员信息缺失时完成独立部分并明确边界。

本轮至少验收：
- 两个长 key 前缀相同仍是不同 event；同 key 并发只受理一次。
- UI 超时重试不新建事件；成功重放返回一致完整结果。
- 在药物/结论写成功后、结果或 checkpoint 提交前注入崩溃，恢复后没有重复效果。
- 租约过期重领后，旧 worker 的 complete/fail/heartbeat/write 被拒绝。
- 永久错误不自动重试；429/网络故障按策略退避，预算与尝试次数有上限。
- 未授权读取/修改/恢复被拦截；日志无密钥或患者正文。
- 所有受影响的原有安全/记忆/Agent/API 回归通过；报告实际命令、退出码、环境和产物，不伪造结果。

最终交付：修改文件与原因、迁移及回滚方法、实际测试结果、未解决问题和下一阶段接口。将实现报告与探针/测试输出保存到 docs/reliability-implementation/。若需要手工配置真实身份方或人工服务，写出具体配置说明和当前不可用行为，不把模拟集成说成真实生产能力。
```
