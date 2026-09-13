# 最终集成记录（2026-09-13）

**基线**：`01d208d`（`integration/baseline-2026-09-13`，= 快照 `30f5dd4` + [CONTRACT.md](./CONTRACT.md)）
**集成分支**：`integration/parallel-delivery`
**worktree**：`D:\py\HealthAssistant.worktrees\integration`（原工作区 `D:\py\HealthAssistant` 全程未动）

四路任务各自的交付报告：[A](./A.md) ｜ [B](./B.md) ｜ [C](./C.md) ｜ [D](./D.md)。

---

## 0. 一句话

四个分支**没有一处文本冲突**，但集成阶段仍修掉了 **4 个真实缺陷**（§2.1–§2.4）——全部是
"合起来才暴露"的那一类：接口两端各自自洽，接上去却不通。前端与真实后端的浏览器闭环
12/12 通过，D 的独立验收从基线 `9 通过 / 8 失败 / 13 未测到` 变成
**`29 通过 / 2 失败 / 0 未测到`**；剩下的 2 条是**范围外的既有缺陷**（§3.1），
不是任何一方的回归，**也没有**被删掉或降级。

> 本节不把脚本规划器的成功记作真实 Agent 自主成功。除 D 的验收套件与浏览器闭环外，
> 本轮**没有调用任何真实模型**；真实模型能力未通过的部分见 §6。

---

## 1. 交付范围核对

四个 commit 的文件集合与 [OWNERSHIP.md §1](./OWNERSHIP.md) 的所有权清单**逐项一致**，
没有越界改动：

| 任务 | 分支 | commit | 文件数 | 越界 |
|---|---|---|---|---|
| A 答案可信性 | `codex/answer-grounding` | `d1a06e4` | 5 | 无 |
| B 长期跟进 | `codex/followup-runtime` | `0f0d512` | 6 | 无 |
| C 安全体验前端 | `codex/safety-experience` | `906a845` | 9 | 无 |
| D 独立验收 | `codex/independent-acceptance` | `3b36d62` | 2 | 无 |

- **凭据 / 患者数据 / 生产库 / 构建产物**：四个 commit 的路径清单里没有任何
  `.env` / `*.db` / `node_modules` / `dist/` / `output/` / `__pycache__` 条目；
  逐行扫描 diff 没有命中凭据样式（唯一命中 `token` 的是 `follow_up_runs.lease_token`，
  是内部租约标识，不是凭据）。
- 「每个文件只有一个所有者」被遵守，因此**没有**需要按 §7 拒绝的越权改动。

---

## 2. 集成阶段修掉的缺陷

### 2.1 【B／严重】调度把 `confirmed` 当成可执行的前置条件

`followup_runtime.scan_follow_ups` 与消费循环都写了 `if not follow_up.get('confirmed'):
continue`。这与契约冲突：

- §4.5：「已安排」与「已确认」是**两件事**；
- §4.7：`schedule_state` 必须能前进，**不得**"永远停在 scheduled 来伪装成功"。

后果是**长期跟进对整个主路径失效**——处置端点（`accepted_monitoring`）是本轮之前
**唯一**的写入路径，它产出的 `confirmed` 恒为 `false`，于是每一条经由它建立的安排
到期后永远停在 `scheduled`。

B 自己的用例没有覆盖到：**每一个触发用例都先调 `confirmed_arrangement(...)`**（先确认再扫），
所以那道闸门从未被未确认的安排走到过。修法是把 `confirmed` 从两处闸门里去掉
（也把 B 的 `test_an_unconfirmed_arrangement_never_triggers` 改成断言它原本要守的东西：
**没有可触发的东西就不开工**，即 `kind='arrangement'` 停在 `unscheduled`）。
新增回归用例 `test_an_arrangement_that_is_not_confirmed_still_runs`。

> B 当时的关切（"没确认就不该有人在后台替用户开工"）没有被丢掉：未到期不执行、取消即
> 不再执行、备忘永不执行，三条都还在且都有用例。

### 2.2 【A↔B】依赖失效的交接指向一个不存在的函数

