# 用药协管员：可审计的家庭用药记忆

**记住父母健康史、主动预警用药冲突、全程可审计的“用药协管员”。**

## What it is

This repository is a single-caregiver, single-patient engineering demonstration. It turns Chinese medicine-label evidence and caregiver events into a durable SQLite record: patient facts, current medication state, change events, warnings, citations, and unresolved conflicts. The default path is deterministic and offline-first, so the UI and scripted demo can be replayed without an LLM or network access.

## What it is not

It is **not a medical device, diagnostic system, prescriber, or clinical decision-support service**. It does not diagnose, prescribe, or tell a person to start, stop, or change a medicine. Severe, uncertain, uncited, and conflicting results are explicitly escalated to a doctor or pharmacist. The metrics below are software-engineering measurements, not clinical validation or evidence of patient-outcome safety.

## Architecture

```mermaid
flowchart LR
    A["MNBVC 中文药品说明书"] --> B["parse<br/>结构化章节与引用"]
    B --> C["Hybrid RAG<br/>BM25 + BGE"]
    C --> D["DDI engine"]
    K["KEGG fast path<br/>结构化相互作用"] --> D
    C --> F["RAG / LLM fallback<br/>精确中文摘录门槛"]
    F --> D
    D --> M["Three-layer memory<br/>SQLite: semantic / episodic / working"]
    M --> AG["Agentic loop (Stage 6)<br/>LLM decides every cycle · code enforces safety only"]
    AG --> S["Safety boundary<br/>拒绝诊断/处方 · 强制升级"]
    S --> UI["Streamlit UI<br/>单照护者 / 单患者"]
    UI -->|care event| AG
    E["Evaluation loop<br/>regression replay + held-out baseline"] -. "回归保护与缺口反馈" .-> D
    E -. "记忆/代理行为检查" .-> AG
```

## Pipeline walkthrough

1. **Data → parse.** `stage0/` keeps the MNBVC-derived label corpus and the existing parser. Relevant Chinese sections are chunked with source metadata.
2. **Parse → hybrid RAG.** `rag.py` combines local BM25 and BGE/FAISS retrieval. A degraded exact-token path is available when the embedding runtime is unavailable.
3. **RAG → DDI engine.** `ddi_engine.detect()` normalizes Chinese brand/generic names, expands mapped compounds and enumerates ingredient pairs. KEGG is the fast corroboration path; grounded RAG/LLM extraction is a bounded fallback. A warning is not accepted as cited evidence unless its quote is an exact substring of a retrieved Chinese chunk.
4. **DDI → memory.** `MemoryStore` writes patient facts, medication versions, warning episodes, conclusions, and audit rows. A conflict links both sides without silently choosing one.
5. **Memory → agent.** `MedicationCoordinatorAgent.handle(CareEvent)` runs one LLM decision per plan → act → observe → reflect cycle when `--llm-planner` is enabled. The model sees the event, patient snapshot, tool schemas, observations and trace, chooses its tool and free arguments, and decides when to answer. `ResponseComposer` writes the final text from actual warnings, conflicts and memory references. The default remains offline and deterministic. In LLM mode the deterministic planner runs only after provider/parse failure. Schema or safety rejection is recorded and sent back to the LLM on the next cycle, without selecting a replacement action.
6. **Agent → UI.** `stage0/app.py` calls that real agent and real `MemoryStore`; it does not duplicate detection or memory logic. The UI exposes the current ledger, citations, audit references, and a cross-session query.

## Why exact recall instead of embeddings for memory?

Embeddings are useful for finding label evidence, but they are the wrong source of truth for safety-critical patient facts. The memory layer therefore uses exact SQLite keys and immutable versions for age, allergies, renal/hepatic function, chronic disease, preferences, and medication records. Exact `(namespace, key)` recall gives deterministic “what is currently recorded?” answers, stable references such as `memory:semantic:3@v1`, and a clear audit trail. Time decay only changes episodic retrieval ranking; it never deletes the underlying event. When two safety-critical facts disagree, `conflicts` stores both sides as open instead of allowing a similarity score to erase history.

## The agent loop and safety boundary

The loop is deliberately event-driven rather than a fixed chatbot script:

```text
CareEvent
  → plan one tool
  → act and observe
  → reflect on confidence, citations, failures, and contradictions
  → re-plan or respond through SafetyBoundary
```

