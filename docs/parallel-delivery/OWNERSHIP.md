# 并行交付：文件所有权与环境约定

**基线 commit**：`30f5dd4`
**接口约定**：[CONTRACT.md](./CONTRACT.md)
**准备时间**：2026-09-13

---

## 0. 基线是怎么来的

原工作区 `D:\py\HealthAssistant` 在 `master @ 984c600` 上有大量未提交工作：
**42 个文件被修改、43 个删除、84 个未跟踪**。直接拿旧 HEAD 当基线会丢掉最近几轮的全部成果。

基线 `30f5dd4` 是那一刻工作树的**完整快照**：

- 纳入：当前源码、必要测试、配置模板、`docs/` 下的设计文档与轮次产物。
- 纳入：实现所需的未跟踪源码（84 个新文件，含 `stage0/review/`、`stage0/safety_*.py`、
  `frontend/src/features/safety/`、`stage0/harness/evidence_acquire.py` 等）。
- 保留：当前有意进行的源码删除（43 个）。
- **排除**：`.env`（凭据）、`stage0/memory.db`（真实患者库）、`output/`、`.artifacts/`、
  `.playwright-cli/`、`node_modules/`、`.venv/`、`__pycache__/`、构建产物。

快照通过**临时索引文件**生成，原工作区的文件与 `.git/index` 全程未被改写
（验证见 §5）。

> **秘密检查**：`stage0/.env` 是唯一含凭据的本地配置（`DEEPSEEK_API_KEY`、
> `SILICONFLOW_API_KEY`、`ZHIPU_API_KEY` 等）。它虽无 `.py` 之类后缀、且被 `.gitignore`
> 覆盖，但仍被显式排除，未进入基线。84 个未跟踪文件已逐个扫描，**无密钥、无凭据**。

---

## 1. 文件所有权

**每个文件只有一个所有者。**不属于你的文件一律只读冻结，需要改动走
[CONTRACT.md §7](./CONTRACT.md) 的范围外变更流程。

### A — 答案可信性

| 文件 | 状态 |
|---|---|
| `stage0/investigation.py` | 已存在，修改 |
| `stage0/harness/default_tools.py` | 已存在，修改 |
| `stage0/harness/evidence.py` | 已存在，修改 |
| `stage0/answer_grounding.py` | 新增（如需） |
| `stage0/test_answer_grounding.py` | 新增 |
| `docs/parallel-delivery/A.md` | 本任务简报，可写 |

### B — 长期跟进

| 文件 | 状态 |
|---|---|
| `stage0/safety_cases.py` | 已存在，修改 |
| `stage0/care_tasks.py` | 已存在，修改 |
| `stage0/server.py` | 已存在，修改 |
| `stage0/memory.py` | 已存在，修改 |
| `stage0/safety_checks.py` | 已存在，修改 |
| `stage0/followup_runtime.py` | 新增（如需） |
| `stage0/test_followup_runtime.py` | 新增 |
| `docs/parallel-delivery/B.md` | 本任务简报，可写 |

### C — 安全体验（前端）

| 文件 | 状态 |
|---|---|
| `frontend/src/features/safety/` | 已存在（整目录），修改 |
| `frontend/src/api/client.ts` | 已存在，修改 |
| `frontend/src/api/types.ts` | 已存在，修改 |
| `frontend/src/api/queryKeys.ts` | 已存在，修改 |
| `docs/parallel-delivery/C.md` | 本任务简报，可写 |

### D — 独立验收

| 文件 | 状态 |
|---|---|
| `stage0/test_parallel_product_acceptance.py` | 新增 |
| `scripts/parallel-product-browser-acceptance.js` | 新增（确有需要时） |
| `docs/parallel-delivery/D.md` | 本任务简报，可写 |

### 共享冻结文件（四路任务都不得修改）

其余全部文件，特别是：

- `stage0/product.py`（`ProductStore.command` / `revisions()` / `ProductError`）
- `stage0/read_models.py`（25 个只读模型与分页信封）
- `stage0/agent.py`、`stage0/harness/*`（A 的三个文件除外）
- `stage0/review/`（整个材料复核模块）
- `.github/workflows/offline-closeout.yml`、`frontend/vite.config.ts`
- `docs/frontend/api-contract.md`（有意保留的既有文档；其若干描述已过时，见 §4）

---

## 2. 环境与端口

每个 worktree 是独立检出，**互不共享运行时状态**。

