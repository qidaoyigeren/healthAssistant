"""Bounded, explainable ContextPacket builder for the medication coordinator.

The packet assembles the memory a reply may use, in a fixed, explainable
priority order: open conflicts, safety-critical facts, current medications,
pending-verification reports, low-risk preferences, then task-relevant
history.  Key sections that do not fit the budget mark the packet incomplete
instead of being silently dropped.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any

try:
    from .memory import SAFETY_CRITICAL_NAMESPACES
except ImportError:  # Support ``python stage0/memory_context.py``.
    from memory import SAFETY_CRITICAL_NAMESPACES  # type: ignore

try:
    from .memory_search import search_history
except ImportError:
    from memory_search import search_history  # type: ignore


@dataclass
class ContextPacket:
    task_query: str
    generated_at: str
    sections: list[dict[str, Any]] = field(default_factory=list)
    included_refs: list[str] = field(default_factory=list)
    excluded: list[dict[str, Any]] = field(default_factory=list)
    snapshot: dict[str, Any] = field(default_factory=dict)
    complete: bool = True
    truncated_reasons: list[str] = field(default_factory=list)

    def render(self) -> str:
        """Flat text rendering for prompts; section headers make provenance visible."""
        lines = []
        for section in self.sections:
            if not section["included"]:
                continue
            lines.append(f"## {section['name']}")
            lines.append(json.dumps(section["content"], ensure_ascii=False, sort_keys=True))
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# Selection order is part of the contract: safety information first, recent
# chat history last.  History can be dropped by decay; conflicts and critical
# facts may not (they mark the packet incomplete when they do not fit).
SECTION_ORDER = (
    ("open_conflicts", 1),
    ("critical_facts", 2),
    ("current_medications", 3),
    ("pending_verification", 4),
    ("preferences", 5),
    ("relevant_history", 6),
)
KEY_SECTIONS = {"open_conflicts", "critical_facts", "current_medications"}


def build_context(
    store: Any,
    *,
    task_query: str = "",
    max_chars: int = 2400,
    history_limit: int = 5,
) -> ContextPacket:
    state = store.query_state()
    used = 0
    packet = ContextPacket(
        task_query=task_query,
        generated_at=state["known_at"],
        snapshot={
            "valid_at": state["valid_at"],
            "known_at": state["known_at"],
            "medications_revision": state["meta"]["medications_revision"],
            "semantic_revision": state["meta"]["semantic_revision"],
        },
    )

    candidates: dict[str, list[dict[str, Any]]] = {
        "open_conflicts": [
            {
                "ref": item.get("ref"),
                "subject": item.get("subject_key"),
                "description": item.get("description"),
                "created_at": item.get("created_at"),
            }
            for item in state["open_conflicts"]
        ],
        "critical_facts": [
            {
                "ref": item["ref"],
                "namespace": item["namespace"],
                "key": item["fact_key"],
                "value": item["value"],
                "status": item["status"],
                "valid_from": item["valid_from"],
            }
            for item in state["facts"]
            if item["namespace"] in SAFETY_CRITICAL_NAMESPACES
        ],
        "current_medications": [
            {
                "ref": item["ref"],
                "name": item["display_name"],
                "dose": item["dose"],
                "route": item["route"],
                "schedule": item["schedule"],
                "start_at": item["start_at"],
            }
            for item in state["medications"]
        ],
        "pending_verification": [
            {
                "ref": item["ref"],
                "mention": item["payload"].get("mention") or item.get("subject_key"),
                "mode": item["payload"].get("mode"),
                "reported_text": item["payload"].get("reported_text"),
                "occurred_at": item["occurred_at"],
            }
            for item in store.retrieve_episodic(event_types=["caregiver_message"], limit=10)
            if item.get("needs_verification")
        ],
        "preferences": [
            {
                "ref": item["ref"],
                "key": item["key"],
                "value": item["value"],
            }
            for item in store.current_semantic(["preference"])
        ],
        "relevant_history": [],
    }
    if task_query:
        search = search_history(store, task_query, limit=history_limit)
        candidates["relevant_history"] = [
            {
                "ref": item["ref"],
                "event_type": item["event_type"],
                "subject_key": item.get("subject_key"),
                "occurred_at": item["occurred_at"],
                "payload": item.get("payload"),
            }
            for item in search.get("results", [])
        ]
        packet.snapshot["search_mode"] = search.get("mode")

    for name, priority in SECTION_ORDER:
        items = candidates.get(name, [])
        content = json.dumps(items, ensure_ascii=False, sort_keys=True)
        cost = len(content)
        if used + cost <= max_chars:
            used += cost
            packet.sections.append({
                "name": name, "priority": priority, "included": True,
                "reason": "fits_budget", "char_cost": cost, "content": items,
            })
            packet.included_refs.extend(
                item["ref"] for item in items if item.get("ref")
            )
            continue
        # Key sections must not silently vanish; the packet is then incomplete
        # and the caller must narrow the question or read more via tools.
        reason = "budget_exceeded"
        packet.sections.append({
            "name": name, "priority": priority, "included": False,
            "reason": reason, "char_cost": cost, "content": [],
        })
        packet.excluded.extend(
            {"ref": item.get("ref"), "section": name, "reason": reason}
            for item in items if item.get("ref")
        )
        if name in KEY_SECTIONS:
            packet.complete = False
            packet.truncated_reasons.append(f"{name}:{reason}")
    packet.snapshot["char_used"] = used
    packet.snapshot["max_chars"] = max_chars
    return packet
