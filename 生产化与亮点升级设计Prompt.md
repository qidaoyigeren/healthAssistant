# 用药协管员：生产化与四大亮点升级设计 Prompt

适用仓库：`D:\py\HealthAssistant`。代码精读与核对日期：2026-09-05（基于 e3fcc78 工作区）。

使用方式：将下文「完整 Prompt」整段交给能读取本仓库的开发 Agent。它要求先交付设计、接口草案、迁移方案和验证计划；本轮不要求改造业务代码。末尾附有分阶段实现指令，可按 Stage 逐段使用。

本文件与《记忆模块升级设计Prompt.md》的关系：前一份 Prompt 的 P0/P1/P2 已落地（受控写入、双时间、依赖失效、重查任务、FTS 历史检索、可信度面板）。本文件不再重复设计这些内容，而是回答四个新问题：**记忆模块如何从"已实现语义"升级为"可测量的超级亮点"；Agent 循环如何补齐预算、熔断与验证层；RAG 如何针对已知缺口提升；以及整个系统如何获得请求级幂等、服务边界与性能证据。**

## 设计建议与现状依据

主线定位：**把一个诚实的单机工程演示，升级为"每个亮点都有独立指标、每个失败模式都有设计回应"的可部署系统。** 四条主线的优先级按你的目标排序：记忆（超级亮点）→ Agent → RAG → 生产化幂等与性能。

### 现状总评

先说结论：这个项目**不是普通 demo**。代码里已经有双时间 `query_state`、存储层策略守卫、T0/T1 两阶段事务固化、scope revision 失效传播、带租约的重查任务表、三闸门响应检查、以及 s01–s15 记忆场景回归（[eval_memory.py](stage0/eval_memory.py)）。大部分作品集项目没有其中任何一项。

它仍是 demo 的原因不在器官，而在骨架：Streamlit 既是前端又是后端（[app.py](stage0/app.py)）；没有请求级幂等键（只有事件级 `event_key`+`payload_hash`）；Agent 循环同步阻塞、无预算熔断；trace 只存在于内存中的 `AgentResponse`；没有备份/导出/部署/CI；性能主张没有 p50/p95 证据。四条主线分别补齐这些。

### 主线 A：记忆模块（目标：超级亮点）

| 代码位置 | 已有能力 | 本次精读发现 |
| --- | --- | --- |
| `stage0/memory.py:1455` `query_state` | valid_at/known_at 双时间读取，后录入更正不泄漏进早 known_at 视图 | 冲突的解决/撤销不参与时间重建：`conflicts` 只按 `created_at<=known AND status='open'` 过滤（L1501-1507），今天 resolved 的冲突在历史视图里仍显示 open。`resolved_at` 已存但未用于过滤——一行条件即可修复的 AS-OF 缺口 |
| `stage0/memory.py:806` `_conclusions_citing_semantic_key_tx` | 语义事实变更→引用它的结论失效 | 全表扫描 `status='current'` 的结论并逐条 Python 解析 JSON（L806-823），无依赖索引表；每次事实写入都触发。当前量级可跑，但没有"结论依赖结论"的传递性失效，规模上升后是 O(全部当前结论×每次写入) |
| `stage0/memory.py:848` `_invalidate_medication_dependents_tx` | 药单集合级失效：新增药物也能失效旧预警（scope revision 机制） | 失效粒度是全局 revision 比较：**任何**药物变更使**所有**依赖药单的当前结论失效（L856-867）。新增与旧预警完全无关的药也会触发重查——保守正确，但会造成重查风暴。按药物对（pair）粒度建立依赖可实现选择性失效 |
| `stage0/memory.py:1665` `recheck_pending` | 持久化重查任务、attempts≥3 判 failed、无 hook 时保持 open 不假装安全 | ① 租约无过期：置 `running` 后（L1684-1689）无 lease timeout，进程崩溃则任务永远卡在 running；② `agent.py:1741` `_recheck_hook` **每个任务**重新调用一次完整 `detect()`（同一药单重复检测 N 次）；③ 重查只重跑 DDI 检测，condition/conclusion（过敏/肝肾/年龄类）的旧结论只能走保守"未检出"模板，不重跑 RAG 条件检查 |
| `stage0/memory.py:1072` `consolidate_interaction` | T0 裸事件持久化→锁外抽取→T1 原子投影；event_key 幂等重放 | 没有跨事件的情景→语义巩固任务：N 次一致的待核实报告（`needs_verification=1`）不会被晋升为语义事实，也不会因矛盾被阻断。`verification_status` 字段（recorded_as_reported/verified）存在但无状态机推进它 |
| `stage0/memory.py:1267` `retrieve_episodic` | 显著性×半衰期排序 | `as_of` 仍只参与衰减计算不过滤行（L1285），与旧 Prompt 记录一致；AS-OF 事件读取依赖 `query_state` 或手工 SQL |
| `stage0/agent.py:1771` `handle` | 完整 plan→act→observe→reflect 循环 + trace | `AgentState`（observations/trace/cycle）纯内存，进程崩溃即丢；无断点续跑。事件本身幂等（重放会 dedup），但循环没有检查点 |
| 全局 | audit_log 覆盖所有变更 | `memory.db` 无备份/导出/导入路径。这是单家庭的用药史，备份故事本身是产品特性，也是"生产化"最便宜的加分项 |

