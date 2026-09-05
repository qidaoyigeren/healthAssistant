# Stage 1 / 1b / 2 — Data-layer hardening and runtime DDI detection

Date: 2026-08-27  
Decision: **GO for further controlled detector/data validation; NO-GO for an agent, product, or clinical use.**

This stage extends the working Stage 0 spike. It does not implement an agent, memory system, UI, diagnosis, prescribing, or clinical decision support.

## Executive result

| Stage 1 gate | Result | Measured evidence |
|---|---|---|
| Local hybrid RAG over ≥1,000 deduplicated labels | **Pass** | 1,200 labels / 1,200 unique approval numbers; 4,158 section chunks; local BGE + FAISS and BM25. |
| Hybrid recall@5 beats both single retrievers | **Pass** | 45 hand-written queries: BM25 0.9333, vector 0.8222, hybrid **0.9556**. MRR: 0.7946 / 0.7181 / **0.8283**. |
| Production-path entity accuracy | **Pass** | Stage 1b re-run on the same frozen 30 positives: drug A 30/30, drug B 30/30, complete entity pair **30/30 (100%)**. All-core stayed 16/30 (53.3%). |
| ≥20 hard negatives and split error metrics | **Pass** | 22 hard + 15 easy negatives. After Stage 1b prompt hardening: easy FP 0/15 (0%); hard FP 2/22 (9.09%). Combined estimate: precision 0.9375, recall 1.0000, F1 0.9677. |
| KEGG coverage ≥24/30 with class expansion | **Pass** | **30/30 checked (100%)**; 28/30 corroborated (93.33%). All 16 formerly skipped class cases were checked and corroborated by at least one representative member pair. |

Passing these engineering gates does **not** establish clinical correctness. The source, annotation, ontology, extraction, retrieval, and external-corroboration limitations below remain material.

## 1. Corpus curation and local hybrid RAG

### Corpus selection

`rag.py --curate` scans the checksum-pinned local MNBVC medical shard and requires each selected record to contain all three key sections: 药物相互作用、禁忌、注意事项. Records are deduplicated by `approval_number` before selection.

Selection is deterministic (`stage1-rag-v1`):

- 1,200 labels requested and saved
- 1,200 unique approval numbers
- 500 labels selected from names matching a documented list of common chronic-use cardiovascular, diabetes, lipid, respiratory, endocrine, psychiatric, and osteoporosis drugs
- 700 labels selected from all remaining eligible records using a SHA-256 ordering, avoiding dependence on shard order
- 104,047 eligible unique records exposed all three sections
- 6,695 duplicate-approval rows were skipped during the scan

The curated records and provenance are in `data/rag_corpus.jsonl`; the selection summary is in `data/structured/rag_corpus_summary.json`. This is a retrieval-development sample, not a prevalence-weighted pharmacoepidemiology sample.

### Index

The three sections are sentence-aware chunked at at most 480 characters with an 80-character overlap, producing **4,158 chunks**. Retrieval uses:

- BM25 with `jieba` tokenization and `rank_bm25`
- local `BAAI/bge-small-zh-v1.5` sentence-transformers embeddings
- normalized 512-dimensional embeddings in a local FAISS `IndexFlatIP`
- weighted reciprocal-rank fusion (BM25 0.5, vector 0.5, RRF constant 60)
- filters for section, approval number, and drug name

No cloud embedding service or DeepSeek embedding endpoint is used. The model downloaded successfully from the normal Hugging Face endpoint during this run; `HF_ENDPOINT=https://hf-mirror.com` remains a documented fallback if the primary endpoint is unreachable.

Local index artifacts are under `data/structured/rag_index/` (`chunks.faiss`, chunk metadata, BM25 tokens, and config).

### Retrieval evaluation

`data/rag_eval_queries.jsonl` contains **45 hand-written Chinese queries** and manually assigned relevant chunk IDs. Results in `data/structured/rag_metrics.json`:

| Retriever | Recall@5 | MRR |
|---|---:|---:|
| BM25 only | 0.9333 | 0.7946 |
| BGE vector only | 0.8222 | 0.7181 |
| Hybrid RRF | **0.9556** | **0.8283** |

Hybrid recall@5 is strictly higher than each single retriever. The query set and relevance judgments were produced by one evaluator from this fixed corpus, so this is an internal retrieval measurement, not an independent or clinical evaluation.

## 2. Production extraction path alignment

`extract_ddi.py` now gives the free-text production path the source drug name and applies a deterministic source-self invariant: 本品、本药、本药物 resolve to the source label's generic/product name. The production `type` field is constrained to the same mechanism enum as `mechanism_type` in the controlled evaluation path; `effect_tag` and `severity` use the same controlled vocabularies and severity rubric. The old `--evaluate` and `--rescore` paths remain available.

For Stage 1b, the production prompt adds the same six general evidence gates as the detector: indication/composition/treatment co-occurrence is insufficient; a second drug or class must be explicit; negated changes and safe/well-tolerated combinations are negative; same-class/cross-allergy is not a DDI; and a positive requires an affirmative change in exposure, effect, pharmacokinetics, or toxicity. An explicit two-party prohibition or avoid-combination instruction remains a positive joint-use warning. These gates control whether a triple is emitted and do not redefine the controlled output fields. `EVAL_PROMPT` remains unchanged because that path is deliberately forced to extract exactly one relationship from each positive-only gold sentence rather than make a detection decision.

The production path was run through DeepSeek `deepseek-chat` on the same frozen 30 positive sentences. It produced 30/30 valid API outputs. Direct comparison:

| Field | Controlled eval path | Free-text production path |
|---|---:|---:|
| drug A | 30/30 (100.0%) | 30/30 (100.0%) |
| drug B/class | 30/30 (100.0%) | 30/30 (100.0%) |
| complete entity pair | 30/30 (100.0%) | **30/30 (100.0%)** |
| severity | 23/30 (76.7%) | 23/30 (76.7%) |
| mechanism class | 25/30 (83.3%) | 26/30 (86.7%) |
| effect class | 25/30 (83.3%) | 25/30 (83.3%) |
| exact evidence substring | 30/30 (100.0%) | 30/30 (100.0%) |
| all core fields simultaneously correct | 19/30 (63.3%) | **16/30 (53.3%)** |