| 任务 | worktree 绝对路径 | 分支 | API 端口 | 前端端口 |
|---|---|---|---|---|
| 集成 | `D:\py\HealthAssistant.worktrees\integration` | `integration/baseline-2026-09-13` | 8100 | 5200 |
| A | `D:\py\HealthAssistant.worktrees\answer-grounding` | `codex/answer-grounding` | 8101 | 5201 |
| B | `D:\py\HealthAssistant.worktrees\followup-runtime` | `codex/followup-runtime` | 8102 | 5202 |
| C | `D:\py\HealthAssistant.worktrees\safety-experience` | `codex/safety-experience` | 8103 | 5203 |
| D | `D:\py\HealthAssistant.worktrees\independent-acceptance` | `codex/independent-acceptance` | 8104 | 5204 |

**原工作区占用 8000（API）与 5173（前端）——不要占用。**

### 2.1 启动方式（都用环境变量隔离，不改任何配置文件）

后端：

```bash
STAGE0_DB_PATH="<worktree>/output/parallel/<任务>/memory.db" \
STAGE0_API_PORT=8101 \
python -m uvicorn stage0.server:app --host 127.0.0.1 --port 8101
```

前端（`vite.config.ts` 已支持这两个环境变量，**不需要改配置文件**）：

```bash
cd frontend
STAGE0_DEV_API_TARGET="http://127.0.0.1:8101" npx vite --port 5201
```

### 2.2 数据库隔离

- **真实患者库**：`D:\py\HealthAssistant\stage0\memory.db`（204800 字节，2026-09-05）。
  **绝不连接、绝不修改、绝不复制覆盖。**
- `DEFAULT_DB` 解析为 `stage0/memory.db`，是相对**当前 worktree** 的。
  即使忘了设 `STAGE0_DB_PATH`，worktree 里也只会新建一个空库，不会碰到真库——
  但仍要求显式设置，避免误解。
- 各任务临时库统一放 `output/parallel/<任务>/memory.db`。
  `output/` 与 `stage0/memory.db*` 都已在 `.gitignore` 里，不会污染提交。

### 2.3 测试输出

各任务的测试产物、日志、截图一律写
`output/parallel/<任务>/`（A/B/C/D 分别用自己的子目录），不要写到仓库根或 `docs/`。

### 2.4 Python / Node 依赖

worktree 里没有 `.venv/` 和 `node_modules/`（都是忽略项，未被复制）。直接用原工作区
的 `.venv`（按绝对路径调用即可，Python 会以当前目录为 `sys.path`），
前端依赖按需在各 worktree 的 `frontend/` 下 `npm install`。

---

## 3. 验证基线可用

```bash
cd D:/py/HealthAssistant.worktrees/answer-grounding
D:/py/HealthAssistant/.venv/Scripts/python.exe -m pytest stage0 -q
```

若基线测试有失败，**先报告再用**——四个任务都建立在这个基线上，
基线不干净会让后续每个失败都变得无法归因。

---

## 4. 基线里几处已知的过时描述（不要被误导）

侦察时发现以下文档与代码不符。**以代码为准**，本文件记录以免四个任务各自踩一次：

1. `docs/frontend/api-contract.md` 只记录约 45 个路由，实际应用暴露 **73 个**。
2. 同文件 `:116` 称"`case_view()` 尚未输出 `follow_up`"——**已过时**，
   `safety_cases.py:1124` 已经在输出。
3. `POST /v1/safety-cases/{case_id}/investigate` 文档归在 safety 名下，
   实际实现在 `care_tasks.py:1412`。
4. `frontend/src/api/types.ts` 声明的 `answered_parts` 元素（`:664-670`）只有 5 个键，
   而后端实际下发 11 个。**后端比类型声明宽**，新增字段不会自动出现在 TS 里。
5. 事项问题项上 `question_strategy` 与 `strategy` 两个键指的是同一件事
   （见 [CONTRACT.md §1.5](./CONTRACT.md)）。

---

## 5. 原工作区完整性

基线制作前后，原工作区指纹完全一致：

| 指标 | 制作前 | 制作后 |
|---|---|---|
| `HEAD` | `984c600c018236eb8f9fecb47eff32fb8426849c` | 一致 |
| 分支 | `master` | 一致 |
| `git status --porcelain` 摘要 | `c72095f5…` | 一致 |
| `git diff` 摘要 | `f33e3c64…` | 一致 |
| `git diff --cached` 摘要 | `da39a3ee…`（空 = 无暂存） | 一致 |
| `.git/index` 摘要 | `e7552bea…` | 一致 |
| 状态行数 | 120 | 一致 |

**原工作区未被修改，用户的暂存状态未被触碰。**