Medication add/remove/dose-change events trigger a real DDI check against the current list. Patient facts can trigger a local label search for allergy, age, renal, or hepatic cautions. Low-confidence class inference triggers another retrieval goal and remains labelled as inference. Procedure exposure can create an explicit conflict between a reported clinical action and a label warning. The boundary rejects diagnosis/prescribing requests, rejects uncited warnings, and appends “建议咨询医生/药师” for severe, unknown, low-confidence, failed-tool, or conflict cases.

Stage 6 uses one `CANONICAL_PROPOSAL_SCHEMA` in the function definition, prompt payload and validator. `decision` selects `tool` or `respond`; a tool decision supplies its registered name and schema arguments. Extra fields are ignored and rationale/purpose are optional annotations. Reads, DDI checks and searches have no required policy order; the LLM also receives the memory snapshot directly. The guard protects consolidation before an answer, real observed-warning provenance (from `ddi_check` or `rag_search` observations) before warning persistence, grounded conflict links and user-visible clarification safety. It hydrates safety-critical evidence and the current medication list from actual memory, recording argument corrections. There is no code-generated operational plan in the model payload.

`response_safety.py` checks the composed text for Chinese/English diagnostic or medication directives, invented citations/references, missing warning provenance, missing conflict sides and required escalation. Each warning must keep its recorded effect, citation and memory reference together. Rejected composition switches to a logged template, which is also checked in LLM mode. Clarifications carry the same warnings/conflicts through this path. The unchanged `SafetyBoundary` remains the last gate. These finite text checks and four synthetic scenarios are engineering evidence, not a universal guarantee of natural-language clinical safety.

`--llm-planner` is an explicit third-party-data opt-in. The default mode is local and deterministic. Planner payloads are bounded and omit credentials; API keys remain in the gitignored `stage0/.env`. The selected provider still receives the current CareEvent and the minimum recent state needed to plan, which can contain patient information, so do not enable this flag unless that disclosure is acceptable for the configured provider.

## Evaluation

The table intentionally reports **both** the reproducible regression replay and the isolated held-out baseline. The regression numbers are **not generalization accuracy**. Both rows are a **small single-evaluator engineering baseline, not clinical validation**.

| Evaluation slice | Scope | Precision | Recall | F1 | Severity accuracy | High-risk severity accuracy | Chinese citation coverage |
|---|---|---:|---:|---:|---:|---:|---:|
| Regression replay (**not generalization**) | 26 replay lists; 41 gold pair occurrences; persisted KEGG/RAG evidence | 1.0000 | 1.0000 | 1.0000 | 1.0000 | 6/6 required severe pairs flagged | 1.0000 |
| Held-out live baseline | 26 isolated lists; leakage audit passed; 15 TP / 0 FP / 5 FN | 1.0000 | 0.7500 | 0.8571 | 0.6667 | 0.4444 | 0.4000 |

The held-out result is the honest product signal: precision is strong in this small sample, but the recall gap is concentrated in theophylline-class pairs; KEGG-only `P` defaults to moderate and causes severity errors; and nine of fifteen warnings lack a Chinese citation. Do not quote the regression `1.0000` as accuracy. Full artifacts remain in `stage0/data/structured/engine_regression_metrics.json` and `stage0/data/structured/engine_heldout_metrics.json`.

The checked-in Stage 5 artifact is the explicit live TokenDance `glm-5.3-flash` run over four demo outcomes with deterministic DDI/RAG fixtures. It records 30 attempts: 11 accepted, 17 schema-rejected, and two malformed-JSON/protocol errors. Fifteen of 23 action cycles used same-cycle deterministic fallback (65.22%); no unsafe proposal reached the executor, one cycle-aligned safe divergent action was accepted, there were no extra cycles or clarifications, and final outcomes agreed 4/4 with the deterministic baseline. The fallback rate is an honest model-quality limitation. The separate offline fake-provider run observed 30/30 accepted, zero fallback, eight cycle-aligned safe divergences, zero unsafe execution, and the same 4/4 agreement; those offline numbers verify wiring rather than model quality. Both modes are software behavior evaluation, not clinical validation. See `stage0/data/structured/planner_eval_metrics.json`.