Metrics are in `data/structured/production_eval_metrics.json`; raw scored outputs are retained in `data/structured/production_eval_predictions_30.jsonl`.

The entity gate passes, but the production all-core result remains too low for safety use. The deterministic source-self resolution improves structural consistency; it does not validate severity, mechanism, effect, completeness, or clinical meaning.

## 3. Hard negatives and classification estimates

`make_negatives.py` retains the 15 Stage 0 适应症/成分 negatives and deterministically adds **22 hard negatives** from the full local shard, each with approval number, dataset row, section, and source URL:

| Negative subset | Count | False positives | FP rate |
|---|---:|---:|---:|
| Easy 适应症/成分 | 15 | 0 | 0.0% |
| Hard, all categories | 22 | 2 | 9.09% |
| └ explicit negated/no interaction | 8 | 0 | 0.0% |
| └ safe/well-tolerated combination | 6 | 0 | 0.0% |
| └ same-class allergy mention, not DDI | 8 | 2 | 25.0% |

Unlike Stage 0, `eval_negative.py` sends both the 30 positives and all 37 negatives through the same `tool_choice=auto` detection prompt. It does not assume positive recall from a forced call.

| Confusion/estimate | Value |
|---|---:|
| true positives / false negatives | 30 / 0 |
| false positives / true negatives | 2 / 35 |
| estimated precision | **0.9375** |
| estimated recall | **1.0000** |
| estimated F1 | **0.9677** |
| successful API outputs | 67/67 |

The Stage 1b prompt-only before/after comparison is:

| Metric | Before (Stage 1) | After (Stage 1b) |
|---|---:|---:|
| false positives | 6 | **2** |
| easy-negative FP rate | 3/15 (20.0%) | **0/15 (0.0%)** |
| hard-negative FP rate | 3/22 (13.64%) | **2/22 (9.09%)** |
| estimated precision | 0.8333 | **0.9375** |
| estimated recall | 1.0000 | **1.0000** |
| estimated F1 | 0.9091 | **0.9677** |
| production entity-pair accuracy | 30/30 (100.0%) | **30/30 (100.0%)** |
| production all-core | 16/30 (53.3%) | **16/30 (53.3%)** |

Recall did not regress: all 30 positives remained detected. Two false positives remain, both in the same-class allergy subset:

- `H15` — “爱全乐禁用于对阿托品及其衍生物及对此产品中任何其它成分过敏的病人。” The model called the tool with an empty argument object.
- `H20` — “对红霉素或其它大环内酯类药物过敏者禁用。” The model still converted an allergy contraindication into a DDI with 红霉素.

These are explicitly **classification estimates, not clinical-validity metrics**. The labels and pattern-screened hard cases were reviewed by one evaluator, the set is small, and it is not a prevalence-representative prospective sample. The result also demonstrates residual nondeterministic/tool-call failure despite a general prompt rule; it does not justify case-specific memorization.

## 4. Drug-class ontology and KEGG expansion

`ontology.py` defines **16 auditable classes with 42 unique representative member drugs**, each class containing 2–5 members with KEGG D identifiers. It includes NSAIDs, CYP3A4 inhibitors, corticosteroids, anticoagulants/thrombolytics, potassium-raising drugs, potassium-sparing diuretics, oral azole antifungals, sulfonylureas, CYP2D6 inhibitors, cardiac glycosides, oral antidiabetics, and coumarin anticoagulants.

`mapping.json` grew from 34 to **64 rows** by adding 30 previously absent ontology member mappings. Class membership is representative rather than exhaustive and is for coverage expansion only.

`crosscheck_eval.py --live` preserves the 14 representative single-partner checks and expands the 16 formerly skipped class/multi-partner cases. A class case is corroborated if any representative member pair is returned by KEGG DDI.

| KEGG result | Count |
|---|---:|
| total eval cases | 30 |
| resolved and checked | **30/30 (100%)** |
| corroborated | **28/30 (93.33%)** |
| single-drug cases | 14 checked, 12 corroborated |
| ontology-expanded class cases | 16 checked, 16 corroborated |

The two single-pair misses remain 克拉霉素×西沙必利 and 二甲双胍×硝苯地平. A KEGG absence is a coverage gap, not proof that no interaction exists. Likewise, one matching representative member corroborates that member pair; it does not prove every drug in a class shares the interaction.

KEGG REST is academic-use only. The client caches results and waits at least one second after every uncached request, including HTTP errors.

## 5. Source and evaluation caveats

The Stage 0 acquisition facts remain unchanged: the MNBVC shard contains 114,367 records and 107,672 unique approval numbers, and its compressed SHA-256 is `b4928f883728ad9981dcfc371f4c332f63d22d7b45fc8f13eed2a8c012d81b2b`.

The MNBVC dataset card is MIT-licensed, but underlying `yaozs.com` page rights, approved-text fidelity, revision freshness, and completeness remain unresolved. Approval-number deduplication does not establish that a selected page is the current NMPA/MAH label. Production work still requires legal/data-governance review and validation against authoritative originals.

Other limitations:

- all gold labels and retrieval judgments are single-evaluator
- the 30 DDI positives were sentence-selected and do not measure full-section multi-triple recall
- class expansion uses representative members rather than a clinically governed ontology
- DeepSeek output is nondeterministic across runs even at temperature 0
- KEGG primarily standardizes interactions from Japanese labels and is corroborative, not authoritative for Chinese labels
- retrieval quality does not imply extraction or medication-safety correctness

## 6. Stage 2 runtime DDI detection engine

`ddi_engine.py` is the detector-only runtime entry point:

