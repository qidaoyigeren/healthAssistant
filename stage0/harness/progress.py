"""Run progress events, resumable cancellation and no-progress detection.

Harness P2.  Three contracts over the same SQLite file (additive tables only):

* ``ProgressEventStore`` — a small, product-facing progress ledger per run.
  Events are stable, replay-idempotent (graph node re-runs fold onto the same
  ``event_id``) and carry NO unreviewed medical text, raw graph state or
  hidden reasoning: only event kind, cycle, tool name and coarse counters.
  The API serves them with a numeric cursor for reconnect-safe polling.
* cancellation — a durable ``run_cancel_requests`` row plus an in-process
  ``threading.Event`` registry the runners wire into ``RunContext``.
  ``cancel_requested`` and ``cancelled`` are distinct states; cancellation is
  idempotent, role/scope checked at the API edge, CAS-guarded against
  terminal runs, and never rolls back already-committed domain effects.
* ``NoProgressTracker`` — loop-level detection of repeated identical read
  attempts.  A single repeat is a structured ``no_progress`` feedback (NOT a
  safety violation); after a configurable threshold the loop stops re-planning
  and finishes safely.  State is persisted per run so a restart does not reset
  the counter, and signatures include the patient revision / corpus version so
  real progress (facts changed, new information) is never misclassified.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from typing import Any

# Product-facing event vocabulary (P2 constraint 11).  Deliberately small and
# free of clinical content — no warning bodies, no raw tool results.
EVENT_ACCEPTED = "accepted"                  # 任务已受理（出队）
EVENT_ORGANIZING = "organizing"              # 整理记录（memory_read/write、澄清等）
EVENT_RETRIEVING = "retrieving"              # 检索依据（rag_search）
EVENT_CHECKING_RISKS = "checking_risks"      # 核对风险（ddi_check）
EVENT_WAITING_REVIEW = "waiting_review"      # 等待人工审核
EVENT_COMPLETED = "completed"                # 完成
EVENT_FAILED = "failed"                      # 失败
EVENT_CANCEL_REQUESTED = "cancel_requested"  # 收到取消请求
EVENT_CANCELLED = "cancelled"                # 取消完成

# Tools map to the product vocabulary; unknown tools keep a generic event.
TOOL_EVENT_KINDS = {
    "rag_search": EVENT_RETRIEVING,
    "ddi_check": EVENT_CHECKING_RISKS,
    "memory_read": EVENT_ORGANIZING,
    "memory_write": EVENT_ORGANIZING,
    "ask_clarification": EVENT_ORGANIZING,
    "read_evidence": EVENT_ORGANIZING,
}

TERMINAL_RUN_STATUSES = frozenset({"succeeded", "degraded", "failed", "cancelled"})

PROGRESS_DDL = """
CREATE TABLE IF NOT EXISTS run_progress_events (
    event_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    kind TEXT NOT NULL,
    cycle INTEGER,
    tool TEXT,
    detail_json TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_run_progress_run ON run_progress_events(run_id, seq);
CREATE TABLE IF NOT EXISTS run_cancel_requests (
    run_id TEXT PRIMARY KEY,
    state TEXT NOT NULL CHECK(state IN ('requested','cancelled')),
    requested_by TEXT,
    reason TEXT,
    requested_at TEXT NOT NULL,
    settled_at TEXT
);
CREATE TABLE IF NOT EXISTS run_progress_state (
    run_id TEXT PRIMARY KEY,
    last_signature TEXT,
    repeats INTEGER NOT NULL DEFAULT 0,
    stopped_reason TEXT,
    updated_at TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _stable_event_id(run_id: str, kind: str, cycle: Any, tool: str | None,
                     detail_key: str) -> str:
    """Replay-stable id: the same logical event re-emitted by a graph node
    replay folds onto the same row instead of duplicating."""
    key = json.dumps([run_id, kind, cycle, tool, detail_key], ensure_ascii=False)
    return "pe-" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:20]


class ProgressEventStore:
    """Append-only progress ledger (same database file as the domain store)."""

    def __init__(self, connection: sqlite3.Connection, lock: threading.RLock | None = None):
        self.connection = connection
        self._lock = lock or threading.RLock()
        with self._lock:
            self.connection.executescript(PROGRESS_DDL)
            self.connection.commit()

    # ---- emit ---------------------------------------------------------------

    def emit(self, run_id: str, kind: str, *, cycle: int | None = None,
             tool: str | None = None, detail: dict[str, Any] | None = None) -> str | None:
        """Emit one event.  Returns the event_id, or None when the store
        failed — progress events are observability, never the business path."""
        try:
            detail_key = hashlib.sha256(json.dumps(detail or {}, ensure_ascii=False,
                                                   sort_keys=True, default=str)
                                        .encode("utf-8")).hexdigest()[:12]
            event_id = _stable_event_id(run_id, kind, cycle, tool, detail_key)
            with self._lock, self.connection:
                seq = self.connection.execute(
                    "SELECT COALESCE(MAX(seq),0)+1 FROM run_progress_events WHERE run_id=?",
                    (run_id,)).fetchone()[0]
                self.connection.execute(
                    """INSERT OR IGNORE INTO run_progress_events
                       (event_id,run_id,seq,kind,cycle,tool,detail_json,created_at)
                       VALUES(?,?,?,?,?,?,?,?)""",
                    (event_id, run_id, seq, kind, cycle, tool,
                     json.dumps(detail or {}, ensure_ascii=False), _now()))
            return event_id
        except Exception:
            return None

    # ---- cursor read ---------------------------------------------------------

    def events_since(self, run_id: str, after_seq: int = 0, *, limit: int = 200,
                     scope_id: str | None = None) -> dict[str, Any]:
        """Cursor page.  ``snapshot`` marks that the cursor predates retained
        history (pruned) — the client should reset its dedup window."""
        with self._lock:
            rows = self.connection.execute(
                "SELECT event_id,seq,kind,cycle,tool,detail_json,created_at "
                "FROM run_progress_events WHERE run_id=? AND seq>? ORDER BY seq LIMIT ?",
                (run_id, after_seq, limit)).fetchall()
            latest = self.connection.execute(
                "SELECT COALESCE(MAX(seq),0) FROM run_progress_events WHERE run_id=?",
                (run_id,)).fetchone()[0]
            next_retained = self.connection.execute(
                "SELECT MIN(seq) FROM run_progress_events WHERE run_id=? AND seq>?",
                (run_id, after_seq)).fetchone()[0]
        events = [{"event_id": row["event_id"], "seq": row["seq"], "kind": row["kind"],
                   "cycle": row["cycle"], "tool": row["tool"],
                   "detail": json.loads(row["detail_json"] or "{}"),
                   "created_at": row["created_at"]} for row in rows]
        # Cursor reconnect contract: a gap between the client's cursor and the
        # next retained event (pruned history) means the page is a SNAPSHOT,
        # not a contiguous replay — the client resets its dedup window.
        snapshot = after_seq > 0 and next_retained is not None and next_retained > after_seq + 1
        return {"run_id": run_id, "events": events, "latest_seq": latest,
                "snapshot": snapshot, "scope_id": scope_id}

    def prune(self, *, older_than_days: float, active_run_ids: set[str]) -> int:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=older_than_days)).isoformat(timespec="seconds")
        with self._lock:
            rows = self.connection.execute(
                "SELECT DISTINCT run_id FROM run_progress_events WHERE created_at<?",
                (cutoff,)).fetchall()
            removed = 0
            for row in rows:
                if row["run_id"] in active_run_ids:
                    continue
                with self.connection:
                    self.connection.execute(
                        "DELETE FROM run_progress_events WHERE run_id=?", (row["run_id"],))
                removed += 1
        return removed


