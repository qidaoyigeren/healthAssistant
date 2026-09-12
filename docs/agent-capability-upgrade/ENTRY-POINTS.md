# 入口清单：哪一个能跑，哪个是历史

**为什么有这份文件**：项目里累积了 22 个脚本，其中几个能发起真实远程调用、并且**固定了产品已经不再使用的配置**。跑错入口会得到"看起来权威、实际与本轮配置不符"的结果——这正是 2026-09-11 那轮踩过的坑（旧入口固定 `retries=1`，而产品默认已经变了）。

**规则：要出结论，只跑"当前"栏里的入口。** 历史入口一律要显式确认才跑，且产物自带 `superseded` 标记。

---

## 当前 · 会产生结论

| 入口 | 远程 | 作用 | 备注 |
|---|---|---|---|
| `scripts/run-planner-live-acceptance-v3.py` | 是 | 冻结 k=3 在线批次 | 需 `--enable-live`；目录不可复用；派发前写 manifest；`effective_config()` **解析**端点而非复述常量 |
| `scripts/model-qualification-probe.py` | 是 | 换模型前的契约资格验证 | 检查 `tool_choice="required"`、具名函数、必填嵌套参数是否被遵守 |
| `scripts/verify-agent-closeout.py` | 可选 | 完整离线收尾（34 模块 + 开发集） | 默认离线；`--live-repeats` / `--only-live` 会发远程调用 |
| `scripts/planner-wire-probe.py` | **否** | 抓**序列化后的真实请求**与响应解析 | 模拟传输层。缺 `arguments` 的根因就是靠它确定的，不是猜的 |
| `scripts/analyze-planner-metrics.py` | **否** | 逐次指标 | 纯离线读产物 |
| `scripts/latency-baseline.py` | **否** | 延迟基线重算（每次调用的 token 拆分 + 墙钟、每回合调用数与墙钟、延迟/输出 token 比率） | 纯离线读产物；`planner_latency_ms_each` **直接 import** `analyze-planner-metrics.py`，不另建口径；缺字段一律记 null，不回填 |
| `scripts/replay-agent-closeout.py` | **否** | 零网络回放既有失败提案 | |

## 历史 · 需要显式确认才跑

| 入口 | 远程 | 过时在哪 | 防护 |
|---|---|---|---|
| `scripts/run-planner-live-acceptance.py` | 是 | 固定 `official_zhipu` / `glm-4.7-flash` / `PLANNER_PROVIDER_RETRIES=1`，产品已改为 tokendance / `glm-5.3-flash` | **默认拒绝**，需额外 `--reproduce-superseded-protocol`；manifest 带 `superseded: true` + `superseded_by` |
| `scripts/run-a5-live.py` | 是 | A5 能力轮的单任务 live 跑，文档字符串自述为 "official Zhipu glm-4.7-flash free tier" | 仅文档标注（无 CLI 可加护栏）；**不要用它出当前结论** |

## 产品侧（与规划器验收无关）

以下脚本属于产品/浏览器验收轮，**不属于 agent 规划器的验收入口**，不应被用来评价规划器：

`product-live-api-acceptance.py`、`product-live-cold-detector.py`、`product-live-extractor-diagnostic.py`、`product-live-question-browser.js`、`product-live-question-final-browser.js`、`product-full-browser-acceptance.js`、`product-live-browser-acceptance.js`、`product-p1-browser-acceptance.js`、`harness-browser-acceptance.js`、`run-live-layer-c.sh`、`agent-capability-demo.js`、`a5-demo-api.py`、`agent-capability-ablation.py`。

## 夹具生成（离线，无远程）

`create-product-dev-tasks.py`、`create-product-ocr-samples.py`。

---

## 指纹作用域（与入口强相关）

产物里的 `source_fingerprint` **只有在同作用域下才可比**。作用域定义在 `stage0/source_fingerprint.py`：

| 作用域 | 覆盖 | 谁用 |
|---|---|---|
| `runtime` | `stage0/**/*.py` | `run_eval.py` 产物 |
| `app` | + `frontend/src/**/*.{ts,tsx}` | `verify-agent-closeout.py` |
| **`repo`（约束性）** | + `scripts/*.py` | 在线批次入口 |

**跨作用域比较必然不等**，看起来就像"源码变了"。要引用可比指纹，一律用 `repo` 作用域。本文件描述的区分由 `test_live_regressions.py` 的 `test_fingerprint_scopes_are_nested_and_deliberately_distinct` 锁定。