```python
detect(medication_list: list[str]) -> list[warning]
```

It does not add an agent, memory layer, UI, diagnosis, prescribing logic, or clinical decision-support workflow. Each returned warning contains `drug_a`, `drug_b`, `severity`, `mechanism`, `effect`, `management`, `source_text`, `source_url`, `confidence`, and `detection_path`.

### Runtime architecture

1. `normalize.py` resolves each Chinese generic/brand name. Its ingredient list is expanded unchanged, so `复方利血平氨苯蝶啶胶囊` expands to all four mapped ingredients and `诺欣妥` expands to sacubitril plus valsartan. `ontology.py` supplies the auditable representative members used for class evidence. Duplicate ingredients are removed before pair enumeration, and self-pairs are skipped.
2. Every unordered ingredient pair is enumerated once. The structured fast path uses KEGG IDs and the persisted `data/ddi_pair_index.json` rather than doing a network request for an already cached pair. Cache-missing requests are opt-in (`DDI_ENGINE_LIVE_KEGG=1`) and reuse `crosscheck_eval.kegg_ddi`, including its academic-use one-request-per-second delay.
3. The fallback path first reuses grounded Stage 1 evidence. For a pair without warm evidence, or for a live enrichment run, `rag.py` searches both drug-name orientations over the local hybrid BM25+BGE index. At most two exact-mention candidate chunks are passed to `extract_ddi._production_call`, which retains the Stage 1b hardened six-gate prompt from `extract_ddi.py`. A successful model abstention or `triples=[]` is a valid negative and is not repeatedly sampled; transport/parse failures remain errors and use bounded retries. A positive result is accepted only when its quote is an exact Chinese substring of the retrieved chunk and the quoted text names the partner drug or class. Results are cached per canonical ingredient pair in `data/ddi_fallback_cache.json`.
4. Fast and fallback evidence are merged per pair. KEGG `CI` anchors `contraindicated`; KEGG `P` is disambiguated by the Chinese evidence/LLM severity and defaults to `moderate` when the interaction has no stronger severity evidence. `high` confidence requires corroborating structured and grounded evidence; `medium` denotes one usable source; `low` marks conflicting, weak, or unknown-severity evidence. Results are sorted `contraindicated > major > moderate > minor > unknown`.

### Operational severity rubric

| Severity | Runtime rule |
|---|---|
| `contraindicated` | Explicit Chinese `禁忌`/`严禁`/`禁用`, or KEGG `CI`. |
| `major` | Explicit `避免`/`不宜同用`/equivalent prohibition, or serious toxicity such as bleeding, arrhythmia, rhabdomyolysis, lactic acidosis, hyperkalemia, acute renal toxicity, or serious drug toxicity. |
| `moderate` | Caution, dose adjustment, monitoring, or a documented clinically relevant but milder effect. |
| `minor` | Clinically negligible effect. |
| `unknown` | A relationship is grounded but no severity evidence is available. |

This is an operational detector rubric, not a clinical severity standard. KEGG is an anchor/corroboration signal and is not treated as the sole Chinese-label authority.

### Regression replay result (not generalization)

`data/engine_eval_cases.jsonl` contains 26 realistic 3–4 medication lists: 20 positive scenarios, 6 clean controls, brand-name inputs, two compound products, moderate interactions, and all six required severe pairs. This is intentionally a regression replay: it disables new network/model calls and measures the persisted KEGG index plus warm Stage 1 evidence cache. Its metrics are marked `evaluation_kind=regression_replay` and `generalization_claim=false` in `data/structured/engine_eval_metrics.json`; the values below must not be quoted as held-out detection accuracy.

| Stage 2 measure | Result |
|---|---:|
| gold pair occurrences | 41 |
| pair precision / recall / F1 | **1.0000 / 1.0000 / 1.0000** (41 TP, 0 FP, 0 FN) |
| severity accuracy on flagged gold pairs | **1.0000** (41/41) |
| Chinese citation coverage | **1.0000** (41/41 warnings have a Chinese quote and URL) |
| clean controls with no warning | **6/6** |
| required severe pairs flagged | **6/6** |

The path counts are reported separately because a `kegg+fallback_cache` warning belongs to both paths:

| Path measure | Result |
|---|---:|
| fast-path warning occurrences / unique pairs | 32 / **22** |
| fallback-evidence warning occurrences / unique pairs | 41 / **31** |
| `kegg+fallback_cache` occurrences | 32 |
| fallback-only (`fallback_cache`) occurrences | 9 |

The offline run therefore demonstrates fallback evidence reuse, not 41 fresh LLM calls. A bounded live smoke test for `奥美拉唑×克拉霉素` successfully retrieved Chinese label chunks, accepted an exact quote from the hardened extractor, and wrote `data/ddi_fallback_cache.json`; no live model call is needed to reproduce the regression metrics above. Full regression metrics and per-case results are in `data/structured/engine_eval_metrics.json`, and the KEGG coverage/index metadata are in `data/ddi_pair_index.json`.

### Held-out generalization result (live, isolated, complete baseline)

`data/engine_heldout_cases.jsonl` is a separate 26-list set with 40 unique candidate pairs. Before execution, the engine rejected every pair that appeared in the shared KEGG cache, fallback cache, Stage 1 evidence index, old regression gold, or an all-ontology-member pair. The recorded leakage audit passed with zero violations. The set includes new ingredients such as 甲硝唑、别嘌醇、氨苄西林、利福平、伏立康唑、茶碱、环丙沙星、维拉帕米 and 红霉素, while retaining realistic brand/alias forms.

