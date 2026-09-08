"""Controlled read-result reuse: same-run dedup + cross-run cache.

Harness P2.  Two distinct layers, both strictly READ-ONLY:

* same-run reuse — an identical read (same normalized signature: tool,
  arguments, scope, patient revision, corpus version) inside one run is
  served from memory instead of being re-dispatched.  A repeat is still
  reported to the no-progress tracker; reuse only makes it cheap.
* cross-run cache — a durable ``tool_cache`` table keyed by the full
  signature INCLUDING scope_id, patient revision, tool version and corpus
  version, with TTL and status.  Patient-specific results therefore never
  share a query-only key, and any fact/corpus/tool change is automatically
  a different key (invalidation by construction).

Safety rules (P2 constraints 5-7):

* ONLY successful read results are cached.  Failures, ``unknown`` outcomes,
  empty retrieval and stale results are never stored, so a cache hit can
  never stand in for "no risk".
* writes, review actions and permission checks are never cached: the
  executor consults reuse only for ``kind='read'`` AND
  ``idempotency='pure'`` AND ``cacheable=True`` specs.
* cache ≠ operation receipt: a receipt proves a domain effect happened; the
  cache merely saves recomputation of a pure read.  New genuine events are
  never swallowed by either.
* an explicit ``refresh: true`` argument bypasses the cache READ (still
  stores a fresh result) — the "active refresh" channel; it is stripped
  before schema validation so it never reaches a handler.
* every flag is wired and defaults OFF; eval compares both sides.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any

try:
    from .progress import read_signature
except ImportError:  # pragma: no cover - script-style import
    from progress import read_signature  # type: ignore


CACHE_DDL = """
CREATE TABLE IF NOT EXISTS tool_cache (
    cache_key TEXT PRIMARY KEY,
    scope_id TEXT NOT NULL,
    tool TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('ok')),
    result_json TEXT NOT NULL,
    result_hash TEXT NOT NULL,
    corpus_version TEXT,
    tool_version TEXT,
    patient_revision INTEGER,
    created_at TEXT NOT NULL,
    expires_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tool_cache_scope ON tool_cache(scope_id, tool, created_at);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _env_flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


class ToolCacheStore:
    """Durable cross-run cache of successful pure-read results."""

    def __init__(self, connection: sqlite3.Connection, lock: threading.RLock | None = None,
                 *, ttl_seconds: float = 900.0):
        self.connection = connection
        self._lock = lock or threading.RLock()
        self.ttl_seconds = ttl_seconds
        with self._lock:
            self.connection.executescript(CACHE_DDL)
            self.connection.commit()

    def get(self, cache_key: str, *, scope_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self.connection.execute(
                "SELECT result_json,expires_at FROM tool_cache WHERE cache_key=? AND scope_id=?",
                (cache_key, scope_id)).fetchone()
        if row is None:
            return None
        if row["expires_at"] <= time.time():
            with self._lock, self.connection:
                self.connection.execute("DELETE FROM tool_cache WHERE cache_key=?", (cache_key,))
            return None
        return json.loads(row["result_json"])

    def put(self, cache_key: str, *, scope_id: str, tool: str, result: Any,
            corpus_version: str | None = None, tool_version: str = "",
            patient_revision: int | None = None, ttl_seconds: float | None = None) -> None:
        ttl = self.ttl_seconds if ttl_seconds is None else ttl_seconds
        content = json.dumps(result, ensure_ascii=False, default=str)
        digest = __import__("hashlib").sha256(content.encode("utf-8")).hexdigest()[:16]
        with self._lock, self.connection:
            self.connection.execute(
                """INSERT OR REPLACE INTO tool_cache
                   (cache_key,scope_id,tool,status,result_json,result_hash,corpus_version,
                    tool_version,patient_revision,created_at,expires_at)
                   VALUES(?,?,?,'ok',?,?,?,?,?,?,?)""",
                (cache_key, scope_id, tool, content, digest, corpus_version, tool_version,
                 patient_revision, _now(), time.time() + ttl))

    def prune(self, *, older_than_days: float = 7.0) -> int:
        cutoff = time.time() - older_than_days * 86400
        with self._lock, self.connection:
            cursor = self.connection.execute("DELETE FROM tool_cache WHERE expires_at<=?", (cutoff,))
            return cursor.rowcount


class ReuseCoordinator:
    """The single object the executor talks to.  Counters expose the eval
    metrics (hits / misses / stores) without touching the span schema."""

    def __init__(self, *, connection: sqlite3.Connection | None = None,
                 lock: threading.RLock | None = None,
                 scope_id: str = "local-demo",
                 same_run: bool | None = None, cross_run: bool | None = None,
                 ttl_seconds: float = 900.0):
        self.same_run_enabled = _env_flag("STAGE0_RUN_REUSE") if same_run is None else same_run
        self.cross_run_enabled = _env_flag("STAGE0_READ_CACHE") if cross_run is None else cross_run
        self.scope_id = scope_id
        self.cache = (ToolCacheStore(connection, lock, ttl_seconds=ttl_seconds)
                      if (self.cross_run_enabled and connection is not None) else None)
        # run_id -> {signature: value}
        self._run_cache: dict[str, dict[str, Any]] = {}
        self._lock = threading.RLock()
        self.stats = {"same_run_hits": 0, "cross_run_hits": 0, "misses": 0, "stores": 0,
                      "refreshes": 0}

    # ---- lookup ------------------------------------------------------------

    def lookup(self, *, run_id: str, tool: str, arguments: dict[str, Any],
               scope_id: str | None, patient_revision: Any, corpus_version: Any = None,
               tool_version: str = "", force: bool = False) -> dict[str, Any] | None:
        """A cached result, or None.  ``force=True`` (or ``refresh: true`` in
        the arguments) bypasses the cache READ — the active-refresh channel;
        the fresh result is still stored for later runs."""
        force = force or bool(arguments.get("refresh"))
        if force:
            self.stats["refreshes"] += 1
        key = read_signature(tool, arguments, scope_id=scope_id,
                             patient_revision=patient_revision,
                             corpus_version=corpus_version, tool_version=tool_version)
        if not force and self.same_run_enabled:
            with self._lock:
                value = self._run_cache.get(run_id, {}).get(key)
            if value is not None:
                self.stats["same_run_hits"] += 1
                return {"value": value, "source": "same_run", "key": key}
        if not force and self.cross_run_enabled and self.cache is not None:
            value = self.cache.get(key, scope_id=scope_id or self.scope_id)
            if value is not None:
                self.stats["cross_run_hits"] += 1
                return {"value": value, "source": "cross_run", "key": key}
        self.stats["misses"] += 1
        return None

    def store(self, *, run_id: str, tool: str, arguments: dict[str, Any], value: Any,
              scope_id: str | None, patient_revision: Any,
              corpus_version: Any = None, tool_version: str = "") -> None:
        """Store a SUCCESSFUL pure-read result.  Never called for failures."""
        key = read_signature(tool, arguments, scope_id=scope_id,
                             patient_revision=patient_revision,
                             corpus_version=corpus_version, tool_version=tool_version)
        if self.same_run_enabled:
            with self._lock:
                self._run_cache.setdefault(run_id, {})[key] = value
        if self.cross_run_enabled and self.cache is not None:
            self.cache.put(key, scope_id=scope_id or self.scope_id, tool=tool,
                           result=value, corpus_version=str(corpus_version) if corpus_version else None,
                           tool_version=tool_version, patient_revision=patient_revision)
        self.stats["stores"] += 1

    def reset_run(self, run_id: str) -> None:
        with self._lock:
            self._run_cache.pop(run_id, None)