# ---- cancellation ------------------------------------------------------------

CANCEL_EVENTS: dict[str, threading.Event] = {}
_CANCEL_LOCK = threading.Lock()


def cancel_event_for(run_id: str) -> threading.Event:
    """The in-process cancellation handle a runner binds into RunContext."""
    with _CANCEL_LOCK:
        event = CANCEL_EVENTS.get(run_id)
        if event is None:
            event = threading.Event()
            CANCEL_EVENTS[run_id] = event
        return event


def release_cancel_event(run_id: str) -> None:
    """Drop the handle when the run reaches a terminal state (worker side)."""
    with _CANCEL_LOCK:
        CANCEL_EVENTS.pop(run_id, None)


def cancel_state(store: Any, run_id: str) -> str | None:
    """None (never requested) | 'requested' | 'cancelled'."""
    try:
        with store._lock:
            row = store.connection.execute(
                "SELECT state FROM run_cancel_requests WHERE run_id=?", (run_id,)).fetchone()
        return row["state"] if row is not None else None
    except Exception:
        return None


def is_cancel_requested(store: Any, run_id: str) -> bool:
    return cancel_state(store, run_id) is not None


def request_cancel(store: Any, run_id: str, *, actor: str = "api",
                   reason: str | None = None) -> dict[str, Any]:
    """Idempotent, CAS-guarded cancellation request.

    Contract:

    * an unknown run → ``{"state": "unknown_run"}``;
    * a terminal run (succeeded/degraded/failed) → ``{"state": "already_final"}``
      — cancellation never rewrites history, and already-committed domain
      effects are never rolled back;
    * a run already cancelled → ``{"state": "cancelled"}`` (idempotent);
    * a run already requested → ``{"state": "requested"}`` (idempotent);
    * otherwise the durable row is created, the run row is CAS-moved to
      ``cancelled`` ONLY from a non-terminal status, an open review case of
      this run is closed (a late reviewer decision can no longer revive it)
      and the in-process cancel event is set for the running turn.

    The API layer verifies role/scope before calling; nothing here trusts
    model output.
    """
    run = store.workflow_run_get(run_id)
    if run is None:
        return {"state": "unknown_run"}
    existing = cancel_state(store, run_id)
    if existing == "cancelled":
        return {"state": "cancelled", "run_status": run["status"]}
    if run["status"] == "cancelled":
        # The CAS below already moved the run (another request raced ahead);
        # report the settled state, never a stale 'requested'.
        return {"state": "cancelled", "run_status": "cancelled"}
    if run["status"] in TERMINAL_RUN_STATUSES - {"cancelled"}:
        return {"state": "already_final", "run_status": run["status"]}
    if existing == "requested":
        return {"state": "requested", "run_status": run["status"]}
    with store._lock, store.connection:
        previous = store.connection.execute(
            "SELECT status FROM workflow_runs WHERE run_id=?", (run_id,)).fetchone()
        if previous is None or previous["status"] in TERMINAL_RUN_STATUSES - {"cancelled"}:
            # Lost the race against a publish/失败 — final state wins.
            return {"state": "already_final",
                    "run_status": previous["status"] if previous else None}
        if previous["status"] == "cancelled":
            state_row = store.connection.execute(
                "SELECT state FROM run_cancel_requests WHERE run_id=?", (run_id,)).fetchone()
            if state_row is not None:
                return {"state": state_row["state"], "run_status": "cancelled"}
        store.connection.execute(
            """INSERT INTO run_cancel_requests(run_id,state,requested_by,reason,requested_at)
               VALUES(?,'requested',?,?,?)
               ON CONFLICT(run_id) DO UPDATE SET state=state""",
            (run_id, actor, reason, _now()))
        store.connection.execute(
            "UPDATE workflow_runs SET status='cancelled',updated_at=? WHERE run_id=? "
            "AND status NOT IN ('succeeded','degraded','failed','cancelled')",
            (_now(), run_id))
        # Pending resume tasks of this run can no longer revive it.
        store.connection.execute(
            "UPDATE resume_tasks SET status='cancelled' "
            "WHERE run_id=? AND status='pending'", (run_id,))
    # Waiting-review runs: close the open case so a late decision is refused.
    _close_open_review_case(store, run_id, actor)
    cancel_event_for(run_id).set()
    return {"state": "cancelled", "run_status": "cancelled"}