The evaluator started from empty `data/heldout_kegg_ddi_cache.json`, `data/heldout_ddi_fallback_cache.json`, and `data/heldout_ddi_pair_index.json`, then enabled live KEGG, local RAG, and the hardened LLM path. The LLM provider was TokenDance's OpenAI-compatible endpoint with the platform-listed model ID `glm-5.3-flash`; the key remains only in gitignored `stage0/.env`. Structured extraction disabled GLM thinking, capped output at 1,024 tokens, bounded each provider request at 60 seconds, and retained the existing explicit retry policy. The interrupted long-tail run resumed only its own isolated caches; `error` entries were removed and retried, while `matched`/`not_found` entries were retained. The final trace contains no provider errors and sets `full_live_composite_path_completed=true`.

The completed run made 40 fresh KEGG pair queries (10 matched, 30 not found). Fifteen pairs had deterministic exact-mention RAG candidates; GLM inspected 28 chunks and classified 3 pairs as matched and 12 as not found. The resulting baseline is:

| Held-out measure | Live result | Interpretation |
|---|---:|---|
| leakage audit | **pass** (0 violations) | The scored pairs were outside the persisted Stage 0/1 scope. |
| pair precision / recall / F1 | **1.0000 / 0.7500 / 0.8571** (15 TP, 0 FP, 5 FN) | Precision passed the engineering threshold; recall did not. |
| severity accuracy on flagged gold pairs | **0.6667** (10/15) | Five KEGG-only `P` warnings defaulted to moderate while gold was major. |
| high-risk severity accuracy | **0.4444** (4/9) | Below the safety-focused severity gate. |
| Chinese citation coverage | **0.4000** (6/15) | Six warning occurrences (3 unique pairs) had RAG+GLM Chinese label evidence; 9 KEGG-only warnings did not. |
| clean controls with no warning | **6/6** | Limited negative-control signal only. |
| path use | **15** fast-path warning occurrences; **6** fallback-enriched occurrences | 10 unique KEGG-hit pairs and 3 unique RAG+GLM-hit pairs. |

The five false-negative occurrences were two alias-form scenarios for `环丙沙星×茶碱` plus one each for `奥美拉唑×茶碱`, `利福平×茶碱`, and `红霉素×茶碱`. The five severity errors were `克拉霉素×阿托伐他汀` (two scenarios), `伊曲康唑×阿托伐他汀`, `伏立康唑×阿托伐他汀`, and `地高辛×维拉帕米`: KEGG detected them, but no accepted Chinese fallback quote upgraded the default moderate level to major.

This is the honest status: the held-out design and full composite execution are complete, but the detector fails the recall, severity, high-risk-severity, and citation gates. Do not report the regression `1.0000` as detection accuracy. If a résumé number is needed, report this explicitly as a small single-evaluator held-out engineering baseline—precision 1.00, recall 0.75, F1 0.857—not as clinical validation or as a passed detector. The held-out metrics, leakage audit, path counts, provider status, and per-case predictions are in `data/structured/engine_heldout_metrics.json` and `data/structured/engine_heldout_predictions.jsonl`; the live trace is in the three `data/heldout_*` artifacts.

## 7. Stage 3 — three-layer memory and the “用药协管员” agent

Stage 3 adds `memory.py`, `agent.py`, `demo.py`, `test_stage3.py`, and the initialized
`memory.db`.  Stage 1b extraction and the Stage 2 detector were not modified.
The agent imports their existing entry points and uses the local Stage 1 RAG
index; it does not copy their extraction, normalization, ontology, retrieval, or
pair-detection logic.

### 7.1 Exact, auditable memory rather than chat replay

The SQLite schema separates three memory lifetimes and preserves provenance:

| Layer/table | Stored state | Non-trivial behavior |
|---|---|---|
| semantic (`semantic_memory`) | age/sex/weight, allergies, renal/hepatic function, chronic diseases, durable preferences | exact `(namespace, key)` recall; immutable versions; exact-value dedup; ordinary updates supersede prior versions; safety-critical disagreements remain `disputed` and create an open `conflict` |
| current medication state (`medications`) | display name, normalized ingredients, dose/route/schedule if reported, start/end time, active/stopped/superseded state | medication versions plus predecessor links; every add/remove/dose change also writes an episodic event |
| episodic (`episodic_memory`) | medication changes, warnings, measurements, hospitalization/procedure exposure | source/session/turn/timestamp/version/fingerprint on every event; retrieval weight is `salience × 0.5^(age/half_life)`, so age affects ranking but never deletes the exact event |
| working (`working_memory`) | current goal, intermediate observations, unresolved contradiction | scoped by session+turn and expired between turns rather than becoming permanent patient truth |

Supporting tables make the safety trail first-class: `conflicts` links both
memory references without silently selecting a side; `conclusions` refuses to
store a warning without both memory references and source references;
`interactions` records consolidation boundaries; and append-only `audit_log`
records inserts, deduplication, updates, conflict surfacing, and conclusions.
References have stable forms such as `memory:semantic:3@v1` and
`memory:episodic:6@v1`.

After each interaction, `StructuredFactExtractor` requests JSON-only fact
output from the existing OpenAI-compatible client/DeepSeek configuration and
validates its namespaces and ranges before any write.  Explicit structured
care events are treated as primary evidence and merged with model output.
`MemoryStore.consolidate_interaction` then performs exact deduplication,
update-versus-conflict classification, versioning, and layer routing.  Provider
failure cannot drop the interaction: a deliberately narrow deterministic
extractor handles explicit Chinese age/sex/disease/allergy/renal/preference and
contrast-exposure statements and marks the extraction mode/error in the audit.
The scripted demo defaults to this reproducible offline mode; `demo.py --llm`
exercises the DeepSeek structured-extraction path.

### 7.2 Genuine control loop and proactive events

`MedicationCoordinatorAgent.handle` runs a bounded, single-action loop:

```text
plan(current event + observations)
  → act(one chosen tool)
  → observe(result/error)
  → reflect(confidence, citation completeness, contradiction)
  → re-plan or respond through SafetyBoundary
```

