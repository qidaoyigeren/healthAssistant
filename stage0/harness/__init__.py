"""Agent Harness (P1): shared runtime contracts for both runners.

Modules:

* ``errors``    — stable tool-error taxonomy (never one bucket for permission,
                  evidence and internal failures; graph interrupts and budget
                  exhaustion are pass-through, not tool results).
* ``schema``    — the single JSON-schema validator shared by the planner
                  prompt, the guard and the executor (one rule, one
                  implementation).
* ``runtime``   — ``RunContext``: trusted scope/principal, identity, patient
                  revision, budget handle, cancellation and trace correlation.
                  Only JSON-safe fields ever enter a checkpoint.
* ``tools``     — ``ToolSpec`` / ``ToolResult`` / ``ToolExecutor`` and the
                  light observational hooks (before_model/after_model,
                  before_tool/after_tool, before_publish).  The runner owns
                  routing, the executor owns execution constraints; the safety
                  boundary is NOT a hook and cannot be disabled by a plugin.
* ``evidence``  — immutable ``EvidenceStore`` and the authorised
                  ``read_evidence`` tool (P1-B).
* ``context``   — bounded field-semantic patient views with explicit omission
                  markers (P1-B).
* ``summary``   — structured run summaries replacing hash-only digests (P1-B).
* ``manifest``  — immutable ``RunManifest`` per run (P1-C).
* ``observability`` — call/attempt spans, replay dedup, optional OTel export
                  (P1-C).
"""
