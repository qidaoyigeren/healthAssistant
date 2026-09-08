"""Immutable EvidenceStore and the authorised ``read_evidence`` tool.

Harness P1-B.  The model never sees full retrieved documents inline and never
supplies paths or URLs: retrieval results are captured as immutable evidence
records (content + hash + corpus/index version + retrieval parameters + the
patient revision they were fetched against), the planning view keeps only
``evidence_id`` plus a relevant excerpt, and the full original can be read
back through the authorised, scope-checked ``read_evidence(evidence_id,
offset, limit)`` tool.

Trust rules:

* evidence ids are content-derived and scoped; a read of a nonexistent id and
  a read of another scope's id return the SAME error — probing ids cannot
  reveal whether other patients' evidence exists;
* content hashes are re-verified on read; a mismatch is refused;
* patient-specific evidence is never served outside its scope;
* raw evidence lives in the domain store — excerpts in planning views are
  derived views and can never be promoted back to original evidence.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

try:
    from .errors import ToolErrorKind, ToolExecutionError
except ImportError:  # pragma: no cover - script-style import
    from errors import ToolErrorKind, ToolExecutionError  # type: ignore


GENERAL_LABEL = "general_label"
PATIENT_SPECIFIC = "patient_specific"

# Reads are bounded: a single read returns at most this many characters.
MAX_READ_LIMIT = 2000

EVIDENCE_DDL = """
CREATE TABLE IF NOT EXISTS evidence_records (
    evidence_id TEXT PRIMARY KEY,
    scope_id TEXT NOT NULL,
    run_id TEXT,
    source_uri TEXT,
    content_ref TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    corpus_version TEXT,
    retrieval_params_json TEXT,
    retrieved_at TEXT NOT NULL,
    patient_revision INTEGER,
    access_class TEXT NOT NULL CHECK(access_class IN ('general_label','patient_specific')),
    content TEXT NOT NULL,
    content_chars INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_evidence_scope ON evidence_records(scope_id, retrieved_at);
"""


def content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def evidence_id_for(*, scope_id: str, source_uri: str | None, content: str,
                    corpus_version: str | None) -> str:
    """Deterministic id: re-capturing identical content is idempotent."""
    key = json.dumps([scope_id, source_uri or "", content_hash(content), corpus_version or ""],
                     ensure_ascii=False)
    return "ev-" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:20]


@dataclass
class EvidenceRecord:
    evidence_id: str
    scope_id: str
    run_id: str | None
    source_uri: str | None
    content_ref: str
    content_hash: str
    corpus_version: str | None
    retrieval_params: dict[str, Any]
    retrieved_at: str
    patient_revision: int | None
    access_class: str
    content: str

    def to_dict(self, *, include_content: bool = False) -> dict[str, Any]:
        data = asdict(self)
        if not include_content:
            data["content_chars"] = len(data.pop("content"))
            data.pop("retrieval_params", None)
        return data


class EvidenceStore:
    """SQLite-backed immutable evidence store (same database file as the
    MemoryStore; additive tables only)."""

    def __init__(self, connection: sqlite3.Connection, lock: threading.RLock | None = None,
                 *, scope_id: str = "local-demo"):
        self.connection = connection
        self._lock = lock or threading.RLock()
        self.scope_id = scope_id
        with self._lock:
            self.connection.executescript(EVIDENCE_DDL)
            self.connection.commit()

    # ---- capture -----------------------------------------------------------

    def put(self, *, content: str, source_uri: str | None, run_id: str | None = None,
            content_ref: str = "inline", corpus_version: str | None = None,
            retrieval_params: dict[str, Any] | None = None,
            patient_revision: int | None = None,
            access_class: str = GENERAL_LABEL, scope_id: str | None = None) -> EvidenceRecord:
        if access_class not in {GENERAL_LABEL, PATIENT_SPECIFIC}:
            raise ValueError(f"unknown access_class: {access_class}")
        scope = scope_id or self.scope_id
        record = EvidenceRecord(
            evidence_id=evidence_id_for(scope_id=scope, source_uri=source_uri,
                                        content=content, corpus_version=corpus_version),
            scope_id=scope, run_id=run_id, source_uri=source_uri,
            content_ref=content_ref, content_hash=content_hash(content),
            corpus_version=corpus_version, retrieval_params=dict(retrieval_params or {}),
            retrieved_at=_now(), patient_revision=patient_revision,
            access_class=access_class, content=content,
        )
        with self._lock, self.connection:
            self.connection.execute(
                """INSERT OR IGNORE INTO evidence_records
                   (evidence_id,scope_id,run_id,source_uri,content_ref,content_hash,
                    corpus_version,retrieval_params_json,retrieved_at,patient_revision,
                    access_class,content,content_chars)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (record.evidence_id, record.scope_id, record.run_id, record.source_uri,
                 record.content_ref, record.content_hash, record.corpus_version,
                 json.dumps(record.retrieval_params, ensure_ascii=False), record.retrieved_at,
                 record.patient_revision, record.access_class, record.content,
                 len(record.content)))
        return record

    # ---- controlled read-back ----------------------------------------------

    def read(self, evidence_id: str, *, scope_id: str | None = None,
             offset: int = 0, limit: int = MAX_READ_LIMIT) -> dict[str, Any]:
        """Authorised read-back.  Missing evidence, cross-scope evidence and
        tampered content all surface as EVIDENCE_UNAVAILABLE so an attacker
        probing ids learns nothing about other scopes."""
        if not isinstance(evidence_id, str) or not evidence_id.strip():
            raise ToolExecutionError(ToolErrorKind.EVIDENCE_UNAVAILABLE, "evidence_id is required")
        if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
            raise ToolExecutionError(ToolErrorKind.INVALID_ARGUMENTS,
                                     "offset must be a non-negative integer", recoverable=True)
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1 or limit > MAX_READ_LIMIT:
            raise ToolExecutionError(ToolErrorKind.INVALID_ARGUMENTS,
                                     f"limit must be between 1 and {MAX_READ_LIMIT}", recoverable=True)
        scope = scope_id or self.scope_id
        with self._lock:
            row = self.connection.execute(
                "SELECT * FROM evidence_records WHERE evidence_id=?", (evidence_id,)).fetchone()
        if row is None or row["scope_id"] != scope:
            # Same error for missing and foreign-scope: no existence oracle.
            raise ToolExecutionError(ToolErrorKind.EVIDENCE_UNAVAILABLE,
                                     "evidence is not available in this scope")
        if row["access_class"] == PATIENT_SPECIFIC and scope != self.scope_id:
            raise ToolExecutionError(ToolErrorKind.EVIDENCE_UNAVAILABLE,
                                     "evidence is not available in this scope")
        content = row["content"]
        if content_hash(content) != row["content_hash"]:
            raise ToolExecutionError(ToolErrorKind.EVIDENCE_UNAVAILABLE,
                                     "evidence content hash mismatch; refusing to serve")
        total = len(content)
        if offset >= total:
            return {"evidence_id": evidence_id, "content": "", "offset": offset,
                    "returned_chars": 0, "total_chars": total, "truncated": False,
                    "source_uri": row["source_uri"], "content_hash": row["content_hash"]}
        slice_ = content[offset:offset + limit]
        return {"evidence_id": evidence_id, "content": slice_, "offset": offset,
                "returned_chars": len(slice_), "total_chars": total,
                "truncated": offset + len(slice_) < total,
                "source_uri": row["source_uri"], "content_hash": row["content_hash"]}

    def get_meta(self, evidence_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self.connection.execute(
                "SELECT evidence_id,scope_id,source_uri,content_ref,content_hash,corpus_version,"
                "retrieved_at,patient_revision,access_class,content_chars FROM evidence_records "
                "WHERE evidence_id=?", (evidence_id,)).fetchone()
        return dict(row) if row is not None else None

    # ---- retention ---------------------------------------------------------

    def prune(self, *, older_than_days: float, active_run_ids: set[str],
              protected_evidence_ids: set[str]) -> dict[str, int]:
        """Delete orphaned evidence: older than the retention window, not part
        of an active/parked run and not referenced by any review case,
        conclusion or checkpoint that still depends on it.  Returns counts."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=older_than_days)).isoformat(timespec="seconds")
        with self._lock:
            rows = self.connection.execute(
                "SELECT evidence_id,run_id FROM evidence_records WHERE retrieved_at<?",
                (cutoff,)).fetchall()
            stale = [dict(row) for row in rows]
            removed = 0
            for item in stale:
                if item["evidence_id"] in protected_evidence_ids:
                    continue
                if item["run_id"] and item["run_id"] in active_run_ids:
                    continue
                with self.connection:
                    self.connection.execute(
                        "DELETE FROM evidence_records WHERE evidence_id=?", (item["evidence_id"],))
                removed += 1
        return {"considered": len(stale), "removed": removed}


# ---- excerpt selection -------------------------------------------------------


def select_excerpt(content: str, query: str = "", *, max_chars: int = 240) -> dict[str, Any]:
    """Pick the most relevant PARAGRAPH, not the first 200 characters.

    Deterministic: paragraph boundaries are 。；\n breaks, scoring is term
    overlap with the retrieval query.  The excerpt is a DERIVED view — it
    never carries original-evidence citation rights (see harness/summary).
    """
    text = content.strip()
    if len(text) <= max_chars:
        return {"excerpt": text, "start": 0, "total_chars": len(content),
                "omitted_chars": 0, "selection": "full"}
    paragraphs = [p for p in re.split(r"(?<=[。；])|\n", text) if p and p.strip()]
    if not paragraphs:
        paragraphs = [text]
    terms = [t for t in re.findall(r"[㐀-鿿]{2,}|[A-Za-z0-9]+", query) if len(t) >= 2]
    best, best_score, best_start = paragraphs[0], -1, 0
    cursor = 0
    for paragraph in paragraphs:
        stripped = paragraph.strip()
        score = sum(1 for term in terms if term.lower() in stripped.lower())
        if score > best_score:
            best, best_score, best_start = stripped, score, cursor
        cursor += len(paragraph)
    excerpt = best[:max_chars]
    return {"excerpt": excerpt, "start": best_start, "total_chars": len(content),
            "omitted_chars": len(content) - min(max_chars, len(best)),
            "selection": f"paragraph:{best_score}term-overlap" if terms else "paragraph:first",
            "truncated_paragraph": len(best) > max_chars}


def capture_from_rag_result(store: EvidenceStore, result: dict[str, Any], *,
                            run_id: str | None, query: str,
                            patient_revision: int | None,
                            scope_id: str | None = None) -> list[dict[str, Any]]:
    """Capture retrieved chunks as immutable evidence and attach the planning
    view (ids + excerpts).  The original ``results`` entries are NOT modified —
    downstream citation verification keeps reading the raw text."""
    corpus_version = result.get("corpus_version")
    view: list[dict[str, Any]] = []
    for item in result.get("results") or []:
        if not isinstance(item, dict) or not item.get("text"):
            continue
        record = store.put(
            content=str(item["text"]), source_uri=item.get("source_url"),
            run_id=run_id, content_ref=f"rag:{item.get('drug_name') or ''}:{item.get('section') or ''}",
            corpus_version=item.get("corpus_version") or corpus_version,
            retrieval_params={"query": query, "drug_name": item.get("drug_name"),
                              "section": item.get("section"), "mode": result.get("mode")},
            patient_revision=patient_revision, access_class=GENERAL_LABEL,
            scope_id=scope_id)
        excerpt = select_excerpt(str(item["text"]), query)
        view.append({"evidence_id": record.evidence_id, "excerpt": excerpt["excerpt"],
                     "excerpt_start": excerpt["start"], "total_chars": excerpt["total_chars"],
                     "omitted_chars": excerpt["omitted_chars"], "source_uri": item.get("source_url")})
    return view


def capture_from_ddi_warnings(store: EvidenceStore, result: dict[str, Any], *,
                              run_id: str | None,
                              patient_revision: int | None) -> list[dict[str, Any]]:
    view: list[dict[str, Any]] = []
    for warning in result.get("warnings") or []:
        text = warning.get("source_text")
        if not text:
            continue
        record = store.put(content=str(text), source_uri=warning.get("source_url"),
                           run_id=run_id, content_ref=f"ddi:{warning.get('drug_a')}x{warning.get('drug_b')}",
                           corpus_version=warning.get("corpus_version") or result.get("corpus_version"),
                           retrieval_params={"drug_a": warning.get("drug_a"),
                                             "drug_b": warning.get("drug_b"),
                                             "detection_path": warning.get("detection_path")},
                           patient_revision=patient_revision, access_class=GENERAL_LABEL)
        view.append({"evidence_id": record.evidence_id, "excerpt": select_excerpt(str(text))["excerpt"],
                     "source_uri": warning.get("source_url")})
    return view
