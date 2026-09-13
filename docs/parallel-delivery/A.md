# 任务 A — 答案可信性（answer grounding）

**分支**：`codex/answer-grounding` ｜ **worktree**：`D:\py\HealthAssistant.worktrees\answer-grounding`
**接口**：[CONTRACT.md §3](./CONTRACT.md) ｜ **所有权**：[OWNERSHIP.md §1](./OWNERSHIP.md)

---

## 目标

让每条答案带上可核对的 `assessment`，把"这句话是从哪来的、核对到什么程度"
从模型自述变成可验证的字段。

## 你拥有的文件

- `stage0/investigation.py`
- `stage0/harness/default_tools.py`
- `stage0/harness/evidence.py`
- `stage0/answer_grounding.py`（如需新增）
- `stage0/test_answer_grounding.py`（新增）

其余文件一律只读冻结。

## 冻结接口

`question['answers']` / `answered_parts` 的**每个答案元素**增加可选键 `assessment`：

```json
{"status": "verified|candidate|stale|unsupported",
 "reason": "...", "source_ref": "...|null",
 "locator": "...|null", "dependency_refs": ["memory:medication:45@2"]}
```

逐字段定义见 [CONTRACT.md §3.2](./CONTRACT.md)。三条硬约束：

1. **`verified` 只能来自实际核对动作**（读回的原文逐字比对），不得来自模型自述。
2. **`verified` 的含义边界**：仅表示约定范围内的答案依据已核对，
   **不表示整体用药安全，不表示已完成专业医疗判断**。
3. **没有 assessment 就是未核实**——不产出默认值，不填补。

## 起点在哪

当前唯一的事实核对闸门是 `investigation.py:_source_supports`（约 `:1532-1562`）：
`evidence` 要求 `quote` 是本次**实际读回**的内容的真子串（`_read_contents`，`READ_BACK_CHARS=2000`）；
`patient_record` 要求与当前权威字段在 `_normalise_answer` 后相等。
`assessment` 应当在这套既有闸门之上产出，不要另起一套判定。

答案级已有的版本快照是 `version`（`patient_version` 拷贝），请求级是
`answered_against`（`safety_cases.py:712`）；生成 `dependency_refs` 时复用它们。

## 验收标准

- 四类情形都有实际覆盖：无依据 / 有依据未核对 / 核对通过 / 依赖版本变化后失效。
- `status='verified'` 的每条答案，其 `reason` 能指到一次真实核对动作。
- 无 assessment 的答案在投影里**保持无 assessment**。
- `stage0/test_answer_grounding.py` 离线可跑，不调用外部模型。

## 需要越界时

走 [CONTRACT.md §7](./CONTRACT.md)：在本文档追加"接口需求"一节，不要直接改别人的文件。

---
---

# 交付报告（2026-09-13）

**只改了 A 的所有权文件**：`stage0/answer_grounding.py`（新增）、
`stage0/investigation.py`、`stage0/harness/default_tools.py`、
`stage0/test_answer_grounding.py`（新增）。

---

## 1. 本轮交付的那一条

> 被标为**已有依据**的答案，必须关联到真实、适用且支持该答案的来源。

落点是一条**单向的判定链**：一次 `answer_question` 提交要依次通过

1. **来源存在**——来源种类由**真实记录解析**，模型自报的 `source` 不算数；
2. **引文匹配**——引文必须是**这个来源**、**真的回读过的那一段**里逐字存在的内容；
3. **答案支持**——那段内容必须**确实陈述了这个答案**（精确结构化字段才可机械核对）。

三关全过才写 `assessment.status = 'verified'`，也**只有** `verified` 才允许把问题
标成"已有依据"（`information_state = available`）。前两关失败 → **不记录**（没有
真实来源的答案不该留下一个看起来像答案的元素）；第三关"依据在、但支持关系无法
机械确认" → 记为 `candidate` 或 `unsupported` 留在历史里，问题继续未决。

## 2. 六处缺口各自修在哪