B 的 `recheck_answer_dependencies` 用 `getattr(answer_grounding, 'recheck_dependencies')`
去取 A 的判定接口，而 A 从未提供这个函数——所以每次触发都返回 `unavailable`，
**这条路径是死代码**，而且 `follow_up_runs.result_json.answer_recheck` 里记录的
只是"没做"。

A 交付的是三个原语（`versions_from_snapshot` / `dependency_state` / `revalidate`），
没有协调器。集成时把协调器实现在 **B 的 runtime 里**（而不是塞进 `answer_grounding`）：
后者在文件头声明"不认识数据库、真实记录一律由调用方注入"，让它去认 `product` 和
`care_task` 会破坏那条边界。协调器只做"找到答案 → 问 A → 搬回去"，判定规则全部来自 A。

两处只有真跑起来才看得见的语义：

- 本仓库改剂量是**取代**（`memory:medication:1@v1` 直接不在当前用药集合里，新记录换了
  id），所以"用药类依赖查不到版本"= 来源已不可用 → 走 A 的 `withdraw`（`unsupported`），
  不是"没变"。**只对真能查的那一类下判断**，其余（如 `memory:conclusion:`）如实记进
  `unchecked`，不猜。
- 降级只写进调查是不够的：`answered_parts` 是**事项上的副本**，不重投影前端就看不到。
  协调器把结果一并搬到那份投影上。

新增用例：`test_only_the_answer_whose_dependency_changed_is_downgraded`（含"只有受影响
的那一条被降级"）、`test_a_change_no_answer_depends_on_is_left_alone`。

### 2.3 【A】用户回答不写 `assessment`

`care_tasks._sync_answers_to_investigation` 直接往 `question['answers']` 追加 7 键元素，
没有 `assessment`。按 §3.4 读出来就是"未核实"——对一条**有真实提交记录**的用户报告
是丢信息（§3.5 规定 `user_reported` 记 `candidate`）。A 的 §6.2 提过这一条，
但那条路径在 B 的文件里，A 没有改。集成时按 A 给的补丁落地。

### 2.4 【A→B→C／严重】模型核对通过的答案**到不了界面**

`care_tasks` 只给 `status == 'open'` 的问题登记 `required_inputs`；而消费方读答案的
**唯一**入口 `answered_inputs` 来自 `required_inputs` 里 `status='answered'` 的那些。
于是一条**模型自己在调查里核对通过**的答案不产生任何请求，案件视图里既不在
`required_inputs` 也不在 `answered_inputs`——C 的「已经查清的部分」永远看不到它。

后果是 A 的核心交付在界面上**只剩下用户回答那一种**，而用户回答按定义只能是
`candidate`：C 认真做的 5 个视觉状态里，`verified` / `stale` / `unsupported` 三个
**无人可见**。

修法是在运行收尾处补一条**投影**（`SafetyCaseStore.project_answered_questions`），
把已答问题登记成 `status='answered'` 的请求，随后既有的 `_sync_questions_to_case`
把 `answered_parts`（含 `assessment`）填上。两个坑：

- **不能走 `require_input`**：那条路径会记一条 `input_requested` 历史（"请求您补充
  信息"）。对一条从没问过用户的问题，那是一条**假记录**——所以单开一个投影方法。
- **必须写 `answered_against`**：既有的 `retire_stale_answers` 按它判断"这条回答是不是
  针对旧版本给的"。不写就会被读成版本对不上而**立刻重开**，一条刚核对通过的答案退回
  "等您补充"。

### 2.5 D 的验收里几处"钉子钉在了旧行为上"

**这不是 D 的缺陷**——D 是按**冻结契约**写的，而契约与 A 的实际交付在两处不一致，
D 无从知道。集成时按实际语义重新对准（断言**没有变弱**，多数更强）：

