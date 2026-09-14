# 入口清单：哪一个能跑，哪个是历史

**为什么有这份文件**：项目里曾经累积到 48 个脚本，其中几个会发起真实远程调用、并且
**固定了产品已经不再使用的配置**。跑错入口会得到"看起来权威、实际与本轮配置不符"的结果。

2026-09-13 的长期用药安全主线重构删除了已结束实验的**执行入口**（脚本、夹具、每轮一套的
浏览器验收/回放/打分脚本）。它们的结论没有被删除——见
[docs/safety-mainline-2026-09-13/HISTORY-INDEX.md](../safety-mainline-2026-09-13/HISTORY-INDEX.md)。

**规则：要出结论，只跑下面"当前"栏里的入口。**

---

## 当前 · 会产生结论

| 入口 | 远程 | 作用 |
|---|---|---|
| `scripts/verify-agent-closeout.py` | 否（默认） | **默认验证入口**：全部 `stage0/test_*.py` + 冻结开发集评测 + 主线产品验收，按类别分开报告。`--live-repeats` / `--only-live` 才发远程调用 |
| `scripts/safety-mainline-live-acceptance.py` | 是 | **有限**真实模型验收：一个安全事项的 Agent 调查。上限（`--max-calls` / `--wall-seconds` / `--max-cycles`）在开跑前打印 |
| `scripts/safety-mainline-browser-acceptance.js` | 否 | 浏览器验收：起隔离合成后端 + Vite + Chromium，走通「看事项 → 回答补问 → 看到变化」。断言基于页面上真的显示了什么 |
| `scripts/safety-mainline-demo.py` | 否 | 可操作的主线演示：用药变化 → 必要检查 → 事项 → 补问 → 跨会话恢复 → 重新复核。隔离临时库 |
| `stage0/safety_browser_fixture.py` | 否 | 上面那条浏览器验收用的隔离宿主（临时库、脚本化检测器、**不调模型**） |
| `scripts/analyze-planner-metrics.py` | 否 | 逐次指标；被 `stage0/test_planner_reliability.py` **直接调用**，不另建口径 |
| `scripts/replay-agent-closeout.py` | 否 | 零网络回放既有失败提案；被 `stage0/agent_evals/run_eval.py` 调用 |
| `scripts/create-product-dev-tasks.py` | 否 | 离线重新生成 `stage0/product_evals/tasks/dev` 夹具 |
| `python -m stage0.test_parallel_product_acceptance --report <path>` | 否 | 独立验收：答案可信性 / 长期跟进 / 整条闭环。按类别分开报告**通过 / 未通过 / 未测到**——"未测到"**不**折算成通过。脚本化规划器，不调模型 |
| `scripts/review-visit-live-acceptance.py --out <path>` | **是** | **有限**真实模型验收：一次回访（场景 B）。上限（`--max-calls` / `--wall-seconds` / `--max-tokens`）在开跑前打印；等待用户输入期间不消费模型。失败后不追加批次、不换模型、不扩预算 |
| `python -m stage0.test_review_visit_flow` | 否 | 回访的三个产品场景（信息充分 / 出现相关变化 / 跟进行动未完成），走真实 HTTP 端点 + 真实 worker |

## 常用开发入口（不是脚本）

| 命令 | 作用 |
|---|---|
| `python -m unittest stage0.<模块>` | 单个测试套件 |
| `python -m stage0.test_safety_mainline_e2e --report <path>` | 主线验收 + 分类报告 |
| `python -m stage0.eval_memory --ablate` | 记忆场景消融。**保留的非默认实验**：`selective_invalidation` 的当前默认值由它的消融结果支撑 |
| `python -m stage0.backup --backup\|--export\|--restore\|--verify` | 本地备份/导出/恢复 |
| `python -m uvicorn stage0.server:app` | 后端服务（单写进程） |
| `cd frontend; npm run dev \| npm run build` | 前端 |

## 已删除 · 结论去这里查

| 类别 | 删掉了什么 | 结论在哪 |
|---|---|---|
| 模型/规划器实验 | `run-planner-live-acceptance{,-v3}.py`、`model-qualification-probe.py`、`planner-wire-probe.py`、`run-a5-live.py`、`a5-demo-api.py`、`agent-capability-ablation.py`、`latency-baseline.py`、`latency-diagnostic.py` | `docs/agent-capability-upgrade/{planner-closeout*,latency*,provider-switch,model-qualification*}/RESULT.md` 或 `implementation-report.md` |
| 受控对照实验 | `model-tool-controlled.py`、`retrieval-feedback-*.py`、`tool-history-*.py`、`diag-*.py`、`analyze-*.py` | 同名目录下的 `RESULT.md` |
| 在线/浏览器验收 | `product-live-*.py/js`、`product-{full,p1}-browser-acceptance.js`、`harness-browser-acceptance.js`、`run-material-review-browser.js`、`material-review-*.py/js` | `docs/product-upgrade/*/`、`docs/harness-upgrade/*/`、`docs/agent-capability-upgrade/material-*/RESULT.md` |
| 夹具 | `*_browser_fixture.py`、`agent_closeout_fixture.py`、`product_live_acceptance.py` | 同上；隔离夹具随其脚本一并删除 |
| 引擎评测入口 | `eval_negative.py`、`make_negatives.py`、`harness_p2_eval.py`、`harness_p3_eval.py`、`product_quality_eval.py`、`run_heldout_v2.py`、`run_{harness,product}_acceptance.py` | `stage0/REPORT.md`、`docs/production-upgrade-2026-09-05/`、`docs/harness-upgrade/` |

P3 委派实验（`harness/delegation.py`、`BATCH_READ_SPEC`、`DELEGATE_TASK_SPEC`）**默认关闭**，
本轮按该结论删除；A4 的确定性双角色复核仍默认开启。

---

## 指纹作用域（与入口强相关）

产物里的 `source_fingerprint` **只有在同作用域下才可比**。作用域定义在 `stage0/source_fingerprint.py`：

| 作用域 | 覆盖 | 谁用 |
|---|---|---|
| `runtime` | `stage0/**/*.py` | `run_eval.py` 产物 |
| `app` | + `frontend/src/**/*.{ts,tsx}` | `verify-agent-closeout.py` |
| `repo` | + `scripts/*.py` | 在线批次入口 |

**跨作用域比较必然不等**，看起来就像"源码变了"。要引用可比指纹，一律用 `repo` 作用域。
本文件描述的区分由 `test_live_regressions.py` 的
`test_fingerprint_scopes_are_nested_and_deliberately_distinct` 锁定。
