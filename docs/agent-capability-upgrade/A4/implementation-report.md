> Historical stage report. Current implementation and acceptance: [2026-09-10 closeout](../closeout-2026-09-10/implementation-report.md). Original observations below are retained; they are not the latest status.

# A4 implementation report — 受限多 Agent 实验

Status: engineering infrastructure **pass** (4/4 multi-agent tests; A1–A3 suites and
batch-1 sweeps OK). **Real multi-agent value is unavailable**: no model provider was
authorized for this run, so the shipped workers are deterministic read-only pipelines
carrying honest labels — they do NOT demonstrate autonomous-agent gains, and no
A/B/C adopt recommendation can be made from this stage.

**Default-state change (user-directed, 2026-09-09)**: the two-role review is now
**default ON** (`multi_review_enabled()` defaults true) and only runs when its
explainable trigger holds (open evidence conflict / wide claim set). It remains
closeable via `AGENT_MULTI_REVIEW_ENABLED=0`. Verified: 27 A1–A4 tests OK,
dev set unchanged 13/13, backend sweep 205 OK. This changes cost/coverage only —
the write path, labels and divergence semantics are unchanged.

## What was built

`stage0/harness/multi_agent.py` (`multi-agent-review@1`), on top of the Harness P3
delegation contract:

- **Two fixed roles only**: `evidence_researcher` (retrieval proposals, bounded
  steps, own stop reason `bounded_steps`/`exhausted_candidates`) and
  `evidence_checker` (independent RAW re-read of evidence — receives the claim,
  fact context and refs, never the researcher's argument — and applies the hard
  lexical rules). Every result carries `worker_kind` and the ACTUAL model used
  (`deterministic` / `deterministic-hard-rules` here). A model-worker mode is
  deliberately NOT wired: without an authorized provider it would be simulation,
  which the protocol forbids.
- **Coordinator = parent agent** (`agent.py` integration): runs only when
  `AGENT_MULTI_REVIEW_ENABLED=1` AND an explainable trigger holds
  (`open_evidence_conflict` or `wide_claim_set` ≥6 claims); otherwise the
  single-agent path is kept unchanged. Findings are attached additively as
  `answer_bundle.multi_review` (also in `run_open_review` results for care tasks);
  the investigation state itself is never modified through this path.
- **Scope cannot expand**: `_validate_refs` drops any worker-returned ref that is
  not in the parent's own observed scope with matching identity; dropped refs keep
  the claim's gap explicit. Workers have no write tools, no review rights, no
  recursion (P3 executor roles unchanged).
- **Divergences are data**: support+opposition coexisting or parent/checker
  disagreement is recorded per claim in `divergences` — never resolved by voting
  or confidence averaging.
- **One ledger**: per-worker usage (`cycles`, `calls`, `usage_unknown`) sums into
  `review.usage` on the same task result; per-review dispatch caps and a deadline
  bound the experiment; over-cap claims are reported `not_dispatched` with the
  reason instead of being silently skipped.
- **Frontend**: `InvestigationCard` shows independent-review findings, unresolved
  divergences and the read-only disclaimer — no invented expert identities, no
  internal dialogue.

## Actual commands and results

```powershell
.venv/Scripts/python.exe -m unittest stage0.test_multi_agent_review -v   # 4/4 OK
.venv/Scripts/python.exe -m unittest stage0.test_multi_agent_review stage0.test_agent_investigation stage0.test_agent_open_tasks stage0.test_agent_adaptive
#   → 27 tests OK
# batch-1 suites re-run individually: live_regressions, stage8_agent, harness_p0..p3,
#   memory_p0..p2, reliability_p0..p2 — all OK
cd frontend; npm run build   # tsc -b && vite build OK
```

Test coverage: default-off and no-trigger keep the single-agent path; the conflict
trigger runs both labelled roles and records the divergence; forged refs cannot
expand scope; deadline=0 reports every claim `not_dispatched` with the reason.

## Experiment status vs the A4 protocol

| protocol item | status |
|---|---|
| A (single agent) / B (B+batch) / C (B+model delegation) comparison | **unavailable** — requires authorized real-model workers |
| discovery of unsupported conclusions, key omissions, cost/latency | **unavailable** — no real runs |
| worker越权/伪造引用/注入/过期事实/超时/取消受控处理 | **pass (engineering)** — read-only roles, ref revalidation, caps, deadline, deterministic workers |
| adopt recommendation | **not made** — no quality evidence; the experiment stays closeable and off by default |

Scripted/deterministic workers demonstrate the controlled plumbing; per the A0/A1
honesty rules they are counted as engineering replay, never as multi-agent quality.

## Limitations

- Model workers (real tool selection + stop decisions inside the bounded budget)
  require a provider authorization decision before the experiment can run.
- Statistical independence of checker verdicts is not claimed — the checker shares
  the lexical rule implementation with the parent by design (hard rules are code).