| 用例 | 原断言 | 现在断言 | 为什么 |
|---|---|---|---|
| `CitationTests` | 引文路径会产出带 `quote` 的答案元素 | 引文确实回读过；该提交在**支持关系**那一段被拒（`quote_does_not_state_value`）、不留答案元素、不产生任何 `verified` | A 的设计是"引文属实但支持关系不成立 → 拒绝"，比"记下来但不置 verified"更强 |
| `test_a_professional_opinion_is_never_verified` | 专业意见的答案元素不是 `verified` | 提交在**提案校验**就被拒（`source` 不在模型可声明的集合里），连工具都没执行 | 本项目未连接真实医护服务，`professional` 不是模型能声明的种类 |
| `test_the_status_is_not_a_single_constant` | 用户回答场景里出现 >1 种 status | **同一件事项、同一次调查**里造成两种情形：对当前权威记录逐项核对（`verified`）+ 开放字段的引文答案（`candidate`） | 原场景只能产出 `candidate`；改成两种情形才有反虚假通过的意义 |
| `test_cancelling_stops_the_arrangement_from_advancing_again` | 到期安排先 `pump` 再取消 | 到期安排**在首次扫描之前**取消，再扫描 | §2.1 修好之后，已触发过的安排本就不可取消（409），原顺序验不到"取消让旧安排失效" |
| `test_a_failed_follow_up_run_leaves_the_case_unresolved` | 失败后 `resolution_basis is None` | 失败后依据**原样不变**；若安排确已触发，则要求关联执行任务状态为 `failed` | `resolution_basis` 在进入持续跟进时就被合法登记（`monitoring_arrangement`，记的是"凭什么说风险还在"），不是关闭依据；把它的存在当成缺陷是误读 |
| `CitationTests._provider` | 不带 `source_ref` 调 `answer_question` | 带上真实回读过的证据 id | A 把 `source_ref` 收成必填（服务端据此解析真实来源）。不给的话验的是"缺参数会不会被拒"，是另一回事 |

另外，`test_question_contract` 的两条（A 的 §6.1）与 `test_safety_cases` /
`test_safety_mainline_e2e` 的两条（B 的 §6.1）断言的正是本轮**明文要求修掉的缺陷**，
按两位给出的补丁改成了目标语义并保留其意图。

### 2.6 文档与实现的偏差（已回填契约）

- §3.2 示例写 `"source_ref": "evidence:8842"`，而服务端作用域校验只认
  `memory:` / `ev-` / `safety-case:` / `material:` 与材料条目的规范形状
  `<case_id>/<item_id>`——按字面产出会被服务端自己的解析器拒绝。示例已改成 `ev-<id>`。
- §1.4 示例写 `@1` / `@2`，**真实形状是 `@v<版本号>`**（`memory:medication:45@v2`）：
  `answer_grounding.parse_versioned_ref` 只认后者，按 §1.4 字面写 `dependency_refs`
  会解析不出任何依赖。调度侧的条件匹配按 `@` 切头比较，两种写法都能匹配。

`docs/frontend/api-contract.md` 补上了三个跟进端点、`assessment` 与 `follow_up` 的
新增字段（该文件原本就过时，只记 45 个路由而实际 73 个；本次只补不删）。

---

## 3. 集成阶段**没有**做、且理由明确的事

### 3.1 `assess_claim` 的"实体名重合即已有依据"（D 的 O-1 / O-2）

两条验收仍为**失败**，**没有**被删掉、跳过或降级成"未测到"：

- `test_claim_support_is_not_inferred_from_a_shared_entity_name`
- `test_a_question_read_as_available_can_name_its_answer`

根因：`investigation._sync_question_from_claim` 在 claim 被词法筛（`evidence_quality.assess_claim`）
判成 `supported_by_span` 时，把**问题**直接置成 `information_state='available'`，
**绕过了 A 的判定链**，也不产生任何答案元素。于是一条只与问题共享实体名、内容讲的
是别的事的材料，能把"合成药乙目前的服用频次是什么？"读成"已查清"，而界面指不出
查清它的是哪条答案——与 §4.5 的 `confirmed=true` 却没有确认记录是同一类缺陷
（状态宣称完成了，却没有任何东西支撑它）。

**为什么不在这轮修**：

1. 它是**基线缺陷**，不是任何一方的交付（D 也单独成类标注"范围外"）；契约 §3 只覆盖
   `answers` / `answered_parts` 这条路径。
2. 唯一**有原则**的修法是让"问题是否已有依据"只由 A 的判定链产生——即拿掉 claim 路径
   置 `available` 的能力。那是安全主线核心机制的设计变更，不是合并。
   另一条路是给 claim 路径补一套判定——那正是任务书禁止的"保留两套来源判定"。
