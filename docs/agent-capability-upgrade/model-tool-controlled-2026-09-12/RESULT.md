# 模型与工具接口受控对照 · 2026-09-12

**技术选择：第 3 项。保留当前模型与 B1 默认接口，保留材料核对和有界部分报告；B2 保持显式实验开关，自主调查继续默认关闭。停止本轮采样。**

实验 A **未验证**：仅发现代码白名单、历史候选实验及配置，未找到可追溯的候选模型明确用户授权，不据此调用其他模型。实验 B 完成预登记的 16 个交错任务运行：B1/B2 都是 **0/8 取得相关原文、0/8 完整完成、0/8 合理等待且无策略降级**。两臂各有 4/8 个任务遭遇连接故障。结论是“没有获得足以采用新接口的改善证据”，**不是证明模型或接口是唯一瓶颈，也不是证明 B2 无效**。

没有修改提示词反复跑批，没有替换失败样本，没有新增 Agent、迁移框架或扩大产品范围。以下所有模型能力数字仅来自本轮真实调用；脚本验证另列。

## 1. 基线与配置冻结

- 初始 HEAD：`984c600c018236eb8f9fecb47eff32fb8426849c`，工作区有既存修改，全部保留。
- 修改前工作区快照指纹：`84e427d2739f366fd8315d8303a20efcd00dbec5576d2d523a643b336ad57745`。快照包含 113 个代码、任务与诊断文件，不复制密钥。
- [INITIAL.json](../../../output/model-tool-controlled-2026-09-12/INITIAL.json) 记录初始状态、逐文件 SHA256 和历史产物哈希；`initial-source/` 保存原文件；`historical/` 保存旧报告与所有旧 A/B/B2 结果。
- 真实取数前的 [FREEZE.json](FREEZE.json) 记录本轮完整源码指纹、任务、评分、模型与预算。`frozen-source/` 保存实际运行源码；[SUMMARY.json](SUMMARY.json) 核对取数结束时源码和所有实际请求配置均未漂移。
- 冻结的 8 个任务来自 `visitprep_dev.json`。前三个是材料缺项、冲突、首次检索无结果；后五个是此前已调试过的回归样本，**均不称独立留出集**。数据原始 `schema_version` 仍为 `visitprep-task@1`，评分实现是 `visitprep-eval@2`；原始任务完整存档，不将字段名改写成新的来源证明。

旧结果复核：8 个任务里 2 个出现被接受的 `rag_search`，0 个提出 `read_evidence`，0 个完成，受支持 claim 为 0。原始旧结果是缩减后的 planner trace，未保存完整工具观察，故“检索执行”依旧报告及接受轨迹可核对，**检索是否取得有效原文不能从接受动作补造**。尤其不能把“2/8 发起检索”写成“2/8 取得原文”。旧账本 46 次，旧 trace 38 次，差 8 次恰为 8 个被拒提案；没有给旧记录伪造新请求 ID。旧 B2 命名属于上轮修复版本，与本轮工具接口 B2 不是同一含义。

| 项目 | 两臂共同冻结值 |
|---|---|
| 当前模型 | `siliconflow / Qwen/Qwen2.5-7B-Instruct` |
| 端点 | `https://api.siliconflow.cn/v1` |
| 提示与策略 | 原 investigation system prompt 不变；temperature=0；tool_choice 原策略；模型自拟子问题 |
| 输出配置 | max_tokens=4096；thinking=disabled；实际 kwargs 逐请求保存 |
| 每任务硬上限 | 10 次请求、180 秒、150000 token；周期上限沿用任务的 10/12 |
| 总上限 | 16 次任务运行、160 次请求、2880 秒任务时间；不复跑 |
| 重试 | SDK=0；429 重试=0；原有最多一次解析重试仍消耗同一硬额度；不退款 |
| 搜索预算 | 原 `expected.search_budget` 或 3；首次无结果任务仍为 1，不能据此考察二次改写能力 |
| 其他 | LLM verifier、记忆 LLM、批量读、委派、跨运行复用关闭；相同的本地合成语料与确定性精确检索 |
| 顺序 | 同一任务两臂相邻，逐任务交替 B1→B2 / B2→B1 |
| 唯一处理变量 | `AGENT_EVIDENCE_INTERFACE` 及由接口自然生成的目录、参数和观察 |