**推荐设计方向**（详见完整 Prompt 第三节）：① `conclusion_dependencies` 依赖索引表替代全表扫描，支持按 pair/namespace 的选择性失效与传递性失效；② 重查工人健壮性：租约过期回收 + 同批次按"药单哈希"去重的批量检测 + condition 结论重查；③ 冲突解决时间重建（`resolved_at` 过滤）+ 巩固任务状态机（pending→verified/disputed）；④ 记忆指标套件扩展（失效覆盖率/误失效率/AS-OF 正确率已有雏形，需固化进 CI）。

### 主线 B：Agent 设计（目标：第二个亮点）

| 代码位置 | 已有能力 | 本次精读发现 |
| --- | --- | --- |
| `stage0/agent.py:757` `CANONICAL_PROPOSAL_SCHEMA` + `:947` `materialize` | 提示词/校验器/物化三处共享同一 schema；安全关键参数（药单、警告正文、refs）由代码从真实观察注水，LLM 只能选逻辑操作 | 已是"LLM proposes, code disposes"的正确形态，无需重写 |
| `stage0/agent.py:1307` `prompt_payload` | 有界 event/snapshot（`_compact`，L1353-1363） | **L1334 `"observations": [asdict(item) for item in state.observations]` 无压缩**，且 L1338 `"recent_trace": state.trace` 里每个 observe 条目又内嵌完整 `observation`（L1815）——同一份数据在 payload 里出现两次且不设上限。RAG 结果含 480 字符×5 chunk，16 cycle 上限下 payload 逐轮膨胀，token 成本近似二次增长，长循环有撑爆上下文风险。这是明确的修复点 |
| `stage0/agent.py:1779` `handle` 对 `PlanningRejected` 的处理 | 拒绝原因经 `recent_trace` 反馈给 LLM 下一轮 | **无连续拒绝熔断**：安全拒绝不触发紧急兜底（只有 provider/parse 错误走 `_fallback`，L1485-1492），持续提出不安全提案的模型会烧满 max_cycles=16 次 LLM 调用后才降级。需要"连续 N 次安全拒绝→emergency fallback"的断路器 |
| `stage0/agent.py:1671` `MedicationCoordinatorAgent.__init__` | max_cycles=16 上限 + 达上限显式降级 | 无 token 预算与 wall-clock 预算；每轮 planner 调用已有 `latency_ms`（L1569）但只是记录，不参与决策。`extract_ddi.py:268-284` 已有 60s 默认超时与环境重试，交互式回合里 60s 太长且 planner/composer 不分层 |
| `stage0/response_safety.py:15` AUTHORITY 正则 + `agent.py:2006` `_compose_response` | 三层闸门：composer→规则 checker→SafetyBoundary；伪造 ref/URI、无源警告、缺升级语都会被拦截 | 验证层是纯规则。README 记录的 Stage 6 事实：4/4 live 组合初版被拒、检查后确认 3 个是误报（良性免责声明/冲突摘要/否定式拒绝）后修正规则——说明规则检查器已到复杂度上限，需要**只能否决不能增内容**的 LLM Verifier 作为中间层，规则 checker 降为确定性底座 |
| `stage0/agent.py:1825` `_act` | 工具异常→ok=False 观察，不中断循环 | 工具全串行：`ddi_check` 与 `rag_search`、`memory_read` 互不依赖却不能并行；单回合延迟=Σ(每轮 planner 延迟+工具延迟) |
| trace | 每轮完整 trace 进 `AgentResponse.tool_trace`，UI 可展开 | trace 不持久化：跨会话无法查询"当时为什么这么答"；`memory_search` FTS 只覆盖 episodic payload |

