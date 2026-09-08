# 产品升级实现核查与收尾

日期：2026-09-07。范围：当前工作区的产品 P0–P1，以及这些已实现功能所需的综合验收。最终结果以 [acceptance-summary.json](acceptance-summary.json) 为准。本轮不把后续路线图当作已实现能力。

最终本地验收：**pass**。后端 **304 项测试，失败 0、错误 0、跳过 0**；真实浏览器 **14 项检查通过**；P1 开发任务 **8/8**；原 Harness P1 回归、依赖检查和 TypeScript/Vite 构建通过。全开发集为 11 通过 + 1 项 P2 能力 unavailable；空独立验证集为 unavailable。验收期间源码指纹一致。本轮临时 8011/5181 服务和专用浏览器会话已关闭。

首次全量回归仅剩一项旧断言仍要求将 fake_hybrid 用作版本；已将夹具改为明确提供 synthetic-corpus-v1，保留原文与版本检查，并保留 [修正前日志](unit-before-version-fixture.log)。之后完整重跑得到上述结果。

## 实现程度

| 阶段 | 实际范围 | 状态与证据 |
|---|---|---|
| P0 | 能力核对、五类对象的契约草案、开发任务与评测入口 | 已有；本轮修复空集和无效断言，独立 held-out 仍缺失 |
| P1 | 证据回读、精确高亮、事实解释、变更影响、最小 AnswerBundle | 实现与收尾检查见统一工件；包含 legacy 与 LangGraph 集成回归 |
| P2 | 材料候选、药单差异、逐项确认 | 未实现；现有用药录入表单不等价于材料核对 |
| P3 | CareTask、跨会话任务状态机、就诊摘要 | 未实现；现有 run 恢复不等价于持续业务任务 |
| P4 | 版本化 fallback 缓存、重排、claim 支持判定、补检索 | 未实现；本轮来源版本修复不等价于 P4 完成 |
| P5 | 实际文档/OCR、页码定位、字段纠错 | 未实现 |
| P6 | 已实现功能的综合验收 | 本轮补齐适用于 P0–P1 的入口；完整 P2–P5 演示未完成 |

因此不报告“整个升级计划全部完成”。本轮完成对象是已有 P0–P1 实现的核查与收尾。

## 本轮修复

1. **证据详情误报 verified。** 原先只调用 get_meta，存在一行元数据就标记完整性已验证，未核对 scope 或正文哈希。现先经受控 read 校验再返回元数据；外范围/篡改内容显示 unavailable，不暴露其元数据。详情的 evidence_available 同步采用真实回读状态。
2. **来源版本不真实。** 原捕获链路把 RAG mode、DDI detection_path 当成 corpus_version。现在仅使用明确版本字段，未知保持 null；检索方式另记在 retrieval_params。补回 get_meta 中遗漏的 content_ref。历史证据记录保持不变，不假装已修复或重新核实其旧元数据。
3. **更正影响混入历史操作。** 原先 since 只筛 changed_facts，而 affected_conclusions 查全局 stale。现在新受控固化操作给实际变更审计附带 run_id，随变更持久化；回执带归属版本标记，查询只关联同一 run 的失效审计。统计与明细在单写者锁内读取，保留重查后继与真实状态。
4. **客户端时钟与假零影响。** 前端改用 runId。无回执/无历史归属时明确不可用；不会回退为全部历史记录或“零影响”。since 兼容接口解析带时区的 ISO 时间，并筛选同一窗口中的失效审计；它是时间窗口，不承诺单次操作归属。
5. **分页与状态展示。** 影响接口返回总数及截断标记；事实计数排除结论审计。证据抽屉以 evidence_id 隔离本地分页状态，防止直接切换证据时拼接其他原文。已有重查结果的旧结论明确显示历史状态。
6. **评测可误通过。** 空 held-out/空筛选结果现在 unavailable；原 `or True` 与“hash 非空”检查改为真实内容和泄漏检查。测试模拟错误页面和错误响应，确认评分器会拒绝。
7. **验收脚本无法收尾。** 修复 unittest skip 对象不能 JSON 序列化的问题。原先依赖固定端口、可能 skip 的浏览器单测移到独立必需验收门；它不再混入纯单元测试发现，也未取消验收要求。
8. **浏览器与图路径清理。** 浏览器脚本要求合成 fixture 身份，读取当前页面 origin，验证真正的失效明细与刷新后的归属。临时测试服务关闭时释放 runner/checkpointer，避免 Windows 上数据库句柄未关闭。