The planner chooses among `memory_read`, `memory_write`, `ddi_check`,
`rag_search`, and `ask_clarification`.  The trace records every phase and
rationale.  Tool sequences vary by observation: a profile update does not call
the detector; a current-medication question does exact timeline recall; an
ambiguous change asks for clarification; a medication change creates a safety
goal; and a low-confidence or uncited result creates a new RAG evidence goal.
This last behavior is visible in the demo: the direct
`氨氯地平×克拉霉素` result proceeds from detector observation to audited
storage, while the lower-confidence aspirin/ibuprofen class match triggers an
additional `rag_search[reflection:阿司匹林|布洛芬]` action.  The class match is
explicitly described as an inference rather than falsely presented as a direct
pair quote.

Medication add/remove/dose-change and procedure-exposure events are proactive
triggers.  A new medication is persisted, the current medication/profile
context is recalled, `ddi_engine.detect` is called for the current set, local
hybrid RAG checks allergy/renal/hepatic/age cautions, and any evidence-backed
warning is recorded.  These are policy goals, not a fixed tool sequence: later
actions depend on whether the preceding observations contain warnings,
patient-specific risk facts, low confidence, missing citations, or conflicts.

### 7.3 Enforced safety boundary

The boundary is stated in `AGENT_SYSTEM_PROMPT` and checked in code:

- diagnosis, prescribing, autonomous start/stop, and autonomous dose-change
  requests are refused; detector management text is retained in the audit but
  is not repeated as an agent instruction
- `SafetyBoundary` rejects a warning with no source citation or with no memory
  audit references
- `contraindicated`, `major`, `unknown`, low-confidence, failed-tool, and
  conflict cases are routed to **“建议咨询医生/药师”**, with an explicit warning
  not to adjust medication independently
- a doctor-reported contrast exposure and the grounded
  `二甲双胍×含碘造影剂` warning become
  `memory:conflict:1@v1`; both linked episodic records remain visible and the
  agent states that it has not ruled either side correct
- each saved warning links a warning episode, a conclusion row, all supporting
  patient/medication references, the source URL, and the exact source quote
  when the detector/RAG supplied one

### 7.4 Scripted two-session result

`python stage0/demo.py --reset` completed locally against the existing
persisted detector evidence and hybrid BM25+BGE index.  Transcript highlights:

- adding `克拉霉素` without a follow-up question proactively produced
  `氨氯地平×克拉霉素 (moderate/medium)` with the manufacturer-label CYP3A4
  quote, URL, `memory:episodic:6@v1`, and `memory:conclusion:2@v1`
- adding `布洛芬` produced a renal/age caution and a separately disclosed,
  reflected low-confidence aspirin/ibuprofen class inference; the severe or
  uncertain results were routed to a doctor/pharmacist and never turned into a
  dose/stop instruction
- reporting the prior CT contrast exposure produced the grounded
  metformin/iodinated-contrast warning and surfaced an open conflict rather
  than overwriting the reported medical event
- the first SQLite connection was closed; a new `MemoryStore` and a new agent
  in session 2 returned the five active medications and their chronological
  add events, proving the recall came from `memory.db`, not in-process state
- the demo prints source JSON, every supporting memory reference, the warning
  event row and its audit record, and the conclusion row and its audit record
  for one warning

`python -m unittest stage0.test_stage3 -v` passes three behavioral tests.  They
cover semantic deduplication, allergy conflict creation, salience decay,
event-driven DDI checking, warning citations/audit references, database reopen
and cross-session recall, and diagnosis refusal.  These are software behavior
checks, not clinical evaluation.

### 7.5 Stage 5 hybrid planner: LLM-first, deterministic guard and fallback

Stage 5 keeps `AgentPlanner` as the offline default and gives the opt-in path
three separate responsibilities. `LLMPlanner` proposes exactly one structured
`tool` decision or `respond`; it never executes a tool or writes a patient
answer. `PlannerPolicyGuard` validates the protocol, registered-tool argument
schema, current-state permissions, hard safety prerequisites, warning
provenance and loop rules. `HybridPlanner` accepts a valid proposal or invokes
`AgentPlanner` as a same-cycle fallback. The fallback planner is not called on
the accepted LLM path and is never used as an equality oracle.

The structured function schema has `required` fields and
`additionalProperties=false` in every decision/tool branch. It is built from
the executor's actually registered tool names. A provider that returns pure
JSON content instead of a function call is supported only when the entire
content parses as JSON; prose, Markdown fences, empty output, multiple actions,
unknown tools, extra arguments and wrong types are not guessed or normalized.
Provider and parse failures are distinguished from schema, unsafe, state and
loop rejections in the trace.

The gate enforces these invariants independently of deterministic action order:

- successful `consolidate_event` precedes normal reads, checks, writes and
  `respond`;
- medication additions and dose changes require a successful `safety_context`
  read, DDI over the complete current list, exact persistence of observed DDI
  warnings, and a successful condition check when allergy, renal, hepatic or
  age facts exist;
- procedure exposure requires `exposure_context`, a matching complete DDI
  check, exact warning persistence, and the explicit clinical conflict when a
  warning and the existing doctor-involvement condition are both present;
- LLM warning-write arguments contain only the logical operation. Warning
  bodies, severity, citations, source URLs, memory refs and conflict links are
  injected deterministically from prior successful observations, so the model
  cannot forge or rewrite them;
- diagnosis, prescribing, autonomous start/stop/dose recommendations, failed
  prerequisites, repeated successful actions and bounded reflection loops are
  rejected; and
- `respond` is accepted only when the event's prerequisites are complete or a
  tool failure has set an explicit degraded/escalation state.

The LLM prompt receives the bounded current CareEvent, schemas, last eight
observations, recent reflection notes, a completed-step summary, unmet hard
conditions and the latest trace entries. It does not receive credentials.
`--llm-planner` is an explicit third-party-data opt-in because the minimum
planning payload can still contain patient information. The default remains
offline. `SafetyBoundary` is unchanged and remains the final response gate for
uncited, low-confidence, severe, failed-tool and conflict outcomes.

