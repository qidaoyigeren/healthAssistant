# 完整用药变更流程（2026-09-14）

**基线**：`feat/review-visit` @ `773a112`（与 `master` 同一提交，"第二次回访"那一版）
**上一份记录**：[REPORT.md](REPORT.md) ｜ **上一轮设计**：[CHANGE-BASED-DESIGN.md](CHANGE-BASED-DESIGN.md)

本轮把"用户报告的真实用药变化"接成一条完整的链：**自然语言描述 → 待确认候选 →
必要补问 → 确认 → 正式记录 → 必要安全检查 → 相关事项更新 → 回访继续**。
覆盖新增、停用、换药、恢复、纠错五类操作。

> **前提更正。** 本轮原计划建立在"自然语言用药变化登记已完成"的版本上。
> 经核对，该轮**从未落地**：`master`/`feat/review-visit` 都停在 `773a112`，
> 全部 6 个 worktree 干净且无该模块，磁盘上没有任何相关文件。
> 因此本轮把两轮合并为一次交付。

---

## 一、事实前提（都在本仓库复现或读到）

1. **必要检查已经是"按当前药单全量重扫"**。`safety_checks._execute` 对
   `TRIGGER_MEDICATION_SET` 调 `current_findings(memory, detector)`，取的是当前全部
   active 药，不是被改动的那一对。所以"新药开始时不能只检查被替换的那两种药"
   **结构上已经成立**——本轮补的是**行为断言**，不重建检测机制。
2. **停药进不了候选通道**。`CareTasks.record_input` 明确拒绝
   `action not in (None, 'add', 'dose_change')`。
3. **恢复服用会丢链接**。`_apply_medication_change_tx` 只把 `predecessor_id` 指向
   `status='active'` 的那一行；停药后没有 active 行，于是恢复行的
   `predecessor_id=NULL`，回不到那条停用记录。
4. **`_iso(None)` 返回 now**。`remove` 不带时间时写 `end_at = now()`，且**没有
   basis 记录**——一个没有来源的停药时间会被当成实际发生时间展示。
5. **`episode_anchor` 的结果已经持久化**。`dedup_key` 里含
   `episode_anchor → incarnation_id` 的输出，而 `incarnation_id` 沿**可变**
   `status` 回溯。改变一条历史行的 status 会改变其它行的阶段归属，但**已经写进
   事项 key 的那个字符串不会跟着修正**。
6. **关闭路径的第一道闸是版本闸**。`closure_evidence` 要求关联结论的
   `input_revision` 等于当前 `revisions()`；药单一动 `scope_revision('medications')`
   就跳，旧结论一律不能批准新状态。停药后事项落到 `needs_recheck`，不是 `resolved`。

---

## 二、用药链：稳定阶段标识，不依赖可变 status

**否决**的方案：靠"把被误登记的 `stopped` 行改成 `superseded`"来让
`incarnation_id` 重新算回原阶段。理由见事实前提 5——那个算法读的是可变状态，
而它的输出**已经固化进事项身份**。改状态救不回旧 key，只会让新旧记录对不上。

**采用的方案**：给每一行一个**不可变的阶段标识**。

`medications` 新增五列（迁移沿用 `start_at_basis` 已有的那套写法）：

| 列 | 取值 | 说明 |
|---|---|---|
| `episode_id` | INTEGER | **服用阶段**身份＝该阶段第一版的行 id。写入时定死，此后**永不重算** |
| `operation` | `add`/`resume`/`dose_change`/`correction`/`legacy_unknown` | **产生这一行的那次操作**。`remove` 不产生行，所以不会覆盖它 |
| `corrects_id` | INTEGER / NULL | 这一行纠正的是哪一条记录（纠错关联） |
| `end_at_basis` | `reported`/`reported_vague`/`unknown`/`recorded_time`/`legacy_unknown` | 停药时间的来源 |
| `time_text` | TEXT / NULL | 用户**原话里的时间表达**（如"上周"），原样保留 |