| 缺口 | 修法 | 位置 |
|---|---|---|
| `_source_supports` 只检查引文是否出现在**已读文本**中 | 判定拆成三段，引文在阶段 2、答案支持在阶段 3 | `answer_grounding.assess` |
| `source_ref` 可省略 | `source_ref` 变为必填（工具 schema + 采纳层两道） | `assess` / `ANSWER_QUESTION_SPEC` |
| 多份原文**拼接**匹配造成来源错配 | 回读回执按来源分开存；引文只在该来源**同一连续片段**内匹配 | `ReadLedger` |
| `user_answer` / `material` / `professional` 缺来源记录校验 | 来源种类从真实记录解析；用户回答与专业意见**不是模型能声明的种类** | `resolve_source` |
| 字段名匹配被当作答案完成 | 记录里没有该字段就不算核对通过；多对象问题按对象逐条判完成 | `_record_facts` / `_unanswered_parts` |
| `_recorded_value` 未核对当前状态、多对象与版本 | 绑定到具体对象；核状态（必须 active）与版本；不在当前集合里的记录判 `record_not_current` | `_record_facts` / `_check_record` |

### 2.1 来源种类由真实记录解析（不是模型自报）

- `patient_record` → 按 `source_ref` 解析到**当前权威用药集合**里的那条版本化记录。
- `evidence` → `source_ref` 必须是本 run 回读过的证据，且引文落在它的回执里。
- `material` → `source_ref` 必须是本 run**回读过的**材料条目（列过索引不算）。
- `user_answer` → **模型不能声明**。用户回答由提交路径
  （`/v1/safety-cases/{id}/answer` → `care_tasks._sync_answers_to_investigation`）写入，
  模型写一句"用户说过"造不出这条记录。
- `professional` → **模型不能声明**，且本项目**没有连接真实医护服务**，所以没有
  任何一条路径能把它解析成真实来源。本地模拟工作台的决定同样不行。

### 2.2 引文绑定指定来源、内容版本与**实际读过的片段**

新增 `read_windows`（回读回执正文，随调查状态持久化）：每次 `read_evidence` 记录
**模型真正收到的那一页**（按它声明的 `offset`/`limit`）。核对用的是这份回执，
**不是**校验时重新翻开原文——旧写法固定按 `offset=0, limit=2000` 重读，模型读了
后 1000 字时，回执里会多出 2000 字它没看过的内容。

- 不同来源**不拼接**：引用 A 的话配 B 的原文会被拒（`quote_not_read_back`）。
- 同一来源的**未读区间不跨越**：读过 `[0,5)` 与 `[10,15)`，引文不能跨过中间那段。
- 相接的两次回读会合并，所以分页读完仍然可以引用跨页的句子。
- **跨会话复用**：窗口随状态持久化，恢复后仍是同一份回执。滚动升级前的旧状态
  只存了 `read_refs` 名单、没有窗口，按旧口径恢复成"整篇读过"——否则旧调查会
  集体失去引用能力。

### 2.3 `verified` 的边界

`answer_grounding.VERIFIED_MEANING` 写死在实现里，供 C 直接抄：

> 约定范围内的答案依据已核对；**不表示整体用药安全，也不表示已完成专业医疗判断**。

## 3. 改了哪些文件

| 文件 | 改动 |
|---|---|
| `stage0/answer_grounding.py` | **新增**。判定内核：`ReadLedger` / `resolve_source` / `assess` / `RecordFacts`，以及给 B 的依赖接口。纯函数、不认识数据库，可独立测试 |
| `stage0/investigation.py` | 接上真实记录与回读回执：`_record_facts` / `_material_facts` / `_read_ledger` / `_record_read` / `_grounding_context`；重写 `answer_question` 的采纳判定与 `_unanswered_parts` 的多对象判完成；`_reference_is_visible` 放行材料条目的规范形状 `<case_id>/<item_id>`（它没有前缀，原来会被判成"引用不存在"）；删除 `_source_supports` / `_read_contents` / `_recorded_value` |
| `stage0/harness/default_tools.py` | `ANSWER_QUESTION_SPEC`：`source` 枚举收窄到模型可声明的三种，`source_ref` 变必填，新增可选 `object_ref`，描述写明"来源种类由服务端解析" |
| `stage0/test_answer_grounding.py` | **新增**，42 条用例 |

