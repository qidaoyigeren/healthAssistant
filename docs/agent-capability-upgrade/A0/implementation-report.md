> Historical stage report. Current implementation and acceptance: [2026-09-10 closeout](../closeout-2026-09-10/implementation-report.md). Original observations below are retained; they are not the latest status.

# A0 implementation report

Completed the baseline protocol and runnable isolated evaluation before changing
Agent decisions. See [protocol](../../../..//stage0/agent_evals/README.md), frozen
source/config and per-task baseline JSON in this directory. The source archive
contains current Python files including pre-existing working changes; git HEAD
alone would not reproduce that implementation. No formal patient data changed.

Actual commands (PowerShell, repository root):

```powershell
.venv/Scripts/python.exe -m stage0.agent_evals.run_eval --freeze --policy baseline --out docs/agent-capability-upgrade/A0/baseline-replay.json
.venv/Scripts/python.exe -m stage0.agent_evals.run_eval --policy baseline --path tools --out docs/agent-capability-upgrade/A0/baseline-tools.json
.venv/Scripts/python.exe -m stage0.agent_evals.run_eval --policy baseline --negative-control --out docs/agent-capability-upgrade/A0/negative-control.json
```

13/13 new-contract tasks fail on the old implementation in each path (exit 1);
the baseline predominantly clarifies intent instead of delivering an evidence
coverage report. All responses, observations and failure categories are retained.
The negative control fails, as required. Baseline capture is complete; baseline
task quality is **fail**, not an engineering regression in pre-existing features.

Existing evidence capture, traces, scoped operation receipts, budget and bundle
are reused. Execution/goal/answer projection is additive to the evaluation and
will be populated from the A1 bundle; unknown legacy goals remain unknown.

Read the latest live reports before implementation: TokenDance had timeout and
response-validation failures; the subsequent authorized Zhipu glm-4.7-flash run
had 429/1305. Its business `succeeded` was explicitly not LLM-answer success.
No new remote run has been executed here. Real model quality, provider health
and independently collected Agent held-out are unavailable. Existing DDI
held-out is present; product held-out is a different unavailable partition.

Limitations: tools mode uses the actual local fallback on synthetic labels,
not embeddings/online search; the restart development scenario reopens between
events. Mid-run crash recovery requires the A1 integration tests. These are
engineering comparisons, not clinical validation or measured model gains.