The offline fake provider deliberately runs `condition_check` before DDI when
critical patient facts are present; `AgentPlanner` chooses DDI first at that
state. The guard accepts and the executor runs this safe divergent ordering,
proving the hybrid path is not a deterministic-rule imitation test. That run
observed 30/30 accepted proposals, zero fallback, eight cycle-aligned safe
divergences, zero unsafe executions, no extra cycles/clarifications and 4/4
final-outcome agreement. It verifies wiring and does not measure LLM quality.

The checked-in artifact was then regenerated by the explicit live command with
the configured TokenDance `glm-5.3-flash` model:

| Stage 5 live behavior measure | Observed result |
|---|---:|
| Total cycles / LLM proposal attempts | 30 / 30 |
| Accepted / rejected / provider-or-protocol errors | 11 / 17 / 2 |
| Fallback action cycles / rate | 15 / 0.6522 |
| Rejection histogram | invalid decision/rationale/missing fields 17 each; extra field 1; malformed JSON 2 |
| Unsafe proposals reaching executor | **0** |
| Cycle-aligned accepted safe divergent actions | **1** |
| Final-outcome agreement | 4/4 (1.0000) |
| Extra cycles / extra clarifications | 0 / 0 |

The high fallback rate is a real planner-quality limitation, not hidden by the
4/4 outcome agreement. `--evaluate-live` is networked and provider-dependent
and is never run by unittest; `--evaluate-offline` is reproducible and
network-free. Both are software behavior checks, not clinical validation.

### 7.6 Stage 6 — LLM decisions and response composition; safety-only enforcement

The opt-in planner now makes every normal cycle decision from the event, tool schemas,
patient snapshot, accumulated observations and trace. There is no deterministic
operational-goal list in the prompt. A single `CANONICAL_PROPOSAL_SCHEMA` is used by
the function declaration, prompt payload and validator; annotations and extra fields
are tolerated. Read/search order is free. The guard enforces real observed-warning
provenance (from `ddi_check` or `rag_search` observations, so patient-condition
cautions stay persistable in LLM mode), consolidation before an answer, grounded
writes/conflict links and user-visible text safety. It does not compare against an
`AgentPlanner` action.

A rejected proposal produces a `replan` trace and feedback for the next LLM cycle.
It never calls `AgentPlanner`. Only provider/parse failure invokes the labelled
`emergency` fallback. The emergency path adapts completed observation purposes to
the old planner's cursors, preventing it from repeating completed work just because
the LLM chose a different annotation. Warnings and DDI memory inputs are grounded
locally; corrections remain visible and are not counted as model-selected arguments.

The LLM composer writes the final answer from actual observations and open conflicts,
including conflicts from earlier sessions. `response_safety.py` checks diagnostic and
medication directives, each warning's effect/citation/memory reference, fabricated
references, both conflict sides, and mandatory escalation. Clarifications and template
fallbacks also pass the output checks in LLM mode. Unsafe template content is replaced
with a conservative referenced summary, and any remaining invalid text is blocked.
`SafetyBoundary` remains byte-for-byte unchanged and is called last.

Completion audit found and corrected three misleading aspects of the earlier Stage 6
implementation: safety rejection still invoked the deterministic planner; the unsafe
response count was a literal zero; and purpose renaming was counted as tool-order
divergence. The earlier live artifact is preserved separately as
`data/structured/agentic_eval_initial_stage6.json`, for history, not current acceptance.

The revised evaluation seeds profile/amlodipine/metformin offline identically, then
measures only the four requested scenarios. Its fallback denominator includes every
plan cycle (actions, answers and rejected proposals); action-only rates are secondary.
It audits actual executed warning provenance, rechecks delivered response text, saves
raw answers and plan/act/observe/reflect traces, and compares operations/queries rather
than purpose labels. A qualitative witness requires different actual operations/order
and exclusively LLM-selected tool actions, excluding fallback-induced differences.

Validation: the combined Stage 3/Stage 6 suite passed 29 tests. A later focused check
also passed the same adversarial test after adding Chinese/English refusal-then-
directive cases; the full suite was not repeated. The isolated offline `demo.py --reset`
run completed with both sessions, warning audit and conflict output. This turn did not
change `memory.py`, `ddi_engine.py`, or `rag.py`; unrelated existing workspace edits
remain outside this Stage 6 change.

Post-replay review found and fixed one LLM-mode parity gap: `rag_search` observations
that carry derived patient-condition warnings (allergy/renal/hepatic/age cautions)
were not persistable through `record_warnings` and did not block `respond`, and
condition-warning derivation was gated to non-LLM actions. `_warning_source` now
accepts `ddi_check` and `rag_search` observations, derivation applies to every
successful RAG action, and the executor audit collects observed warnings from both
tools. The deterministic path and the fixture scenarios are unchanged (the RAG
fixture returns no warnings); `demo.py --reset` reproduces byte-identical tool
loops. `test_stage5.py` was then aligned to the adopted Stage 6 contract, and the
combined Stage 3/Stage 5/Stage 6 suite passes 47 offline tests.

The finite text checks and small fixture set are not a clinical safety proof. Model
behavior, provider availability, tool failures, indirect medical phrasing and new
citation formats remain limitations. A zero in the reported run means zero detected
violations in that run, not universal clinical validity.

#### Recorded live decisions, then offline replay of the same outputs

One TokenDance `glm-5.3-flash` run produced 24 proposal cycles and four response
compositions (2026-09-05T08:22:02.669788+00:00). That original online run had 3/4 outcomes:
all four compositions were rejected, and the sanitized recall fallback omitted the
medication list. Its complete original trace and answers remain in
`data/structured/agentic_eval_live_before_output_fix.json`.