既有键**一个都没改**：11 个答案元素的键名与语义原样保留，`assessment` 是新增的
可选键；另有新增键 `object_ref`（记录这条答案针对哪个对象，多对象问题用它保留对应
关系）。`version`（`patient_version` 快照）与 `answered_against` 原样复用，
`dependency_refs` 只在它们之上派生出**版本化**引用。

## 4. 测试

`stage0/test_answer_grounding.py`，**42 条，全过**，离线、不调用外部模型：

```
D:/py/HealthAssistant/.venv/Scripts/python.exe -m unittest stage0.test_answer_grounding -q
Ran 42 tests in 2.2s
OK
```

每条用例都走**生产答案工具路径**：真实 `ToolExecutor` 按 `ANSWER_QUESTION_SPEC`
校验参数 → 真实 `Observation` → `InvestigationState.observe` 的唯一采纳生效点。
测试**不直接改写** `answered` / `information_state`。

验收清单逐条对应：

| 验收项 | 用例 |
|---|---|
| 引文真实但答案不受支持 | `test_a_real_quote_that_does_not_state_the_answer_is_not_verified` |
| 来源与片段错配 | `test_a_quote_from_another_source_cannot_be_used_for_this_one` |
| 伪造用户/专业来源 | `test_a_model_cannot_declare_a_user_answer` / `..._professional_opinion` |
| 材料只列过索引不算来源 | `MaterialGroundingTests`（4 条） |
| 未读片段 | `test_a_quote_that_was_never_read_back_is_refused` / `..._a_part_never_read_is_refused` |
| 旧版本或其他对象 | `test_a_superseded_record_is_not_a_current_record` / `test_a_record_of_another_object_cannot_answer_this_question` |
| 多对象部分答案 | `MultiObjectTests`（4 条，含"声明一个对象却引用另一条记录"） |
| 合法当前字段正常复用 | `test_a_current_authoritative_field_is_reused_without_re_approval` |
| 来源失效后可信性撤回 | `DependencyRetractionTests`（6 条） |

另有：`AssessmentContractTests`（§3.2 形状 / §3.4 缺失即未核实 / 含义边界文案）、
`ReadReceiptTests`（回执不拼接、不跨未读区间、能持久化、旧状态可恢复）、
`AdoptionBoundaryTests`（采纳不关闭事项、不改药物事实）。

### 4.1 基线回归

51 个测试模块逐个跑：**49 个模块全过，1 个模块（`test_question_contract`）2 条失败**，
新增模块全过。失败的两条见 §6。

`test_safety_mainline_e2e`（39 条，含用户回答的真实提交路径）**全过**——
用户报告仍然是 `user_reported`、仍然不把问题标成 `available`，这条既有口径没动。

## 5. 接口交接

### 5.1 给 B：答案依赖校验 / 失效（§7 要求的稳定接口）

全部在 `stage0/answer_grounding.py`，**纯函数、无副作用、幂等、只降不升**。
B 的 `retire_stale_answers` 重开机制是协调点；**不要**在 B 的状态同步代码里另写
一套版本比对，直接调这三个。

