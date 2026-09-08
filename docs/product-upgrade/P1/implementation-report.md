# P1 实施报告：证据原文回读、解释和变更影响

日期：2026-09-07。本轮交付"用户能展开一条提醒，查看其事实依据和证据原文，并在相关事实变化后理解提醒状态"的纵向功能：后端 API + 前端入口 + 测试 + 评测。所有验证均为本轮实际执行；未运行的项目如实标注。

> 后续收尾核查发现本报告的元数据验证、影响归属、来源版本和浏览器验收存在缺口，已补充修复。当前状态以 [closeout/README.md](../closeout/README.md) 和对应 acceptance-summary.json 为准；下述历史测试数量不替代本次结果。

## 1. 实现

### 1.1 受控证据读取 API

- `GET /v1/evidence/{evidence_id}?offset=&limit=`（[read_models.py](/d:/py/HealthAssistant/stage0/read_models.py) `evidence_read`）：
  - scope 来自已认证 Principal（`_guard` + `p.scope_id`），请求参数不能扩大可访问范围；
  - 有界分页（1≤limit≤2000，与 EvidenceStore 一致）、读取时内容哈希复验；
  - 缺失 / 跨 scope / 篡改统一 404 `evidence_unavailable`（无存在性预言机）；哈希不匹配在 `details.integrity=hash_mismatch` 标注；
  - 返回来源类型、来源 URI、**来源版本（未知保持 null，不虚构）**、检索时间、正文长度、读取范围、完整性状态；只按内容寻址 evidence_id 读取，不接收任何文件路径。
- 服务端 EvidenceStore 与 MemoryStore 共享连接与单写者锁（server.py，与 agent 内部同模式）；DDL 幂等追加。

### 1.2 结论/警告 ↔ 证据真实关联

- 捕获链路：`_capture_evidence`（default_tools.py）在 ddi/rag 捕获证据时把 `evidence_id` 附加到对应 warning dict（ddi 按 source_text 有无 1:1 对齐；rag 按原文文本映射条件警告）→ planner 确定性替换路径（agent.py `materialize`）原样携带 → `_warning_sources` 写入 source_refs → `record_warnings_batch` 原子落库到结论 citations。
- 历史记录：source_refs 无 evidence_id 或证据已不存在时，详情接口返回 `evidence_refs[].status="unavailable"`（reason=`no_evidence_link`/`evidence_missing`），**不虚构原文、版本或证据 ID**。

### 1.3 解释结果与变更影响摘要

- 预警详情（`GET /v1/alert-records/{id}`）新增：
  - `evidence_refs[]`：每条来源的可读性解析（available 含元数据 / unavailable 含原因）；
  - `explanation`：事实依据（fact_refs=memory_refs）、依据版本（input_revision）、当前档案版本（scope_revision）、重查任务真实状态（dependency_tasks，open/running/done/failed）、重查后继结论。全部来自真实依赖查询，无模型参与。
- `GET /v1/change-impact?since=`：changed_facts 来自 audit_log 真实事实变更（附可追溯 memory_refs）；affected = 系统实际失效的结论（status='stale'）附真实 recheck 状态与后继；unaffected = 仍 current 的结论；**summary 计数 = 同一响应中明细列表长度（同一查询口径）**。语义：stale = 依据变化待重查，≠ 风险解除；重查失败/未运行时旧结论保持待重查。

### 1.4 最小 AnswerBundle

- `answer-bundle@1` 附加在事件响应（`answer_bundle` 字段，向后兼容可选）：claims（warning/conflict，含 evidence_refs）、fact_refs、evidence_refs、patient_revision、unresolved_questions、coverage（是否已保存、响应来源、降级原因）。
- 由 `_finalize` 在**安全门之后**从最终响应派生——正文与卡片同一份可信数据，bundle 不引入新事实、不绕过安全检查。legacy 与 LangGraph 两条 runner 路径均透传。

### 1.5 前端

