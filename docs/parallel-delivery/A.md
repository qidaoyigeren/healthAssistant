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