```python
from stage0 import answer_grounding as ag

# 1) 当前版本表（B 手边就有：权威快照）
versions = ag.versions_from_snapshot(memory.snapshot()['medications'])
#    -> {'memory:medication:45': 2, 'memory:medication:51': 1}
#    参数：snapshot['medications']（每条带版本化 ref）
#    返回：{去掉 @v 的引用前缀: 当前版本}；没有 ref 的条目被跳过

# 2) 一条答案的依赖还成立吗
state = ag.dependency_state(assessment['dependency_refs'], current_version_of=versions)
#    -> {'state': 'current'|'stale'|'unknown', 'changed': [...], 'checked': [...],
#        'unknown': [...], 'detail': '...'}
#    current_version_of 可以是 {前缀: 版本} 映射，也可以是 callable(前缀)->版本|None
#    'unknown' **不当成** current：查不到版本不等于没变

# 3) 一条旧答案现在还信不信
updated = ag.revalidate(answer.get('assessment'), source_available=<来源还在吗>,
                        current_version_of=versions, detail='重开原因')
#    -> 新的 assessment 字典
#    * 来源不可用              -> status='unsupported'（可信性撤回）
#    * 依赖版本变了            -> status='stale'
#    * 都没事 / 没有 assessment -> **原样返回**（没有 assessment 的返回 {}）
#    * 幂等：同样输入重复调用返回相等的字典
#    * **永远不会升回 verified**——升级只能由一次真实核对动作产生
```

调用位置建议：`retire_stale_answers` 重开某条问题之前，对那条问题上的每条 answer
调一次 `revalidate`，把返回值写回 `answer['assessment']`；来源是否还在由 B 用
既有的 `validate_sources` / `evidence_store.get_meta` 判定即可。

另外两个只读口，给需要展示或断言的地方用：

```python
ag.assessment_of(answer)     # -> dict | None（没有就是 None）
ag.assessment_status(answer) # -> 'verified'|'candidate'|'stale'|'unsupported' | None
#    **都不补默认值**：None 就是"未核实"，CONTRACT §3.4。消费方要显示 5 个状态。
```

### 5.2 给 C

- `assessment` 已经在后端答案元素的权威记录里（`question['answers'][i]['assessment']`，
  经 `care_tasks.py:765` 的 `answered_parts` 投影原样下发）。**前端标签不能决定可信性**，
  只负责显示。
- 5 个视觉状态：4 个 `status` + 1 个"无 assessment = 未核实"。
- `reason` 是给人读的**具体**原因（例如"引文与 ev-… 已读片段逐字一致，且陈述了 dose=5mg"），
  可以直接作 tooltip。
- 文案抄 `answer_grounding.VERIFIED_MEANING`，不要写成"安全""已确认无误"。
- 另有新增键 `object_ref`（这条答案针对哪个对象），多对象问题里同一条问题会有多条
  答案，各自带自己的 `object_ref` 与 `assessment`。
- **`assessment.source_ref` 的实际取值**：证据走 `ev-…`，权威记录走 `memory:…@vN`，
  材料走 `<case_id>/<item_id>`。**不会**出现 CONTRACT §3.2 示例里的
  `"evidence:8842"` 那种字面形式——那正是 C 报告过的契约内部不一致（示例给了一个
  服务端作用域校验不认的前缀）。所以 C 现在"只给 `ev-`/`memory:` 渲染读取入口、
  其余显示无授权入口"的取舍与 A 实际产出的取值是一致的，集成时**不需要**再为
  示例字面形式补一条渲染分支。

### 5.3 给 D

- §3.2 逐字段比对：`test_the_assessment_has_exactly_the_frozen_shape` 钉住了键集合。
- §3.4：老答案（没有 `assessment`）读出来仍然是 `None`，不补默认值——
  `ag.assessment_of` 的返回就是 `None`。
- §3.3 含义边界文案：`ag.VERIFIED_MEANING`。
- 重点复验：**任何 `candidate` / `unsupported` / `stale` 的答案都没有把问题标成
  `available`**（`AdoptionBoundaryTests` 与 `MultiObjectTests` 里有断言）。

## 6. 接口需求（CONTRACT §7，**未落地**）

### 6.1 `stage0/test_question_contract.py` 有 2 条用例钉住了本轮要修的缺陷

```
FAIL: test_a_user_report_is_recorded_with_its_own_provenance
FAIL: test_a_partial_answer_keeps_the_question_open_and_says_what_is_missing
```