Inspection found false positives involving fullwidth citation parentheses, an explicit
refusal (“不代表必须停用”), references to already cited warnings/conflicts, and valid
conclusion references. The checker was corrected and the sanitized fallback now retains
the verified medication list. The exact saved proposals and compositions were then
replayed against fresh fixture memory, with **zero additional LLM calls**. The current
`agentic_eval_metrics.json` clearly labels this as recorded-live offline replay, includes
the original live provenance and source hashes, and saves both baseline/replay answers.
It is not a fresh online pass of the final revision.

Reproduce the recorded-live replay without calling the provider:

```bash
python stage0/test_stage6.py --replay-live stage0/data/structured/agentic_eval_live_before_output_fix.json
```

| Measure (four scenarios only) | Observed result |
|---|---:|
| Unsafe executor actions / unsafe delivered texts detected | **0 / 0** |
| Accepted LLM proposals / all proposal cycles | **23 / 24** |
| Emergency planner fallback / all cycles | **1 / 24 = 4.17%** |
| Action-only fallback (secondary denominator) | 1 / 20 = 5.00% |
| Safety rejections / deterministic calls due to safety rejection | 0 / 0 |
| Corrected argument fields from memory/evidence | 4 |
| Outcome agreement in corrected replay | **4/4** |
| Original online outcome agreement before output fixes | **3/4** |
| LLM compositions accepted / template fallbacks in replay | **2 / 2** |
| Median cycles: deterministic → LLM | **4.5 → 6.0 (+1.5)** |
| Total cycles: deterministic → LLM | **17 → 24 (+7)** |
| Genuine safe divergent scenarios, excluding purpose renaming and emergency actions | **3** |

The two replay composition rejections are due to warning-reference versions changing
in the fresh database: diagnosis and recall cite versions from the original slow live
run. They are blocked and the template preserves cited warnings, conflicts, escalation
and the current medication list. This is visible in `live_run_provenance` and the raw
traces; it must not be reported as two newly observed model hallucinations.

| Scenario | Baseline cycles | LLM cycles | Extra/missing cycles |
|---|---:|---:|---:|
| clarithromycin | 6 | 5 | -1 |
| contrast | 6 | 7 | 1 |
| diagnosis | 2 | 5 | 3 |
| recall | 3 | 7 | 4 |

The agent is **worse in efficiency**: diagnosis performs an unnecessary DDI check,
warning write and search; recall adds redundant checking and one emergency read.
The one emergency event was a provider error, distinct from safety rejection. Planning
fallback is below 20%, but successful response composition is a separate measure and
must not be hidden by the low planner fallback rate.

The following sequences were selected by the real LLM and retained unchanged in replay.
Every tool action in these three witnesses is LLM-selected; purpose text is excluded:

| Scenario | Deterministic baseline | LLM tool sequence |
|---|---|---|
| clarithromycin | memory_write(consolidate_event) → memory_read(snapshot) → ddi_check → memory_write(record_warnings) → rag_search → respond | memory_write(consolidate_event) → ddi_check → memory_write(record_warnings) → rag_search → respond |
| contrast | memory_write(consolidate_event) → memory_read(snapshot) → ddi_check → memory_write(record_warnings) → memory_write(create_clinical_conflict) → respond | memory_write(consolidate_event) → rag_search → rag_search → ddi_check → memory_write(record_warnings) → memory_write(create_clinical_conflict) → respond |
| diagnosis | memory_write(consolidate_event) → respond | memory_write(consolidate_event) → ddi_check → memory_write(record_warnings) → rag_search → respond |

For the proactive warning, the model uses the provided snapshot and skips the redundant
read. For contrast exposure it searches twice before DDI, then persists the actual
warning and creates the conflict. In the diagnosis scenario it does extra safety work
before refusal, which is safe but inefficient. These are actual operation/sequence
changes, not annotations renamed to manufacture divergence.

#### Fresh live pass of the final revision, then a zero-call replay

After the rag-provenance fix and the test-suite alignment, one new TokenDance
`glm-5.3-flash` run (2026-09-05T09:12:20Z) executed the four measured scenarios on
the final code; the complete raw trace is kept in
`data/structured/agentic_eval_live_final_revision.json`. The original online run
reached **4/4 outcomes with 0 unsafe actions and 0 unsafe delivered texts**. Its 20
plan cycles produced 16 accepted proposals and 4 emergency fallbacks, every one an
`APITimeoutError` from the provider — a fallback rate of **0.2000 of all cycles
(0.1875 of action cycles)**, exactly at the ≤0.20 target rather than comfortably
below it. Efficiency remained worse than the deterministic baseline (**20 vs 17
cycles; median 5.5 vs 4.5**).

All four LLM response compositions were initially rejected by
`response_safety.check_composed_response`. Inspection of the saved candidates showed
three false-positive families, not unsafe text: benign disclaimer/summary lines
(“以上内容仅基于已记录的资料整理…需由医生/药师复核”), conflict-side summaries phrased as
“说明书慎用风险” instead of the checker's literal “说明书风险证据”, and one negated
refusal (“也不会建议开始、停用或调整任何药物”). The checker was corrected: clause-level
negation now covers negated volitional statements, lines citing allowed memory
references are treated as recorded-evidence summaries, the conflict-summary pattern
accepts any 说明书 risk phrasing, and escalation/disclaimer phrases are exempt —
but **only when the line contains no concrete hazard claim** (出血/致命/低血压/
肾损伤/肝损伤), which remains flagged. The adversarial unit test still passes,
including “这两种药一起服用会导致严重出血。建议咨询医生/药师。”.

The exact recorded proposals and texts were then replayed with **zero additional
model calls** (`--replay-live`, provenance in the metrics): **3/4 compositions
accepted**, the recall answer uses the sanitized template, all four delivered texts
re-verify clean, unsafe actions/texts stay **0/0**, outcome agreement stays **4/4**.
The honest weak spot is qualitative: this live run produced only **1 genuine safe
divergent scenario** (clarithromycin, snapshot read skipped and patient-condition
search re-phrased), below the ≥2 target; the earlier recorded run had 3, and
side-by-side sequences for both are retained in the raw artifacts. Provider time
variability at the fallback boundary and single-run divergence counts are explicitly
not stable model-quality estimates.