The current Stage 6 artifact comes from a fresh live TokenDance `glm-5.3-flash` pass of the final revision: **4/4 outcomes live, 0 unsafe executor actions, 0 unsafe delivered texts**, 16/20 accepted proposals with 4 provider-timeout emergency fallbacks (**0.2000 of all cycles, exactly at the target boundary**), and worse efficiency (**20 vs 17 cycles; median 5.5 vs 4.5**). All four live compositions were initially rejected by the response checker; inspection showed false positives (benign disclaimers, conflict-side summaries, one negated refusal), the checker was corrected with the concrete-hazard rule intact, and a zero-call replay of the same recorded outputs then accepted **3/4 compositions** with all delivered texts re-verified clean. The honest weak spot: this live run yielded only **1 genuine safe divergent tool sequence** against the ≥2 qualitative target (an earlier recorded run, retained as `agentic_eval_live_before_output_fix.json`, yielded 3). Raw online/replay responses, provenance, and side-by-side sequences are in [the metrics](stage0/data/structured/agentic_eval_metrics.json) and [the Stage 6 report](stage0/REPORT.md#76-stage-6--llm-decisions-and-response-composition-safety-only-enforcement). **GO for controlled engineering evaluation; NO-GO for product/clinical use or for treating single-run fallback, divergence, and composition counts as stable model quality.**

## Business value / who pays

The first buyer is the adult child paying for a parent’s safer, less fragmented medication history (C端). A later B2B extension could package the same auditable workflow for养老机构 and insurers: medication reconciliation, handoff evidence, unresolved-conflict visibility, and a review queue. That is a product hypothesis, not current deployment scope; this demo has no authentication, multi-tenancy, cloud service, or clinical governance.

## Setup and run

Use Python 3.11+ from the repository root:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r stage0/requirements-stage1.txt

# Deterministic, offline-first interactive UI.
streamlit run stage0/app.py

# Optional structured fact extraction. Streamlit passes app arguments after --.
streamlit run stage0/app.py -- --llm

# Separately opt in to LLM planning with deterministic validation/fallback.
streamlit run stage0/app.py -- --llm-planner

# Scripted two-session walkthrough.
python stage0/demo.py --reset

# Same walkthrough with LLM-first planning. Provider failure safely falls back.
python stage0/demo.py --reset --llm-planner
```

In the UI, **清空并重新开始** removes only `stage0/memory.db` and its SQLite sidecars. **开启新会话（保留记忆）** closes and reopens the database with a fresh agent, which makes cross-session recall visible. The app starts with the real local agent and memory; it does not require an API key in its default mode. See [DEMO.md](DEMO.md) for a ≤3-minute click-by-click run.

The existing Stage 0/1/2 reproduction commands, data lineage, and detector details remain in [stage0/REPORT.md](stage0/REPORT.md). The current offline Stage 3 and Stage 6 behavior/safety tests can be run with:

```powershell
python -m unittest stage0.test_stage3 stage0.test_stage6

# Rebuild a separate network-free fixture artifact.
python stage0/test_stage6.py --evaluate-offline --metrics stage0/data/structured/agentic_eval_offline.json

# Replay the saved live run with no new model calls.
python stage0/test_stage6.py --replay-live stage0/data/structured/agentic_eval_live_before_output_fix.json

# Explicit live evaluation; writes agentic_eval_metrics.json and raw traces.
python stage0/test_stage6.py --evaluate-live
```

## Known limitations

- The held-out labels and controls are small and single-evaluator; they are not clinical validation.
- KEGG is corroborative, not authoritative Chinese-label evidence.
- Source freshness and label coverage are not guaranteed; the curated local corpus is an MNBVC-derived sample.
- The held-out recall gap is concentrated in theophylline-class pairs (`茶碱`).
- KEGG `P` interactions default to `moderate` when stronger severity evidence is absent, producing the observed high-risk severity gap.
- Class inference (for example aspirin × ibuprofen) is deliberately low-confidence and escalated, not presented as a direct label claim.
- Stage 6 planning sat exactly at the 0.20 fallback target in the fresh live pass (4 provider timeouts in 20 cycles) and took 3 extra cycles. The 3/4 accepted compositions come from a zero-call replay after one checker correction; only 1 live scenario qualified as a safe divergent tool order against the ≥2 target. Single-run counts are not stable model-quality estimates.
- The UI is intentionally one caregiver/one patient, with no auth, tenancy isolation, clinical review workflow, or cloud persistence.
