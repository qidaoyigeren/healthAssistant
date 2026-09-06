# 人工交接闭环（P2 设计，P0 只提供授权接口）

## 1. 分流矩阵

| 路由 | 触发 | 接收角色 | 行为 |
| --- | --- | --- | --- |
| `need_user_input` | 药名/时间/对象等关键字段缺失 | 用户本人 | 一次具体澄清；保存暂停状态（现有 ask_clarification 语义并入） |
| `needs_clinical_review` | 严重警告、证据矛盾、来源不足且涉及风险判断 | 授权医生/药师 | 建单 + 安全等待回复；**真实角色需真实配置** |
| `needs_support` | provider 故障、任务卡住、提交/恢复失败 | 技术支持 | 无权解释或裁定用药风险 |
| `policy_refusal` | 诊断/开药/越权请求 | — | 立即拒绝；人工不用于洗白违规请求 |
| `urgent_guidance` | 命中经专业审核的紧急规则 | — | **固定指引立即呈现**，不排普通人工队列；随后可附加交接 |

软件分流设计，不含临床判定阈值。紧急指引内容必须预先由专业人员审核后固化为常量（P2 前保持现有升级文案）。

## 2. review_cases 状态机

见 [02-architecture.md](02-architecture.md) 第 5 节。要点：

- `overdue` 是仍待处理的运营状态；**超时绝不默认通过，无人接单不产生任何"已确认"语义**。
- 唯一建单规则：`(event_id, reason_code, open_flag)` 逻辑键唯一——重放/崩溃重建不产生第二个工单；用户补充新事实 → 新 event → 允许新工单。
- 未接入真实人工服务时：UI 显示"尚未接入人工服务，可导出咨询摘要（含结构化用药/风险摘要与证据引用）"；演示 reviewer 页面带显著"模拟"标记，不伪称医生已接单。
- 只有持久化工单成功才显示"已提交"；真实队列接收成功才可声称"已转接"。

## 3. 工单字段

scope/patient、case_id、event/run/thread/interrupt id、reason_codes、priority、status、assignee、due_at、revision（CAS 用）、结构化用药与风险摘要、证据引用、原始记录入口、已做/未做检查、允许的审核操作、创建/领取/处理时间。通知只发最小化摘要或受保护链接，凭证不发。

## 4. 审阅与恢复流程

1. `open_review` 幂等建单（事务③）并生成安全等待回复；`await_review` 节点仅 `interrupt()`；释放执行租约——Worker 线程与数据库事务不得等待人工。
2. reviewer 授权查询队列；`expected_revision` CAS 接单，重复接单只生效一次。
3. 结构化决策动作：`request_more_info / confirm_reported_fact / resolve_conflict / reject_candidate / close_with_safe_guidance`，受角色限制；**不能**提交任意 graph goto、工具名、SQL 或 state patch。
4. 决策 API：鉴权 + 对象范围 + case/interrupt 版本 + revision 校验 + Idempotency-Key（重复回调只生效一次）；决策记录与 `resume_task` 同事务写入。
5. Worker 消费 resume_task：重新验权 → 加载最新患者事实 → 比对审阅依据；等待期间药物/过敏等已变化 → `review_stale`，重新检测/审阅，**旧审批不授权新状态**。
6. 只有验证通过的决策由**服务端**转换为 `Command(resume=...)`；恢复后仍经过工具权限、证据与最终回复检查。工单关闭与图恢复结果可追踪，不以单次 HTTP 返回认定成功。

## 5. SLA 与无人接单

- SLA 参数可配置（演示用短 SLA 测超时流）；真实响应时限取决于排班与承诺，UI 不虚构"5 分钟医生回复"。
- 逾期：状态转 `overdue`、进入 reassigned 候选、计数上报（P3）；期间用户侧持续可见"等待专业审核中"，不降级为自动答复风险判断。
- 撤销：非终态可 cancel，需理由 + 权限 + 审计。
- 用户等待期间可提交新事件（不阻塞）；新事实导致旧 review 变 stale 的联动见上文第 4.5 步。
