# 前端轮验收报告(2026-09-06)

范围:「真实产品前端生成 Prompt」一轮交付 —— 独立 Web 前端(frontend/,React + TypeScript +
Vite + TanStack Query + React Hook Form + Zod + Tailwind 4 + Radix)与最小后端适配。
基线:2026-09-05 Stage 8/9/10 实施后的工作区(未提交变更之上增量开发,未回退/未清库)。

## 一、真实执行过的检查与结果

### 后端(单元/集成,unittest)

- `stage0/test_frontend_read_models.py`(新增)**13/13 通过**:
  committed 响应携带结构化 operation_outcomes;deduplicated/unresolved 真实 outcome;
  预警读模型列表/详情/替代链(severity 如实 null);cursor 分页覆盖同秒记录(5 条 limit=2
  分 3 页不重不漏);药物全版本含 dose_change 前驱链与「未记录剂量如实为空」;冲突全状态查询
  + 动作历史;sides 引用解析;memory ref 精确版本解析、非 memory 引用/未知层/坏版本 422;
  fact verify → verified、争议事实 → blocked_by_conflict、retract → retracted、空依据 422;
  recheck-tasks 引用缺失如实 null;sessions/session-events/trace;overview 计数;
  导出/备份/校验/下载 + 路径穿越拒绝 + 未知产物 404。
- 既有回归门禁:`test_stage3/5/6 + memory_p0/p1/p2 + stage8 + stage10 + test_frontend_read_models`
  → **130/130 OK(9.5s)**。
- 未纳入门禁的 `test_reliability_p1` 8 项因环境缺少 `langgraph` 失败 —— **先于本轮即存在的
  环境限制**,与本次改动无关(本轮改动后该组失败模式与改动前一致)。

### 前端(类型检查 / 构建)

- `tsc -b --noEmit` 通过(strict 全开,含 noUncheckedIndexedAccess)。
- `npm run build`(tsc + vite build)通过:472.89 kB JS(gzip 139.73 kB)+ 22.07 kB CSS。
- Vite dev server 冒烟:页面正常服务;`/v1` 代理正确转发到 `127.0.0.1:8000`
  (冒烟时后端未在 8000,代理返回目标连接错误 —— 代理链路本身验证通过)。

### 端到端(真实 FastAPI + 专用临时 SQLite,STAGE0_DB_PATH 指向 %TEMP%\mcp-e2e,合成测试数据)

按前端调用顺序完整执行,以下为实际观察到的返回:

1. 空库 `/v1/overview` 全 0 —— 无虚构数据;前端显示登记引导空状态。
2. `register_profile`(78 岁/女/52kg/青霉素过敏/高血压)→ committed,operation_outcomes
   逐条 `inserted`(semantic refs 可见)。
3. `profile_update` 只提交体重增量 → 真实 `update` outcome。
4. `medication_change add 氨氯地平 5mg` → `add` outcome + medication/episodic ref。
5. `dose_change 10mg` → `dose_change`;版本链中旧版本 dose 为 null(未记录如实)。
6. `remove 阿司匹林`(无此在用药)→ **`unresolved` outcome**(不显示「已成功停用」)。
7. 同键异载荷 → 422 `idempotency_key_reused`;同键同载荷 → `Idempotent-Replay: true`。
8. state/alert-records/history(倒序)/sessions/session-events(含持久化 response 与
   request 元数据)全部真实返回。
9. `POST /v1/memory/fact-actions verify` → `verified`;`retract` → `retracted`。
10. `/v1/history/search?q=氨氯地平` → `mode: fts5_trigram`,命中 2 条(历史候选)。
11. 导出 → `memory-*.json.gz`(episodic 行数与库内一致);verify → format ok;
    下载 16352 字节;备份 → `memory-*.db`(schema 4-p2)。
12. 历史回溯:`known_at=用药提交之前` → 当时 meds=0、仅 3 条当时已知 facts
    (**后补录的用药信息未泄漏到较早知悉时点**);`/v1/memory/state` 双时间参数独立生效。
13. `/v1/sessions/{sid}/turns/{turn}/trace` → 10 条已落库 trace(plan/act/observe/reflect)。

## 二、验收标准逐项对照(逐项验证,未执行的如实标注)

