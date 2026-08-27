# Resume narrative

I built a single-caregiver medication-coordination demo that turns Chinese medicine-label evidence and caregiver events into an auditable workflow: hybrid BM25+BGE retrieval, a KEGG-plus-grounded DDI detector, three-layer SQLite memory, and an event-driven plan/act/observe/reflect agent behind a Streamlit UI. A leakage-audited held-out baseline reached precision 1.0000, recall 0.7500, and F1 0.8571; these are small engineering measurements, not clinical validation.

- Implemented semantic, episodic, working, medication, conclusion, conflict, and append-only audit records with stable `memory:...` references and exact recall.
- Added proactive medication-change checks, patient-context retrieval, low-confidence reflection, explicit conflict surfacing, and a refusal/safety boundary for diagnosis and prescribing.
- Preserved a reproducible offline path and a separate optional LLM structured-extraction path; the UI drives the real Stage 3 modules.
- Ran regression replay separately from the held-out evaluation and kept a zero-violation leakage audit.
- Documented real limits: theophylline-class recall gap, KEGG `P` severity defaults, citation coverage 0.4000, single evaluator, stale-source risk, and no clinical governance.

