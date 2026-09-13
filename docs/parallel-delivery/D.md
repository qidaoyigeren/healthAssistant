# 任务 D — 独立验收（independent acceptance）

**分支**：`codex/independent-acceptance` ｜ **worktree**：`D:\py\HealthAssistant.worktrees\independent-acceptance`
**接口**：[CONTRACT.md](./CONTRACT.md) ｜ **所有权**：[OWNERSHIP.md §1](./OWNERSHIP.md)

---

## 目标

按**冻结接口**独立验收 A/B/C 三方交付，重点是把"看起来完成了"和"确实完成了"分开。

## 你拥有的文件

- `stage0/test_parallel_product_acceptance.py`（新增）
- `scripts/parallel-product-browser-acceptance.js`（新增，确有需要时）

其余文件一律只读冻结——**你没有修复权，只报告。**

## 验收设计的三条原则

1. **接口级而非实现级**：按 [CONTRACT.md](./CONTRACT.md) 的字段名与枚举断言，
   不 import 实现者的私有函数。
2. **反例优先**：每条契约至少有一个"违反时应当失败"的用例。
3. **区分"没做"与"做了但错"**：两者报告口径不同，不要合并成一个失败。

## 必须专门覆盖的点

### 答案可信性（[CONTRACT.md §3](./CONTRACT.md)）

- 无 `assessment` 的答案 ⇒ 消费方按**未核实**，**不得**默认 `verified`。
- `status` 四值是否真的都出现过，还是实现只会吐一个值。
- `verified` 的 `reason` 是否能指到一次真实核对动作，还是无信息量的套话。
- **`verified` 被当成"整体用药安全"** ——任何这类文案或字段语义都是缺陷。

### 长期跟进（[CONTRACT.md §4](./CONTRACT.md)）

- **只给 `at` 不给确认 ⇒ `confirmed` 必须为 `false`。**（本次最关键的反例）
- 存量老记录（`confirmed=true` 但无 `confirmed_at`/`confirmation_ref`）⇒ 读作 `false`。
- 未知 `condition.kind` ⇒ `422`，**不得**静默降级成 `arrangement`。
  同理检查：降级成"永不触发"也是缺陷。
- naive（无时区）`at` ⇒ `422`。
- `schedule_state` 是否真能前进，还是永远停在 `scheduled`。
  后者**不算实现**，要如实报告。
- `cancel` 之后历史字段是否仍在。
- 三个端点是否真的走 `key` 幂等 + `expected_revision` CAS（重放与冲突各一发）。

### 环境

- 验收全程不得触碰 `stage0/memory.db`。用 `STAGE0_DB_PATH` 指向
  `output/parallel/independent-acceptance/` 下的临时库。
- 后端端口 8104，前端端口 5204（不要占用原工作区的 8000/5173）。

## 边界

- **不运行真实模型。**全部离线。
- 验收发现的问题写进本文档，**不直接改 A/B/C 的文件**。
- 需要跨模块改动时走 [CONTRACT.md §7](./CONTRACT.md)。

## 报告要求

每个验收点给三样东西：**断言了什么**、**实际观察到的**、**判定**（通过 / 未通过 / 未测到）。
"未测到"是合法的结论，不要用"通过"去填。
