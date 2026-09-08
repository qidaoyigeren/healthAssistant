# 产品升级最小对象契约（P0 定义，P1–P6 渐进实现）

日期：2026-09-07。范围：本文件只约定对象身份、关联、scope、revision、状态与引用；不创建任何空服务。每个对象标注"状态"——已存在（复用）/ 本轮新增 / 后续阶段新增。字段仅列与跨阶段契约有关的部分，实现时可增加字段但不允许改变已定义字段的语义。

## 通用约定

- `scope_id`：患者数据隔离边界。所有患者相关对象必须携带；服务端从已认证 Principal 推导，不接受请求参数指定可访问范围。
- `revision`：权威患者事实版本号（复用现有 `patient_revision`，MemoryStore 单调递增）。对象引用事实时记录当时 revision，用于 CAS 与失效判断。
- `status`：只使用显式枚举；未完成不是错误，失败必须区分原因。
- 引用一律用稳定 id（evidence_id / fact_ref / item_id），不用位置或文本快照充当身份。

## 1. DocumentArtifact（原始材料，P2 新增）

材料文件/结构化文本入库后的不可变记录。

| 字段 | 类型/语义 |
|---|---|
| document_id | 稳定 id |
| scope_id | 患者范围 |
| kind | `structured_text` \| `csv` \|（P5 起）`pdf` \| `image` |
| content_hash | SHA-256，入库即定 |
| raw_uri / raw_ref | 服务端生成的存储引用；不是用户提供的任意路径 |
| parser_version | 解析器标识与版本 |
| imported_count | 该材料被导入为 ImportBatch 的次数（材料身份 ≠ 导入身份） |
| created_at | 入库时间 |

关联：ExtractionCandidate.source_document_id → 此 id。同一材料可多次导入，产生多个 ImportBatch。

## 2. ExtractionCandidate（抽取候选，P2 新增；P5 扩展定位）

从材料抽出的待确认字段候选。**确认前不进入权威患者事实。**

| 字段 | 类型/语义 |
|---|---|
| candidate_id | 稳定 id |
| source_document_id | → DocumentArtifact |
| import_batch_id | → ImportBatch（一次导入操作身份） |
| scope_id | 患者范围 |
| field | `drug_name` \| `dose_raw` \| `dose_unit` \| `frequency_raw` \| `date` \| `subject` … |
| raw_text | 材料中的原文 |
| locator | 真实行列/字符偏移（P2）；P5 起为 page/bbox 或 `unavailable`。**不伪造定位** |
| confidence | 解析器置信度；不是医学正确概率 |
| status | `pending` \| `confirmed` \| `rejected` \| `superseded` |

## 3. ReconciliationCase（药单差异核对项，P2 新增）

一个候选/权威药单之间的一个差异项。

| 字段 | 类型/语义 |
|---|---|
| item_id | 稳定差异项 id |
| import_batch_id | → ImportBatch |
| scope_id / subject_id | 患者范围 / 材料主体 |
| base_revision | 核对时权威快照 revision（CAS 基准） |
| kind | `new_candidate` \| `not_listed_in_material` \| `dose_diff` \| `frequency_diff` \| `possible_duplicate` \| `unconfirmed_subject/drug/date` |
| candidate_ref | → ExtractionCandidate（可空） |
| fact_ref | 权威事实引用（可空） |
| raw_values | 材料/权威两侧原文（单位与频次保留原文） |
| status | `open` \| `confirmed_as_material` \| `kept_current` \| `needs_info` \| `stale`（revision 变化后失效） \| `applied` |
| receipt_id | 应用成功后的写入回执（部分完成合法） |

规则：`not_listed_in_material` 不默认产生停用事件；每次成功提交后更新 base_revision 并重算剩余项。

## 4. CareTask（跨会话照护任务，P3 新增）

| 字段 | 类型/语义 |
|---|---|
| task_id | 稳定 id |
| scope_id / subject_id | 患者范围 / 主体 |
| goal_type | 版本化任务契约标识（如 `reconcile_med_list@1`） |
| contract_version | 契约版本；模型可提议下一步但不能改契约 |
| base_revision | 任务建立时患者 revision |
| required_outputs / missing_inputs | 产物与缺口清单（完成校验依据） |
| waiting_reason | `waiting_input` \| `waiting_review` \| null |
| result_refs | 产物引用（receipt/evidence/document） |
| due_at | 可选；到期只是待办状态，不自动执行/批准 |
| status | `ready` \| `running` \| `waiting_input` \| `waiting_review` \| `completed` \| `failed` \| `cancelled` |
| revision | 任务自身版本（乐观锁） |

规则：等待输入与专业审核分别建模；恢复按 task_id + 幂等键，不按聊天文本匹配；任务级预算跨 run 累计；completed 必须由确定性完成校验判定。

## 5. AnswerBundle（结构化结果，P1 引入最小版）

版本化字段，附加在既有响应上；正文与前端卡片由同一 bundle 派生。

| 字段 | 类型/语义 |
|---|---|
| bundle_version | 如 `answer-bundle@1` |
| claims | 结论/警告列表：{claim_id, kind: conclusion\|warning, statement, status: `current`\|`stale_pending_recheck`\|`historical`\|`insufficient_evidence`} |
| fact_refs | claim 依赖的患者事实引用（含 revision） |
| evidence_refs | 证据引用：{evidence_id, quote, locator, source_version?, integrity: `verified`\|`hash_mismatch`\|`unavailable`} |
| patient_revision | 生成时权威 revision |
| change_impact | （P1）{changed_fact_refs, affected_conclusion_ids, recheck_status, new_conclusion_refs}；统计与明细同口径 |
| unresolved_questions / open_conditions | 未决条件与缺口 |
| coverage | 已覆盖/未覆盖问题；预算耗尽不是"全面完成" |

规则：安全检查仍作用于最终交付文本；`insufficient_evidence` 不等于"没有风险"。

## 阶段归属速览

| 对象 | P0 | P1 | P2 | P3 | P4 | P5 |
|---|---|---|---|---|---|---|
| DocumentArtifact / ImportBatch | 契约 | — | 实现 | 引用 | — | 扩展定位 |
| ExtractionCandidate | 契约 | — | 实现 | 引用 | — | 扩展 page/bbox |
| ReconciliationCase | 契约 | — | 实现 | 引用 | — | — |
| CareTask | 契约 | — | — | 实现 | — | — |
| AnswerBundle | 契约 | 最小实现 | — | — | 扩展 coverage/verdicts | — |
