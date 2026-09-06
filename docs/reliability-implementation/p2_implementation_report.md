# Reliability P2 实施报告（人工处理闭环）

实施日期：2026-09-06。范围：**仅 P2**（在 P0/P1 基础上），依据 `docs/reliability-design/06-human-handoff.md`（状态机/分流/恢复流程）与 `07-staged-plan.md`（P2 验收）。未部署；未连接真实人工（显式边界，见下）；未触碰真实 `memory.db`。

## 一、修改文件与原因

| 文件 | 变更 |
| --- | --- |
| [stage0/memory.py](../../stage0/memory.py) | 三张新表：`review_cases`（`logic_key UNIQUE` 幂等建单 + 开单时**事实快照**（medication_set_hash / scope revision）+ revision CAS + due_at/round）、`review_decisions`（`idempotency_key UNIQUE` + 五类结构化动作 CHECK + outcome 轨迹）、`resume_tasks`（`operation_id=resume:{decision_id}` UNIQUE，pending→consumed CAS）。方法：`open_review_case`（幂等）、`claim_review_case`（CAS，同人重领幂等/他人 409 语义）、`record_review_decision`（**事务③**：决策 + resume 任务同事务；重复键重放返回原 decision）、`pending_resume_tasks/consume_resume_task`、`set_review_decision_outcome`、`close_review_case`、`mark_overdue_review_cases`（逾期=运营状态，绝不自动通过） |
| [stage0/graph_runner.py](../../stage0/graph_runner.py) | 图新增节点：`open_review`（幂等建单 + 固定代码文案的**安全等待响应**）、`await_review`（**仅 `interrupt()`，零副作用**——恢复会重跑整节点）、`apply_review`（权威再校验 + 有界效果写入）；条件边 `compose → open_review \| publish`、`apply_review → await_review \| publish`。分流规则为代码所有：severe（contraindicated/major）警告或未决 conflict → `needs_clinical_review`；`STAGE0_REVIEW_ENABLED` 显式开关（默认关，legacy 路径不能挂起、不做 review）。审阅决策**不能注入交付文本**：五类动作对应五段固定文案，reviewer 的 basis 只入审计 |
| [stage0/server.py](../../stage0/server.py) | Worker：`drain_resume_tasks`（消费 resume 任务→重验 run 状态→`runner.resume`→收敛 case/run 状态）、每轮逾期扫描、等待态发布（任务 done + 等待响应单事务发布，**立即释放租约**——人工等待不占 Worker/锁/事务）。端点：`GET /v1/review-cases`、`POST /v1/review-cases/{id}/claim`、`POST /v1/review-cases/{id}/decisions`（Idempotency-Key 必填）、`POST /v1/review-cases/{id}/cancel`、`GET /v1/review-cases/{id}/summary`（可导出咨询摘要）。事件状态端点以 `workflow_runs` 最新结果为权威（等待态→等待响应；收敛→最终结果），POST 重放同口径。local-demo principal 增 `reviewer` 角色（模拟审阅用） |
| [stage0/api_client.py](../../stage0/api_client.py) | reviewer 方法（列表/摘要/接单/决策/取消），决策客户端生成幂等键 |
| [stage0/review_app.py](../../stage0/review_app.py)（新） | 模拟审阅员工作台（Streamlit），显著标记"模拟，非真实医护"：工单列表/摘要、CAS 接单、五类结构化决策表单、摘要 JSON 导出、幂等提交 |
| [stage0/app.py](../../stage0/app.py) | API 模式下：等待审核横幅（工单号/轮次/SLA 截止 + "超时不会自动通过"）+ 咨询摘要导出按钮 |
| [stage0/test_reliability_p2.py](../../stage0/test_reliability_p2.py)（新） | 10 个 P2 回归测试 |

## 二、闭环流程（已实现并测试锁定）

```text
事件受理 → 图执行 → compose 检出 severe/conflict（STAGE0_REVIEW_ENABLED=1 时）
  → open_review：幂等建单（logic_key）+ 安全等待响应（固定文案 + 已过安全检查的正文）
  → await_review：interrupt 挂起；worker 发布等待结果并释放租约（run=waiting_review）
  → reviewer：queue（reviewer 角色）→ claim（CAS）→ 五类结构化决策（Idempotency-Key + expected_revision）
      事务③：review_decisions + resume_tasks 同事务
  → worker 消费 resume 任务 → runner.resume(Command(resume=...))
  → apply_review：决策重验 + **事实时效核验**（medication_set_hash / scope revision vs 开单快照）
      ├─ 事实未变 → 有界效果（resolve_conflict 走既有审计 API / episodic clinical_review 记录 / 纯文案收尾）
      │              → publish → run 收敛，case resolved，交付文本=固定审核文案+已检查正文
      └─ 事实已变 → review_stale：决策拒用、旧案 cancelled、自动开新一轮（round+1）、run 继续等待
```

关键设计点：