def _close_open_review_case(store: Any, run_id: str, actor: str) -> None:
    try:
        cases = store.review_cases(statuses=["open"])
    except Exception:
        return
    for case in cases:
        if case.get("run_id") == run_id:
            try:
                store.close_review_case(case["id"], status="cancelled",
                                        reason="run_cancelled", actor=actor)
            except Exception:
                pass


def mark_cancelled(store: Any, run_id: str) -> None:
    """Settle the request row once the run actually stopped (worker side)."""
    try:
        with store._lock, store.connection:
            store.connection.execute(
                "UPDATE run_cancel_requests SET state='cancelled',settled_at=? WHERE run_id=?",
                (_now(), run_id))
    except Exception:
        pass


# ---- no-progress detection -----------------------------------------------------


def read_signature(tool: str, arguments: Any, *, scope_id: str | None,
                   patient_revision: Any, corpus_version: Any = None,
                   tool_version: str = "") -> str:
    """Normalized read signature (P2 constraint 1): tool identity, canonical
    arguments, patient scope+revision and corpus version.  Two signatures are
    equal only when the world they read has not changed, so a changed revision
    (write, review round, new user information) is always fresh progress."""
    try:
        canonical = json.dumps({"tool": tool, "args": arguments,
                                "scope": scope_id, "rev": patient_revision,
                                "corpus": corpus_version, "v": tool_version},
                               ensure_ascii=False, sort_keys=True, default=str)
    except Exception:
        canonical = repr((tool, arguments, scope_id, patient_revision))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]


