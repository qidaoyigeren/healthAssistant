> Historical stage report. Current implementation and acceptance: [2026-09-10 closeout](../closeout-2026-09-10/implementation-report.md). Original observations below are retained; they are not the latest status.

# A1 implementation report — 信息缺口驱动规划与证据覆盖检查

Status: engineering replay **pass** (13/13 development tasks, both paths); real model quality,
provider availability and independent held-out remain **unavailable**. No remote model call was
made in this stage. `stage0/investigation.py` is a run artifact bound to the existing single-action
planner, tool runtime, evidence store and safety boundaries — not a new framework.

## What was built

- `stage0/investigation.py` (`investigation@1`, contract `medication-evidence-review@1`):
  serializable state — goal, patient version, claims, gap list (`patient_fact_missing` /
  `evidence_missing` / `evidence_conflict` / `tool_failure` / `source_invalid`), per-claim
  assessments with `source_status`/`condition_status`/`time_status`, queries, read refs, content
  hashes, no-progress counter and termination reason. `restore()` rejects foreign versions
  (migration required), foreign scopes and contract coverage mismatches.
- Gap linkage for planner proposals: `ToolAction` carries `gap_id` + `expected_observation`;
  `proposal_errors()` (enforced in `PlannerPolicyGuard`) rejects responding before termination,
  forged gap ids, tools outside `{memory_read, rag_search, read_evidence, ask_clarification,
  ddi_check}`, reading evidence never observed in scope, clarifying without a recorded missing
  fact, and questions that do not match a `patient_fact_missing` gap. `memory_write` proposals
  still go through the existing write policy/receipt guard. `authority` gap can only be resolved
  by a full `memory_read` snapshot. Completion cannot be fabricated: `decision: respond` before a
  code-owned termination reason is rejected.
- Progress semantics (`observe()`): tool output counts only after a scope- and hash-checked
  re-read through the authorised `EvidenceStore`; duplicate chunks or repeated queries are
  **no progress** (bounded stop), unknown tool-visible references cannot join the investigation.
  Conflicts cannot be resolved by user choice or votes — they open an `evidence_conflict` gap and
  stop at `waiting_review`.
- Hard rules in `evidence_quality.assess_claim`: source not current, incomplete content,
  unverified date, missing/wrong entity, unknown applicability, negation mismatch and non-exact
  citation each force `insufficient`/`contradicted`; supported+contradicted coexisting keeps the
  claim `insufficient` (opposing evidence is never swallowed). No model semantic check was added,
  so no extra flag is needed; model self-reported confidence does not exist in this path.
- Bounded stop reasons: `checks_completed`, `waiting_input`, `waiting_review`, `no_progress`,
  `budget_insufficient`, `unrecoverable_failure`, `cancelled`. `unknown`/`insufficient` claims and
  open gaps always enter the final bounded report; the report states insufficient ≠ no risk.
- Context assembly: `planner_view()` keeps goal, authority versions, claim/gap/conflict state and
  relevant semantic facts; evidence bodies are read on demand via `read_evidence`. The full
  authoritative snapshot is always read outside model context (`authority_validation` marker).
- Integration points: `agent.py` (`_prepare_investigation`/`_decide`/`_reflect`/`_respond`,
  opt-in via `AGENT_INVESTIGATION_ENABLED=1`, legacy path unchanged when off; old checkpoints
  without `investigation_policy` stay legacy), `graph_runner.py` (checkpoint round-trip restores
  `investigation@1` or refuses with a migration error), answer bundle gains additive
  `execution_status`/`goal_status`/`answer_status`/`investigation` fields. The final response is
  a template projected from the same investigation state the bundle serializes — text, card and
  structured status share one source, all passing the existing final response check.
- Frontend: `InvestigationCard.tsx` renders checked scope, open gaps, termination reason and
  evidence read-back (existing `EvidenceDrawer`); `AssistantPage.tsx` mounts it for restored and
  live turns and de-duplicates restored vs live tasks by idempotency key.
- Evals: `stage0/agent_evals/run_eval.py --policy gap` scores the A0 rubric against the
  investigation state (question recall with denominators, invalid questions, false completion,
  duplicate effects, model/tool calls, latency, tokens, unknown usage).

## Actual commands and results (PowerShell, repo root)

```powershell
.venv/Scripts/python.exe -m unittest stage0.test_agent_investigation -v
#   Ran 9 tests ... OK
.venv/Scripts/python.exe -m unittest stage0.test_live_regressions stage0.test_stage8_agent
#   stage0.test_harness_p0..p3 stage0.test_memory_p0..p2 stage0.test_reliability_p0..p2
#   → Ran 205 tests ... OK
.venv/Scripts/python.exe -m unittest stage0.test_product_tasks stage0.test_product_evals
#   stage0.test_product_evidence_quality stage0.test_product_reconciliation
#   stage0.test_frontend_read_models stage0.test_stage10_server stage0.test_product_closeout
#   → Ran 49 tests ... OK
.venv/Scripts/python.exe -m stage0.agent_evals.run_eval --policy gap --path replay --out docs/agent-capability-upgrade/A1/dev-replay-final.json
#   engineering_status: pass, summary {total: 13, passed: 13}
.venv/Scripts/python.exe -m stage0.agent_evals.run_eval --policy gap --path tools --out docs/agent-capability-upgrade/A1/tools-replay-final.json
#   engineering_status: pass, summary {total: 13, passed: 13}
cd frontend; npm run build   # tsc -b && vite build → built in 4.27s
```

## Same dev-set comparison vs A0 baseline (replay path)

| metric | A0 baseline | A1 gap policy |
|---|---|---|
| tasks passed | 0/13 | 13/13 (replay and tools paths) |
| model calls | 0 | 0 real; 4 scripted fault-injection attempts in the `model_rate_limit` family (counting added after `dev-replay.json` was captured — per-task scores identical, see `dev-replay-final.json`) |
| tool calls | 30 | 57 (targeted reads/searches driven by gaps) |
| latency | p50 ≈ 66 ms | p50 ≈ 67–90 ms (deterministic local tools) |
| real tokens | 0 | 0 |

Acceptance behaviours verified by tests: already-known facts are not re-asked (question set =
missing fields only); missing key facts park the run at `waiting_input` with concrete field
questions; contradicting evidence keeps claims `insufficient` and stops at `waiting_review`;
repeated query/no-new-chunks stops at `no_progress`; tool failure or budget exhaustion preserves
existing warnings and never claims a complete check; crash after domain commit resumes from
checkpoint without resetting budget or duplicating receipts; a patient-version change invalidates
assessments and forces a fresh full authority read.

## Availability distinctions

- `engineering_status: pass` — deterministic replay + real local RAG adapter over the synthetic
  corpus, negative control still fails (scorer can fail, no missing-data pass).
- `real_model_quality: unavailable` — no authorized provider run in this stage; the scripted
  planner path (`test_model_script_uses_gap_links_but_cannot_fabricate_completion`) is engineering
  replay, not model quality.
- `independent_held_out: unavailable` — development set is author-synthetic; collection/sealing
  protocol is defined in A0.

## Limitations / next-stage notes

- Investigation is a per-run artifact; cross-session persistence and open-task contracts are A2.
- Coverage is bounded to ≤12 claim pairs, 3 searches, 12 evidence chunks per run (limits are
  recorded as gaps, not silent drops).
- Browser walkthrough of the card is exercised at the type/build level here; the full scripted
  browser demo (start → wait → restart → fact correction → resume → evidence read-back) is the A5
  deliverable.