| 场景 | 结果 |
| --- | --- |
| 空数据库首次打开 | ✅ E2E 步骤 1 + 前端空状态分支(无示例数据,不自动创建) |
| 登记患者情况 | ✅ E2E 步骤 2(提交→任务→落库→可读;重启后仍在——SQLite 持久化,单测覆盖服务重启语义) |
| 修改档案(仅增量) | ✅ E2E 步骤 3(delta 计算只提交变更字段;未变字段不产生重复事实确认) |
| 新增用药 | ✅ E2E 步骤 4 |
| 记录剂量变化(新旧可追溯) | ✅ E2E 步骤 5 + 单测 MedicationRecordTests |
| 记录停用(unresolved) | ✅ E2E 步骤 6 |
| 连点/网络重发(同事件单投影) | ✅ 单测 IdempotencyTests(重放/并发同键)+ 提交引擎同键复用;**浏览器内双击 E2E 未执行**(提交按钮有 busy 防抖,引擎层有同键保护) |
| 同键异请求 422 | ✅ E2E 步骤 7 |
| 刷新/断网恢复 | ✅ 引擎设计 + 单测 level;**真实断网浏览器场景未执行**(sessionStorage 恢复 + 服务端 session-events 双路径已实现) |
| 任务真实失败展示 | ✅ 单测(失败键 409、retry 端点 409);任务托盘展示服务端 error 原文 |
| 预警与来源核对 | ✅ 读模型单测 + 预警详情页(memory ref 应用内解析、http 来源新开、本地指针不构造读取、缺失如实展示) |
| 结论失效和复查 | ✅ 单测 AlertRecordTests + RecheckTaskTests;复查执行结果文案来自服务端返回(no_hook 如实提示) |
| 关键事实矛盾(双方可见) | ✅ 单测 ConflictRecordTests(sides 并排) |
| 冲突核实/重开/撤销 | ✅ 单测(动作落库、历史一致);reopened 使结论 stale 的联动由既有 memory 测试覆盖 |
| 事实核实受阻 | ✅ 单测 blocked_by_conflict 返回 200 + outcome,前端展示「被待核实冲突阻塞」而非核实成功 |
| 历史回溯 | ✅ E2E 步骤 12(valid_at/known_at 独立;future leak 防护实测) |
| 超过 500 条历史 | ✅ cursor 分页单测(服务端分页,非前端截断);>500 条真实数据集未构造 |
| 跨会话查询 | ✅ E2E 步骤 8(服务端会话恢复);新会话不丢旧任务(任务列表独立关联 session_id) |
| 部分 trace/历史缺失 | ✅ trace 空态文案「没有已落库的执行记录」;未保存助手正文如实显示(不补造) |
| 导出与备份 | ✅ E2E 步骤 11(快照一致性行数核对;校验来自服务端) |
| 服务停止 | ✅ 前端错误态(不显示假 0;读失败保留上下文);**后端运行中断网的真实浏览器观测未执行** |
| 移动端与键盘 | ⚠️ 实现了 390px 单列/底部导航/更多抽屉/Escape/焦点返回/aria-live,**未做真实浏览器多视口截图核验**(见第三节) |
| 构建与回归 | ✅ 130/130 后端 + tsc/build 通过(真实执行记录见上) |

## 三、真实限制与未执行项(不编造通过)

1. **浏览器截图未生成**:本环境无浏览器自动化能力,规格要求的桌面/移动截图(空状态、
   提交后列表、预警证据、冲突详情、历史模式、错误状态)未执行。布局与状态分支已按规格实现,
   视觉核验(390/768/1440 视口、焦点环、对比度)需人工在浏览器完成。
2. **真实浏览器交互 E2E 未执行**:上述前端行为以「引擎/组件实现 + 后端 E2E」为证据;
   Playwright 级别的点击流测试未编写。
3. **LLM 规划路径未联调**:本轮 E2E 全部运行在确定性规划(AGENT_LLM_PLANNER 未开);
   前端对 LLM 路径的展示(更长的 processing、BUDGET_DEGRADED_NOTICE 文案)有对应状态,
   但未实测。
4. **路由深链接刷新回退**依赖部署时静态服务器的 SPA fallback,`vite preview` 已验证基础路径。
5. 打印样式已实现,打印效果未实测。
6. `test_reliability_p1` 需要安装 `langgraph` 才能运行(先存环境限制)。

## 四、本轮新增/修改的仓库文件

- 新增 `stage0/read_models.py`(读模型 + 最小写适配)、`stage0/test_frontend_read_models.py`。
- 修改 `stage0/server.py`(挂载读模型路由;`/v1/memory/state` open_conflicts 增量归一化;
  事件结果含 operation_outcomes)、`stage0/agent.py`(AgentResponse.operation_outcomes + 收集)、
  `stage0/memory.py`(ConsolidationResult.semantic_outcomes)。
- 新增 `frontend/`(完整应用,见 frontend/README.md)、`docs/frontend/api-contract.md`。
- 数据库变更:**无新表/新列**(全部复用既有结构;ConsolidationResult 新字段随 result_json
  序列化,旧行读取兼容——缺字段为空列表)。
- 未触碰:`stage0/memory.db`、既有数据工件、Streamlit 默认路径。
