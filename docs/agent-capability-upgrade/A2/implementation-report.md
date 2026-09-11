> Historical stage report. Current implementation and acceptance: [2026-09-10 closeout](../closeout-2026-09-10/implementation-report.md). Original observations below are retained; they are not the latest status.

# A2 implementation report — 开放目标的持久化与增量重规划

Status: engineering acceptance **pass** (6/6 new open-task tests, full backend sweep
252+31 tests OK, frontend tsc+build OK). Real model quality / provider availability /
independent held-out: **unavailable** — no remote model call in this stage; the new
contract runs on the deterministic gap-driven planner and is honest about it
(`investigation.mode = deterministic/scripted/llm`).

## What was built

New bounded open contract `evidence_review@1` (first version deliberately narrow:
evidence review **around existing patient records and materials**, producing a bounded
report plus questions to confirm — not a generic arbitrary-goal platform):

- `stage0/care_tasks.py`
  - `CONTRACTS['evidence_review']` v1: outputs `['investigation_report']`, allowed
    tools (all pre-existing), `max_steps: 12`; task budget defaults to the contract.
  - Task persistence adds: `goal` (validated 4–300 chars), `input_versions`
    (medications/semantic/materials revisions), `investigation` (serialized
    `investigation@1` state), `subgoals`, `additional_questions`, `invalidations`,
    `partial_report_refs`. Subgoals are a code-derived projection (authority read →
    per-claim evidence checks, deps only on `authority`, acyclic by construction);
    user/model-proposed questions become `user_question` subgoals with status
    `recorded` — **code cannot complete a free-text question**, so it stays in the
    report's open-items section. Proposal validation: cap `MAX_CLAIMS`, dedupe,
    2–80 chars.
  - `_execute_evidence_review`: freshness check per scope → selective invalidation
    policy (below) → `agent.run_open_review(...)` bounded by the contract →
    persist an `investigation_report` artifact (partial unless `checks_completed`)
    → status mapping: `waiting_input` (with per-gap structured questions),
    `waiting_review`, `completed` (requires the artifact persisted — completion is
    code-controlled; a report that says "insufficient" is NOT a completed review),
    `cancelled`, `failed` (budget/no_progress/unrecoverable — partial report retained,
    effects retained).
  - `record_input` (`POST /v1/care-tasks/{id}/input`): structured supplement through
    existing controlled write paths only — medications via
    `memory.apply_medication_change` (idempotent `add`), semantic facts via
    `write_semantic_fact` with `source='caregiver-input'` and a namespace allow-list
    (recorded as reported; user confirmation is a record, never a clinical approval).
    Text questions validated before any domain write so a failed question cannot
    leave committed writes behind.
  - `GET /v1/investigation-reports/{id}` for report read-back.
- `stage0/agent.py` — `run_open_review(goal, run_id, scope_id, initial_state, max_cycles)`:
  the bounded plan→act→observe→reflect loop for a persisted task, reusing the SAME
  `_decide` (investigation + planner policy guard), `_act` (shared executor,
  evidence capture, receipts) and `_reflect`; no new tool. Budget runs under
  `budget_scope` on a `care-task:*` workflow run registered in the existing run
  ledger; per-cycle budget gate; cancellation via the existing `cancel_event_for`;
  run status mapped into `workflow_runs`.
- `stage0/investigation.py` — selective invalidation: `medications` change ⇒ full
  reset (claims derive from the medication list); semantic-only change ⇒ assessments
  and read refs invalidated (applicability must be re-verified) while collected
  evidence, content hashes and past **searches are reused**; materials change ⇒
  source revalidation on restore. Supplemented facts close their recorded
  `patient_fact_missing` gaps (no re-asking an answered question) and
  `condition:<claim>` gaps close when re-verified. Applicability v1 is lexical and
  conservative: a chunk stating a population condition stays `insufficient` until a
  matching fact is recorded **in the versioned fact store** — never by model assertion.
- `stage0/server.py` — `POST /v1/events` accepts optional explicit `task_id`
  (validated: exists, `evidence_review`, `waiting_input`; recorded with the expected
  revision). The worker continues **exactly that named task** after the turn
  (`_auto_resume_care_task`): no guessing among multiple waiting tasks, stale
  expected revisions are skipped, and the resume goes through the same revision +
  spend checks as a manual continue (budget never resets).

## Selective invalidation policy (recorded per task in `invalidations`)

| change during wait | effect |
|---|---|
| `medications` | full re-check (claims/assessments/gaps/evidence reads/searches reset) |
| `semantic` | applicability re-verified against corrected fact; evidence bodies re-read; searches and collected evidence reused |
| `materials` | source revalidation; unchanged facts reused |
| undecidable dependency | conservative re-check with the reason recorded |

## Actual commands and results

```powershell
.venv/Scripts/python.exe -m unittest stage0.test_agent_open_tasks -v        # 6/6 OK
.venv/Scripts/python.exe -m unittest stage0.test_agent_investigation ...    # backend sweep, 252 tests OK
.venv/Scripts/python.exe -m unittest stage0.test_stage10_server stage0.test_product_closeout
#   stage0.test_product_tasks — 31 tests OK (after fixing the EventIn.task_id plumbing)
cd frontend; npm run build   # tsc -b && vite build OK
```

Acceptance demonstrations (all in `stage0/test_agent_open_tasks.py`):

- Open task waits with concrete per-gap questions; fresh service instances over the
  same SQLite file continue the same task (restart = new `CareTasks`/`ProductStore`).
- Semantic correction during wait → selective invalidation, evidence/search reuse,
  completion after incremental re-check; medication changes close the task
  (completed is terminal — a new goal needs a new task).
- Duplicate submit replays the receipt with no double spend or extra run; budget and
  child-run accounting accumulate across runs (`resource_budget` over `child_run_ids`).
- Budget exhaustion → honest `failed` with retained partial report; never completed.
- Cancel while waiting is terminal, effects retained; input after cancel rejected.
- Worker auto-resume honours only the named task at the expected revision.

## Crash-safety statement

The executor itself only reads and saves artifacts inside the command transaction;
the only domain effects are (a) caregiver input writes through idempotent
`apply_medication_change` / receipted `record_input`, and (b) agent `record_warnings`
proposals through the existing write policy + receipts. A crash after those domain
commits but before task save leaves no task-side duplication on retry (receipt
replay / idempotent add); the graph-level crash-after-commit test (A1) covers
checkpoint-side budget preservation.

## Availability distinctions

- `engineering_status: pass` — deterministic planner; scripted/LLM modes only change
  `investigation.mode`, never the write/verification path.
- `real_model_quality: unavailable`; `independent_held_out: unavailable` — the
  development demonstrations are synthetic; no clinical validation.
- A4 not executed here; multi-run orchestration remains single-agent.

## Limitations

- `evidence_review` v1 covers medication evidence review only; the contract check
  rejects anything else by design.
- `user_question` subgoals are recorded and reported but never auto-completed by code.
- Semantic-change invalidation is conservative (applicability always re-verified);
  "unrelated fact" reuse is at the evidence/search level, not the assessment level.
