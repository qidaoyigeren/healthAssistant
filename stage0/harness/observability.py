"""Call-level observability: spans, replay dedup, optional OTel export.

Harness P1-C.  Contract:

* every model call (planner/composer/verifier) and tool dispatch gets a
  stable ``call_id``/``span_id`` tied to the run/operation identity, parent
  span, duration, usage references, receipt/cache hits, guard rejections and
  degradation reasons;
* checkpoint REPLAYS are deduplicated by a deterministic key
  (``run_id + kind + source + cycle + args_hash``): a replayed node bumps
  ``replay_count`` on the existing span instead of appending a duplicate,
  while a genuinely NEW attempt (different outcome) gets a fresh
  ``attempt_no`` row — before/after dedup counts are queryable;
* the durable usage/retry ledger stays in ``llm_attempts`` (single owner,
  P0); spans are observability metadata and NEVER authoritative;
* OpenTelemetry/OpenInference export is strictly optional and off by
  default (``STAGE0_OTEL_EXPORT=1`` plus an installed ``opentelemetry``
  package); export failures and queue overflow never touch the business
  path, and the domain audit/budget ledgers are unaffected by sampling.
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

try:
    from .runtime import RunContext
except ImportError:  # pragma: no cover - script-style import
    from runtime import RunContext  # type: ignore

logger = logging.getLogger("stage0.harness.observability")

SPAN_DDL = """
CREATE TABLE IF NOT EXISTS call_spans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    span_id TEXT NOT NULL UNIQUE,
    trace_id TEXT,
    parent_span_id TEXT,
    run_id TEXT NOT NULL,
    turn_id TEXT,
    cycle INTEGER,
    kind TEXT NOT NULL,
    source TEXT,
    call_id TEXT NOT NULL,
    attempt_id TEXT,
    dedup_key TEXT NOT NULL,
    attempt_no INTEGER NOT NULL DEFAULT 1,
    status TEXT,
    error_kind TEXT,
    duration_ms REAL,
    payload_chars INTEGER,
    receipt_replayed INTEGER NOT NULL DEFAULT 0,
    guard_rejected INTEGER NOT NULL DEFAULT 0,
    degradation TEXT,
    started_at TEXT NOT NULL,
    replay_count INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_call_spans_run ON call_spans(run_id, dedup_key);