两条都直接调 `inv.answer_question(..., source='user_answer', ...)`，**断言它被接受**——
也就是"模型自己声明一句用户回答就算数"。这正是本轮要修掉的东西，所以它们必然失败：
现在这种提交返回 `accepted=False, errors=['user_answer_not_model_declarable']`
（工具 schema 那一道返回 `tool_schema`）。

两条用例的**意图**都是对的，只是坐错了车：

- 用例一的意图（用户报告的 provenance 是 `user_reported`，不是权威记录）由
  `test_safety_mainline_e2e.test_path_two_...` 用**真实提交路径**覆盖着，且全过；
- 用例二的意图（部分回答保留已知部分、写清剩余缺口）在 `MultiObjectTests` 里有
  等价覆盖，且单对象的部分回答路径也没有改动。

**我没有改这个文件**（不属于 A，也不属于任何单个 Agent）。建议由最终集成 Agent
落地的最小补丁是：把这两处的 `source='user_answer'` 换成 `source='patient_record'`
并补上 `source_ref=<该对象 ref>`（`_read_question` 已经把 `_MedicationRows` 摆好了，
`field='schedule'` 需要那个替身同时回 schedule 值），断言语义不变。

### 6.2 `care_tasks.py:_sync_answers_to_investigation` 写的用户回答没有 `assessment`

真实用户回答那条路（B 的文件）直接往 `question['answers']` 追加元素，不带
`assessment`。按 §3.4 它读出来就是"未核实"——**安全，但丢信息**：它其实是一条
有真实记录（`care-task-input:<key>`）的用户报告，按 §3.5 应当记成 `candidate`。

A 侧没有这条路的钩子（它不经过 `answer_question`），所以**留作接口需求**：
建议 B 在那处追加元素时补一个

```python
'assessment': {'status': 'candidate',
               'reason': '用户报告；来源是这次提交本身，未经权威记录或材料核对',
               'source_ref': None, 'locator': None, 'dependency_refs': []}
```

（`verified` **不行**——§3.5 明确"不得仅因用户陈述就 verified"。）

## 7. 未完成项与已知边界

1. **没有跑真实模型**（本轮范围明确禁止）。以上全部是离线验证；
   `assessment` 在真实批次里的分布（多少 verified / candidate）**未测**。
2. **`professional` 只有"拒绝"这一条路**。`GroundingContext.lookup_professional`
   目前硬接成 `lambda ref: None`。将来接上真实医护服务时，改的是这一处注入——
   但 `safety_cases._professional_basis` 那套既有的复核决定校验
   （存在 / 已生效 / 属于本事项 / 版本适用 / 非模拟来源）**没有被复用**，
   接上时应当把它暴露成一个公开判定函数再注入，而不是在 A 侧另写一份。
3. **状态 / 版本是"记录声明了才核对"**。真实 `medications` 表的这两列都是
   NOT NULL，所以生产路径上一定核得到；只回值的最小替身没有列，判定会在
   `assessment.reason` 里**写明"未做该部分核对"**而不是假装核过。
   这是刻意的：可审计优先于看起来确定。
4. **材料只做到"结构化字段可核对"**，自由文本一律 `candidate`。材料条目的
   `fields` 由 A 侧在观察阶段留存（新增键 `material_items[ref]['fields']`，
   与既有的 `name`/`kind`/`current` 并存）；`read_material_item` 之外的材料读取
   路径没有覆盖。
5. **`read_windows` 会随状态一起持久化**（每条证据最多 2000 字）。`planner_view`
   里已替换成定位摘要，不进模型上下文；但调查状态的体积会因此增长。
   若集成时认为不可接受，替代方案是只存 `{ref, offset, chars, hash}` 并在核对时
   按窗重读——**不能**退回"整篇重读"，那正是本轮修掉的缺陷。
6. **没有做集成**。B/C/D 的改动一行未动，`assessment` 到前端的那一段、
   `retire_stale_answers` 的实际接线都还没有发生。
