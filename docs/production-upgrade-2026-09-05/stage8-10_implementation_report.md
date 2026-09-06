# Stage 8 / 9 / 10 实施报告

- 实施日期：2026-09-05。基线：`528b3a5`（Stage 7 提交）。
- 本轮范围由用户决策驱动（同日确认）：**/events 异步优先（202+轮询）**、**重排接受下载（Stage 11 实施时生效）**、**约 1 个月节点**——即 Stage 8 核心 + Stage 10 全部 + Stage 9 语料修复（C1）一轮完成，外加 Stage 7 文档轮。
- 纪律：默认离线路径行为不变（Streamlit 直连仍是默认；API 为 `STAGE0_API_URL` 显式开启的部署形态）；未清空/未改动现有 `memory.db` 与 `stage0/data/` 既有工件；未测量的数字一律「待测」。

## 一、文档轮（先于 Stage 8）

[README.md](../../README.md) 新增 "Memory deepening (Stage 7)" 章节、全量测试命令更新；[stage0/REPORT.md](../../stage0/REPORT.md) 新增 §8（依赖索引/选择性失效/重查健壮化/AS-OF/巩固）+ GO 条目 + 复现命令。

## 二、Stage 8（Agent 健壮化）变更清单

| 项 | 实现 |
| --- | --- |
| B1 回合预算 | `TurnBudget`（`AGENT_TURN_BUDGET_SECONDS` 默认 120s / `AGENT_TURN_TOKEN_BUDGET` 默认 150k 估算单位 / max_cycles）；循环顶部三预算取先触发；降级响应头部追加固定代码常量 `BUDGET_DEGRADED_NOTICE`（「本次处理在预算内未完成全部检查，结果可能不完整；建议咨询医生/药师」），且在最终安全检查**之前**追加——校验文本与交付文本严格一致；预算条目进 trace |
| B2 payload 有界化 | `Observation.cycle` 字段；`_bounded_observations`（当前轮+前 2 轮全量、更早仅 `{tool,purpose,ok,cycle,result_size,result_digest}`；RAG chunk 文本截 200 字符）；`_bounded_trace`（去内嵌 observation、proposal 截 400）。**红线**：压缩只作用于给 LLM 的序列化视图，`materialize`/响应路径继续读原始 Observation——测试断言压缩后 state 原件未动 |
| B3 熔断 | 连续安全拒绝 ≥2（`PLANNER_SAFETY_REJECTION_LIMIT`）→ 本回合剩余周期走 `HybridPlanner._fallback` 确定性规划，不再消耗 LLM 调用；首次拒绝仍反馈 LLM 重规划（既有 Stage 6 语义保留）；trace 记 `circuit_break` 决策 |
| B4 否决式 Verifier | `ResponseVerifier`（opt-in：`--llm-verifier` / `AGENT_LLM_VERIFIER` / 构造器注入）；verdict 只能放行语义标记或否决（附行号+逐字摘录，**代码回验**摘录确实出现在该行，否则视为 verifier 失败）；硬闸门（fabricated ref/URI、missing escalation/refusal、warning 结构、conflict sides）永不可裁决；任何 verifier 失败回退完整规则集（`_last_verifier_info` 记录）；干净规则通过不烧 verifier 调用（成本取舍，见偏差 2） |
| B6 trace 持久化 | `turn_traces` 表（并入 4-p2 迁移块，IF NOT EXISTS 对已开库幂等补建）；`record_turn_trace`/`traces_for_turn`；agent 每轮 flush，失败一次性停用（trace 落库绝不打断回合）；与 audit_log 分工：过程 vs 数据变更 |
| B7 超时分层 | verifier 独立 15s 超时（`AGENT_VERIFIER_TIMEOUT_SECONDS`，自建 OpenAI client，max_retries=0）；planner/composer 维持 60s（live 实测 18–51s 接受调用，压缩会误伤）；后台 outbox 任务 120s+租约（Stage 10 落地） |

**测试证据**：`test_stage8_agent` **14/14**（预算×3、熔断×2、payload×3、verifier×4、trace×2）；含 Stage 8 的全量回归 **106/106**（Stage 10 改动前的时点）+ `eval_memory` **28/28**。