`episode_id` 取**整数**是刻意的：它与历史 `incarnation_id()` 的返回值同域，所以迁移把
历史行回填成**当时**那个算法的输出之后，重算出的 `episode_anchor` 与迁移前逐字节相同
——存量事项的身份不漂移。

`start_at_basis` 的取值集合同步加入 `reported_vague`。

### 三者分开表达

* **记录版本关系** = `predecessor_id` 链（哪一版取代了哪一版）；
* **真实服用阶段** = `episode_id`（患者实际连续服用的那一段）；
* **纠错关系** = `corrects_id`（这一行在撤回哪一条记录）。

三者互相独立，谁也不从谁的 status 反推。

### 阶段指派规则

| 操作 | 新行的 `episode_id` | `predecessor_id` |
|---|---|---|
| 首次新增 | **新建**（= 该行自己的 id） | 该 key 的最后一行（若有） |
| 真实停用后恢复（`resume`） | **新建**（= 该行自己的 id） | 指定的那条 **stopped** 行 |
| 阶段内调整（`dose_change`） | **继承**前驱的 `episode_id` | 当前 active 行 |
| 误登记停用的纠正（`correction`） | **继承**被纠正行的 `episode_id` | 被纠正行 |

**恢复的前驱必须由具体记录 ID + 版本指定**（§收敛要求一），不接受"存在同名
stopped 行"这种按名称选前驱的做法。候选层保存 `record_id` + `record_version`。
若指定的前驱在确认时已经不再满足条件（不是 stopped、或不是该 key），按冲突处理。

### 纠错的语义

误登记停用的纠正：**保留被纠正行的原状**（它确实是 `stopped`，`end_at` 与
`operation='add'` 原样留着——那是历史），新增一行 `operation='correction'`、
`corrects_id=<被纠正行>`、`episode_id=<被纠正行的 episode>`、`status='active'`，
`start_at` 继承被纠正行的开始时间与 basis（患者是连续服用的，不是今天才开始）。

于是：阶段回到原来那一段（因为 `episode_id` 直接继承，不靠状态回溯）；
原错误记录**仍在链上**且带明确纠正指向；**其它任何行的阶段都不受影响**——
这正是"已有真实恢复记录不能因另一条历史纠错被悄悄合并"的结构性保证。

**已有后继真实记录时的纠正**：若被纠正的 stopped 行之后已经存在一条真实的
resume（同 key、另一个 `episode_id`、仍 active），纠错会造出**第二条 active 行**。
这种情况**拒绝写入**，返回显式冲突，说明"这条停药记录之后已有新的服用记录，
纠正它会与之冲突"，要求用户先明确怎么处理。**不**自动作废那条真实恢复记录——
那等于用一次历史纠错抹掉一段真实用药史。

### 历史可查性

`remove` 是**原地更新**（`status→stopped`、写 `end_at`），不新增行、不改
`operation`、不动 `start_at`。所以：

* "服药开始" = 该行的 `start_at` + `start_at_basis` + `operation`（`add`/`resume`）；
* "后来停止" = 该行的 `status='stopped'` + `end_at` + `end_at_basis`。

两者都能单独查到，`remove` 覆盖不掉开始事件。停用同时写一条
`medication_remove` 的 episodic 事件（既有机制），这是第二处可查点。

### 向后兼容

迁移时按**当时**的 `incarnation_id()` 结果回填 `episode_id = ep:legacy:<n>`，
`operation='legacy_unknown'`，`end_at_basis` 按"有 `end_at` 但无来源"记为
`legacy_unknown`——**不因为旧值存在就回填成 `reported`**。

`episode_anchor` 改为读 `episode_id`。对历史行，回填值就是旧算法的输出，所以
**同一件事项重算出的 `dedup_key` 与迁移前逐字节相同**——存量事项身份保持不变。
这一条要写成断言（迁移前后各算一次 anchor 比对），而不是只测
`incarnation_id` 的返回值。

---

## 三、候选层：操作语义 + 明确引用

候选从 `{name, field, before, after}` 升级为：

