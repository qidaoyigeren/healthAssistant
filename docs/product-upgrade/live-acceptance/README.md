# 真实调用链测试与修复记录 · 2026-09-08

后续更新：[智谱官方 GLM-4.7-Flash 切换复测](zhipu-retry.md)。冷缓存抽取通过、332 项回归通过；完整问答受到供应商 429 / 1305 容量限制，严格验收仍失败。

**结论：真实调用已执行，但严格全链路验收仍未通过。** 材料业务流程通过；真实规划、混合检索、KEGG、模型抽取均有调用证据。开放式问答遇到模型超时，最终交付的是明确标注不完整、保留真实引用的兜底回答，不能算作模型回答成功。此前 `final-acceptance/` 的 pass 仅代表较早的本地合成工程验收。

## 测试实际使用了什么

- 模型：现有 TokenDance 凭据和 `glm-5.3-flash`。没有替换模型响应、DDI detector 或 RAG；没有修改凭据。
- 后端：真实 `create_app` 默认 Agent、LangGraph、SQLite、outbox、预算账本和最终输出校验。
- 检索：现有本地语料、BM25 + BGE + FAISS；KEGG 缓存缺失时发起真实 HTTP 请求。已有结构化证据命中与新模型抽取分别记录。
- 材料：合成用药表，真实 RapidOCR、本地文档解析、真实 API 和浏览器确认。
- 隔离：专用 `output/product-live-20260908/` 数据库与缓存。未打开正式患者库。所有患者输入均为合成内容。
- 基线测试启用 180 秒单次运行预算、12 次调用上限、100,000 token 准入预算；首轮传输超时 45 秒，之后恢复 60 秒。提取输出上限从原配置 1,024 调整为本次试验的 4,096。额外 360 秒诊断单独记录，不替代 180 秒结果，也不是产品默认预算。

## 实测结果

| 场景 | 结果与证据 |
| --- | --- |
| 新增两种药物 | 两条事件落库并完成真实检测；68.4 / 102.7 秒。生成了说明书来源的警告。模型草稿被最终校验拒绝，实际使用模板回答。 |
| 材料完整流程 | 19 项浏览器检查通过：上传、OCR、定位、纠错、确认、后台重查、缺药名待办、继续任务、摘要下载、移动端布局。[记录](../../../output/product-live-20260908/browser.json) |
| 初次开放式问答 | 109.4 秒后降级：参数校正产生不合法的 null，随后模型超时。无可交付引用。 |
| 参数修复后问答 | 真实 DDI 与 `hybrid_bm25_bge` 执行成功；183.9 秒后最终输出失败，定位到预算兜底对 unknown 警告的格式不匹配。 |
| 兜底修复后 360 秒诊断 | 265.2 秒；规划第 5 次请求超时，运行正确标为 degraded。返回 3 条已记录警告，正文引用可回读且完整性 verified；模型回答未交付。[记录](../../../output/product-live-20260908/question-browser-final.json) |
| 冷缓存抽取，原配置 | 真实检索找到明确原文，但两次抽取未产生可用证据。单次额外诊断证明 `finish_reason=length`、输出 1,024 token，其中 1,019 为推理 token，工具结果为空。 |
| 冷缓存抽取，4,096 上限 | 首条真实抽取得到原文支持的 `kegg+rag+llm` 结果；第二条重复正文的请求超时，因此为 degraded，不计正常通过。[记录](../../../output/product-live-20260908/cold-after/result.json) |
| 正文去重后的冷缓存复测 | 抽取请求从 2 次减为 1 次，但该次供应商请求超时，仍为 fail。没有继续反复采样来取得 pass。[记录](../../../output/product-live-20260908/cold-final/result.json) |
| 实际失败检查点回放 | 修复后的预算兜底保留全部 3 条警告及来源，通过最终校验；0 次模型调用。[记录](../../../output/product-live-20260908/fallback-replay.json) |
| 重启后的旧任务恢复 | 旧事件执行期限已过，API 拒绝继续；没有重置预算或新增模型调用。期限内恢复仍由工程回归覆盖。 |
| 页面重复回答 | 按幂等身份去重，浏览器确认只显示一份问答、刷新可恢复、新会话隔离；0 次模型调用。[记录](../../../output/product-live-20260908/ui-dedup.log) |

业务材料链的 19 项浏览器检查在第一轮真实配置下执行；随后改动通过针对失败场景的真实问答、冷缓存测试、页面复查和后端回归验证。各次输入、配置和结果分别保留，不宣称所有截图来自同一版代码或同一次运行。

## 已修复的实现问题

