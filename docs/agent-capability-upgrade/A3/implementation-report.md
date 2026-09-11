> Historical stage report. Current implementation and acceptance: [2026-09-10 closeout](../closeout-2026-09-10/implementation-report.md). Original observations below are retained; they are not the latest status.

# A3 implementation report — 任务路由、预算分配和失败恢复

Status: engineering acceptance **pass** (8/8 adaptive tests, 228 + 49 batch sweeps OK,
dev-set per-task outcomes unchanged). Real model quality / provider availability /
independent held-out: **unavailable** — no remote model call in this stage.

## What was built

**1. Explicit routing with a recorded basis** (`stage0/router.py`, `request-router@1`):

| route | condition | basis example |
|---|---|---|
| `exact_query` | explicit `query_current_medications` API type, or an exact single-purpose NL medication ask | `explicit_api_event_type:query_current_medications` |
| `contract_flow` | explicit domain-write event types (medication_change, measurement, …) | `explicit_api_event_type:medication_change` |
| `open_planning` | open NL requests; compound requests that CONTAIN a "药单" clause keep all goals | `compound_request_keeps_all_goals` |
| `legacy` | investigation disabled, or the safety-boundary pre-check refuses | `safety_boundary_precheck` |

The route decision is made once per request in `_prepare_investigation` and recorded
additively in the answer bundle as `route` + `route_basis` (checkable strings, never
model statements). Key guarantee (A3.1): a natural-language compound request such as
"现在吃什么药？另外帮我核查一下相互作用的证据" is routed to the open planner by
clause-level detection — the "药单" keyword can no longer pull the whole request into
the exact query path and drop the rest.

**2. Phase budget with wrap-up reservation** (`stage0/agent.py`):
`wrap_up_reserve(max_cycles)` reserves ~15% (min 1) of the SAME cycle budget for
verification/delivery. Once only the reserve remains, new retrieval/read tool steps
are refused (`budget_reserved_for_wrapup`, unfinished items recorded) while wrap-up
tools (`memory_write` record_warnings, `ask_clarification`) still run — they ARE the
wrap-up. The bundle records `phase_budget: {limit_cycles, wrap_up_reserved,
cycles_used, token_usage}` — reserved vs actual is visible; the reserve is a
partition, never an extra allocation. Batching (`batch_read`, Harness P3) and
concurrency remain separate mechanisms; writes never bypass their ordering.

**3. Result-fingerprint re-plan avoidance**: in `run_open_review`, a proposal whose
(tool, arguments) fingerprint is identical to the previous executed proposal is not
re-executed — the run terminates `no_progress` (`no_progress:repeated_proposal`)
instead of burning cycles on pointless re-planning. Deterministic single-action steps
continue to be chosen by code; the planner is only consulted through the same
gap-driven path.

**4. Failure taxonomy** (`stage0/server.py`): `classify_error` now sends provider
429/rate-limit errors to the bounded app-level retry path (`retryable` with existing
backoff+jitter and attempt caps); new `error_detail()` reports the finer class
(`rate_limit` / `timeout` / `transient` / `permanent` / `effect_unknown` /
`validation`) with the retry decision. No retry amplification: the LLM SDK client is
created with `max_retries=0` by default (`LLM_MAX_RETRIES`/`TOKENDANCE_MAX_RETRIES`,
extract_ddi), so SDK and application retries never multiply. Durable waits stay on
the existing outbox/resume queues. Provider routing remains the existing
env-configured `resolve_llm_config` (Zhipu/TokenDance); no new provider was added.

**5. Status separation preserved**: `execution_status` / `goal_status` /
`answer_status` (A1) are unchanged; route and phase budget are additive bundle
fields. A wrap-up-reserved turn reports degraded execution and an incomplete goal —
never a false completion.

## Actual commands and results

```powershell
.venv/Scripts/python.exe -m unittest stage0.test_agent_adaptive -v        # 8/8 OK
# batch 1: agent/harness/memory/reliability + A1/A2/A3 test files
.venv/Scripts/python.exe -m unittest stage0.test_agent_investigation stage0.test_agent_open_tasks stage0.test_agent_adaptive stage0.test_live_regressions stage0.test_stage8_agent stage0.test_harness_p0..p3 stage0.test_memory_p0..p2 stage0.test_reliability_p0..p2
#   → Ran 228 tests ... OK
# batch 2: product/server suites
.venv/Scripts/python.exe -m unittest stage0.test_product_tasks stage0.test_product_evals stage0.test_product_evidence_quality stage0.test_product_reconciliation stage0.test_frontend_read_models stage0.test_stage10_server stage0.test_product_closeout
#   → Ran 49 tests ... OK
.venv/Scripts/python.exe -m stage0.agent_evals.run_eval --policy gap --path replay
#   → 13/13, per-task outcomes identical to dev-replay-final.json
```

Note: running the product suites (which load RapidOCR/onnxruntime models) together
with the full long sweep in ONE Python process can segfault natively on this Windows
machine (pre-existing environment flakiness, reproducible independent of this
stage's changes; all suites pass individually and in the two batches above).

## Acceptance coverage (tests in `stage0/test_agent_adaptive.py`)

- Routing: explicit API types deterministic; compound NL keeps all goals; simple NL
  ask stays exact; off-flag and refusal stay legacy; route recorded in the bundle.
- Wrap-up reserve: reserve is a partition; with `max_cycles=3` and more work than
  budget, the agent stops new tool work, still delivers a bounded report with the
  disclaimer, and does NOT claim `completed` (quality floor over speed).
- Fingerprints: a repeated identical proposal stops bounded, strictly under budget.
- Taxonomy: 429 → retryable + `rate_limit` detail; timeout → retryable + `timeout`;
  `EffectUnknownError` → `effect_unknown`; param errors → permanent.

## Availability distinctions

- `engineering_status: pass` — deterministic paths only.
- `real_model_quality: unavailable` — no authorized provider run; provider health
  under fault injection is exercised only via the classification/backoff code paths,
  not against a live degraded provider.
- p50/p95 latency with a real model remains unavailable (no live runs in this stage).

## Limitations

- Phase budget covers cycles (planning steps); token-level phase accounting reuses
  the existing turn-budget ledger and shows `0/unknown` in deterministic runs.
- The quality non-inferiority gate is enforced as "reserved turns still deliver the
  bounded report and never claim completion" on the synthetic dev set; a live-model
  non-inferiority comparison remains unavailable.