```
{ id, visit_id, case_id,
  operation: 'add'|'stop'|'dose_change'|'resume'|'correction'|'missed_dose',
  target: { name, ingredients?,                       # 只用于解析与展示
            matched_by: 'current'|'stopped'|'alias'|'unmatched',
            record_id, record_version, record_ref,    # 权威身份
            scope_id, episode_id },                   # 归属与阶段
  changes: {dose?, schedule?, route?, start_at?, end_at?},
  before: {...}, before_ref: 'memory:medication:<id>@v<n>',
  occurred: {text, value|null, precision, basis, tz},
  group: {id, role: 'replace_from'|'replace_to'} | null,
  reported_overlap: bool|null,
  origin: {kind: 'note'|'manual', note_id?, interpretation_revision?},
  status, conflict, applied }
```

**名称、alias、`matched_by` 只用于解析与展示，不作最终写入身份。** 写入身份是
`record_id` + `record_version` + `scope_id`。

### 确认时的核对（全部在**同一个写入事务内**）

1. **对象归属**：`scope_id` 必须等于当前作用域；
2. **对象状态**：`record_id` 那一行必须仍存在，且满足该操作的前提
   （stop/dose_change 要求 `active`；resume/correction 要求 `stopped`）；
3. **记录版本**：`record_ref` 与当前行比对。
   **不只比字段值**——A→B→A 之后值相同但版本已变，旧候选仍必须判为过期；
4. **组一致性**：同组另一条若已处置，不得把这一条写成"换药完成"。

任一条不符 → 冲突（§八）。`before` 一律由服务端从权威记录派生。

---

## 四、时间：精度与来源分开

* **登记时间**（`created_at`）与**实际发生时间**（`start_at`/`end_at` + basis）
  分开表达，界面上也不合并。
* `occurred_at` 缺省时**不得**写 now 再显示为实际发生时间。缺省即
  `basis='recorded_time'`，界面读作"系统登记时间，不是患者报告的时间"。
* **"上周"不是时间戳**。`precision ∈ {exact, day, week, month, vague, unknown}`；
  `reported_vague` 时 `value=NULL`，**不任意挑一天**；`time_text` 保留原话，接收
  时区一并留档。
* **纠错时间 ≠ 实际开始或停止时间**。纠正产生的新行，其 `start_at` 继承被纠正
  行的开始时间，不写"今天"。
* 历史时间来源判不出来时用 `legacy_unknown`，**不因为旧值存在就回填 `reported`**。

---

## 五、意图按操作分别表达

解释器输出一段整段摘要 + **每个操作各自的**发生状态：

```
{ summary, operations: [ { operation, when: 'occurred'|'planned'|'question'|'correction'|'unclear',
                           target{name, quote}, changes{...},
                           time{text, precision, basis}, quote, uncertain[], group_role } ],
  unsupported: [...], question: {...} }
```

"旧药已经停了，新药打算明天开始" → **两条操作**：`stop/when=occurred` 与
`add/when=planned`。不用一个全局 intent 把其中一半丢掉。

| 用户的话 | `when` | 可确认候选 | 正式药单 |
|---|---|---|---|
| "上周已经停了药甲" | `occurred` | 生成 stop | 确认后才动 |
| "打算停药甲" | `planned` | **不进入可确认列表**，落成计划 | 不动 |
| "药甲要不要停" | `question` | 不生成 | 不动 |
| "不是停药，只是昨天漏了一次" | `occurred`，`operation='missed_dose'` | 记成用户报告，**不写药单** | 不动 |
| "之前登记停用是填错了" | `correction` | 生成 correction | 确认后才动 |

* **漏服是可记录的用户报告**，不硬塞进 `unclear`；但它**不停止整段服用记录**，
  不产生任何用药写入。
* **计划不进入可执行的变更确认列表**。用户事后报告"已经开始了" → 关联原计划
  （`plan_ref`）并生成**新的实际操作候选**，重新核对版本与时间。

动作只能由 `when=occurred|correction` 且对象唯一解析成功时产生。**关键词出现
不构成动作依据**。

---

## 六、组确认与幂等

### 组