1. **参数校正与工具契约不一致**：开放式问题没有确定焦点药物时，省略可选 `focus_medication`，不再注入 null；实际用药集合仍由权威状态提供。
2. **截断误记为阴性**：长度截断、内容过滤或缺少强制工具结果抛出明确解析错误；只有完整的 `triples=[]` 是有效阴性。解析错误使用单独缓存状态，不重复采样直到出现阳性。
3. **预算兜底被自身校验拦截**：统一警告格式，unknown 保持 unknown，缺少效应时仅展示“已记录警告”，保留引用与记忆记录。未放宽最终校验。
4. **相同正文重复抽取**：按来源药品、目标药品和正文去重；保留替代来源信息。选择策略版本进入缓存键，旧策略结果不会静默复用。
5. **同一回答显示两次**：服务端已有完整结果时优先使用历史记录；未完成的本地提交仍保留进度与重试入口。跨会话占位数据按 session 过滤。
6. **独立重查运行缺少终态**：正常、预算失败与执行异常分别落为 succeeded / degraded / failed；保留原预算。历史测试中的旧 running 记录不伪造回填为成功。

回归入口：[test_live_regressions.py](../../../stage0/test_live_regressions.py)。最终后端 **331 项通过，0 失败、0 错误、0 跳过**，见 [unittest-summary.json](unittest-summary.json)；[30 个开发场景](product-dev.json) 全部通过。前端类型检查和构建通过，仍有约 509 KB 主包体积提示。

## 调用量与审计

[调用汇总](../../../output/product-live-20260908/usage-summary.json)：32 次真实模型请求尝试、9 次 KEGG HTTP 尝试。供应商实际返回的已知 token 小计 99,856；另有 5 次远端用量未知和 1 次失败估算记录。这是所有成功、失败、复测与诊断的合计，不是完整账单或效果分数。未取得可信单价，因此不编造金额。

[live-summary.json](../../../output/product-live-20260908/live-summary.json) 为 fail；保存阶段门禁、账本、报告时源码指纹。`run_manifests.json` 保存每次运行开始时的版本，`checkpoint-diagnostics.json` 保存实际工具参数、观察、校验和模型降级原因。截图为 [最终兜底界面](../../../output/playwright/live-product-question-deduplicated.png)。原始失败日志均保留。

## 复现步骤

以下命令会使用现有模型接口。为每次完整流程选择新的输出目录，避免把旧幂等键或旧任务混入新的测试。

```powershell
$env:PYTHONUTF8 = '1'
$env:TOKENDANCE_MAX_TOKENS = '4096'
$env:AGENT_TURN_BUDGET_SECONDS = '180'
.venv/Scripts/python.exe -m stage0.product_live_acceptance --serve --out output/product-live-new --port 8012
```

另一个终端准备基线并启动前端：

```powershell
.venv/Scripts/python.exe scripts/product-live-api-acceptance.py --phase seed --out output/product-live-new
$env:STAGE0_DEV_API_TARGET = 'http://127.0.0.1:8012'
npm --prefix frontend run dev -- --host 127.0.0.1 --port 5183 --strictPort
```

用 Playwright CLI 打开 `http://127.0.0.1:5183/materials`，依次运行 `scripts/product-live-browser-acceptance.js` 和 `scripts/product-live-question-browser.js`。本机 CLI 使用 Codex 自带 Node，避免系统 Node 的退出断言。问答最多轮询约 300 秒；额外 360 秒诊断使用 `product-live-question-final-browser.js`。保存 transcript 到对应目录的 `browser.log`、`question-browser.log` 等，不把 CLI 错误算作成功。

```powershell
.venv/Scripts/python.exe scripts/product-live-cold-detector.py output/product-live-new/cold-final
.venv/Scripts/python.exe -m stage0.product_live_acceptance --out output/product-live-new
.venv/Scripts/python.exe -m stage0.run_harness_acceptance --unit-only --out docs/product-upgrade/live-acceptance
```

重启测试宿主使用相同目录加 `--resume`；该参数不会重新生成任务预算，不会使过期请求复活。

## 仍未达标的部分

最高优先级是当前模型服务的单次调用耗时和超时：将整体运行预算从 180 秒增至 360 秒仍未消除单次请求超时。需在固定工作负载下验证模型/供应商、推理与输出预算、上下文体积及调用次数，不能靠无限提高超时或重试次数宣称稳定。

模型草稿的引用契约合规性仍需改进；本轮初始两个回答被校验拦截。已有确定性事实写入、证据回读与安全兜底不能替代这一质量门禁。真实模型 A/B/C、独立盲测和专业评审没有执行。