### 主线 C：RAG 检索质量

| 代码位置 | 已有能力 | 本次精读发现 |
| --- | --- | --- |
| `stage0/rag.py:316` `search` | BM25(jieba)+BGE/FAISS 加权 RRF 融合（可调权重），section/approval/drug 过滤 | 无重排阶段；无查询改写——检索 query 是调用方拼好的原文 |
| `stage0/rag.py:41` `CHRONIC_DRUG_TERMS` | 慢病药优先的确定性语料挑选（1200 标签） | **茶碱类不在优先词表里**（L41-49 无 茶碱/氨茶碱/多索茶碱）。held-out recall 缺口集中在茶碱类的根因很可能在语料层而非检索层：茶碱标签只能靠 diverse_hash_sample 随机入选，覆盖率天然不足。修复必须动语料或引入回退检索策略，仅调融合权重救不了 |
| `stage0/rag.py:309` `_vector_order` | FAISS IndexFlatIP 精确检索 | L313 `self.index.search(vector, len(self.chunks))` 对全量索引检索后在 Python 里过滤 eligibility——每次查询的向量检索成本与语料总量成正比；应使用 `faiss.IDSelector` 或预分片索引。当前 10k chunk 可跑，规模化后是首要瓶颈 |
| `stage0/rag.py:305` `_bm25_order` | rank_bm25 全量打分 | 同样全量计算，无查询缓存；单次几十 ms 尚可 |
| `stage0/agent.py:160` RAGTool 降级路径 | 嵌入运行时不可用时的确定性 token 重叠降级 | L163 **每次降级调用都从磁盘重读 `chunks.jsonl`**，无 memoization——降级模式下每次 rag_search 都是一次全量文件解析 |
| `stage0/ddi_engine.py:674` `_merge_warning` | KEGG CI 定 contraindicated；P 由标签证据消歧 | KEGG `P` 无证据时默认 `moderate`——README 已记录的 severity 误差来源；需要"severity 证据缺失"显式标注而非默认值 |
| `stage0/rag.py:347` `retrieval_metrics` | recall@5 + MRR，30 条人工 query | 无 nDCG；无分 section 指标；query 集与相关性标注为单人 |
| README 已记录 | held-out citation coverage 0.40 | KEGG 来源的 warning 没有中文说明书引用的"二次补检"机制：`_warning_sources`（agent.py:356）对无 URI 的警告只标 `local_detector_provenance`，不尝试再检索一次中文标签来补引用。已知缺口没有对应的修复路径 |

### 主线 D：服务化、幂等与性能

| 代码位置 | 已有能力 | 本次精读发现 |
| --- | --- | --- |
| `stage0/memory.py:1097` 事件级幂等 | `event_key` 唯一索引 + `payload_hash` 校验 + committed 结果重放；`IdempotencyKeyReused` 拒绝同键不同载荷 | 这是**领域级**幂等，已经很完整。缺的是**请求级**：没有 API 层，客户端网络重试没有 `Idempotency-Key` header 语义。两层幂等的分工（请求级管 API 语义、指纹级管领域语义）是最好的生产化叙事 |
| `stage0/memory.py:613` 连接管理 | WAL + foreign_keys + timeout=30 + 进程内 RLock；T1 单事务原子提交 | 单写者模型对单家庭部署是正确架构；多 worker 部署需要显式的写路径收敛（单写进程或队列），文档目前没有说明这个边界 |
| LLM 调用（extract/planner/composer/ddi fallback） | 60s 默认超时 + 环境重试（extract_ddi.py:268-284） | 全部同步内联在请求路径上：provider 超时=回合超时，Stage 6 live 的 4 次超时直接变成 emergency fallback。无 outbox/异步重试——LLM 副作用与业务事务没有解耦 |
| `stage0/ddi_engine.py:97` `_write_json` | 原子替换写缓存 | 文件级缓存无跨进程锁；多进程同时写会相互覆盖（当前单进程部署下不触发） |
| `stage0/app.py:551` | 每次渲染读取 snapshot + `_stored_warning_records`（timeline(500) + 全量 conclusions JSON 解析） | Streamlit 每次交互全脚本重跑，这两个 O(历史) 调用在每次 widget 交互都执行，无缓存；历史增长后 UI 交互延迟线性上升 |
| 性能证据 | planner `latency_ms`、cycle 数、fallback 率已进 metrics JSON | 无端到端 p50/p95、无压测、无并发行为说明；README 的性能主张没有可复现脚本 |
| 部署 | `requirements-stage1.txt` + `.env`（gitignored） | 无 Dockerfile、无 CI、无健康检查、无结构化日志/trace 关联、无配置校验（pydantic-settings 级别） |