**与设计的偏差**：
1. **token 预算默认 24k → 150k**：设计稿 24k 假设 provider usage 口径；实施用 chars/1.5 估算（每轮重发完整 payload，JSON/catalog 开销被高估），24k 会在第 2–3 轮误降级真实尺寸回合（全量回归捕获）。150k ≈ 完整 16 轮有界回合的兜底；wall-clock 仍是 live 主限制器；usage 口径接入是 Stage 11 埋点。
2. **干净规则通过不调用 verifier**（设计流程图含「始终过 verifier」的补充否决权）：为省每次回复一次 LLM 调用，verifier 仅在规则产生语义标记需要裁决时运行。补充否决仅在 reject 场景生效（findings 附加）。
3. **规则层未精简**：设计 B4 的「规则回归简洁」未做——verifier 先以裁决层落地（语义误报可被清除），规则豁免补丁保留为安全底座；精简留待 verifier 误报/漏报数据积累后（诚实取舍，避免「精简了规则但没开 verifier」的裸奔窗口）。

**测量工件**：`docs/production-upgrade-2026-09-05/payload_budget.json`——合成 RAG 尺寸观察栈（每轮 5×480 字符 chunk）：12 轮观察时 payload 77k → 9k 字符（估算 token 51k→6k）；6→12 轮增量从「全量叠 6 份」降为 6 行摘要（增量 < 2.4k 字符）。

## 三、Stage 9（C1 语料修复）变更清单

- `CHRONIC_DRUG_TERMS` 补 `茶碱、氨茶碱、多索茶碱`（[rag.py](../../stage0/rag.py)，held-out 5 个 FN 全为茶碱对的语料层根因）。
- 以新 seed `stage1-rag-v2` 重建：语料 `stage0/data/rag_corpus_v2.jsonl`（**1200 标签 = 500 慢病优先 + 700 哈希多样，与 v1 同构**）、索引 `stage0/data/structured/rag_index_v2`（**4051 chunks**，v1 为 4158）。v1 工件原样保留。
- **血缘数字**：v2 语料含 **90 个茶碱类标签**（氨茶碱片、多索茶碱、复方茶碱麻黄碱等）——v1 中茶碱只能靠哈希抽样入选（≈0）。血缘记录：`rag_corpus_summary_v2.json`。
- **held-out 重跑（拿归因数字）：待执行**——需要 KEGG 网络与 LLM provider，且须清 RAG 侧 fallback 缓存以保证归因干净；本轮 shell/网络窗口未完成，作为遗留项（见限制）。45 条检索 query 集的相关性标注针对 v1 chunk id，v2 基线需重新人工标注（单人标注局限照旧），不在本轮。

## 四、Stage 10（服务化 + 两层幂等 + outbox）变更清单

| 文件 | 变更 |
| --- | --- |
| [server.py](../../stage0/server.py)（新） | FastAPI 应用工厂 `create_app(db_path, agent_factory, worker_thread)`；模块级 `app` 惰性创建（import 不打开 live 库）；端点：`POST /v1/events`（**异步 202 + 受理/入队同事务**）、`GET /v1/events/{key}`（轮询）、`/v1/memory/state|timeline|conflicts`、`/v1/alerts`、`/v1/conflicts/{id}/actions`、`/v1/rechecks`、`/v1/health`；四类错误模型（validation/safety/provider/internal 统一 `{"error":{code,category,message,trace_id}}`，含 pydantic 校验错误接管）；`OutboxWorker` 单线程工人（租约/回收/attempts≥3 判 failed/完成回填幂等键；统一消费 durable rechecks） |
| [memory.py](../../stage0/memory.py) | `idempotency_keys` + `outbox_tasks` 表（并入 4-p2 迁移块，增量幂等）；`accept_api_event`（**幂等键认领与 outbox 入队单事务**——崩溃不可能留下有键无任务）、`claim/complete/fail_outbox_task`（与 dependency_tasks 同款租约语义）、`complete_idempotency_key` |
| [api_client.py](../../stage0/api_client.py)（新） | httpx 客户端：`submit_event`（提交+轮询+超时）、四个读端点；UI 重试以客户端幂等键防双记 |
| [app.py](../../stage0/app.py) | `STAGE0_API_URL` 显式开启 API 客户端模式（读走 API、事件走异步提交+轮询、重置按钮禁用并说明）；**默认直连路径零改动**（`API_CLIENT=None` 分支与原代码等价） |
| requirements-stage10.txt（新） | fastapi 0.141.1 / uvicorn 0.52.4 / httpx 0.28.1（从工作 .venv 冻结；此前已装未声明的问题一并解决） |

