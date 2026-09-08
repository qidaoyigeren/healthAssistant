# product_evals：产品能力开发评测

协议、失败分类与 held-out 隔离规则：`docs/product-upgrade/P0/eval-protocol.md`。

## 运行

```powershell
# 全部开发任务（含如实 unavailable 的未实现能力）
.venv\Scripts\python.exe -m stage0.product_evals.run_eval --suite dev --out <结果.json>

# 只跑指定阶段
.venv\Scripts\python.exe -m stage0.product_evals.run_eval --suite dev --phase P1
```

退出码：`0`=全部通过；`1`=存在失败；`2`=存在 unavailable（不允许整体 pass）。

## 要点

- 执行器驱动**真实服务栈**（TestClient + 临时合成库 + 脚本检测器），与前端走同一条代码路径；无真实模型调用。
- 任务文件：`tasks/dev/*.task.json`；断言类型：`read_page` / `api_status` / `db_state` / `bundle_shape`。
- `tasks/held_out/` 目前为空（unavailable）——采集与隔离规则见该目录 README，禁止用开发集改名充当盲测。
- 自检：`python -m unittest stage0.test_product_evals`（必败样例非零退出、缺数据不输出 pass）。

## 当前状态（2026-09-07，P0–P1 轮）

12 个开发任务：11 通过、1 unavailable（`dev-p0-inv-009` 未确认候选隔离，依赖 P2 reconciliation 模块）。数据集版本 `dev-1`；扩充至 30–50 个任务是计划而非完成指标。