"""

REPLAYABLE_KINDS = {"tool", "model"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class CallSpanStore:
    """Persists spans into the domain SQLite file (additive table) with
    replay dedup.  All failures are swallowed at the logging level — spans
    are metadata, never the business path."""

    def __init__(self, connection, lock=None):
        self.connection = connection
        self._lock = lock or threading.RLock()
        with self._lock:
            self.connection.executescript(SPAN_DDL)
            self.connection.commit()

    def record(self, span: dict[str, Any]) -> None:
        """Insert a span, or fold it into an existing row when it is a
        checkpoint replay (same dedup key AND same outcome signature)."""
        try:
            with self._lock, self.connection:
                existing = self.connection.execute(
                    "SELECT id, attempt_no, attempt_id, status, error_kind, receipt_replayed FROM call_spans "
                    "WHERE run_id=? AND dedup_key=? ORDER BY attempt_no DESC LIMIT 1",
                    (span["run_id"], span["dedup_key"])).fetchone()
                if (existing is not None
                        and existing["attempt_id"] == span.get("attempt_id")
                        and existing["status"] == span.get("status")
                        and (existing["error_kind"] or None) == span.get("error_kind")
                        and bool(existing["receipt_replayed"]) == bool(span.get("receipt_replayed"))):
                    self.connection.execute(
                        "UPDATE call_spans SET replay_count=replay_count+1 WHERE id=?",
                        (existing["id"],))
                    return
                attempt_no = (existing["attempt_no"] + 1) if existing is not None else 1
                self.connection.execute(
                    """INSERT INTO call_spans(span_id,trace_id,parent_span_id,run_id,turn_id,cycle,
                        kind,source,call_id,attempt_id,dedup_key,attempt_no,status,error_kind,
                        duration_ms,payload_chars,receipt_replayed,guard_rejected,degradation,
                        started_at,replay_count)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0)""",
                    (span["span_id"], span.get("trace_id"), span.get("parent_span_id"),
                     span["run_id"], span.get("turn_id"), span.get("cycle"), span["kind"],
                     span.get("source"), span["call_id"], span.get("attempt_id"),
                     span["dedup_key"], attempt_no, span.get("status"), span.get("error_kind"),
                     span.get("duration_ms"), span.get("payload_chars"),
                     1 if span.get("receipt_replayed") else 0,
                     1 if span.get("guard_rejected") else 0, span.get("degradation"),
                     span.get("started_at") or _now()))
        except Exception:
            logger.debug("span persistence failed", exc_info=True)

    def spans_for_run(self, run_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.connection.execute(
                "SELECT * FROM call_spans WHERE run_id=? ORDER BY id", (run_id,)).fetchall()
        return [dict(row) for row in rows]

    def dedup_report(self, run_id: str) -> dict[str, int]:
        with self._lock:
            row = self.connection.execute(
                "SELECT COUNT(*) AS rows_, COALESCE(SUM(replay_count),0) AS replays "
                "FROM call_spans WHERE run_id=?", (run_id,)).fetchone()
        return {"stored_spans": row["rows_"], "replays_folded": row["replays"],
                "raw_events": row["rows_"] + row["replays"]}


def _dedup_key(kind: str, source: str, cycle: Any, args_hash: str | None) -> str:
    return f"{kind}:{source}:{cycle}:{args_hash or '-'}"


class SpanRecorder:
    """Hook-based span collector.  Attach to the agent's ``HarnessHooks``;
    open spans are paired via (run_id, kind, source) — the worker processes
    one run at a time, so the pairing is single-deep."""

    def __init__(self, store: CallSpanStore | None = None, exporter: "OTelExporter | None" = None):
        self.store = store
        self.exporter = exporter
        self._open: dict[str, dict[str, Any]] = {}

    # ---- hook adapters -----------------------------------------------------

    def attach(self, hooks) -> None:
        hooks.add("before_model", self._before_model)
        hooks.add("after_model", self._after_model)
        hooks.add("before_tool", self._before_tool)
        hooks.add("after_tool", self._after_tool)
        hooks.add("before_publish", self._before_publish)

    @staticmethod
    def _ctx_id(event):
        ctx = event.ctx
        return (getattr(ctx, "run_id", None), getattr(ctx, "turn_id", None),
                getattr(ctx, "trace_id", None))

    @staticmethod
    def _parent_span_id(event) -> str | None:
        """Harness P3: a delegated worker's RunContext carries
        ``parent_span_id`` = the parent run's trace id, so parent→child
        correlation is queryable in call_spans without any evidence content."""
        return getattr(event.ctx, "parent_span_id", None)

    def _before_model(self, event):
        run_id, turn_id, trace_id = self._ctx_id(event)
        key = f"model:{event.kind}:{run_id}"
        self._open[key] = {"kind": "model", "source": event.kind, "run_id": run_id,
                           "turn_id": turn_id, "trace_id": trace_id,
                           "cycle": event.meta.get("cycle"),
                           "started": time.perf_counter(), "started_at": _now()}

    def _after_model(self, event):
        run_id, _, trace_id = self._ctx_id(event)
        key = f"model:{event.kind}:{run_id}"
        opened = self._open.pop(key, None)
        meta = event.meta or {}
        span = {
            "kind": "model", "source": event.kind, "run_id": run_id, "turn_id": opened and opened.get("turn_id"),
            "trace_id": trace_id, "parent_span_id": None,
            "cycle": meta.get("cycle", opened and opened.get("cycle")),
            "status": meta.get("status", "ok"),
            "error_kind": meta.get("error_code"),
            "duration_ms": round((opened and (time.perf_counter() - opened["started"]) * 1000)
                                 or meta.get("latency_ms") or 0, 3),
            "payload_chars": meta.get("payload_chars"),
            "guard_rejected": bool(meta.get("guard_rejected")),
            "degradation": meta.get("fallback_kind"),
            "receipt_replayed": False,
            "args_hash": None, "started_at": opened and opened.get("started_at"),
        }
        self._emit(span)

    def _before_tool(self, event):
        run_id, turn_id, trace_id = self._ctx_id(event)
        key = f"tool:{event.tool}:{run_id}"
        self._open[key] = {"turn_id": turn_id, "trace_id": trace_id,
                           "started": time.perf_counter(), "started_at": _now()}

    def _after_tool(self, event):
        run_id, _, trace_id = self._ctx_id(event)
        key = f"tool:{event.tool}:{run_id}"
        opened = self._open.pop(key, None)
        meta = event.meta or {}
        span = {
            "kind": "tool", "source": event.tool, "run_id": run_id,
            "turn_id": (opened or {}).get("turn_id"), "trace_id": trace_id,
            "parent_span_id": self._parent_span_id(event), "cycle": meta.get("cycle"),
            "status": "ok" if meta.get("ok") else "error",
            "error_kind": meta.get("error_kind"),
            "duration_ms": round((time.perf_counter() - (opened or {}).get("started", time.perf_counter())) * 1000, 3),
            "payload_chars": None,
            "guard_rejected": False,
            "degradation": None,
            "receipt_replayed": bool(meta.get("replayed")),
            "args_hash": meta.get("args_hash"),
            "started_at": (opened or {}).get("started_at"),
        }
        self._emit(span)

    def _before_publish(self, event):
        run_id, turn_id, trace_id = self._ctx_id(event)
        span = {"kind": "publish", "source": "publish", "run_id": run_id, "turn_id": turn_id,
                "trace_id": trace_id, "parent_span_id": None, "cycle": None,
                "status": "ok", "error_kind": None, "duration_ms": None,
                "payload_chars": None, "guard_rejected": False,
                "degradation": (event.meta or {}).get("degraded_reason"),
                "receipt_replayed": False, "args_hash": None, "started_at": _now()}
        self._emit(span)

    # ---- finalization ------------------------------------------------------

    def _emit(self, span: dict[str, Any]) -> None:
        run_id = span.get("run_id") or "unknown"
        call_id = uuid.uuid4().hex
        span_id = hashlib.sha256(json.dumps(
            [run_id, span.get("kind"), span.get("source"), span.get("cycle"), span.get("args_hash"),
             call_id]).encode()).hexdigest()[:16]
        record = {
            **span, "call_id": call_id, "span_id": span_id,
            "dedup_key": _dedup_key(span.get("kind") or "?", span.get("source") or "?",
                                    span.get("cycle"), span.get("args_hash")),
        }
        if self.store is not None and record.get("kind") in REPLAYABLE_KINDS | {"publish"}:
            self.store.record(record)
        if self.exporter is not None:
            self.exporter.export_span(record)


class OTelExporter:
    """Opt-in OTLP/HTTP export through the SDK's bounded background queue.

    Only the scalar allowlist crosses the transport. SDK queue overflow can
    drop telemetry; persisted call_spans remain the local audit source.
    Explicit flush/shutdown belongs to lifecycle/acceptance, never a turn.
    """

    MAX_QUEUE = 1024

    def __init__(self):
        self.enabled = False
        self.dropped = 0
        self._provider = None
        if not _env_flag("STAGE0_OTEL_EXPORT"):
            return
        try:
            from opentelemetry.sdk.resources import Resource
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import BatchSpanProcessor
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
            # Own provider: do not mutate the application's global provider.
            # Explicit Resource avoids exporting arbitrary resource env values.
            self._provider = TracerProvider(resource=Resource({"service.name": "stage0.harness"}))
            self._provider.add_span_processor(BatchSpanProcessor(
                OTLPSpanExporter(timeout=2), max_queue_size=self.MAX_QUEUE,
                max_export_batch_size=128, schedule_delay_millis=500,
                export_timeout_millis=2500))
            self._tracer = self._provider.get_tracer("stage0.harness")
            self.enabled = True
        except Exception as exc:  # pragma: no cover - depends on env
            logger.info("OTel export requested but unavailable: %s", type(exc).__name__)
            self.enabled = False

    def export_span(self, span: dict[str, Any]) -> None:
        if not self.enabled:
            return
        try:
            from opentelemetry.trace import Status, StatusCode
            fields = ("run_id", "turn_id", "trace_id", "parent_span_id", "span_id",
                      "call_id", "attempt_id", "cycle", "kind", "source", "status",
                      "error_kind", "duration_ms", "payload_chars", "receipt_replayed",
                      "guard_rejected", "degradation")
            attrs = {f"harness.{key}": value[:256] if isinstance(value, str) else value
                     for key in fields if isinstance((value := span.get(key)), (str, bool, int, float))}
            end = time.time_ns()
            duration = max(0, float(span.get("duration_ms") or 0))
            item = self._tracer.start_span(
                f"harness.{span.get('kind', 'unknown')}.{span.get('source', 'unknown')}",
                attributes=attrs, start_time=end - int(duration * 1_000_000))
            if span.get("status") == "error":
                item.set_status(Status(StatusCode.ERROR))
            item.end(end_time=end)
        except Exception:  # pragma: no cover
            self.dropped += 1
            logger.debug("otel export failed", exc_info=True)

    def flush(self, timeout_millis: int = 3000) -> bool:
        if not self.enabled or self._provider is None:
            return True
        try:
            return self._provider.force_flush(timeout_millis=timeout_millis)
        except Exception:
            return False

    def close(self) -> None:
        self.enabled = False
        provider, self._provider = self._provider, None
        if provider is not None:
            try:
                provider.shutdown()
            except Exception:
                logger.debug("otel shutdown failed", exc_info=True)


def _env_flag(name: str) -> bool:
    import os
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


def attach_recorder(agent, memory_store) -> SpanRecorder:
    """Build the recorder for an agent, persisting spans next to the domain
    data and wiring the optional OTel exporter."""
    store = CallSpanStore(memory_store.connection, memory_store._lock)
    exporter = OTelExporter()
    recorder = SpanRecorder(store=store, exporter=exporter)
    recorder.attach(agent.hooks)
    agent.span_store = store
    return recorder
