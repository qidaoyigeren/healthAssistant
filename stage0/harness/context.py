"""Bounded, field-semantic patient views for planning prompts.

Harness P1-B.  Replaces the generic ``value[:12]`` list compaction that used
to silently drop critical items (medication 13 was invisible while the
planner believed it saw the full list).  Rules:

* safety-critical collections — current medications, allergy/renal/hepatic
  facts, open conflicts — are NEVER silently truncated: either the whole
  collection fits, or the view carries an explicit ``omitted_count`` and
  ``truncated: true`` marker so no omission can pass as a completed check;
* preference and long-history sections may be truncated, but always with an
  explicit marker;
* determinstic DDI inputs are never taken from these views — they come from
  the authoritative store (guard grounding), as before.
"""
from __future__ import annotations

from typing import Any

# Key collections that must not be silently cut.  Medications and conflicts
# get generous caps (explicit-marker territory, far above realistic counts).
KEY_COLLECTION_LIMITS = {
    "medications": 64,
    "open_conflicts": 32,
}
CRITICAL_FACT_LIMIT = 64
DEFAULT_LIST_LIMIT = 12
DEFAULT_ITEM_CHARS = 600

OMITTED_KEY = "__omitted__"


def _marker(omitted: int, section: str) -> dict[str, Any]:
    return {OMITTED_KEY: True, "omitted_count": omitted, "section": section,
            "note": f"该视图仅显示部分条目，另有 {omitted} 条未显示；关键判断前必须用工具读取完整集合。"}


def bounded_list(items: list[Any], *, limit: int, section: str,
                 item_chars: int = DEFAULT_ITEM_CHARS) -> list[Any]:
    """Truncate with an explicit omission marker — never silently."""
    out: list[Any] = []
    for item in items[:limit]:
        if isinstance(item, str) and len(item) > item_chars:
            out.append(item[:item_chars] + f"…[截断，原文{len(item)}字]")
        else:
            out.append(item)
    omitted = len(items) - len(out)
    if omitted > 0:
        out.append(_marker(omitted, section))
    return out


def bounded_patient_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Bounded view over a patient memory snapshot, keyed by field semantics."""
    view: dict[str, Any] = {}
    for key, value in snapshot.items():
        if not isinstance(value, list):
            view[key] = value
            continue
        if key == "semantic":
            critical = [item for item in value
                        if isinstance(item, dict) and item.get("namespace") in
                        {"allergy", "renal_function", "hepatic_function", "chronic_disease", "sex"}]
            other = [item for item in value if item not in critical]
            view[key] = bounded_list(critical, limit=CRITICAL_FACT_LIMIT, section=key) + \
                bounded_list(other, limit=DEFAULT_LIST_LIMIT, section=f"{key}:other")
        else:
            limit = KEY_COLLECTION_LIMITS.get(key, DEFAULT_LIST_LIMIT)
            view[key] = bounded_list(value, limit=limit, section=key)
    return view


def view_is_complete(view: Any) -> bool:
    """True when no part of the view carries an omission marker."""
    if isinstance(view, dict):
        if view.get(OMITTED_KEY):
            return False
        return all(view_is_complete(value) for value in view.values())
    if isinstance(view, list):
        return all(view_is_complete(item) for item in view)
    return True


def omissions(view: Any) -> list[dict[str, Any]]:
    """Collect the omission markers so a caller can surface every truncation."""
    found: list[dict[str, Any]] = []
    if isinstance(view, dict):
        if view.get(OMITTED_KEY):
            found.append({k: v for k, v in view.items() if k != OMITTED_KEY})
        for value in view.values():
            found.extend(omissions(value))
    elif isinstance(view, list):
        for item in view:
            found.extend(omissions(item))
    return found