* 单条确认 → 部分登记，合法；
* **明确确认整组 → 一次事务原子写入**（`ProductStore.command` 本身就是一个事务，
  整组在一次提交里落盘，失败整笔回滚，不留没有说明的半组）。

**组状态由各成员派生，不是数两条是否 confirmed**：每个成员有自己的
`when`（发生过/计划中）、`status`（待确认/已确认/已放弃/被取代）与
`applied`/`conflict`。渲染逐条陈述，例如：

* 旧药已停、新药仍在计划 → **"旧药停用已记录，新药开始尚未确认发生"**；
  **不**写成"换药只登记了一半"（那句话只适用于两条都已进入确认流程的情形）。

### 幂等

| 情形 | 行为 |
|---|---|
| 同一个 key、同一请求重试 | **返回原有成功回执**（网络超时后的成功重放是正常业务路径） |
| 同一个 key、不同内容 | 409 |
| 不同请求使用**过期候选版本** | 明确冲突 |
| 已成功确认的候选再确认 | 不重复写入（同一 key 走回执；不同 key 走冲突） |

**不是所有第二次确认都是 409。**

**候选创建的幂等身份**包含：来源（note id + 解释版本，或调用方 key）、操作、
目标（`record_ref` 或归一化名称）、变更内容与时间表达的摘要。这样才分得开
"撤销后重新声明"、"同值不同版本"、"重新解释"三种情况；仅追加 `before` 值不够。

---

## 七、受控写入：不扩大通用补充入口的权限

**不**只放宽 `record_input` 的 action 白名单。`record_input` 继续拒绝 `remove`：
普通回答请求不能靠加一个 `action='remove'` 绕过候选确认。

统一底层写入原语：`CareTasks.apply_confirmed_medication_candidates(task, key,
revision, candidates)`——**唯一**接受"已确认候选"的入口。它按 §三 在同一事务内
核对候选对象、记录版本、状态与作用域，再委托既有的
`MemoryStore._apply_medication_change_tx`。请求体里的 `confirmed=true` 一律不读。

`_apply_medication_change_tx` 增加可选 `expect={record_id, record_version, status,
scope_id}`：给了就强制核对，不给按旧行为（既有的 caregiver-input / 材料核对路径
不受影响）。未知 action 继续拒绝。

需要负向验证：普通回答请求带 `action='remove'` 必须被拒；候选未经确认
（`pending`）直接走确认入口必须被拒。

---

## 八、冲突持久化与事务边界

"事务里写 `candidate.conflict` 然后 raise 409"会被 `ProductStore.transaction`
一起回滚。因此拆成**两个事务**：

1. **预检**（自己的事务，幂等键 `{candidate_id}:precheck:{candidate_revision}`）：
   读权威记录核对 §三 四件事；不符就把冲突写进候选并**提交**，返回冲突；
2. 预检通过 → **写入事务**：药物写入 + 必要检查登记 + 候选落 `confirmed` +
   成功回执，**原子提交**；
3. 写入事务内若仍失败（并发/`unresolved`），整笔回滚 → **重新跑一次预检**把冲突
   落盘 → 返回 409。**HTTP 409 不会让刚保存的冲突说明消失**；
4. 写冲突记录时同样核对候选版本（键里带 `candidate_revision`），
   不覆盖更新过的候选。

`_apply_medication_change_tx` 在没有匹配的 active 行时返回 `unresolved` 且
**不报错**。确认路径必须把它转成**未应用结果或冲突**，绝不能把候选显示成成功。

---

## 九、停药后的关闭路径

"不修改 `disposition`"证明不了"停药不会被当作风险消失"，所以沿完整路径核实：

* `closure_evidence` 第一道闸是版本闸：药单一动，旧结论的 `input_revision`
  就对不上，**旧结论无法批准新记录**；
* 第二道闸读 `evaluate_trigger`，它按 `conclusion_dependencies` 判定，只回答
  "**本事项的触发条件**"，不回答"整体用药是否安全"；
* 因此要在**表达上**分开：`trigger_state = trigger_eliminated`（当前药单里
  已经没有这个组合）与"整体风险已解除"是两件事。`closure_evidence` 增加
  `nature` 字段显式区分，界面不把前者渲染成后者；
