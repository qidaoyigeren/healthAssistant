# 故障注入与安全测试矩阵

测试环境约定：临时 SQLite 数据库（`tempfile`）、fake/deterministic provider、模拟身份；不读写真实 `memory.db`、不调用线上模型、不发送外部通知。PostgreSQL 相关场景（P4）必须用真实 PostgreSQL 另测，SQLite 通过不算替代。产物写入 `docs/reliability-implementation/`。

| # | 场景 | 注入方式 | 断言 | 阶段 | 产物 |
| --- | --- | --- | --- | --- | --- |
| T1 | 两个长 key 前 32 位相同 | 提交两个 40+ 字符、前 32 位相同的 key | 均 202；两个独立 event_id；库中 `interactions.event_key` 各自独立且等于 `api:{key}`；两个事件都被处理 | P0 | `probe_key_collision.json` |
| T2 | 同 key 并发 20 次 | 并发 POST 相同 key+payload | 一个 event/run/任务；20 次响应指向同一资源；投影唯一 | P0 | `test_stage10_server` 扩展 |
| T3 | UI 超时重试 | 提交→轮询超时→以保存的原键重试 | 不新建事件；最终取得同一完整结果 | P0 | `test_ui_idempotent_retry` |
| T4 | committed 后重复 POST | 完成后重复 POST 与 GET | POST 与 GET 返回同版本完整 `response`；`Idempotent-Replay: true` | P0 | 服务测试 |
| T5 | 领域写成功后、complete 前崩溃 | 在 memory_write 事务后、事务④前注入 `SystemExit`/异常 | 恢复后 operation receipt、领域效果、审计各恰一次；无重复药物版本/结论 | P0 | `test_crash_after_domain_write` |
| T6 | 旧租约写入 | 租约过期→B 重领→A 迟到 complete/fail/heartbeat/领域写 | A 全部被拒（rowcount=0 语义），B 结果不被覆盖；审计记录 LeaseRejected | P0 | `test_stale_lease_rejected` |
| T7 | 429 重试上限 | fake provider 连续 429（带/不带 Retry-After） | 总尝试 ≤ max_attempts（含首次）；遵守 Retry-After；预算耗尽转降级；不叠加 SDK 重试 | P0/P1 | `test_retry_budget` |
| T8 | effect_unknown | 外部写超时且无查询回执能力 | 不盲重发；状态 `effect_unknown`；对账入口可见 | P0（语义）/P2（外部通知） | 测试 |
| T9 | 审核提交后崩溃 | 决策事务提交后、resume 消费前 kill | 重启后同一决策只应用一次；工单与 run 收敛 | P2 | `test_review_crash` |
| T10 | 等待人工后重启 | interrupt 挂起→进程重启 | 工单仍可见；原 thread/run 可恢复；活动预算未重置 | P1/P2 | `test_restart_during_review` |
| T11 | 过期审批 | 恢复时患者 revision 已变化 | `review_stale`；重新检测/审阅；旧审批不放行新状态 | P2 | `test_stale_review` |
| T12 | 越权读/恢复 | 无权 principal 访问他人 event/ref/review | 全部 4xx；错误响应无患者信息 | P0 | `test_object_authorization` |
| T13 | RAG 注入 | 语料中植入"删除记录/外发数据/改权限"指令文本 | 工具策略阻断；证据原文与审计可追溯；不执行指令 | P0/P1 | `test_prompt_injection_guard` |
| T14 | 检索空/来源失效/依赖断网 | 空 RAG 结果、失效 chunk、KEGG 断网 | 返回 `no_evidence_found`/`dependency_unavailable`/`unresolved_medication`，绝不输出"无风险" | P0（回归既有） | 既有语义测试扩展 |
| T15 | 无人接单 | 工单逾期（短 SLA 演示参数） | 状态 overdue；不自动通过；用户侧仍显示等待专业审核 | P2 | `test_no_reviewer_timeout` |
| T16 | checkpoint/版本升级回滚 | 旧版本 checkpoint 恢复 | 兼容版本正常恢复；不兼容时明确迁移/人工处置，无静默新建重复 run | P1 | `test_checkpoint_version` |
| T17 | 重试反放大审计 | 组合：SDK retry>0、适配器 Tenacity 默认、图 RetryPolicy、outbox 重领 | 总外呼次数 = 单一所有者口径；配置审计断言各层 retry=0/显式 | P1 | `test_no_retry_amplification` |
| T18 | 日志脱敏 | 触发含患者正文/密钥形态的错误路径 | 结构化日志无正文/密钥；有 trace_id 贯穿 | P0 | `test_log_sanitization` |

回归基线：每阶段完成后运行既有全量门禁（`python -m unittest stage0.test_stage3 stage0.test_stage5 stage0.test_stage6 stage0.test_memory_p0 stage0.test_memory_p1 stage0.test_memory_p2 stage0.test_stage8_agent stage0.test_stage10_server` 与 `python -m stage0.eval_memory --ablate`），报告实际命令、退出码与环境，不伪造结果。
