# Agent Harness 最终验收

日期：2026-09-07。范围：当前工作区的本地工程与合成场景验收。**最终状态：PASS**（统一入口总耗时 125.081 秒）。机器依据为 acceptance-summary.json，记录命令结果、依赖版本、代码 SHA-256、截图哈希和未覆盖范围。

本轮先核查实现、报告和测试，随后补写会失败的回归用例，再修复实际问题。没有把旧报告中的测试数量或模拟性能当成本轮结论，没有修改正式患者库或原始语料。原有未提交改动均保留。

本轮全量回归实际执行 **281 项测试，失败 0、错误 0、跳过 0**。浏览器通过 **14 项检查**。P1 为 **8/8** 合成回归场景；P2 比较与 P3 三次重复实验通过，前端 TypeScript/Vite 构建、pip check、验收期间源码不变检查均通过。最终产物生成于 2026-09-07 17:57:51（Asia/Shanghai）。

## P0–P4 完成状态

| 阶段 | 本地实施与验收 | 尚未完成的范围 |
| --- | --- | --- |
| P0 预算与审核正确性 | 已实现；统一测试复核外呼账本、原始预算限制、恢复、幂等和旧审核事实失效 | 真实远端取消、实际账单硬限额、临床签字、断电介质恢复未验证 |
| P1 执行层、证据、观测、评测 | 已实现；本轮补齐真实 OTLP/HTTP 本地传输、恢复入口兼容检查和评测失败门禁 | 独立 held-out 数据集、真实模型质量、Phoenix UI/运维验收未完成；模型侧 read_evidence 已有，前端原文读取 API 未提供 |
| P2 优化、进度和取消 | 已实现；浏览器验证恢复、游标补齐、取消与正常写入；优化保持默认关闭 | 真实负载收益、SSE、跨进程取消和生产并发未验证 |
| P3 只读委派实验 | 实验实施与收尾报告已补齐；修正等量工作负载后重跑 3 方案 × 3 场景 × 3 次 | 使用确定性 worker，没有独立 LLM worker 或真实质量结论；运行开关仍关闭 |
| P4 扩展部署 | 条件未触发，已记录状态与后续验收清单 | PostgreSQL、多 worker、多患者、迁移与备份恢复均未实施/验收 |

这不是“所有 P 级生产上线完成”。本地可验证的缺口已补齐，独立验证集和生产条件仍应保持可见。

## 本轮修复

1. **OTel 原来没有发送 span。** 将占位实现改为可选 SDK TracerProvider + BatchSpanProcessor + OTLP/HTTP exporter；队列有界，传输在后台，属性采用限定标量字段。真实本地接收器解析 protobuf，验证收到 span 且不包含测试敏感正文；导出失败用例验证业务调用不抛异常。
2. **同参数失败后重试的 span 丢失。** 数据库查询补回 attempt_no / attempt_id；不同显式尝试或不同结果独立记录，逻辑重放才折叠。没有 attempt_id 时仍沿用逻辑签名与结果去重，不声称已识别所有底层真实尝试；LLM 预算以独立账本为准。
3. **Manifest 只记录、未保护恢复。** 两个 runner 的恢复路径接入检查，图 run()/resume() 在执行前检查 graph、models、prompts、policy、limits、corpus。原预算继续来自原 run，不被环境放大。新图恢复缺 manifest 时拒绝；旧 legacy 无 manifest 仍标记 provenance unknown，由 P0 缺账本规则保守处理。
4. **资源版本记录不准确。** 修正实际 DDI 配对索引路径、内容指纹、RAG 配置/文本/索引指纹，额外记录包含未提交源文件的源码指纹；review_enabled 使用实际 runner 配置，记录复用与无进展配置。文件缺失是 missing，不能用 hash(null) 伪装版本已知。
5. **评测可误报成功。** P1 把 required_checks、轨迹匹配和意外 ERROR 日志纳入门禁，恢复 logger 状态并移除临时 handler。P2 比较所有重复的安全结论，指标使用真正中位数，基线显式清空优化开关，失败返回非零。
6. **P3 工作量不等价。** 改为完整回读同一组证据，以真实返回页面逐字检查，禁止重复查询充数或空结果通过；任何重复中的失败都不会被汇总隐藏。历史实验结论由新报告取代，历史文件保留。
7. **前端编译通过但首页报错。** 提交引擎提供稳定外部存储快照，修复 React 无限更新；在应用启动时真正调用恢复逻辑；修正进度 snapshot 去重及受理 run_id 记录。
8. **取消与结果展示未闭合。** 后端返回权威取消状态；graph 保留 operation_outcomes，legacy 保留审计字段；取消后的已保存记录不删除，前端显示取消终态并防止迟到确认覆盖终态。正常写入保留操作结果。药物成分对象按中文/英文名称显示，避免 [object Object]。

## 可重复执行

在仓库根目录使用 PowerShell。OTel 是可选运行依赖，但完整本地验收要求安装，缺失时测试 skip 将被统一入口判为未完成：