3. 我尝试测量这次改动的爆炸半径（临时禁用该分支、跑主线套件）时被权限层拦下，
   理由是该改动会削弱一个证据支持闸门。**我没有绕过它**，因此这次评测的爆炸半径
   **未测量**。在这种状态下改主线核心是不负责任的。

**要修的话需要**：一轮独立任务，先量化"关掉 claim→available 之后主线哪些用例变红"，
再决定是给 claim 路径接上 `assessment`，还是把问题可用性统一收敛到 A 的判定链。

### 3.2 其他已知边界（沿用各任务的报告，集成未改变）

- `professional` 只有"拒绝"这一条路（本项目没有连接真实医护服务）。
- 材料只做到"结构化字段可核对"，自由文本一律 `candidate`。
- `retire_stale_answers`（记录变化 → 重开请求）与 §2.2 的协调器是两个入口：前者按
  `answered_against` 重开**请求**，后者让答案的 `assessment` 降到 `stale`/`unsupported`。
  契约 §3.6 说"重开时 assessment 应变为 stale"，**只在 on_event 触发那条路上接上了**；
  例行同步重开那条路没有接 —— 行为上不会谎报（重开后请求是 open，界面不再显示为
  已答），但契约那句话没有被完整兑现。

---

## 4. 验证结果

### 4.1 全量离线检查

`scripts/verify-agent-closeout.py --out output/parallel/integration/final/closeout`（本项目无 pytest）

**45 项：44 项通过，1 项未通过**——未通过的那一项是 `test_parallel_product_acceptance`
（31 个用例，2 处失败），即 §3.1 的 O-1 / O-2。

> **项目默认的离线闸门因此是红的，而且是**故意**留红的。**
> 把它们从 `CATEGORY_OF` 里移出就能让闸门变绿（D 在 [D.md](./D.md) §5 里给了这条路），
> 但那等于把一个已知的产品缺陷从默认门禁里抹掉——「未测到不折算成通过」是这套验收
> 自己的规矩。**要不要豁免是产品决定，不是集成阶段顺手做的事**；这里如实留红。
> 恢复绿色的两条路：(a) 修 §3.1；(b) 显式豁免并写明理由。

产物在 `output/parallel/integration/final/closeout/`（被 `.gitignore` 覆盖，不入提交）。

### 4.2 D 的独立验收

基线 `9 / 8 / 13`（通过 / 失败 / 未测到）→ 集成后 **`29 通过 / 2 失败 / 0 未测到`**
（31 项 = D 原有 30 项 + §4.5 新增的贯穿闭环）。未测到的 0 条说明四项交付
**都真的落地了**；2 条失败即 §3.1，**没有**被删掉或降级。

报告：`output/parallel/integration/final/acceptance.json`。

### 4.3 前端

```
npx tsc -b --noEmit   → 0 error
npx vite build        → 成功，单个 JS chunk
```

生产产物里 fixture 标记串**全部缺席**（`fixture-case-0001` / `合成药甲` /
`fixtureSchedule` / `fixture-trace` / `devFixture` / `fixture 只提供` 逐个 grep = 0），
动态 import 的 chunk 被整体删除。

### 4.4 真实浏览器闭环

**共享脚本**（`scripts/safety-mainline-browser-acceptance.js`，**未修改**）指向本
worktree 的**真实集成后端** + 集成前端：

```
node scripts/safety-mainline-browser-acceptance.js \
  --api-port 8100 --ui-port 5200 \
  --out output/parallel/integration/final/browser
→ status pass（12/12 步通过）
```

这一轮跑的是**集成后的真实后端**（`stage0.safety_browser_fixture`，`MEMORY_ENABLE_LLM=0`）
＋**集成后的前端**，所以它同时证明了「C 调的三个跟进接口在 B 的代码里真实存在」这件事的
下游形态：界面在真实服务上没有回归。

### 4.5 贯穿闭环（集成阶段新增的回归网）

