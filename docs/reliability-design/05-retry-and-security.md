# 错误分类、重试预算、超时与安全分层

## 1. 错误分类决策表

| 类别 | 例 | 是否重试 | 次数口径 | 退避 | 终点 | 所有者 |
| --- | --- | --- | --- | --- | --- | --- |
| 瞬时外部故障 | 网络抖动、可恢复 5xx、429 | 是 | `max_attempts=3` **含首次** | 指数退避 + jitter；有 Retry-After 则遵守；每次检查剩余预算 | 缓存/确定性降级，必要时人工 | 图节点 RetryPolicy 或适配器策略（唯一所有者） |
| 客户端/鉴权错误 | 400/401/403、非法参数、幂等冲突 | 否 | — | — | 明确失败或引导修正 | API 层 |
| 结构错误 | LLM JSON/schema 不合格 | 1 次结构修复，计入统一外呼上限 | — | — | 确定性降级；修复不得取消安全限制 | planner |
| 权限/政策拒绝 | 诊断/处方请求、越权工具 | **否**（非 transient） | — | — | 拒绝或转人工；不换模型绕过 | guard/边界 |
| 证据不足 | 引用缺失、信息缺失、事实矛盾 | 最多一次有目标补检/澄清 | — | — | 仍不足即 abstain，标记不确定性升级 | agent 循环 |
| 数据库短暂冲突 | 死锁/busy | 是（确认事务已回滚后重试**完整短事务**） | ≤3 | 短退避 | 超限进恢复队列，保留原 operation id | store 调用方 |
| 外部效果未知 | 写入超时、响应丢失 | **不盲重试**；先按 operation id 查回执/对账 | — | — | `effect_unknown` 状态，人工/对账 | worker + reconciler |
| 永久内部错误 | 未预期异常、超预算 | 否 | — | — | 失败队列 + 脱敏故障记录；运维显式 retry | worker |

实现要求（P0）：任务失败按上表写入 `last_error_class`；`retryable` 才设置 `next_attempt_at = now + base*2^n + jitter`（建议 base=5s，cap=5min），`permanent/safety` 直接进失败队列；`attempts` 口径统一为"含首次"，与 `_recover_expired_outbox_tx`、rechecks 的语义对齐并用测试固定。

**反放大审计清单**：planner（OpenAI SDK max_retries=0，已核实 Stage 8 verifier 同款）→ 适配器（httpx 无自动重试；若加 Tenacity 显式上限）→ 图 RetryPolicy（P1 显式配置）→ outbox 重领（只做恢复，不叠加）→ 失败任务不得被 UI 轮询路径触发额外重试。

## 2. 超时分层

| 层 | 值 | 说明 |
| --- | --- | --- |
| 单次 planner/composer LLM 调用 | `min(60s, remaining_active_budget - 15s 收尾预留)` | 不足则直接降级，不发调用 |
| verifier | 15s（现状） | 同样受剩余预算约束 |
| 整回合活动预算 | 120s wall-clock（现状默认） | **P0 补丁**：调用前检查剩余预算（现状只查循环顶部，60s 同步调用可跨线） |
| 任务租约 TTL | 300s + **heartbeat 续租**（P0 新增） | 长回合不再被误回收 |
| 队列等待 / 人工 SLA | 分开统计 | 人工等待冻结活动预算，SLA 时钟照走 |
| 进程重启 | 活动预算从持久化值恢复（P1）；P0 至少保证单任务 deadline_at 生效 | 重启不重置预算 |

明确边界：取消 `await` 不代表同步线程或远端请求已停止——HTTP client 必须设置真实 I/O deadline；迟到结果在版本/租约检查时被拒绝（场景 B）。需要硬中止的阻塞计算才用可终止进程隔离（当前无此需求）。

## 3. 过载与背压

- 请求限流：每 principal 每分钟事件数上限（P0 简单计数即可）；过载 429 + Retry-After。
- 并发上限：每患者 1（单写者天然满足）；每依赖（KEGG/provider）并发上限（P3 埋点后调参）。
- 队列容量：outbox 待处理数上限，超过返回 503；失败队列（`status='failed'`）只进不出，需运维显式 retry。
- 降级不减弱安全策略：降级只影响"是否调用 LLM/外部依赖"，不跳过 guard、引用校验、最终边界；不擅自更换数据接收方（不自动把患者数据发往备用 provider）。

## 4. 安全分层（入口 → 输出）

| 层 | 措施 |
| --- | --- |
| 入口 | 判别联合请求模型；body 大小/payload 深度/列表数量/日期时区校验；每 principal 限流 |
| 身份 | `Principal(user_id, roles, authorized_scopes)` 来自可信会话/token；`payload.actor/source` 是自述字段（`claimed_actor`），权限用 `authenticated_actor`；部署 profile 无鉴权配置拒绝启动 |
| 对象授权 | 状态查询、memory ref 解析、冲突操作、（P2）工单/恢复端点全部校验 scope + 对象归属；不可猜 id 不等于授权 |
| 工具 | 固定 allowlist（现状 tools registry 保留）；禁止任意 SQL/shell/文件路径/外部 URL；出站仅限受控域名（KEGG/provider），拒绝本机/内网/云元数据地址 |
| 证据 | 警告绑定结构化来源 + 中文原文 exact substring（现状保留）+ 文档版本/输入时效；检索空= `no_evidence_found`、KEGG 断= `dependency_unavailable`、药名未识别= `unresolved_medication`，均**不得**映射为"无风险"；LLM 自报置信度不作为医学可信度 |
| 输出 | 最终实际交付文本过完整规则集（现状保留：检查文本=交付文本）；verifier 只裁决允许的语义误报，硬闸门（伪造引用/URI、缺升级、warning 结构、conflict sides）不可裁决；人工同意不能解除诊断/处方边界 |
| 数据 | checkpoint/备份/导出纳入访问控制；日志只记 pseudonymous id、错误类别、耗时、token、工具名、引用 id——**患者正文与密钥不进普通日志**；`--llm-planner` 保持显式 opt-in 及其披露警告 |
| 注入 | 用户文本/RAG 文档/记忆/审核自由文本均按不可信数据；guard 模型只是辅助层，授权与工具策略必须在代码（OWASP LLM 分层防御，见 09-sources.md） |

P0 的具体落点：`server.py` 增加 `Principal` 依赖与 `require_role`；`EventIn.source` 保留但审计同时记录 authenticated actor；日志脱敏 helper（错误消息白名单字段结构化输出 + trace_id 贯穿 worker → agent → 工具）。
