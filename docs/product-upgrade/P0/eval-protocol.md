# 产品开发评测协议（P0 建立，P1–P6 扩充）

日期：2026-09-07。目录：`stage0/product_evals/`。原则：评测入口对真实失败返回非零退出码；必需数据缺失时输出 `unavailable`，绝不输出 pass；开发集与 held-out 物理隔离。

## 1. 目录结构

```text
stage0/product_evals/
  __init__.py
  run_eval.py          # 入口：python -m stage0.product_evals.run_eval --suite dev --out <json>
  tasks/
    dev/               # 开发集：允许在调试中查看、迭代（P0 首轮 12 个，扩充至 30–50）
      *.task.json
    held_out/          # 独立封存集：按隔离规则采集；无独立数据时目录只有 README，评测输出 unavailable
  README.md
```

## 2. 任务格式（`*.task.json`）

```json
{
  "task_id": "dev-p1-ev-001",
  "phase": "P1",
  "capability": "evidence_readback",
  "group": "g-evidence-api",        // 隔离分组键：按来源材料/场景模板/药物组合分组
  "title": "正常分页读取证据原文",
  "inputs": { "seed": { "events": [...] }, "call": { "evidence_id": "$ref", "offset": 0, "limit": 100 } },
  "expected": { "type": "read_page", "total_chars_gt": 0, "content_matches_hash": true },
  "requires": ["fixture_db"]        // 缺失的必需外部数据/服务 → unavailable 而非 fail
}
```

- `capability` 对齐对象契约（AnswerBundle / EvidenceStore / ReconciliationCase / CareTask）。
- `inputs.seed` 声明临时库种子事件（明确标注合成材料）；执行器在临时数据库上重建，不触碰正式患者库。
- 断言类型有限集合：`read_page`、`api_status`、`db_state`、`no_write`、`text_contains`、`bundle_shape`。每个任务只用可机器核验的断言。

## 3. 结果格式（run_eval 输出 JSON）

```json
{
  "suite": "dev", "dataset_version": "dev-1", "ran_at": "...",
  "environment": { "provider": "scripted", "note": "合成评测，无真实模型调用" },
  "summary": { "total": 12, "passed": 0, "failed": 0, "unavailable": 12 },
  "failures": [ { "task_id": "...", "category": "assertion_failed", "detail": "..." } ],
  "tasks": [ { "task_id": "...", "result": "passed|failed|unavailable", "duration_ms": 0 } ],
  "overall": "pass|fail|unavailable"   // failed>0 → fail(非零退出)；unavailable 占比 >0 → 不允许整体 pass
}
```

- 退出码：全部通过 → 0；任何 failed → 1；任何 unavailable（非可选 requires）→ 2。
- 任何任务因异常中断按 `failed`（category=`error`）记录，不静默跳过。

## 4. 失败分类（固定枚举，跨阶段复用）

| category | 含义 |
|---|---|
| `assertion_failed` | 断言不满足（业务状态/形状/内容错误） |
| `error` | 执行中抛出异常/崩溃 |
| `setup_error` | 种子/fixture 构建失败 |
| `timeout` | 超过任务时限 |
| `missing_requirement` | `requires` 中的外部条件不存在 |
| `safety_violation` | 越权写入、伪造证据/回执、跨 scope 泄漏（单独归类，出现即最严重） |

关键回归（`safety_violation`、伪造成功、旧事实审批新状态）出现时不进入候选，不参与平均分。

## 5. 开发集首轮任务清单（12 个，均合成）

P1（证据回读/影响）8 个 + P0 不变量 4 个：

1. dev-p1-ev-001 正常分页读取；2. 非法 offset/limit 拒绝；3. 篡改内容哈希拒绝；4. 跨 scope 读取与缺失同错（无存在性预言机）；5. 同一引用重复回读内容一致；6. 未知来源版本保持未知；7. 更正关联事实 → 相关结论 stale 且无关结论不受影响；8. 重查失败时状态不得显示风险解除。
不变量：9. 未确认候选不进入权威事实；10. 响应 bundle 与正文同源（bundle_shape）；11. 幂等重复提交不产生第二次领域效果；12. 安全检查仍作用于最终文本。

## 6. held-out 隔离规则

- 采集：按**来源材料、场景模板或药物组合**分组；同一组不得同时出现在 dev 与 held_out。禁止把已在开发中调试过的用例改名入 held_out；禁止从 held_out 反向调参。
- held_out 一旦用于选型/调参，其结果降级为开发评测；最终验证需采集新数据并记录在结果 JSON 的 `provenance` 字段。
- 当前状态：**held_out = unavailable**（尚无独立采集数据）。`stage0/product_evals/tasks/held_out/` 仅含本规则的 README；run_eval 对空 held_out 套件输出 `unavailable`（退出码 2），不输出 pass。
- 无专业评审时，所有质量指标限定为**工程标注口径**。
