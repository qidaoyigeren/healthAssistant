# 已核查官方来源与剩余假设

## 官方来源（访问日期：2026-09-05）

| 来源 | 用途 | 核验到的关键事实 |
| --- | --- | --- |
| [LangGraph Persistence](https://docs.langchain.com/oss/python/langgraph/persistence) | ADR-001、checkpoint 边界 | InMemorySaver 重启即失；SqliteSaver 定位为本地开发；PostgresSaver 生产用，`setup()` 建表；checkpoint 按 `thread_id` 作用域（Postgres 下 <255 字符）；页面未承诺 checkpoint 与应用库事务原子 |
| [LangGraph Interrupts](https://docs.langchain.com/oss/python/langgraph/interrupts) | 02-architecture 节 4 | `interrupt()` 恢复**从头重跑整个节点**；副作用须幂等或拆到 interrupt 之后/独立节点；不得 try/except 包裹；不得条件化改变 interrupt 顺序（resume 按 index 匹配）；`Command(resume=...)` 是唯一作为图输入的 Command 形式 |
| [LangGraph Fault tolerance](https://docs.langchain.com/oss/python/langgraph/fault-tolerance) | 05 节 1–2 | `RetryPolicy.max_attempts` 默认 3 且**含首次**；默认 jitter on，指数退避 cap 128s；默认重试排除常见编程错误，httpx 仅 5xx；节点 timeout 需 langgraph≥1.2 且**仅 async 节点**（sync 带 timeout 编译期拒绝）；超时尝试的状态写被清除但外部效果可能已落地 |
| [OWASP LLM Prompt Injection Prevention](https://cheatsheetseries.owasp.org/cheatsheets/LLM_Prompt_Injection_Prevention_Cheat_Sheet.html) | 05 节 4 | 分层防御、最小权限、工具 allowlist（方案文档既有引用，本轮沿用其原则） |
| [AWS Transactional Outbox](https://docs.amazon.com/prescriptive-guidance/latest/cloud-design-patterns/transactional-outbox.html) | 04 节 2 | outbox 至少一次投递，消费者须去重——支撑"不宣称端到端 exactly-once" |

未逐字核验、以方案文档既有引用带过的：Tenacity 文档、OpenTelemetry sensitive data、PostgreSQL SELECT locking（P4 阶段实施时复核）。

## 剩余假设

1. **LangGraph 版本未锁**：本仓尚未安装 langgraph。P1 实施时先做依赖解析 + 持久化/重启/interrupt 三项最小冒烟，再写依赖锁；本文引用的语义以访问日期当天官方文档为准，版本落地后复核。
2. **单 evaluator 评测局限照旧**：held-out 标签与对照集小且单人标注；v2 语料的相关性标注需重新人工标注。
3. **无真实审阅员**：所有人工闭环先以显式标记的本地模拟 reviewer 演示；真实角色、SLA、紧急指引内容需真实专业配置。
4. **provider usage 口径未接**：token 预算维持 chars/1.5 估算（Stage 8 已校准 150k 兜底）；usage 接入是 P3 埋点项。
5. **性能目标全部待测**：本文不给出未经测量的 p50/p95 或吞吐数字。
6. **Windows 单机开发环境**：文件锁（本轮探针即遇到 tempdir 清理的 WinError 32）提示测试需先关闭连接再清理临时目录；并发语义以 PostgreSQL 阶段为准。
