"""Fuzzy history retrieval over episodic memory (P2).

Uses SQLite FTS5 with the trigram tokenizer when available (works for Chinese
substrings of 3+ characters); otherwise falls back to exact substring matching
over the stored payload.  Search results are *recall aids only* — similarity
never decides whether a current fact is true.
"""
from __future__ import annotations

import json
import re
from typing import Any

try:
    from .memory import memory_ref
except ImportError:  # Support ``python stage0/memory_search.py``.
    from memory import memory_ref  # type: ignore


_INDEX_TABLE = "history_fts"


def _fts_available(store: Any) -> bool:
    try:
        store.connection.execute(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS {_INDEX_TABLE} "
            "USING fts5(text, episodic_id UNINDEXED, tokenize='trigram')"
        )
        return True
    except Exception:
        return False


def sync_history_index(store: Any) -> int:
    """Index episodic rows not yet in the FTS table. Returns rows added."""
    if not _fts_available(store):
        return 0
    rows = store.connection.execute(
        """SELECT id, event_type, subject_key, payload_json FROM episodic_memory
           WHERE id NOT IN (SELECT CAST(episodic_id AS INTEGER) FROM history_fts)"""
    ).fetchall()
    added = 0
    for row in rows:
        text = " ".join(
            part for part in (
                row["event_type"], row["subject_key"] or "",
                _payload_text(row["payload_json"]),
            ) if part
        )
        store.connection.execute(
            f"INSERT INTO {_INDEX_TABLE}(text, episodic_id) VALUES(?,?)",
            (text, str(row["id"])),
        )
        added += 1
    store.connection.commit()
    return added


def _payload_text(payload_json: str) -> str:
    try:
        payload = json.loads(payload_json)
    except Exception:
        return payload_json or ""
    return json.dumps(payload, ensure_ascii=False)


def _query_tokens(query: str) -> list[str]:
    return [
        token for token in re.findall(r"[㐀-鿿]{2,}|[A-Za-z0-9]{2,}", query)
        if len(token) >= 2
    ]


def search_history(store: Any, query: str, *, limit: int = 5) -> dict[str, Any]:
    """Recall candidates for fuzzy history questions ('上次提到白色药片是什么时候')."""
    tokens = _query_tokens(query)
    if not tokens:
        return {"mode": "no_query", "results": []}
    results: list[dict[str, Any]] = []
    mode = "like_fallback"
    if _fts_available(store):
        sync_history_index(store)
        # Trigram matching needs 3+ character strings; slide windows across the
        # whole token so a mention in the middle of a long run still matches.
        match_terms: list[str] = []
        for token in tokens:
            if len(token) >= 3:
                for start in range(0, len(token) - 2):
                    match_terms.append(token[start:start + 3])
            else:
                match_terms.append(token)
        quoted = " OR ".join(f'"{term}"' for term in dict.fromkeys(match_terms))
        try:
            rows = store.connection.execute(
                f"SELECT episodic_id FROM {_INDEX_TABLE} WHERE text MATCH ? LIMIT ?",
                (quoted, limit * 10),
            ).fetchall()
            mode = "fts5_trigram"
            ids = [int(row["episodic_id"]) for row in rows]
        except Exception:
            ids = []
        if ids:
            placeholders = ",".join("?" for _ in ids)
            rows = store.connection.execute(
                f"SELECT * FROM episodic_memory WHERE id IN ({placeholders})", ids
            ).fetchall()
            by_id = {row["id"]: row for row in rows}
            results = [by_id[i] for i in ids if i in by_id]
    if not results:
        # Degraded path: exact substring over stored payload/subject text.
        conditions = " OR ".join(
            "(payload_json LIKE ? OR subject_key LIKE ? OR event_type LIKE ?)" for _ in tokens
        )
        parameters: list[Any] = []
        for token in tokens:
            parameters.extend([f"%{token}%"] * 3)
        rows = store.connection.execute(
            f"SELECT * FROM episodic_memory WHERE {conditions} ORDER BY id DESC LIMIT ?",
            [*parameters, limit],
        ).fetchall()
        results = list(rows)
        mode = "like_fallback"
    out = []
    for row in results[:limit]:
        item = dict(row)
        item["payload"] = json.loads(item.pop("payload_json") or "{}")
        item["ref"] = memory_ref("episodic", item["id"], item["version"])
        out.append(item)
    return {"mode": mode, "results": out, "tokens": tokens}