* **未知停药时间不参与任何时间推断**——不新增药物作用持续时间或临床阈值；
* 未决问题（`open` / `unknown`）继续阻止关闭。

本轮**不改**关闭条件本身（仍只有确定性检查完成与专业复核两种依据能关闭）。

---

## 十、历史视图

`/v1/medication-records/{id}` 已有全版本链。增加：按 `episode_id` 分**阶段**；
每条标 `operation`；发生时间与登记时间分开（`start_at_basis`/`end_at_basis`/
`created_at`/`time_text`）；换药两条互相可跳转；纠错行指向被纠正行，被纠正行
指向纠正行。前端只改 `MedicationsPage` 详情与 `ReviewVisitPage`，
**不新增全站页面、不建时间线系统**。

---

## 十一、入口

* `POST /v1/safety-cases/{id}/visits/{visit_id}/notes` — 「补充情况」，一段自由文本，
  一次有预算上限的解释调用；`GET .../notes`；`POST .../notes/{id}/retry`（显式）。
* 候选：沿用既有 `.../candidates`、`.../candidates/{id}/confirm|dismiss`；
  新增组确认 `.../candidates/{id}/confirm-group`（按 `group.id` 整组原子写入）。
* 前端只改 `ReviewVisitPage.tsx`（「补充情况」入口 + 理解卡片）与
  `MedicationsPage.tsx`（阶段与纠错历史）。

---

## 十二、验证

**离线**（脚本化解释器 + 真实 HTTP + 真实 worker + 隔离库，从真实候选与确认入口
进入，不直接写库制造通过）：

1. 新增后进入当前记录并触发必要检查；
2. 停用前后记录与历史正确（含 `end_at_basis`、`operation` 不被覆盖）；
3. 恢复服用形成**新阶段**且链接到指定的那条 stopped 行（具体 ID + 版本）；
4. 纠错**不冒充新的实际用药事件**、回到原阶段、原错误记录仍可追溯；
5. **已有后继真实记录时纠正旧记录** → 显式冲突、零写入；
6. 换药部分完成不被展示成"全部完成"，且旧停/新计划时措辞正确；
7. 漏服、计划、询问三条对照：都不改正式药单；
8. 重复确认不重复写入；同 key 重放返回原回执；过期版本冲突；
9. 版本冲突不覆盖新记录（含 A→B→A 同值不同版本）；
10. 变更后事项与回访继续，未决风险不被自动清除（含 `closure_evidence` 的
    `nature` 区分）；
11. **迁移不变式**：历史行回填 `episode_id` 后，重算的 `dedup_key` 与迁移前
    逐字节相同；
12. **负向**：普通回答请求带 `action='remove'` 被拒；`pending` 候选直接确认被拒。

**浏览器**（扩 `scripts/review-visit-browser-acceptance.js`，fixture 注入脚本化
解释器）：带歧义的换药登记——自然描述 → 补问 → 查看候选 → 修正 → 确认 →
查看当前记录、历史与安全检查状态。

**回归**：`python scripts/verify-agent-closeout.py --out output/<新目录>` 全绿。

---

## 十三、有限真实验收

**一次连贯场景**，不是四段加重试：**"旧药已经停了，新药还没开始"并带一次对象纠正**。
开跑前打印上限（调用数 / token / 时间），不追加批次、不换模型、不扩预算。
用量读既有台账；读不到记 `unknown`，不记 0。

**语义歧义应当补问，不靠重复调用猜中**；模型正确识别歧义并提问**也算有效结果**。

四个结论**分开报告**，一项通过不写成整个流程完成：

1. 自然语言理解；
2. 候选确认与原子写入；
3. 用药阶段与纠错历史；
4. 安全事项及回访承接。

---

## 十四、不做

不给停药/换药/恢复的医疗建议；不新建第二套药单、用药历史或变更引擎；
不引入整套事件溯源框架；不改事项关闭条件与答案可信性判定；不新增全站页面；
不做药物作用持续时间或临床阈值。