1. **建单幂等**：`logic_key = event:{event_id}:{reasons}:r{round}`——重放/崩溃重建命中唯一键，不产生第二张工单。
2. **决策事务 + 幂等**：决策与恢复任务同事务；同 Idempotency-Key 重复回调重放原 decision（测试实证）；越 revision 直接 409。
3. **崩溃窗口**：决策已记录、resume 未消费前崩溃 → 重启后 worker 重新消费，`apply_review` 以 `outcome==applied` 幂等去重，同一决策只应用一次（测试 `test_claim_decide_resume_resolves_case` + P0 publish 单事务覆盖）。
4. **旧审批不授权新状态**：恢复前双重核验（worker 预检 run 状态；`apply_review` 内做权威事实比对），stale 走重审而不是放行。
5. **无人接单/逾期**：`mark_overdue_review_cases` 仅置 `overdue` 运营状态，无任何自动通过路径（测试断言无 clinical_review 副作用、无 decision）。
6. **权限**：queue/claim/decision/cancel 全部要求 reviewer（或 ops）角色 + scope 校验；决策 API 不存在表达 graph goto / SQL / 工具名 / state patch 的字段。
7. **未接真实人工的诚实边界**：等待响应、UI 横幅、review_app、摘要导出全部明示"本地演示，尚未接入真实人工服务"；模拟 reviewer 页面显著标记。

## 三、实际测试结果（2026-09-06，Windows 11，Python 3.13.14）

| 命令 | 结果 |
| --- | --- |
| `python -m unittest stage0.test_reliability_p2` | **10/10 OK** |
| 全量 11 套件（stage3/5/6、memory p0–p2、stage8、stage10、p0、p1、p2） | **150/150 OK（19.8s）** |
| `python -m unittest stage0.test_frontend_read_models`（前端轮套件，确认无回归） | **13/13 OK** |
| `python -m stage0.eval_memory --ablate` | **passed 28/28**；四机制全部 load-bearing |

### P2 验收对照（Prompt B）

| 验收项 | 测试 |
| --- | --- |
| 幂等建单 | `test_replayed_open_review_hits_logic_key`、`test_severe_warning_creates_exactly_one_case_and_parks_run` |
| 澄清/技术支持/拒绝路由 | 沿用既有 clarification、错误分类、policy refusal 路径（P0/P1 覆盖）；P2 新增 clinical_review 触发与 waiting 态 |
| CAS 接单、重复接单 | `test_claim_cas_and_decision_idempotency`（错误 revision 拒绝、同人幂等、他人冲突） |
| 决策幂等/重复回调只生效一次 | 同上（replay 返回原 decision，resume 任务仅 1 条） |
| 未接单案件不接受决策 | `test_decision_on_open_case_refused` |
| 建单→等待→接单→决策→恢复→收敛 全链路 | `test_claim_decide_resume_resolves_case`（202→waiting→claim→decision→resume→succeeded/resolved，交付文本含固定审核文案） |
| 等待期间事实变化 → review_stale | `test_review_stale_opens_new_round_and_keeps_run_waiting`（旧案 cancelled、outcome=review_stale、round 2 开启、run 仍 waiting、旧决策无效果） |
| 逾期/无人接单不默认通过 | `test_overdue_is_operational_never_approval` |
| 越权读/决策被拦截 | `test_deployment_reviewer_roles_and_object_scope`（401/409）+ reviewer 角色校验 |
| 摘要导出 | `test_summary_endpoint_shape`（JSON 可导出，含"尚未接入真实人工服务"声明） |

## 四、边界与诚实声明

- **没有真实医生/药师**：整个闭环以 local-demo principal 的 reviewer 角色驱动；真实部署需要真实身份方、排班与 SLA 配置（`STAGE0_AUTH_MODE=deployment` + 角色映射已就位）。UI/导出/工单文案均声明未接入真实人工。
- **紧急固定提示（urgent_guidance）未新增内容**：P2 没有经专业审核的紧急指引语料，现有"建议咨询医生/药师"升级文案原样保留；设计中的紧急分流路由待有审核过的固定内容后再启用（不得由 LLM 或 reviewer 现场生成）。
- **request_more_info 语义**：case 转 `waiting_user`，run 以 `degraded(review_waiting_user)` 终态收束；用户补充走新事件（新 run），与设计一致。
- **resolve_conflict 的审核执行**复用既有 `resolve_conflict` API（审计 + action trail）；`confirm/reject/close` 以 episodic `clinical_review` 记录存档，不改写医学事实、不动 verification 状态机（promotion 仍仅由规则触发）。
- schema 变更：`workflow_runs.status` CHECK 增 `waiting_review`（P1/P2 均未发布，无在野表需重建）；其余三表纯新增。

## 五、回滚

- `STAGE0_REVIEW_ENABLED` 未设/设 0 → 完全回到 P1 行为（graph 不含 review 路由触发）；`AGENT_GRAPH_RUNNER` 关闭 → 回到 P0。三张 review 表可保留不使用。
- 前端"前端轮"（同日另一会话）改动均未触碰；`server.py` 的 `operation_outcomes` 字段保持原样并在其上叠加 `run_status`/`review_case`。

## 六、遗留与下一步

1. **真实人工接入**：角色/SLA/紧急指引内容需真实配置；`review_app.py` 仅演示。
2. **通知投递**：`notification_deliveries` / effect_unknown 真实路径仍未实现（设计 04/05），P3 或随部署轮。
3. **P3 接口**：trace 链（`trace_id → run → review case`）字段已具备；指标（接单率/逾期率/重审率）可从三张表直接聚合。
4. held-out DDI v2 重跑仍未执行（Stage 9 遗留）；性能目标全部待测。