缺陷修复前的失败证据：[negative-before.log](negative-before.log)、[version-negative.log](version-negative.log)。旧全量运行曾有浏览器 skip 及 JSON 序列化异常；以本轮重跑的 unit.log / unittest-summary.json 为准。

## 数据、API 与兼容性

- `GET /v1/evidence/{id}`：权限与哈希校验；content_ref 和真实/未知 corpus_version。
- `GET /v1/alert-records/{id}`：evidence_refs 以真实受控读取结果决定 available/verified。
- `GET /v1/change-impact?run_id=...&limit=...`：按已持久化的操作归属读取影响，返回 attribution、run_id、总数与 truncated。
- `GET /v1/change-impact?since=...`：兼容审计时间窗口；非法或无时区时间返回 422。
- 缺少归属的历史回执返回 409 impact_unavailable，未知操作返回 404。错误不意味着零影响。
- 没有删表/改写正式患者库。归属信息以既有 audit details 和 receipt result 的新增字段存储。旧客户端可忽略新增字段。
- 现有 execute_with_receipt 的领域提交/回执提交窗口仍采用原有领域幂等处理；本轮没有宣称消除该窗口。审计归属随实际写入持久化，重放时不从新时钟猜测旧影响。回执未成功发布时接口保守返回不可用。
- 新捕获方式可能产生不同 evidence_id，旧引用和原文继续保留；关闭新前端入口不会删除历史证据。

## 重现本轮演示

先检查 8011 / 5181 是否空闲。只能使用本轮专用临时 fixture，不能将脚本指向正式患者服务。以下命令在仓库根目录执行；前两个服务分别使用不同终端。

```powershell
.venv\Scripts\python.exe -m stage0.product_p1_browser_fixture --port 8011
```

```powershell
$env:STAGE0_DEV_API_TARGET = 'http://127.0.0.1:8011'
Set-Location frontend
npm.cmd run dev -- --host 127.0.0.1 --port 5181 --strictPort
```

第三个终端启动浏览器，再执行场景：

```powershell
npx.cmd --yes --package @playwright/cli playwright-cli -s=product-closeout open http://127.0.0.1:5181/alerts --browser msedge
npx.cmd --yes --package @playwright/cli playwright-cli -s=product-closeout run-code --filename scripts/product-p1-browser-acceptance.js *> docs/product-upgrade/closeout/browser.log
.venv\Scripts\python.exe -m stage0.run_product_acceptance --out docs/product-upgrade/closeout
```

本机系统 Node 运行 CLI 帮助曾出现 Windows 退出断言，实际验收使用已安装的 CLI 文件与 Codex bundled Node 执行，路径记录于本轮会话。没有替换被测 HTTP 服务或预制浏览器结果。全新重跑应重新启动临时 fixture，以获得同一初始状态。

手动流程：风险与证据页打开提醒详情 → 查看原文并核对高亮 → 打开关联事实 → 用药页记录合成剂量更正 → 展开“本次更正影响了哪些检查” → 刷新后再次读取操作影响。所有剂量与提示均为合成软件验收材料。

## 验证工件

- [acceptance-summary.json](acceptance-summary.json)：命令、退出码、代码指纹、各阶段与未覆盖范围；只汇总 P0–P1 本地工程。
- [unittest-summary.json](unittest-summary.json)、[unit.log](unit.log)：全量后端测试；新增 closeout 回归在 stage0/test_product_closeout.py。
- [browser.json](browser.json)、[browser.log](browser.log)：14 项浏览器检查、截图哈希与 run_id；截图已目视核查。
- [product-p1.json](product-p1.json)：本阶段开发任务。
- [product-dev-all.json](product-dev-all.json)：全开发任务；P2 候选模型任务保持 unavailable。
- [held-out.json](held-out.json)：独立集未提供，不能得到 pass。
- [harness-p1.json](harness-p1.json)：原执行层回归评测。
- [frontend-build.log](frontend-build.log)：TypeScript + Vite 构建。

浏览器门必须同时满足全部必需检查、无 pageerror/5xx、存在且新于运行代码的截图与日志；缺项、过期或执行错误不能整体通过。验收前后源码指纹必须一致。真实模型质量和独立专业评价单独保持 unavailable，不能从工程通过推导医疗有效性。