class NoProgressTracker:
    """Persisted repeat counter per run (restart-safe, replay-safe).

    ``record`` classifies one executed read observation:

    * a NEW signature (world changed / different call) is progress — the
      counter resets;
    * a REPEATED signature with no interleaved domain effect is a no-progress
      step.  The first repeats return ``verdict='repeat'`` so the loop feeds
      the planner structured feedback; at the configured threshold the verdict
      is ``'stop'`` and the loop finishes safely with explicit unfinished items.

    A domain write, a revision change, or a cancel/interrupt are NOT passed
    through here: callers reset the counter on those, so real progress and
    bounded fault recovery are never mis-killed.
    """

    def __init__(self, connection: sqlite3.Connection, lock: threading.RLock | None = None):
        self.connection = connection
        self._lock = lock or threading.RLock()
        with self._lock:
            self.connection.executescript(PROGRESS_DDL)
            self.connection.commit()

    def _row(self, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self.connection.execute(
                "SELECT * FROM run_progress_state WHERE run_id=?", (run_id,)).fetchone()
        return dict(row) if row is not None else None

    def reset(self, run_id: str, *, reason: str = "progress") -> None:
        with self._lock, self.connection:
            self.connection.execute(
                """INSERT INTO run_progress_state(run_id,last_signature,repeats,stopped_reason,updated_at)
                   VALUES(?,NULL,0,?,?)
                   ON CONFLICT(run_id) DO UPDATE SET last_signature=NULL,repeats=0,updated_at=?""",
                (run_id, None if reason == "progress" else reason, _now(), _now()))

    def record(self, run_id: str, signature: str, *, limit: int) -> dict[str, Any]:
        row = self._row(run_id)
        previous = (row or {}).get("last_signature")
        repeats = int((row or {}).get("repeats") or 0)
        if row is not None and row.get("stopped_reason"):
            return {"verdict": "stopped", "repeats": repeats, "signature": signature}
        if previous == signature:
            repeats += 1
        else:
            repeats = 0
        verdict = "progress" if repeats == 0 else ("stop" if repeats >= limit else "repeat")
        with self._lock, self.connection:
            self.connection.execute(
                """INSERT INTO run_progress_state(run_id,last_signature,repeats,stopped_reason,updated_at)
                   VALUES(?,?,?,?,?)
                   ON CONFLICT(run_id) DO UPDATE SET last_signature=excluded.last_signature,
                     repeats=excluded.repeats,stopped_reason=excluded.stopped_reason,
                     updated_at=excluded.updated_at""",
                (run_id, signature, repeats, "no_progress" if verdict == "stop" else None, _now()))
        return {"verdict": verdict, "repeats": repeats, "signature": signature}

    def stop_pending(self, run_id: str) -> bool:
        """True once this run has hit the no-progress threshold (the graph's
        unconditional execute→plan edge consults this at the next plan)."""
        row = self._row(run_id)
        return bool(row and row.get("stopped_reason"))
