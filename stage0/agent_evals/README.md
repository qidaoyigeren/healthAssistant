# Agent decision protocol @1

`dev.json` is 13 author-written synthetic development scenarios, one per family.
It is exposed development data, not independent held-out or clinical review.
Existing DDI held-out (`stage0/data/engine_heldout_cases.jsonl`) remains distinct
from memory, Harness and product evaluations; an empty product held-out says
nothing about DDI data availability.

Each task has `schema_version`, stable `task_id`/`family_id`, `events` (ordered
input events and restart controls), `initial_state`, `materials` with provenance,
`material_source_family`, `expected_gaps`, `required_artifacts`, allowed wait and
terminal states, prohibited outcomes and bounded resources. No unique tool order
is required. Missing names, units and dates are distinct families. Restart here
means reopening the database between interactions; mid-run checkpoint recovery
is additionally a backend invariant test, not inferred from this scenario.

Run from the repository root with `.venv/Scripts/python.exe -m
stage0.agent_evals.run_eval --policy baseline --path replay --out <new.json>`.
Use `--policy gap` for the A1 implementation and `--path tools` for actual local
exact RAG over the same isolated synthetic corpus. Replay fixes tool results;
tools runs production `RAGTool` with its supported local fallback. Neither is a
real-model evaluation or online corpus/search validation. Injected provider 429
is scripted; its usage is not real tokens. All patient stores are temporary.

Baseline application Python source is frozen in A0/baseline-source.zip, including
pre-existing worktree fixes, with revision, source/archive/data hashes in
baseline-config.json. Baseline runs extract this source into a temporary root,
copy the common current evaluator and frozen inputs, and use the same installed
environment. No patient DB, credentials or model cache is archived. Save every
comparison into a new output file. Never re-freeze over the original baseline.

Scores: necessary-question recall is matched required fields / required fields;
zero denominator is not applicable. Invalid questions are fields asked outside
the annotated necessary set. Claim support is supported claims with references /
asserted supported claims (engineering provenance metric, not independent human
entailment). False completion is completion with annotated unresolved conditions.
Final execution, goal and answer states are separate. Tool calls count dispatches;
provider attempts, actual token usage, unknown usage, latency and degradation are
reported separately. Recovery is null where no checkpoint-recovery assertion was
executed. Duplicate receipts are measured from persisted scope/operation keys.
Per-task errors fail; empty/missing independent data is unavailable. A baseline
failure to meet the new contract is preserved, never converted into a pass.

Independent held-out protocol: a separate collector obtains new scenario
families AND new material-source families with consent and deidentification;
paraphrases of this dev set do not qualify. Record source lineage, collector,
collection date, partition keys, schema version and adjudicated outcomes. Seal a
content hash manifest before development access; keep labels access-controlled.
Declare sample count, repeats and resource caps before running. Reveal once;
after examining outcomes or changing implementation, mark exposed and collect a
fresh independent batch for future blind claims. No such batch is available.