```powershell
.venv\Scripts\python.exe -m pip install -r requirements-harness-observability.txt
```

先启动两个仅用于本地验收的服务，分别在两个终端运行；8000/5173 已被使用时应先选空闲环境，不能终止不属于验收的服务：

```powershell
.venv\Scripts\python.exe -m stage0.harness_browser_fixture
```

```powershell
Set-Location frontend
npm.cmd run dev -- --host 127.0.0.1 --strictPort
```

fixture 使用自动创建的临时数据库和脚本 provider，仅绑定 127.0.0.1。`/acceptance/*` 只存在于这个专用入口，没有加入生产 server 的路由。

在根目录执行浏览器验收（本机已安装 Edge）：

```powershell
npx.cmd --yes --package @playwright/cli playwright-cli -s=harness-final open http://127.0.0.1:5173/medications --browser msedge
npx.cmd --yes --package @playwright/cli playwright-cli -s=harness-final run-code --filename scripts/harness-browser-acceptance.js *> docs/harness-upgrade/final-acceptance/browser-acceptance.log
```

本次使用同一 @playwright/cli 的本机 npm 缓存入口直接运行 Node，避开初次 npx 帮助命令的 Windows 退出断言；应用测试未替换为 mock HTTP。浏览器脚本使用实际加载的 Vite 模块 URL，避免 HMR 查询参数产生第二个提交引擎实例。

然后运行统一入口：

```powershell
.venv\Scripts\python.exe -m stage0.run_harness_acceptance --out docs/harness-upgrade/final-acceptance --repeat 3
```

入口执行全部 `stage0/test_*.py`（同一子进程）、P1/P2/P3 评测、pip check、前端 TypeScript + Vite 构建，并核对浏览器结果、截图和源码新旧时间。任何命令失败、测试 skip、浏览器证据缺失或过期、验收期间源码变化，均不能得到整体 pass。stage0 是 namespace package，因此按完整模块名加载测试，不使用会失败的 `unittest discover -s stage0 -t .`，也不为测试发现而改变项目打包方式。

## 工件与检查内容

- `acceptance-summary.json`：最终综合状态、实际环境、耗时、源码指纹及截图哈希。
- `unittest-summary.json` / `unit.log`：全部后端测试的运行数、失败、错误和 skip；新增缺陷回归集中于 `stage0/test_harness_acceptance.py`。
- `negative-before.log`：第一批新增用例在修复前失败的证据；不是当前通过报告。
- `unit-before-fixture-fix.log`：引入严格恢复门后，旧 P0 测试夹具直接构造 checkpoint、漏建 manifest，产生 17 个错误；夹具现已模拟真实启动时保存 manifest 的步骤。`fixture-regression.log` 验证 9 个恢复测试通过；缺 manifest 拒绝恢复的独立负例保持不变。
- `p1-eval.json` / `.txt`：8 个合成回归场景；dev=0、held_out=0 如实保留。
- `p2-eval.json`：相同工作负载关闭/开启优化的比较，包含每次重复原始结果。
- `p3-eval.json` / `.txt`：等量证据回读后的当前候选结论。
- `browser-acceptance.json` / `.log`：真实浏览器的任务恢复、断网重连、进度去重、取消与正常写入等检查；合成 DB 最终仅 2 个 run、1 次用药新增效果。
- `output/playwright/harness-{progress,cancelled,completed}.png`：已目视核对的页面截图。断网测试有预期网络失败；当前正常流程断言无 HTTP 500、无 React pageerror。开发模式的 Router Future Flag 提示不是运行故障。

## 兼容性、回滚和边界

本轮没有删除旧表、回执、证据或历史实验工件。关闭 STAGE0_OTEL_EXPORT 即停止新 exporter 的启用，业务运行不依赖 collector。SDK force_flush 的完成仅表示处理队列完成，不是所有远端接收成功；传输验收另以接收器实收为依据。SDK 队列溢出可能丢遥测，不能代替本地账本。trace 父子关系目前还保留 harness 关联属性，未验证 Phoenix 的完整层级展示。

恢复检查更严格后，旧 manifest 缺字段、资源变化或审核配置变化可能阻止在途图继续。这是显式兼容性变化，需要补充原版本资源证明或制定迁移，不能自动把今天的配置写回历史 manifest。源码指纹用于审计；源码内容本身不等价于运行语义自动迁移，发布时仍须管理 graph/state 版本。

真实模型、独立 held-out 集、真实账单与临床质量的最终验收仍未完成。尤其独立验证集不能由本次已调试的合成场景重新命名获得。P4 的真实数据库、独立进程和患者身份隔离证据也没有产生；参见 `../P4/implementation_report.md`。

OTel 实现依据：[官方 Python exporters 文档](https://opentelemetry.io/docs/languages/python/exporters/)和 [OTLP exporter 文档](https://opentelemetry-python.readthedocs.io/en/latest/exporter/otlp/otlp.html)，具体安装版本记录在验收 JSON 与 requirements-harness-observability.txt。