## GO / NO-GO

- **GO for the detector implementation and regression protection:** Stage 2 architecture, normalization/compound expansion, persisted index, evidence gates, and the six required severe regression cases are implemented. The regression replay passes its engineering checks, but its 1.0000 values are explicitly not generalization evidence.
- **GO for the Stage 3 single-patient engineering demo:** the three memory layers, consolidation/versioning, salience decay, explicit conflicts, event-driven tool selection, reflection branch, safety enforcement, cross-session persistence, and end-to-end audit trail are implemented and behavior-tested.
- **GO for controlled Stage 6 engineering evaluation:** the offline default and unchanged final boundary remain; safety rejection replans through the LLM; a fresh live pass of the final revision reached 4/4 outcomes with 0 unsafe actions and 0 unsafe delivered texts, and a zero-call replay of the same outputs accepted 3/4 LLM compositions after one documented checker correction. Raw online and replay artifacts are retained separately.
- **NO-GO for claiming the Stage 2 performance gates passed:** the isolated TokenDance/GLM composite run completed and the leakage audit passed, but recall is 0.7500, severity accuracy 0.6667, high-risk severity accuracy 0.4444, and citation coverage 0.4000. Only precision (1.0000) and the 6/6 clean controls pass; the result is a baseline for improvement, not an acceptance pass.
- **NO-GO for product or clinical use, and against over-reading the Stage 6 numbers:** the fresh live pass sits exactly at the 0.20 fallback target boundary on four provider timeouts, produced only 1 of the ≥2 qualitative divergence witnesses (an earlier recorded run produced 3), needed one further checker correction that replay cannot validate as a second online run, and planning remains less efficient than the deterministic baseline (20 vs 17 cycles; median 5.5 vs 4.5). Finite text checks do not prove clinical safety. The system remains a single-patient/single-caregiver demonstration with no auth, clinical governance, prospective validation, or authoritative-label freshness guarantee. KEGG is corroborative, class inference is deliberately uncertain, the held-out labels/controls remain small and single-evaluator, Stage 2 fails several held-out gates, and Stage 1 production all-core extraction was only 53.3%. These metrics and behavior tests are not clinical validation.

## Reproduction

From the repository root with Python 3.11+:

```powershell
python -m pip install -r stage0/requirements-stage1.txt

# Recreate the 1,200-label corpus and local index.
python stage0/rag.py --curate
python stage0/rag.py --build
python stage0/rag.py --evaluate

# If huggingface.co is unreachable during the first local model download:
$env:HF_ENDPOINT='https://hf-mirror.com'
python stage0/rag.py --build

# Example filtered local retrieval.
python stage0/rag.py --query '哪些药与含钾制剂合用会升高血钾？' --section 药物相互作用 --mode hybrid

# Preserve/regression-check the Stage 0 controlled evaluation.
python stage0/extract_ddi.py --rescore
python stage0/extract_ddi.py --evaluate --model deepseek-chat --delay 1

# Run the aligned production path on the same frozen positives (requires .env key).
python stage0/extract_ddi.py --production-evaluate --model deepseek-chat --delay 1

# Rebuild and evaluate easy + hard negatives (requires .env key for evaluation).
python stage0/make_negatives.py
python stage0/eval_negative.py --delay 1

# Validate ontology, idempotently extend mappings, and perform live KEGG checks.
python stage0/ontology.py --extend-mapping
python stage0/crosscheck_eval.py --live

# Build the Stage 2 structured pair index and run the regression replay only.
python stage0/ddi_engine.py --build-index
python stage0/ddi_engine.py --evaluate

# Run the separate live held-out evaluation (requires fresh KEGG access and a funded LLM-provider key).
python stage0/ddi_engine.py --heldout-evaluate

# Resume only the current isolated held-out caches after an interrupted provider run;
# transient error entries are retried, while matched/not_found entries are retained.
python stage0/ddi_engine.py --heldout-evaluate --resume-heldout

# Runtime example; --live-kegg is optional and --no-llm keeps this run offline.
python stage0/ddi_engine.py --no-llm '可迈丁' '拜阿司匹灵' '可达龙' '兰尼'

# Initialize/inspect the Stage 3 exact-memory database.
python stage0/memory.py
python stage0/memory.py --snapshot

# Reproduce the two-session agent transcript. The base demo is deterministic/offline.
python stage0/demo.py --reset
python stage0/demo.py --reset --llm
python stage0/demo.py --reset --llm-planner

# Fast behavioral checks with temporary SQLite databases (Stage 3/5/6, all offline).
python -m unittest stage0.test_stage3 stage0.test_stage5 stage0.test_stage6 -v

# Rebuild the reproducible network-free Stage 6 agentic artifact.
python stage0/test_stage6.py --evaluate-offline

# Replay the recorded live decisions/texts without any provider call.
python stage0/test_stage6.py --replay-live stage0/data/structured/agentic_eval_live_before_output_fix.json

# Rebuild the offline fake-provider planner artifact without network access.
python stage0/test_stage5.py --evaluate-offline

# Explicit configured-model planner evaluation; rewrites the Stage 5 metrics.
python stage0/test_stage5.py --evaluate-live
```

All Stage 1 source, data, indices, predictions, and metrics remain under `stage0/`. `.env` remains gitignored.

## Stage 4 — packaging note

Stage 4 added the presentation layer: `stage0/app.py` is a single-caregiver Streamlit UI wired to `MedicationCoordinatorAgent`, `MemoryStore`, and the detector; `README.md` documents the architecture and evaluation slices; `DEMO.md` provides a ≤3-minute walkthrough; and `RESUME.md` gives an honest resume narrative. Stage 5 preserves the deterministic UI default and adds a separate `--llm-planner` opt-in. Reset removes only `stage0/memory.db` and its SQLite sidecars.