### 前沿方案的借鉴边界

以下是机制来源；如何用于本项目属于工程建议，不是这些方案已经替本项目实现的能力。

| 来源 | 可借鉴的机制 | 本项目的取舍 |
| --- | --- | --- |
| [Stripe Idempotency Keys 文档](https://docs.stripe.com/api/idempotent_requests) | 请求级幂等键：首次执行存储响应，重试返回存储结果，同键不同载荷报错 | FastAPI 层 `Idempotency-Key` header → `idempotency_keys` 表；与已有事件级 `event_key` 分层，不重复造 |
| [microservices.io Transactional Outbox](https://microservices.io/patterns/data/transactional-outbox.html) | 业务事务内写 outbox，独立工人投递副作用，保证 at-least-once + 幂等消费 | LLM 调用（抽取/planner/composer/ddi fallback）改为 outbox 任务；重查任务表已是同构先例，可复用模式 |
| [OpenTelemetry GenAI 语义约定](https://opentelemetry.io/docs/specs/semconv/gen-ai/) | gen_ai 工具调用/请求的 span 语义、trace 关联 | 落地为结构化 JSON 日志 + trace_id 贯穿 event→cycle→tool→response；不必引入完整 OTel 栈 |
| [Litestream](https://litestream.io/) | SQLite WAL 流式备份到对象存储 | `memory.db` 的持续备份与恢复演练；单家庭部署下这是最匹配的备份方案，不引入数据库迁移 |
| LangGraph checkpointing / Temporal durable execution | 长程 agent 状态检查点与恢复 | 借鉴"每轮循环后持久化 AgentState"的最小实现（working_memory 或独立表），不引入新框架 |
| [Cormack et al. 2009, Reciprocal Rank Fusion](https://dl.acm.org/doi/10.1145/1571941.1572114) | RRF 融合多路召回 | 已实现加权 RRF；本文件建议在其上加 cross-encoder 重排与多查询扩展 |
| [BAAI bge-reranker-v2-m3](https://huggingface.co/BAAI/bge-reranker-v2-m3) | 中文 cross-encoder 重排 | 召回 top-50 → 精排 top-10；CPU 可跑，与现有 BGE 栈同源 |
| [Lost in the Middle（Liu et al. 2023）](https://arxiv.org/abs/2307.03172) | 长上下文中部信息利用率下降 | 支持有界 ContextPacket 与压缩 observations 的设计，不追求无限上下文 |
| Anthropic《Building effective agents》（2024-12） | workflow vs agent 的取舍、"composability over frameworks" | 现有手写循环符合该建议；升级保持无框架、显式状态机，不迁 LangChain |
| [SQLite WAL 官方文档](https://www.sqlite.org/wal.html) | 单写者/多读者并发语义 | 文档化部署边界：单进程写、多进程读；多租户 SaaS 才迁移 Postgres |

## 完整 Prompt

从下一段「你的任务」开始复制，直到「完整 Prompt 结束」。

---

你的任务：担任熟悉 LLM Agent 系统、数据库工程和可观测性的资深工程师，基于当前 HealthAssistant 仓库（Stage 0–6 已完成、记忆 P0–P2 升级已落地），设计一套**从工程演示到可部署系统**的升级方案，围绕四条主线：记忆模块深化（超级亮点）、Agent 循环健壮化（第二亮点）、RAG 检索质量提升、请求级幂等与性能证据。

我的目标：这是用于技术面试与长期演进的核心项目。每条主线必须解决本仓库的真实问题（现状表已给出文件与行号）、有独立可测量的指标、有失败模式分析。请形成明确推荐和取舍，不要罗列框架。

### 一、先读代码，明确范围与不可破坏的边界

1. 阅读仓库根的 `README.md`、`CONTEXT.md`、《记忆模块升级设计Prompt.md》及其设计文档；精读 `stage0/memory.py`、`stage0/agent.py`、`stage0/rag.py`、`stage0/ddi_engine.py`、`stage0/app.py`、`stage0/response_safety.py`、`stage0/memory_context.py`、`stage0/memory_search.py`；浏览 `stage0/eval_memory.py`、`stage0/test_stage5.py`、`stage0/test_stage6.py` 和 `stage0/REPORT.md` 的评估章节。
2. 检查 Git 工作区已有改动。所有现状判断给出文件、函数及可核对行号；区分「已经实现」「代码阅读发现的不足」「推荐设计」。行号可能随修改漂移，执行时须重新核对。
3. 不可破坏的边界（违反任何一条即方案无效）：
   - 默认离线、确定性路径必须保留；LLM 一切能力维持显式 opt-in（`--llm`、`--llm-planner`、环境开关）。
   - `PlannerPolicyGuard` 的物化原则（安全关键参数由代码从真实观察注水）与 `SafetyBoundary` 最终闸门地位不变。
   - 不诊断、不处方、不建议自行停药改量；severe/低置信/冲突必须升级。
   - 双时间语义、版本链、冲突保留、审计链是既有资产，只允许加严不允许放松。
   - `memory.db` 与 `stage0/data/` 下所有评估工件不被覆盖；新评估写新路径。
4. 单人开发、分阶段实施。优先 SQLite + 现有 Python 栈；任何新依赖（FastAPI 除外属于本方案预期）须给出必要性证据。不引入向量数据库、图数据库、消息中间件、agent 框架。

### 二、四条主线的统一原则

1. **LLM proposes, code disposes** 延伸到所有新组件：Verifier 只能否决不能增内容；巩固任务由规则触发而非模型自评；outbox 工人由代码消费。
2. **每个亮点配一个指标**：延续仓库现有文化（regression replay vs held-out 分离、消融开关、不引用未测量数字）。四条主线各定义一组新指标（见各节）。
3. **失败模式先于 happy path**：每项设计回答"挂了怎么办、挂了之后用户看到什么、恢复后如何续跑"。
4. **诚实边界**：不宣称临床安全、不把单次评测当模型质量、不把逻辑删除当物理擦除、不把单进程 SQLite 当多租户方案。

### 三、主线 A：记忆模块深化（超级亮点）

叙事定位：**别人的 memory 是向量库+相似度打分；本项目是 provenance+双时间+信念修正。** 本阶段把"信念修正"从手写一跳升级为可索引、可选择、可传递的依赖失效，并补齐巩固与重查的完整闭环。

必须覆盖：

1. **依赖索引表**：设计 `conclusion_dependencies`（结论 → 依赖的 fact/medication/pair/scope revision 版本），替代 `_conclusions_citing_semantic_key_tx` 的全表扫描（memory.py:806）。要求：同一事务写入；支持按 pair 选择性失效（新增无关药物不再全量打 stale）；支持传递性失效（结论引用结论）并论证深度上限。给出与现有 `scope_revisions` 的合并或分工。
2. **重查工人健壮化**：租约过期回收（`lease_token` 带时间戳，启动时回收超时 running）；同批次重查按"当前药单哈希"去重检测（修复 `_recheck_hook` 每任务一次全量 `detect()`，agent.py:1741）；condition 类旧结论的重查路径（重跑 RAG 条件检查而非只走保守模板）。定义重查的 at-least-once 语义与去重键。
3. **AS-OF 补全**：冲突解决的时间重建（用 `resolved_at` 过滤历史视图，修复 memory.py:1501）；`retrieve_episodic(as_of=...)` 的过滤语义（现在是仅衰减排序）；给出三个可断言的双时间回归场景。
4. **巩固任务状态机**：`verification_status: recorded_as_reported → verified / disputed`，由照护者确认或 N 次一致报告触发（N 与规则显式定义），矛盾阻断晋升并开 conflict。全部动作进 audit_log。明确"巩固"不改变安全关键事实的 conflict 策略。
5. **可恢复的 Agent 状态（可选，设计预留）**：AgentState 每轮后持久化的最小方案（working_memory 或独立表）、恢复语义（从第 k 轮续跑时 observations 已含的写操作如何不重复——与事件级幂等衔接）。
6. **备份与导出**：`memory.db` 的备份/恢复/导出（JSON 交换格式）设计，含演练命令。数据是家庭用药史，说明备份频率与存放建议，但不承诺云合规。
7. **记忆指标套件**：在 eval_memory.py 的 s01–s15 基础上新增：失效覆盖率（该失效的结论都失效了）/误失效率（不该失效的没失效——选择性失效的直接验证）、重查吞吐与去重收益、AS-OF 正确率、巩固晋升正确率。定义每项的分母与判定器，并设计「全量失效 vs pair 选择性失效」的消融对比。

交付：schema 变更 SQL、依赖图 Mermaid、失效/重查/巩固三段伪代码、迁移方案（从 schema 4-p1 加列加表、保留旧 ref、不回填编造数据）、指标定义表。

### 四、主线 B：Agent 循环健壮化（第二亮点）

叙事定位：**预算化、可熔断、可验证的事件驱动 agent；安全由三层非对称闸门保证。**

必须覆盖：

1. **循环预算**：每回合 token 预算（planner payload 累计 + composer）、wall-clock 预算、cycle 预算三者取先触发的降级路径；预算消耗量进 trace 与 metrics。超预算走确定性兜底并显式告知用户结果可能不完整。
2. **payload 有界化**：修复 `prompt_payload` 的 observations/recent_trace 无压缩问题（agent.py:1334、1338）。设计压缩策略：已完成 cycle 的观察保留摘要（tool/purpose/ok/结果规模），仅最近 1–2 轮保留全量；RAG chunk 文本截断到引用所需长度。给出压缩前后 token 对比测量方法。
3. **连续拒绝熔断**：连续 N 次（建议 2–3）安全拒绝 → 记录原因 → emergency fallback 到确定性 planner，不再消耗剩余 cycle 的 LLM 调用。与现有 provider/parse 错误的 emergency 路径（agent.py:1485）统一为一条熔断策略，metrics 区分熔断原因。
4. **否决式 Verifier**：在规则 checker（response_safety.py）之前或之后插入廉价 LLM 验证层：输入=最终文本+warnings+conflicts+memory_refs，输出=结构化 verdict（fabricated/uncited/missing_escalation/prescribe_risk + 证据行号）。**约束：verdict 只能触发拒绝或标注，永远不能改写文本**；verifier 自身被拒时回退到纯规则路径。用 Stage 6 已记录的 4 个误报样本（README：良性免责声明/冲突摘要/否定式拒绝）作为 verifier 的回归集——设计目标之一是让规则层回归简洁，复杂语义判断上移。
5. **并行只读工具**：`ddi_check`/`rag_search`/`memory_read` 的并行执行方案（asyncio 或线程池），写操作维持串行。说明 SQLite 单连接下的并发读边界与 `check_same_thread=False` + RLock 的实际语义。
6. **trace 持久化**：每轮 trace 落库（新表或 episodic 扩展），跨会话可查"当时为什么这么答"；定义 trace 与 audit_log 的分工避免重复。
7. **超时分层**：planner（交互关键路径）与 composer、后台 outbox 任务使用不同超时/重试配置；给出建议值与依据。

交付：循环状态机图（含预算/熔断/降级转移）、verifier verdict schema、并行执行时序图、新增 trace 表结构、Stage 6 误报样本回归方案、熔断与预算的消融设计。

### 五、主线 C：RAG 检索质量

必须覆盖（按预期收益排序，每项先给当前基线再给目标）：

1. **语料层修复**：`CHRONIC_DRUG_TERMS`（rag.py:41）补茶碱类及 held-out 暴露的其他缺失类目 → 重建语料与索引 → 在同一 held-out 上重跑，报告 recall 变化。这是茶碱缺口的根因修复，须先于检索层调参。
2. **Cross-encoder 重排**：BGE 召回 top-50 → `bge-reranker-v2-m3` 精排 top-10；CPU 延迟实测；`--evaluate` 增加重排前后对比。说明重排不改变 exact-substring 引用门槛（ddi_engine.py:619）。
3. **查询改写**：检索前经 normalize/成分映射扩展（药名→成分+别名多查询），每 (药A, 药B, section) 一条 query，RRF 融合。与 ddi_engine 现有双向查询（ddi_engine.py:531）统一为一个查询构造层。
4. **引用二次补检**：KEGG 来源且无中文引用的 warning，触发一次定向检索（药名+相互作用 section），通过 exact-substring 门槛则补引用——直接攻 citation coverage 0.40 的已知缺口；不通过则保持现状标注（不伪造）。定义补检的预算上限与失败展示。
5. **检索基础设施**：FAISS `IDSelector` 或预分片替代全量检索+Python 过滤（rag.py:313）；查询 embedding 与 BM25 结果的 LRU 缓存；RAGTool 降级路径 memoize chunks（agent.py:163）；section 先验权重进 RRF（相互作用/禁忌/注意事项 章节加权，权重经评测确定而非拍脑袋）。
6. **评测升级**：nDCG@10 + 分 section 指标 + 现有 recall@5/MRR；扩展 query 集（目标 ≥60 条，含茶碱类专项）；重排/改写/先验各自消融。所有对比用同一冻结语料与索引版本，语料变更单独一行报告。

交付：当前基线表、每项改动的预期指标与测量方法、语料变更的数据血缘记录（延续 curate_corpus 的 summary 惯例）、失败模式（重排模型不可用→回退 RRF；查询扩展引入噪声→precision 监控）。

### 六、主线 D：服务化、两层幂等与性能

必须覆盖：

1. **FastAPI 服务层**：`/events`（CareEvent 提交）、`/memory/state|timeline|conflicts`（读）、`/alerts`、`/rechecks`（触发重查）、`/health`。agent/memory 代码不动，只换壳；Streamlit 降级为纯 API 客户端（保留现有 UI 功能）。定义 API 错误模型（区分 validation/safety/provider/internal）。
2. **请求级幂等**：`Idempotency-Key` header → `idempotency_keys` 表（UNIQUE，存 request hash + 响应 + 状态机 in_flight/committed/failed），重试返回存储响应；同键不同载荷 422。写清两层幂等分工文档：请求级管 API 语义，事件级 `event_key`+fingerprint 管领域语义。给出并发同键请求的行为（第二个等待或 409，选一个并论证）。
3. **Outbox**：LLM 副作用（结构化抽取、planner、composer、ddi live fallback）与重查任务统一进 outbox/任务表模式：业务事务内写意图 → 独立工人消费 → 幂等消费台账。至少给出抽取与重查两个改造样例；说明离线默认模式下 outbox 为空、不改变现有行为。
4. **性能工程**：
   - DDI 结果按"排序后药单集合哈希"memoize（进程内 LRU），并在重查批处理中复用（与主线 A 第 2 项衔接）；
   - app.py 每次重渲染的 O(历史) 调用（app.py:551）加缓存或改由 API 客户端增量拉取；
   - 端到端延迟分解埋点：event→consolidate→cycles→tools→respond 各段 p50/p95，结构化 JSON 日志含 trace_id；
   - 压测脚本（locust 或等价）：单照护者典型负载（读多写少）+ 并发写边界说明；结果进 README，延续"数字必须实测"的惯例。
5. **部署与 CI**：Dockerfile（含本地模型缓存卷）、docker-compose（app + 可选嵌入服务）、健康检查、优雅停机（in-flight 幂等键与 outbox 任务的状态迁移）、GitHub Actions 跑全部离线测试与 replay 评估作为回归门禁、pydantic-settings 配置校验。
6. **备份**：与主线 A 第 6 项衔接的定时备份（Litestream 或等价 WAL 流式方案）、恢复演练命令、备份完整性校验。
7. **诚实边界**：单写者 SQLite 在多 worker 部署下的约束文档；多租户 SaaS 化才需要 Postgres+行级隔离的触发条件；不宣称本方案获得 HIPAA/等保合规。

交付：API 契约（OpenCSS/JSON）、幂等键表结构与状态机、outbox 表结构与工人循环伪代码、部署拓扑图、压测方案与通过标准、CI 流水线定义。

### 七、总体评测与消融

1. 每条主线的指标独立成节，禁止跨主线混合归因（RAG 改动不进 agent 指标，除非单独标注）。
2. 统一消融开关风格（沿用 `MemoryStore(ablations=...)`）：`dependency_index`、`selective_invalidation`、`verifier`、`rerank`、`query_expansion`、`request_idempotency`。
3. 所有新指标给出：定义、分母、判定器（代码断言 vs LLM judge+rubric）、失败样例、当前值（未知则写"待测"，禁止填数）。
4. 回归保护：现有 `python -m unittest stage0.test_stage3 stage0.test_stage5 stage0.test_stage6` 与 eval_memory 场景必须全绿；被设计取代的行为（如全量失效）在消融开关下保留可对比。
5. 安全回归：主线 B 的 verifier 与熔断不得降低现有安全断言（`unsafe_actions_reaching_executor` 必须保持为 0 的场景集合不缩小）。

### 八、分阶段实施计划

按优先级给出五个 Stage，每 Stage 含：收益、文件范围、验收用例、回滚方式、单人开发者人日估算与不确定性。另给「只有 1 周」和「有 1 个月」的裁剪方案。

- **Stage 7 记忆深化**（主线 A 全部）：依赖索引+选择性失效、重查健壮化、AS-OF 补全、巩固状态机、备份导出、记忆指标套件。
- **Stage 8 Agent 健壮化**（主线 B）：预算、payload 有界、熔断、verifier、trace 持久化；并行工具可后置。
- **Stage 9 RAG**（主线 C）：语料修复先行，重排/改写/补检次之，基础设施与评测收尾。
- **Stage 10 服务化与两层幂等**（主线 D 1–3）：FastAPI+幂等键+outbox。
- **Stage 11 性能与交付**（主线 D 4–7）：memoization、埋点、压测、Docker/CI/备份演练。

说明取舍：你要求的优先级是记忆>Agent>RAG>生产化，故 Stage 7–9 如上；但 Stage 10 中的请求级幂等依赖最少、面试叙事价值高，若时间紧张可与 Stage 8 并行启动。明确哪些设计先做接口预留（如 AgentState 持久化），哪些必须真正实现才有资格写入简历。

### 九、面试材料

1. 90 秒讲法 + 3 分钟演示脚本（演示主线：改一条过敏史 → 结论选择性失效 → 重查 → 审计链完整；API 层演示幂等重放）。
2. 12 个递进追问及回答要点，必须覆盖：
   - 为什么记忆用关系型+精确键而不是向量库？何时引入向量？
   - 双时间查询解决什么 CRUD 解决不了的问题？你的 known_at 近似（created_at）在什么条件下失真？
   - 选择性失效 vs 全量失效的取舍？误失效和漏失效哪个更危险，你如何量化？
   - LLM planner 被安全拒绝后为什么要有熔断而不是无限重试？
   - Verifier 只能否决不能增内容——为什么这个不对称是安全设计？
   - 两层幂等（请求级/事件级）各防什么？给出各自的失败场景。
   - Outbox 在无消息中间件时如何保证不丢不重？
   - 重排器对中文说明书检索的收益与代价？如何证明不是过拟合评测集？
   - 茶碱 recall 缺口的定位过程（语料层 vs 检索层）说明了什么工程方法论？
   - 单写者 SQLite 的部署边界？什么信号触发迁移 Postgres？
   - 压测怎么设计才对单家庭产品有意义（不是照搬互联网负载模型）？
   - 哪些指标你拒绝给出数字？为什么诚实边界本身是工程能力？
3. 简历表述区分「已实现（含 Stage 0–6）」与「完成该 Stage 后才可写」，所有数字引用本项目实测工件路径。

### 最终交付格式

写入中文 Markdown 设计文档，依次包含：现状审计（对照本文件现状表逐项核实并修正）；四条主线的推荐设计与取舍；Mermaid 架构/状态机/时序图；SQL 与接口草案；关键流程伪代码（选择性失效、重查批处理、幂等键处理、outbox 工人、verifier）；评测与消融矩阵；五 Stage 实施计划；面试材料；机制来源链接。

每个主要模块回答：解决哪个用户问题、何时触发、读写什么、失败怎么办、如何验收。资料不足处先说明可合理假设的范围；确实影响方向且代码无法回答的再集中提问。

请先重新核对现状表中的行号与判断（以当前代码为准），再完成整份设计。不要把输出停留在概念清单。

---

完整 Prompt 结束。

## 设计完成后的实现指令

需要进入编码阶段时，按 Stage 逐段使用。以下是模板；执行前先把「Stage N」替换为目标阶段。

```text
请依据刚完成的生产化与亮点升级设计，只实施 Stage 7（记忆深化）。先核对当前代码与设计的差异，保留工作区已有改动与现有离线路径。在临时数据库完成依赖索引、选择性失效、租约回收、批处理重查、AS-OF 回归与巩固状态机测试，再运行 stage0.test_stage3 / test_stage5 / test_stage6 与 eval_memory 全部场景。产出变更说明、指标对比（消融开关下）、测试证据与仍存在的限制；不要清空现有 memory.db，不要把 Stage 8–11 一起展开，也不要填写未测量的收益数字。
```

```text
请依据刚完成的设计，只实施 Stage 10（FastAPI 服务层 + 请求级幂等键 + outbox 抽取/重查两个样例）。Streamlit 保持可用并改为 API 客户端；幂等重放、同键异载荷、并发同键、outbox 崩溃恢复都必须有测试。默认离线路径行为不得改变。产出 API 契约、变更说明与测试证据；不要展开 Stage 11 的部署与压测。
```