`test_parallel_product_acceptance.IntegrationClosureTests` 把整条链**串起来**跑一遍：
相关变化 → 必要安全检查 → 建立事项 → 取得信息 → **A 的 assessment 经过 B 的投影
原样到达消费方** → 安排（未确认）→ 确认 → 到期触发 → **同一事项**增量恢复
（含"早先那批带 assessment 的答案还在"）。

各环节各自的验收在别的类别里；这一条只回答"它们接得上吗"。

---

## 5. 旧数据与运行前提

### 5.1 实测（`output/parallel/integration/legacy_probe.py`，`output/` 不入提交）

一个"升级前"的库（本轮新增的调度表被删掉、`follow_up` 是旧的 7 键形状、答案没有
`assessment`）用当前代码打开：

| 观察项 | 结果 |
|---|---|
| `follow_up_runs` 表 | **自动重建**（additive，无需停机窗口） |
| 存量 `follow_up.confirmed = true` | 读作 **`false`**（契约 §4.5 要求的可见回退） |
| 存量 `at` | 原样保留 |
| 存量答案的 `assessment` | **不存在**，且**没有被补默认值** → 界面按"未核实"显示 |
| 存量答案本体 | 原样可读（值、`provenance` 都在） |

### 5.2 一处**没有**自动恢复的边界：存量安排不会自己跑起来

存量 `follow_up` 里**没有** `schedule_state` 键（它是本轮新增的），而
`project_follow_up` 只做一件事——把拿不出确认记录的 `confirmed=True` 读成 `false`，
**不补任何默认值**。于是：

- **调度**：`scan_follow_ups` 要求 `schedule_state ∈ {scheduled, due, blocked}`，
  取不到就跳过 → **升级前登记的安排不会被扫描**。
- **界面**：C 的分支落到"待确认的安排"，不会崩、也不会谎称它已排期。

**恢复方式是重新安排**（`POST /v1/safety-cases/{id}/follow-up`，`action: "schedule"`）——
那条路径会用当前代码写出完整的 `schedule_state`。

**为什么不在集成阶段替存量记录补一个默认 `schedule_state`**：那等于让**升级前**登记的
安排在升级后**突然开始**触发后台模型调查，而这些安排当时的语义是"给了一个时间就自动
算已确认"——正是本轮要废止的那套。要不要让它们活过来是**产品决定**，不该由一次集成
顺手做掉。这里如实记下，交给下一轮。

### 5.3 运行前提

- **后台跟进需要 worker 在跑**：跟进只在 `OutboxWorker.drain_once()` 周期里推进；
  `worker_thread=False`（测试/脚本）时必须显式调 `drain_once()`。
  **没有 worker 就没有后台执行**——对外文案不得宣称"后台已在执行"。
- 只做**应用内**跟进：不发短信、邮件或任何外部通知。无需新配置
  （可选 `FOLLOW_UP_LEASE_TTL_SECONDS`，默认 300）。
- **不宣称 exactly-once**：触发身份（`case_id` + 安排 revision + 触发实例）落 `UNIQUE`
  约束，重复扫描与重启命中同一行；用户看不到重复结果，但这是幂等业务写入 + 重放。

---

## 6. 模型与上线边界

- 本轮**没有运行真实模型、没有部署、没有发送外部通知**。验收对象是确定性系统、
  产品交互与接口衔接。
- D 的套件、§4.5 的闭环、§4.4 的浏览器验收**全部使用脚本化规划器**（声明式的
  `proposal_provider`）或合成宿主（`stage0.safety_browser_fixture`，`MEMORY_ENABLE_LLM=0`）。
  **这不能记作真实 Agent 自主成功。**
- 真实模型仍未通过的部分：见 [B.md](./B.md)（限流/延迟）、
  [docs/safety-mainline-2026-09-13/](../safety-mainline-2026-09-13/) 与各轮记忆记录。
  `assessment` 在**真实批次**里的分布（多少 `verified` / `candidate`）本轮**未测**。

---

## 7. 分支与提交

- 分支：`integration/parallel-delivery`（从 `01d208d` 起）
- 合并顺序：A → B → C → D，均 `--no-ff` 保留各自历史
- 集成提交：见本文件同批次的 `Integrate: …` 提交