**异步幂等语义（已实施，随用户决策修订设计 D1/D2）**：
- 同键同载荷：首次 202；重放 202 + `Idempotent-Replay: true`（committed 后携带存储受理）；**并发同键返回同一受理**（任务同一件事，无 409 风暴）——异步模型下不再需要「等待 vs 409」的同步权衡。
- 同键异载荷：422 `idempotency_key_reused`（与事件级 `IdempotencyKeyReused` 文化一致）。
- 底层任务 failed 的键：409 `previous_attempt_failed`（受理时交叉核对任务状态，覆盖「任务失败与键更新之间崩溃」的窗口）。
- 事件级 `event_key` 由幂等键派生（`api:{key}`）——重放在领域层天然去重。
- at-least-once / effectively-once：租约过期回收重执行；事件级 dedup 保证投影唯一（测试断言恢复后 `克拉霉素` 恰好 1 条、结论数不增）。

**测试证据**：`test_stage10_server` **11/11**——异步生命周期（202→轮询→committed + 记忆投影断言）、真实工人线程冒烟、幂等四象限（重放/异载荷 422/失败键 409+新键可用/并发同键）、outbox 崩溃恢复（过期租约回收+单次投影）、读端点与错误模型、冲突动作端点。

## 五、全量回归门禁（已执行，2026-09-05）

- **`python -m unittest stage0.test_stage3 stage0.test_stage5 stage0.test_stage6 stage0.test_memory_p0 stage0.test_memory_p1 stage0.test_memory_p2 stage0.test_stage8_agent stage0.test_stage10_server` → 117/117 OK（7.9s）**
- **`python -m stage0.eval_memory --ablate` → 28/28 通过；policy / bitemporal / dependency / selective_invalidation 四机制全部 load-bearing**
- `stage0/data/structured/rag_corpus_summary.json` 已恢复为 v1 内容（v2 副本在 `rag_corpus_summary_v2.json`）

## 六、仍存在的限制

1. **held-out DDI 重跑未执行**（Stage 9 的归因数字待测）：需 KEGG 网络 + provider key + 清理 RAG 侧 fallback 缓存；命令与注意事项见设计文档 C1 与 REPORT 复现节。
2. **v2 检索基线未建**：45 条 query 的相关性标注绑定 v1 chunk id；v2 需重新人工标注（不在本轮，单人标注局限照旧）。
3. **outbox 的 `extract_facts` / `ddi_live_fallback` 执行器未接入**：类型与工人框架就绪，仅 `process_event` 落地（1 个月裁剪方案的既定取舍）。
4. **server 为单写者拓扑**：`uvicorn --workers >1` 明确不支持（设计 D7）；`/v1/events` 的 provider 类错误（如 LLM 超时）表现为任务 failed 而非 503 响应（回合已异步化）。
5. **payload token 口径为估算**（chars/1.5）：provider usage 接入前，token 预算是粗粒度兜底（已按 150k 校准）。
6. **Streamlit API 模式为最小集成**：读/写/轮询/禁用重置已接；「开启新会话」在 API 模式仅重置 UI 会话号。
7. **Stage 9 的 C2–C6（重排/查询构造/补检/基础设施/评测升级）未做**——按 1 个月裁剪方案属后续轮次。
8. **README/REPORT 的 Stage 8/9/10 章节**：随本报告同轮补齐（见文档轮更新）。

## 七、回滚方式

- Stage 8：预算/熔断 env 关闭（设大值/limit 调高）即回到无预算行为；verifier 默认关闭；`turn_traces` 表纯增量。payload 有界化无开关（行为改进，测试全绿），如需对比可 `git revert` 本轮 agent.py 提交。
- Stage 10：不设 `STAGE0_API_URL` 时 app.py 与全部既有路径逐行为等价；`server.py`/`api_client.py` 独立文件可直接删除；`idempotency_keys`/`outbox_tasks` 纯增量表。
- Stage 9：v2 语料/索引为独立新路径；`CHRONIC_DRUG_TERMS` 增词只影响未来 curate 运行，v1 工件未动。

## 最终门禁（已于 2026-09-05 执行通过，见第五节）
