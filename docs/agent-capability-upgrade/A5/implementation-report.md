> Historical stage report. Current implementation and acceptance: [2026-09-10 closeout](../closeout-2026-09-10/implementation-report.md). Original observations below are retained; they are not the latest status.

# A5 implementation report — 综合验收、消融实验与亮点交付

Status: engineering acceptance **pass**; real-model repeat quality, provider
availability and independent held-out are reported per the live-run artifacts in
this directory (absent = **unavailable**). A4 was executed as a default-off
experiment; its real-model comparison is **unavailable** and no adopt
recommendation is made.

## 1. Ablations on the frozen A0 dev set (engineering, local, no remote calls)

Command: `.venv/Scripts/python.exe scripts/agent-capability-ablation.py --out docs/agent-capability-upgrade/A5/ablation.json`

| configuration | tasks passed | note |
|---|---|---|
| baseline (frozen pre-A1 policy) | 0/13 | legacy planner clarifies intent instead of delivering a bounded report |
| gap planning (A1 current) | 13/13 | replay path |
| gap planning + real local tools | 13/13 | `tools` path |
| gap planning + tight cycle budget (≤4) | 5/13 | honest budget sensitivity: the capability needs its per-run cycles |

The ablations are attributable: planning (baseline→gap) and budget (gap→tight)
are varied one factor at a time on the identical dataset and rubric. Persistent
tasks (A2) and routing/reserve (A3) are verified by their behavioural suites
(`test_agent_open_tasks.py`, `test_agent_adaptive.py`) rather than the fixed-dev
harness, because their subject is persistence and failure behaviour, not
single-turn quality. (A4 is off by default and excluded from the quality table.)

## 2. Data-status protocol

- Development set: reused continuously for repair — declared author-synthetic,
  never claimed as a blind held-out.
- Independent held-out: **unavailable** (collection/sealing protocol defined in
  A0; no independently collected data exists).
- Real-model repeats: declared **k=3** (one exposed dev task per run, official
  Zhipu glm-4.7-flash free tier, 180 s / 8 calls per run) via
  `scripts/run-a5-live.py`. **Actual k=0 in this session**: the launcher could
  not be started — the session's permission layer repeatedly failed on those
  specific invocations (infrastructure limitation, not a provider refusal; the
  credentials resolve correctly and the launcher is preserved). No
  `live-run-*.json` exists, so `real_model_quality` and `provider_availability`
  are **unavailable** — never imputed, and failed sampling is not re-run until
  a pass appears. The declared protocol stands ready for a later authorized run.

## 3. Engineering fault coverage (all mapped to existing tests)

| fault | test |
|---|---|
| commit-then-crash recovery | `test_agent_investigation.test_crash_after_commit_resume_graph_with_flag_off_keeps_budget_and_effects` |
| budget survives recovery / accumulates across runs | same + `test_agent_open_tasks.test_duplicate_submit_replays_receipt_and_budget_accumulates` |
| stale-fact review refusal | Harness P2 suites (`review_stale` re-validation) |
| duplicate input | `test_agent_open_tasks` receipt replay |
| cancel | `test_agent_open_tasks.test_cancel_while_waiting_is_terminal_and_effects_retained`, dev family `cancel` |
| source invalidation | `test_agent_investigation.test_tamper_after_check_removes_support_but_preserves_historic_refs` |
| no progress | dev family `repeat_retrieval`, `test_agent_adaptive.FingerprintGuardTests` |
| provider rate limit | dev family `model_rate_limit`, `test_agent_adaptive.FailureTaxonomyTests` |
| wrap-up failure under reserve | `test_agent_adaptive.WrapUpReserveTests` |
| unauthorized tool | `test_agent_investigation.test_proposal_cannot_skip_checks...` (`investigation_tool_not_allowed`) |
| forged evidence | forged-ref rejection + `test_multi_agent_review.test_worker_cannot_expand_parent_scope` |

## 4. Separated evidence tracks

1. **Engineering regression**: 228 + 49 batch sweeps + focused suites — pass.
2. **Fixed-evidence model evaluation**: dev-set replay/tools — pass (deterministic
   planner; scripted fault injection counted as engineering, not model quality).
3. **Real tool end-to-end**: dev `tools` path over the real local RAG adapter — pass.
4. **Provider availability**: see live-run artifacts / **unavailable**.
5. **Human domain evidence review**: not performed — no independent clinical
   reviewer; no clinical-safety claim is made anywhere.

## 5. Browser demo

`scripts/agent-capability-demo.js` drives the real UI + real API over the real
SQLite store: 发起开放核查 → 发现缺口（等待并保存）→ 重启（无状态 HTTP + SQLite 即重启语义）→
等待期间相关事实更正 → 补充输入 → 增量重查（选择性失效：证据复用、适用条件重核）→ 回读证据 → 最终报告与预算累计.
Screenshots land in `output/a5-demo-*.png`; the run prints every step's observed
server state (not pre-recorded UI). Result: see `demo-result.json` in this
directory (missing = demo did not complete in this environment).

## 6. Known limitations

- The browser demo requires local server + Vite + a chromium install; when the
  environment cannot provide them, the persistence walkthrough remains proven by
  the API-level tests and the demo is reported not-run rather than claimed.
- All numbers above are engineering measurements on synthetic data.

## 7. Three summary claims (each linked to code + evidence)

1. **缺口驱动规划把“只追问”变成“可交付的有界核查”** — same frozen dev set:
   0/13 → 13/13 (`stage0/investigation.py`,
   [A0 baseline](../A0/baseline-replay.json), [A5 ablation](ablation-gap.json)).
2. **开放核查任务跨重启持久并增量重查** — wait/restart/correct-fact/resume with
   selective invalidation and accumulated budget
   (`stage0/care_tasks.py`, [A2 tests](../../stage0/test_agent_open_tasks.py)).
3. **路由、预算预留与失败分类是可核验的** — route + basis in every answer
   bundle, wrap-up reserve as a partition, 429/timeout/permanent taxonomy
   (`stage0/router.py`, [A3 tests](../../stage0/test_agent_adaptive.py)).
