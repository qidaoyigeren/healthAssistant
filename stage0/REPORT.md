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

## GO / NO-GO

- **GO for the detector implementation and regression protection:** Stage 2 architecture, normalization/compound expansion, persisted index, evidence gates, and the six required severe regression cases are implemented. The regression replay passes its engineering checks, but its 1.0000 values are explicitly not generalization evidence.
- **GO for the Stage 3 single-patient engineering demo:** the three memory layers, consolidation/versioning, salience decay, explicit conflicts, event-driven tool selection, reflection branch, safety enforcement, cross-session persistence, and end-to-end audit trail are implemented and behavior-tested.
- **NO-GO for claiming the Stage 2 performance gates passed:** the isolated TokenDance/GLM composite run completed and the leakage audit passed, but recall is 0.7500, severity accuracy 0.6667, high-risk severity accuracy 0.4444, and citation coverage 0.4000. Only precision (1.0000) and the 6/6 clean controls pass; the result is a baseline for improvement, not an acceptance pass.
- **NO-GO for product or clinical use:** Stage 3 is now an agent implementation, but it remains a single-patient/single-caregiver demonstration with no auth, clinical governance, prospective validation, or authoritative-label freshness guarantee. KEGG is corroborative, class inference is deliberately uncertain, the held-out labels/controls remain small and single-evaluator, Stage 2 fails several held-out gates, and Stage 1 production all-core extraction was only 53.3%. These metrics and behavior tests are not clinical validation.

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

# Reproduce the two-session agent transcript. Add --llm to use the configured
# DeepSeek structured fact extractor; the base demo is deterministic/offline.
python stage0/demo.py --reset
python stage0/demo.py --reset --llm

# Fast behavioral checks with temporary SQLite databases.
python -m unittest stage0.test_stage3 -v
```

All Stage 1 source, data, indices, predictions, and metrics remain under `stage0/`. `.env` remains gitignored.

## Stage 4 — packaging note

Stage 4 adds the presentation layer only: `stage0/app.py` is a single-caregiver Streamlit UI wired to the unchanged `MedicationCoordinatorAgent`, `MemoryStore`, and detector; `README.md` documents the architecture and both evaluation slices; `DEMO.md` provides a ≤3-minute walkthrough; and `RESUME.md` gives an honest resume narrative. The default UI path is deterministic/offline-first, with an explicit optional `--llm` mode. Reset removes only `stage0/memory.db` and its SQLite sidecars. No detector, memory, agent, demo, test, data, JSON, or metric module was changed.