任务预算中的历史 120 秒/32 次不是本次生效值：runner 实际下发 180 秒/10 次；每任务 `effective-manifest.json` 和 wire timeout 均保存。冻结前已明确这一点。

实验 A 的候选筛选门槛预登记为：三个代表任务至少 2/3 达到有相关原文、无策略降级的完整完成或合理等待，并且不引入安全失败，才允许进入回归集。由于授权依据缺失，筛选与回归均未执行，未将 B 的结果冒充模型对照。

实验 B 的采用门槛预登记为：无策略降级的完整完成/合理等待至少净增 2 个任务，至少含 1 个回归收益，不丢失 B1 成功项，成对证据测试成功且安全回归通过。结果未达到门槛。

## 2. 消除记账与调用契约干扰

**请求记账。** `BudgetSession` 在持久预留后暴露该次 `attempt_id`；planner 在成功、连接/超时、429、协议拒绝、预算预留后退出等路径保留关联信息；安全拒绝的提案也携带真实 attempts。代码强制终态、确定性步骤和未发生请求的熔断不领取旧 attempts。unit 测试覆盖 429→成功重试→超时的逐 ID 对齐、被拒提案及多调用保留。

本轮 [逐请求对账](request-reconciliation.json)：**59 条持久账本 = 59 条 wire 请求尝试 = 59 条 planner attempts**，逐 ID、状态、响应 token 一致。51 个成功响应中包括 **8 个被安全拒绝的提案**；另有 8 个 `APIConnectionError`，账本为 `unknown`。没有超时、429 或实际重试。不把安全拒绝误列为供应商拒绝，不把连接尝试称为服务端已执行；连接错误是否到达 provider、是否计费均未知。

