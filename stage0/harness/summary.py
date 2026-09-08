"""Structured run summaries (Harness P1-B).

Replaces the hash-only "old observation" digests: a planner looking back at
an earlier cycle sees what the run COMPLETED, which evidence ids were
captured, which issues remain unresolved, which failure categories occurred
and which fact revision the run is working against — deterministically
extracted from real observations.  No LLM summarisation is enabled: the
content is small and deterministic extraction keeps the summary auditable.
"""
from __future__ import annotations

from typing import Any


def summarize_observation(observation: Any) -> dict[str, Any]:
    """Structured summary of ONE observation.  Old observations that predate
    evidence capture carry an explicit ``evidence: "not_recorded"`` marker —
    the absence is visible, never backfilled."""
    summary: dict[str, Any] = {
        "tool": observation.tool, "purpose": observation.purpose, "ok": observation.ok,
        "cycle": observation.cycle,
    }
    result = observation.result
    if observation.ok and isinstance(result, dict):
        summary["key_fields"] = sorted(result.keys())
        for key, out_key in (("warnings", "warning_count"), ("recorded_warnings", "recorded_warning_count"),
                             ("results", "result_count"), ("medications", "medication_count"),
                             ("timeline", "timeline_count")):
            if isinstance(result.get(key), list):
                summary[out_key] = len(result[key])
        if observation.tool == "memory_read" and isinstance(result.get("medications"), list):
            summary["medication_names"] = [item.get("display_name") for item in result["medications"]
                                           if isinstance(item, dict)][:64]
        if result.get("conflict"):
            summary["conflict_ref"] = result["conflict"].get("ref")
        if isinstance(result.get("consolidation"), dict):
            summary["consolidated"] = True
    if not observation.ok:
        summary["error_kind"] = getattr(observation, "error_kind", None) or "unclassified"
        summary["recoverable"] = bool(getattr(observation, "recoverable", False))
    refs = list(getattr(observation, "evidence_refs", []) or [])
    if refs:
        summary["evidence_ids"] = refs
    else:
        summary["evidence"] = "not_recorded"
    return summary


def build_run_summary(state: Any, *, fact_revision: int | None = None) -> dict[str, Any]:
    """Structured whole-run summary for the planner payload.

    ``completed_goals`` lists the real domain effects achieved (consolidation,
    persisted warnings, created conflicts, medication changes); ``unresolved``
    lists what is explicitly NOT finished (failed tools, unrecorded observed
    warnings, degraded reasons).  Both are extracted deterministically from
    observations — nothing is inferred from composed text.
    """
    completed: list[str] = []
    unresolved: list[str] = []
    failure_categories: list[str] = []
    evidence_ids: list[str] = []
    for observation in getattr(state, "observations", []):
        result = observation.result
        if observation.ok and isinstance(result, dict):
            if observation.tool == "memory_write" and "consolidation" in result:
                completed.append("consolidate_event")
            recorded = result.get("recorded_warnings")
            if isinstance(recorded, list) and recorded:
                completed.append(f"record_warnings:{len(recorded)}")
            if result.get("conflict"):
                completed.append("create_clinical_conflict")
            change = result.get("medication_change") or {}
            if isinstance(change, dict) and change.get("outcome"):
                completed.append(f"medication_{change['outcome']}")
        elif not observation.ok:
            category = getattr(observation, "error_kind", None) or "unclassified"
            failure_categories.append(category)
            if getattr(observation, "recoverable", False):
                unresolved.append(f"retry_or_replan:{observation.tool}:{observation.purpose}")
            else:
                unresolved.append(f"tool_failed:{observation.tool}:{observation.purpose}")
        evidence_ids.extend(getattr(observation, "evidence_refs", []) or [])
    if getattr(state, "degraded_reason", None):
        unresolved.append(f"degraded:{state.degraded_reason}")
    return {
        "completed_goals": list(dict.fromkeys(completed)),
        "unresolved_issues": list(dict.fromkeys(unresolved)),
        "failure_categories": sorted(set(failure_categories)),
        "evidence_ids": list(dict.fromkeys(evidence_ids)),
        "fact_revision": fact_revision,
        "summary_kind": "deterministic_extract",
    }
