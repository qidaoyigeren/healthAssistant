# P2：结构化材料核对

实现入口：`stage0/product.py`、`frontend/src/features/materials/MaterialsPage.tsx`。

CSV 导入保留原件哈希、独立导入身份、候选原值、真实行列、纠错轨迹。预览不会修改药单。精确药名/品牌匹配保留成分信息；别名、剂型、规格或重复项有歧义时要求核实，不自动合并。材料未列出只能保留，不能自动停药。缺药名、单位、频次、日期或错误患者均阻止确认。

逐项确认使用 SQLite `BEGIN IMMEDIATE`，复用 `MemoryStore._apply_medication_change_tx`。患者 revision 校验、领域事实、事件、依赖失效、后台检查入队、核对状态及 operation receipt 在同一事务提交。每次确认推进基线并重算余项；外部修改要求显式刷新。故障注入覆盖领域投影后抛错，确认整体回滚；同键重试返回原回执，不再写入。

API：`GET /v1/materials/template`、`POST /v1/materials/csv`、`GET /v1/reconciliations`、`GET /v1/reconciliations/{id}`、`POST /v1/reconciliations/{id}/refresh`、`POST /v1/reconciliations/{id}/items/{item_id}`。最后一个接口接受 `key`、`expected_revision`、`action`、`corrections`，有关联待办时还需 `task_context`。错误 409 表示版本或幂等竞争，422 表示候选尚不完整。

验证见 `stage0/test_product_reconciliation.py`、`stage0/test_product_full_flow.py` 和最终验收的浏览器日志。局限：记录确认是照护者报告确认，不是处方或医学验证；不支持不确定的单位换算，不将材料日期自行解释成停药日期。