- 证据抽屉（[evidence.tsx](/d:/py/HealthAssistant/frontend/src/components/evidence.tsx) `EvidenceDrawer`）：分页读取原文、来源/版本/完整性元数据；**高亮只在原文中找到引用摘录的精确字符串时生成**，找不到明确降级为"未能在已读取的原文中精确定位——不做语义近似高亮"。
- SourceRefItem：有 evidence_id → "查看证据原文"；无 → "原文不可用：这条历史记录没有关联的证据原文（系统不会虚构原文）"。
- 预警详情：状态徽标（当前有效 / 依据变化待重查(不等于风险解除) / 证据不足 / 已按最新事实重查）+ 解释块（依据版本 vs 当前版本、重查状态、新结论入口）。
- 变更影响卡片（submission.tsx `ChangeImpactCard`）：用药更正提交后可展开，展示变更事实/受影响结论/待重查/重查失败计数与明细，`since` 锚定提交时间。
- AnswerBundle 摘要展示在结果卡"审计与来源"内（条目数与上方卡片同源、未决事项、保存状态）。

## 2. API / 数据契约

新增/变更端点（全部复用现有认证/错误模型/单写者）：

| 端点 | 说明 |
|---|---|
| `GET /v1/evidence/{evidence_id}?offset&limit` | 受控原文读取；404=缺失/跨scope/篡改；422=非法分页 |
| `GET /v1/change-impact?since&limit` | 变更影响摘要 |
| `GET /v1/alert-records/{id}`（扩展） | 追加 `evidence_refs`、`explanation` 字段 |
| `POST /v1/events`（响应扩展） | 追加可选 `answer_bundle` 字段 |

兼容性：均为**加字段/加端点**，无删改；旧客户端忽略新字段不受影响。旧库无 evidence 关联的数据以 `unavailable` 如实呈现，无需迁移；关闭新功能（不调用新端点）时系统行为与此前一致。

## 3. 测试与验证（本轮实际执行）

### 3.1 后端（先写失败测试再实现）

`stage0/test_product_p1.py`（10 项，实施前确认红：evidence_id 缺失、/v1/evidence 404、bundle 缺失等；实施后 10/10 通过）：

1. 分页形状与元数据、拼接一致；2. 非法分页参数拒绝（422，无内容泄漏）；3. 缺失与跨 scope 同一错误（无存在性预言机）；4. 篡改内容拒绝 + integrity=hash_mismatch；5. 重复回读一致 + 未知版本保持 null；6. 记录警告 citation 携带可回读 evidence_id 且内容与 source_text 一致；7. 详情 evidence_refs/explanation（available+verified）；8. 历史记录如实 unavailable；9. 更正后相关结论 stale、无关结论不受影响、影响统计=明细；10. 无重查 hook 时永不读作"风险解除"。

`stage0/test_product_evals.py`（3 项）：评测入口必败样例返回非零、缺数据输出 unavailable 不输出 pass、dev 套件驱动真实服务栈。

**全量回归：291 项（281 基线 + 10 新增），失败 0、错误 0，退出码 0。**

### 3.2 开发评测

`python -m stage0.product_evals.run_eval --suite dev`：**12 任务 = 11 通过 + 1 unavailable**（`dev-p0-inv-009` 依赖 P2 候选模型，如实 unavailable，套件整体不标 pass，退出码 2）。覆盖：正常分页/非法参数/篡改/跨scope/重复一致/未知版本/更正影响/重查失败不解除/同源 bundle/幂等重试/安全门。

### 3.3 前端

- `npm run typecheck`：通过（0 错误）。
- `npm run build`：通过（487KB / gzip 144KB，9.3s）。
- 真实浏览器验收：见 §3.4。

### 3.4 真实浏览器五步验收

环境：`stage0.product_p1_browser_fixture`（临时合成库 + 脚本检测器，127.0.0.1:8000）+ Vite dev（5173）+ Playwright（msedge）。脚本：`scripts/product-p1-browser-acceptance.js`。

收尾已通过真实 Edge 浏览器的 14 项检查，覆盖原文精确高亮、未知来源版本、事实详情、真实更正、操作级影响归属、计数与明细一致、刷新后回读、无 React pageerror 和无 HTTP 5xx。见 [browser.json](../closeout/browser.json)。后端使用独立 8011 端口的临时合成库，前端 5181；本段取代先前未填充的验收占位。

## 4. 剩余问题与边界

- 变更影响已改用服务端 run_id 关联实际变更审计；since 仅保留为通用审计时间窗口。缺少归属的历史操作返回明确不可用，不推断为零影响。
- AnswerBundle 为最小版；claims 级引用支持状态（P4 的 supported/contradicted/insufficient）未实现。
- 证据抽屉高亮依赖精确字符串匹配；跨页引用需点"继续读取"后再定位（有明确降级提示）。
- 真实模型实验、独立 held-out、人工质量评价：unavailable（与 P0 报告一致，未并入本轮结论）。