**单调用契约。** 优先发送 `parallel_tool_calls=false`；[供应商当前文档](https://docs.siliconflow.cn/docs/api/chat-completions-post) 未明确承诺此字段的强制语义，实际响应也证明不能依赖它：51 个响应中 **11 个仍含多个调用**。保留原选择规则“本回合首个尚未执行的调用”，仅将选中提案交给安全校验；其他调用保存 ID、参数、未执行原因，放入 trace，并在存在下一次决策时明确反馈。不会自动执行全部调用，更不会将被安全拒绝的选中项称为已执行。终态无下一次请求时，未执行项仍在该步 trace 与原始响应中留存。

**配置证据。** `wire-*.json` 保存真正传入 SDK create 的参数、SDK retry 配置、完整响应或错误、请求/响应 ID、时间；每任务保存账本、原文 EvidenceStore、SQLite、完整工具 trace、调查状态、报告及 manifest。旧结果仅作历史背景，B1 是本轮当前代码重新运行。

## 3. B2 的最小实现与边界

新增 `stage0/harness/evidence_acquire.py`，以共享工具注册方式进入现有执行器和产品路径，默认关闭。模型明确调用 `acquire_evidence(query, top_k?, section?, drug_name?)` 后，工具：

1. 按模型给的查询、过滤条件执行一次原有 `rag_search`，最多 3 条；不改查询、不自行扩大范围。
2. 对候选逐条执行原有 `read_evidence`，每条至多 2000 字，使用原有 principal 权限、EvidenceStore scope 与内容哈希校验。
3. 返回存储原文片段及偏移/长度/来源、corpus version、取证时间、完整性、截断、无结果/失败和未完成核查项。没有发布日期就明确 `publication_date=null`，不把取证时间当发布日期；没有获取整份外部说明书就明确只取得存储片段；本地来源链与哈希通过不代表已在线确认最新版本。

`operations[]` 逐项记录工具内部的搜索、读取和来源元数据检查，归因为 `deterministic_internal`；共享调查状态沿用 B1 的取证和 claim 检查规则。工具返回 `conclusion_approved=false`，不会写入用药事实或批准结论。模型仍决定方向、子问题、查询改写、冲突处理与补问；既存完成/安全/预算护栏保持原样。

工具测试验证原文与元数据、无结果与失败区分、截断、内层权限拒绝、跨 scope、哈希篡改、参数上限和默认关闭。B1 模型可传原 top_k=5，B2 上限为 3；这是预登记接口边界的一部分。本批相关语料每任务最多两条，实际上两次搜索均为空，未发生候选截断，不以减少工具步骤作为自主性提升。

## 4. 逐任务结果

完整机器可读结果见 [per-task-results.json](per-task-results.json)。下表每格为“终止原因；请求数/耗时秒”。所有任务相关原文与材料原文读取均为 0；全部完整完成、合理等待和原文支持报告均为 0。

| 任务 | 阶段 | B1 | B2 |
|---|---|---|---|
| missing-002a | 筛选 | budget_insufficient；8 / 19.9 | 连接失败→budget_insufficient；1 / 5.2 |
| conflict-003a | 筛选 | 连接失败→budget_insufficient；1 / 5.3 | no_progress；7 / 17.4 |
| noresult-005a | 筛选 | budget_insufficient；6 / 13.0 | 连接失败→budget_insufficient；1 / 5.3 |
| full-001a | 回归 | 连接失败→budget_insufficient；1 / 5.3 | 连接失败→budget_insufficient；1 / 5.3 |
| full-001b | 回归 | 连接失败→budget_insufficient；1 / 5.3 | no_progress；6 / 12.2 |
| missing-002b | 回归 | no_progress；6 / 12.8 | 连接失败→budget_insufficient；1 / 5.3 |
| conflict-003b | 回归 | no_progress；6 / 11.4 | no_progress；5 / 14.3 |
| noresult-005b | 回归 | 连接失败→budget_insufficient；1 / 20.0 | no_progress；7 / 18.3 |

| 独立验收轴 | B1 | B2 |
|---|---:|---:|
| 原始响应里的检索/取证调用提案数（含未选中项） | 2 | 2 |
| 模型提出并选中检索/取证提案的任务 | 1/8 | 1/8 |
| 实际执行检索的任务 | 1/8 | 1/8 |
| 取得相关原文的任务 | 0/8 | 0/8 |
| 有原文支持的报告 | 0/8 | 0/8 |
| 旧 rubric 的部分报告内容/结构合格 | 2/8 | 3/8 |
| 完整完成且无策略降级 | 0/8 | 0/8 |
| 合理补问/保留冲突等待且无策略降级 | 0/8 | 0/8 |
| 停在任务允许的终止原因 | 8/8 | 8/8 |
| 相同结果签名的重复次数 | 6 | 9 |
| 请求次数 | 30 | 29 |
| 总任务耗时 | 92.93 秒 | 83.23 秒 |
| 成功响应报告的输入/输出 token | 134494 / 2080 | 131620 / 2278 |
| 用量未知的连接错误请求 | 4 | 4 |

“允许的停止原因”包含预算不足及 no_progress，**8/8 合法停止不等于 8/8 任务达成**。旧 rubric 的 `report_quality.ok` 也不等于有原文支持的报告；2→3 不能作为采用收益。费用没有账单凭证，记为未知，不能把接口名或历史免费印象换算成已证实的零成本。token 汇总只包含成功响应，未知请求未记为 0 token。

**重复计数的观察缺口。** 原有 `observe` trace 在进展检查前序列化，所以 raw result 的 `observation.no_progress` 和由其求和的 `controlled_metrics.repeated_without_new_information` 全为旧值 false/0。没有覆盖原始结果，也没有事后改任务得分。正式表使用已持久保存的 `run_progress_signatures.seen_count - 1`，并另存终止时的无新信息 streak；在 `progress-audit-derived.json` 可追溯。此项仅统计相同结果签名重复，不包含“换参数但无新增”的全部情况，不能据此宣称整体重复率改善。

连接故障两臂各四个，只剩 **conflict-003b 一组成对运行两边都未遇连接故障**。这种服务条件不足以把总耗时或 2→3 的部分报告差异归因到工具接口。没有挑掉故障样本，也没有追加一轮“直到成功”。

## 5. 根据观察调整动作与成对证据验证

真实 B2 `conflict-003b`：模型在重复 `memory_read(snapshot)` 后收到第 1/2 次无进展反馈，下一次自行改选 `acquire_evidence`，查询“确认氨氯地平的用药信息是否准确。”，同时指定 `section=用药信息`。该节名不在合成语料中，搜索返回明确空结果；未取得原文，也未改写后重试，随后按无进展护栏停止。

证据：[wire-005.json](../../../output/model-tool-controlled-2026-09-12/live/14-B2-vp-conflict-003b/wire-005.json)、[result.json](../../../output/model-tool-controlled-2026-09-12/live/14-B2-vp-conflict-003b/result.json)。同一响应提出第二个取证调用，但它没有被执行，trace 明确保留。这个案例支持“会在反馈后换动作”，**不支持调查完成或查询修正成功**。

B1 `missing-002a` 也实际搜索，但使用 `section=适应症`，而相关原文在 `用法用量`，得到空结果。这给下一项工具参数假设提供具体依据，不是纯粹凭模型尺寸猜测。

**成对任务。** `conflict-003a/b` 的目标、权威用药、材料 CSV、预算和第一条原文相同，模型可见输入只改变第二条原文的肯定/否定内容；task_id 和 expected 仅用于运行组织与评分，不进入模型输入。分析脚本逐字段断言此差异，没有按 ID 编写产品特例。

- 离线共用脚本规划器：B1/B2 都在冲突版本留下双方证据并 `waiting_review`，一致版本 `checks_completed`。B2 只合并了取证操作，未改变结论判定。原始证据见 `offline-pair-v2/`。
- 正常产品 API 路径：两接口、两个版本共 4 条完整路径，结果同上；上传、原文 hash 验证、五节报告、患者数据不被修改、取消后保留报告通过。
- **真实模型证据敏感性未验证成功**：这组成对任务真实模型均未取得原文，B1 冲突版本还遇到连接故障。不能以脚本成对成功替代模型敏感性，更不能称其为独立留出评测。

## 6. 回归与产品路径验收

[完整回归记录](../../../output/model-tool-controlled-2026-09-12/regression/engineering.json)：40 个 unittest suite、**583 项测试通过**；4 组既有评测及负向对照符合预期退出码。负向对照仍按预期失败，未放宽安全或完成门槛。

[产品 API 验收](../../../output/model-tool-controlled-2026-09-12/product/acceptance.json) 使用真实 FastAPI routes、持久任务、worker、EvidenceStore，隔离合成数据库、脚本规划器、零远程模型。流程为 `/v1/materials/csv` → `/v1/care-tasks` → resume/worker → investigation-report → `/v1/evidence/{id}` → cancel/report retained。B2 是正常共享产品执行路径上的 opt-in 工具，**并未切换默认配置**。本轮没有 UI 修改或新浏览器验收，不复用旧截图冒充当前验证。

取数前调试失败均保留说明：首次新增单测发现通用 schema validator 不执行数值上限，已在 B2 handler 加硬检查；另一次测试构造器及预算 fixture 初始化错误修正。首个离线工件导出发现 legacy runner 没有 run_manifests 表，改为保存独立 effective manifest 并明确表缺失，首次残留工件在 `offline-pair/`，正式离线结果在 `offline-pair-v2/`。这些是工程调试，不是独立样本。真实批次只运行一次。

## 7. 决策与停止条件

保留本轮必要记账修复、单调用请求和未执行项反馈。默认继续当前模型/B1；B2 仅在 `AGENT_EVIDENCE_INTERFACE=B2` 时启用。自主调查没有达到目标，不开放为已验证能力；可靠材料核对、可读原文查看和明确的部分报告继续保留。

下一项值得验证的假设：**自由填写的 `section` 过滤条件会把本可检索的证据过滤为空**。这由两次真实搜索的参数与冻结语料直接支持。值得在未来单独比较“现有字符串过滤”与“来自语料的合法节名约束/显式无效过滤反馈”，保持同一模型，不再反复改提示词。它尚未实现或验证。

未来若获准继续：先确认传输服务可用，再只做三代表任务，每任务仍最多 10 请求/180 秒；只有至少 2/3 在相关原文与任务结果上改善、没有安全退步，才进入事先冻结回归。筛选未过即停，不扩大预算、换提示或替换失败样本。没有候选模型明确授权，A 继续保持未验证。本轮不自动开展这项后续实验。

## 工件与复现

- [实验配置](FREEZE.json)、[汇总与决定](SUMMARY.json)、[逐任务结果](per-task-results.json)、[逐请求对账](request-reconciliation.json)。
- 原始请求/响应、错误、SQLite、原文、完整 trace 和 manifest：[原始工件目录](../../../output/model-tool-controlled-2026-09-12/live/)。
- `scripts/model-tool-controlled.py --freeze` / `--live` 拒绝覆盖已有批次；不可对本轮目录重复运行，未来需新实验登记。
- `scripts/analyze-model-tool-controlled.py` 从原始结果与持久数据库重建核对表，不重跑模型、不覆盖原始任务结果。
- `scripts/model-tool-product-acceptance.py` 为零远程产品路径验证，同样拒绝覆盖已有验收目录。
