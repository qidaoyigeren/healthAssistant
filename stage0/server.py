"""FastAPI service layer (Stage 10 + Reliability P0): async events, two-layer
idempotency, transactional outbox with lease fencing, server-side identity.

Deployment form (the default Streamlit direct path is unchanged and remains
the offline default):

* ``POST /v1/events`` is **async-first**: it atomically claims the
  request-level ``Idempotency-Key`` and enqueues a ``process_event`` outbox
  task in one transaction, then returns ``202`` with a polling URL.  A single
  in-process worker thread executes agent turns — the single-writer topology
  the memory store is designed for.
* Two idempotency layers stay deliberately separate: the request-level key
  guards API acceptance (same key + same payload replays; same key + different
  payload → 422), while the domain-level ``client_event_id`` (``api:{key}``)
  threaded into the agent turn guards the projection itself.
* Reliability P0: the server generates ``event_id``/``run_id`` (uuid) per
  accepted event; the agent turn's ``turn_id`` is the ``run_id`` — never a
  truncation of the key, so two legal keys sharing a long prefix cannot
  collide.  Worker complete/fail/heartbeat are lease-fenced; task completion
  and the idempotency-key state commit in ONE transaction; failures are
  classified (retryable/permanent/safety/effect_unknown) with backoff+jitter
  before reclaiming.
* Identity: a trusted ``Principal`` is resolved server-side (local-demo by
  default; the ``deployment`` auth mode refuses to start without
  ``STAGE0_AUTH_TOKEN``).  Request-body ``actor``/``source`` are treated as
  claimed, never as authorization.

Run::

    python -m uvicorn stage0.server:app --host 127.0.0.1 --port 8000
    # or: python -m stage0.server  (dev convenience)

Honest boundary: one writer process only (do NOT run uvicorn --workers >1);
readers may scale out against the WAL.  At-least-once execution with
receipt-based effect dedup — not end-to-end exactly-once.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from fastapi import FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

try:
    from .agent import CareEvent, MedicationCoordinatorAgent
    from .graph_runner import make_runner
    from .memory import (
        DEFAULT_DB,
        LeaseRejected,
        MemoryPolicyError,
        MemoryStore,
        memory_ref,
    )
except ImportError:  # Support ``python stage0/server.py``.
    from agent import CareEvent, MedicationCoordinatorAgent  # type: ignore
    from graph_runner import make_runner  # type: ignore
    from memory import (  # type: ignore
        DEFAULT_DB,
        LeaseRejected,
        MemoryPolicyError,
        MemoryStore,
        memory_ref,
    )


logger = logging.getLogger("stage0.server")

IDEMPOTENCY_KEY_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
AUTH_HEADER = "X-Stage0-Token"

# Simple per-principal request rate limit (Reliability P0 背压): requests per
# minute per principal on POST /v1/events.
EVENT_RATE_LIMIT_PER_MINUTE = int(os.getenv("STAGE0_EVENT_RATE_LIMIT", "60"))


class ApiError(Exception):
    """Structured API error following the four-category error model (D1)."""

    def __init__(self, status_code: int, code: str, category: str, message: str,
                 details: dict[str, Any] | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.category = category
        self.message = message
        self.details = details or {}


@dataclass(frozen=True)
class Principal:
    """Trusted identity resolved server-side.  Never derived from the request
    body: body ``actor``/``source`` stay self-claimed fields."""

    user_id: str
    roles: frozenset[str]
    scope_id: str = "local-demo"

    def has_role(self, *roles: str) -> bool:
        return bool(self.roles & set(roles))


def _resolve_principal(request: Request, auth_mode: str, token_roles: dict[str, frozenset[str]]) -> Principal:
    if auth_mode == "deployment":
        token = request.headers.get(AUTH_HEADER, "")
        roles = token_roles.get(token)
        if roles is None:
            raise ApiError(401, "unauthorized", "validation",
                           "missing or invalid credentials")
        return Principal(user_id=f"principal-{uuid.uuid5(uuid.NAMESPACE_URL, token).hex[:12]}",
                         roles=roles)
    # local-demo: explicit single-patient demo principal.  create_app already
    # logged that this profile is in effect.
    return Principal(user_id="local-demo-caregiver",
                     roles=frozenset({"caregiver", "ops", "reviewer"}))


def _require_role(principal: Principal, *roles: str) -> None:
    if not principal.has_role(*roles):
        raise ApiError(403, "forbidden", "validation",
                       "this operation requires one of the roles: " + "/".join(roles))


def _authorize_scope(principal: Principal, scope_id: str) -> None:
    """Object-level authorization: opaque ids never substitute for a scope
    check.  In the single-patient demo every object is 'local-demo'."""
    if principal.scope_id != scope_id:
        raise ApiError(403, "forbidden", "validation",
                       "this object belongs to another scope")


def classify_error(exc: BaseException) -> str:
    """Classify a worker failure for the retry policy (Reliability P0).

    retryable → backoff + reclaim; permanent/safety → failure queue;
    effect_unknown is produced by the external-effect paths (P2), not here.
    """
    if isinstance(exc, MemoryPolicyErrorBase):
        # Storage-layer policy violations (idempotency reuse, unsupported
        # operations) do not heal on retry.
        return "permanent"
    if isinstance(exc, RuntimeError) and "safety boundary" in str(exc):
        return "safety"
    name = type(exc).__name__
    if name in {"TimeoutError", "ConnectionError", "APITimeoutError",
                "APIConnectionError", "SocketTimeoutError"}:
        return "retryable"
    # Unknown failures are retried within the attempt cap — the store keeps
    # the original event identity, so a retry cannot create a second event.
    return "retryable"


# MemoryPolicyError is imported lazily to keep the try/except import block
# above simple; both import styles expose it from the memory module.
def _memory_policy_error() -> type:
    try:
        from .memory import MemoryPolicyError
    except ImportError:  # pragma: no cover - script-style import
        from memory import MemoryPolicyError  # type: ignore
    return MemoryPolicyError


MemoryPolicyErrorBase = _memory_policy_error()


class EventIn(BaseModel):
    event_type: str = Field(min_length=1, max_length=64)
    text: str = Field(min_length=1, max_length=4000)
    payload: dict[str, Any] = Field(default_factory=dict)
    source: str = "caregiver"
    occurred_at: str | None = None
    session_id: str = "default"


class ConflictActionIn(BaseModel):
    action: str
    basis: str
    actor: str = "caregiver"  # self-claimed; authorization uses the Principal
    chosen_ref: str | None = None


class RecheckIn(BaseModel):
    max_jobs: int = 5


class _Heartbeat:
    """Renews the executing task's lease on a background timer so a long
    agent turn (LLM cycles up to the 120s budget) is not reclaimed mid-run."""

    def __init__(self, store: MemoryStore, task_id: int, lease_token: str, ttl: int):
        self._store = store
        self._task_id = task_id
        self._lease_token = lease_token
        self._ttl = ttl
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.lost = False

    def __enter__(self) -> "_Heartbeat":
        interval = max(1.0, self._ttl / 3.0)

        def beat() -> None:
            while not self._stop.wait(interval):
                try:
                    ok = self._store.heartbeat_outbox_task(
                        self._task_id, lease_token=self._lease_token, lease_ttl_seconds=self._ttl)
                    if not ok:
                        self.lost = True
                        return
                except Exception:
                    logger.debug("heartbeat failed for task %s", self._task_id, exc_info=True)

        self._thread = threading.Thread(target=beat, name=f"outbox-heartbeat-{self._task_id}", daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)


class OutboxWorker:
    """Single in-process consumer for outbox tasks and durable rechecks.

    At-least-once delivery via expiring, heartbeat-renewed leases; effectively-
    once effects via operation receipts and the domain-level client_event_id
    inside the agent turn.  Crash recovery re-opens expired leases (attempts
    capped; classified failures back off before reclaiming).  The turn itself
    executes through the routed ``AgentRunner`` (Reliability P1: legacy or
    LangGraph per feature flag / run version).
    """

    def __init__(self, store: MemoryStore,
                 runner_factory: Callable[[], Any],
                 *, poll_interval: float = 0.2, lease_ttl_seconds: int = 300):
        self.store = store
        self.runner_factory = runner_factory
        self.poll_interval = poll_interval
        self.lease_ttl_seconds = lease_ttl_seconds
        self._runner: Any | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def runner(self) -> Any:
        if self._runner is None:
            self._runner = self.runner_factory()
        return self._runner

    @property
    def agent(self) -> MedicationCoordinatorAgent:
        # Rechecks and agent-level integrations keep working through the
        # routed runner's underlying agent.
        return self.runner.agent

    def drain_once(self, *, max_tasks: int = 5) -> list[dict[str, Any]]:
        receipts: list[dict[str, Any]] = []
        for _ in range(max_tasks):
            task = self.store.claim_outbox_task(lease_ttl_seconds=self.lease_ttl_seconds)
            if task is None:
                break
            receipts.append(self._run_claimed(task))
        # Reliability P2: overdue sweep is operational bookkeeping only —
        # timeout never auto-approves a case.
        try:
            self.store.mark_overdue_review_cases()
        except Exception:
            logger.warning("review overdue sweep failed", exc_info=True)
        # Reliability P2: apply recorded review decisions to parked runs.
        try:
            receipts.extend(self.drain_resume_tasks())
        except Exception:
            logger.warning("resume-task pass failed", exc_info=True)
        # Unified loop: durable rechecks ride the same worker.  A recheck
        # failure is logged, never swallowed silently (Reliability P0).
        try:
            self.agent.run_pending_rechecks()
        except Exception:
            logger.warning("pending rechecks pass failed", exc_info=True)
        return receipts

    def drain_resume_tasks(self, *, limit: int = 10) -> list[dict[str, Any]]:
        """Consume pending resume tasks: re-verify, resume the parked run via
        ``Command(resume=...)``, then converge case/run status.  The run-side
        apply_review node re-validates everything (authoritative)."""
        receipts: list[dict[str, Any]] = []
        for task in self.store.pending_resume_tasks(limit=limit):
            decision_id = task["decision_id"]
            record = self.store.review_decision(decision_id)
            if record is None or record.get("outcome") == "applied":
                self.store.consume_resume_task(task["id"], operation_id=task["operation_id"])
                continue
            run = self.store.workflow_run_get(task["run_id"])
            if run is None or run["status"] != "waiting_review":
                # The run can no longer accept this decision (terminal or
                # cancelled): consume without applying, keep the audit trail.
                logger.warning("resume task for run %s skipped: status=%s",
                               task["run_id"], run["status"] if run else "missing")
                self.store.set_review_decision_outcome(decision_id, outcome="unapplicable")
                self.store.consume_resume_task(task["id"], operation_id=task["operation_id"])
                continue
            decision_payload = {
                "decision_id": decision_id,
                "case_id": record["case_id"],
                "case_revision": record["case_revision"],
                "action": record["action"],
                "payload": record.get("payload") or {},
                "actor_id": record.get("actor_id"),
            }
            final = self.runner.resume(task["run_id"], decision_payload)
            self.store.consume_resume_task(task["id"], operation_id=task["operation_id"])
            if "__interrupt__" in final:
                # Re-parked (review_stale round opened): still waiting.
                self.store.workflow_run_update(task["run_id"], status="waiting_review")
            else:
                case = self.store.review_case(record["case_id"])
                if case is not None and case["status"] == "in_review":
                    self.store.close_review_case(
                        record["case_id"], status="resolved",
                        reason=f"review applied: {record['action']}",
                        actor=record.get("actor_id") or "reviewer")
            logger.info("resume task consumed task_id=%s decision_id=%s run_id=%s",
                        task["id"], decision_id, task["run_id"])
            receipts.append({"task_id": task["id"], "status": "resumed",
                             "decision_id": decision_id})
        return receipts

    def _run_claimed(self, task: dict[str, Any]) -> dict[str, Any]:
        payload = task["payload"] or {}
        trace_id = payload.get("trace_id") or "unknown"
        try:
            with _Heartbeat(self.store, task["id"], task["lease_token"], self.lease_ttl_seconds):
                result = self._execute(task)
        except LeaseRejected:
            # Our lease expired and another worker took over; their result
            # wins.  Ours must be discarded, not failed (the task is not ours
            # to fail) and not retried here.
            logger.warning("lease lost on task %s (trace %s); result discarded",
                           task["id"], trace_id)
            return {"task_id": task["id"], "status": "lease_lost"}
        except Exception as exc:
            return self._fail_claimed(task, exc, trace_id)
        key = payload.get("idempotency_key")
        event_key = payload.get("event_key")
        # ONE transaction: task done + key committed + stored response.
        self.store.publish_event_result(
            task["id"], lease_token=task["lease_token"],
            idempotency_key=key, event_key=event_key, result=result)
        # Reliability P2: a turn parked at clinical review publishes its safe
        # waiting response and RELEASES the worker immediately — the review
        # wait never occupies the lease, the thread or a transaction.
        if result.get("run_status") == "waiting_review" and payload.get("run_id"):
            self.store.workflow_run_update(payload["run_id"], status="waiting_review")
        logger.info("outbox task done task_id=%s trace_id=%s event_id=%s run_id=%s",
                    task["id"], trace_id, payload.get("event_id"), payload.get("run_id"))
        return {"task_id": task["id"], "status": "done"}

    def _fail_claimed(self, task: dict[str, Any], exc: BaseException,
                      trace_id: str) -> dict[str, Any]:
        error_class = classify_error(exc)
        # The task error column is the ops audit surface (capped); the log
        # line below deliberately carries only the type and ids — no patient
        # text or secrets (Reliability P0 log sanitization).
        error = f"{type(exc).__name__}: {exc}"
        payload = task["payload"] or {}
        try:
            status = self.store.fail_outbox_task(
                task["id"], lease_token=task["lease_token"], error=error,
                error_class=error_class)
        except LeaseRejected:
            logger.warning("lease lost while failing task %s (trace %s)", task["id"], trace_id)
            return {"task_id": task["id"], "status": "lease_lost"}
        logger.error("outbox task failed task_id=%s trace_id=%s error_class=%s "
                     "exception_type=%s run_id=%s",
                     task["id"], trace_id, error_class, type(exc).__name__,
                     payload.get("run_id"))
        if status == "failed":
            key = payload.get("idempotency_key")
            if key:
                self.store.complete_idempotency_key(key, response={
                    "error": {
                        "code": "event_failed",
                        "category": "internal" if error_class == "permanent" else error_class,
                        "retryable": error_class == "retryable",
                        "recovery": f"POST /v1/events/{key}/retry with a principal holding ops role",
                    }}, status_code=500, failed=True)
        return {"task_id": task["id"], "status": "error", "error_class": error_class}

    def _execute(self, task: dict[str, Any]) -> dict[str, Any]:
        if task["task_type"] != "process_event":
            raise RuntimeError(f"unsupported outbox task type: {task['task_type']}")
        payload = task["payload"]
        event_fields = dict(payload["event"])
        # session_id routes the turn; it is not part of the CareEvent itself.
        session_id = event_fields.pop("session_id", payload.get("session_id", "default"))
        event = CareEvent(**event_fields)
        response = self.runner.run(
            event=event, session_id=session_id,
            turn_id=payload["turn_id"],
            client_event_id=payload.get("event_key"),
            event_id=payload.get("event_id"),
            run_id=payload.get("run_id") or payload["turn_id"])
        audit = response.audit_trail or {}
        return {
            "text": response.text,
            "warnings": response.warnings,
            "conflicts": response.conflicts,
            "audit_trail": audit,
            "safety_status": response.safety_status,
            # Structured per-operation outcomes (frontend round): what the
            # store actually did — never a blanket "operation succeeded".
            "operation_outcomes": list(getattr(response, "operation_outcomes", []) or []),
            "event_id": payload.get("event_id"),
            "run_id": payload.get("run_id"),
            # Reliability P2: waiting-for-review turns publish run status +
            # review case at the TOP LEVEL so clients (UI banner, summary
            # export) see the non-terminal state without parsing audit.
            "run_status": audit.get("run_status"),
            "review_case": audit.get("review_case"),
        }

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()

        def loop() -> None:
            while not self._stop.is_set():
                try:
                    self.drain_once()
                except Exception:
                    # Never let the queue die silently (Reliability P0).
                    logger.exception("outbox worker cycle failed")
                self._stop.wait(self.poll_interval)

        self._thread = threading.Thread(target=loop, name="outbox-worker", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 10.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None
        if self._runner is not None and hasattr(self._runner, "close"):
            self._runner.close()


def create_app(*, db_path: str | Path | None = None,
               agent_factory: Callable[[MemoryStore], MedicationCoordinatorAgent] | None = None,
               runner_factory: Callable[[], Any] | None = None,
               checkpoint_path: str | None = None,
               worker_thread: bool = True,
               auth_mode: str | None = None) -> FastAPI:
    """Build the service app.  ``worker_thread=False`` keeps the worker
    unstarted so tests can drive ``worker.drain_once()`` deterministically.

    ``auth_mode``: ``local-demo`` (default) uses an explicit demo principal;
    ``deployment`` requires ``STAGE0_AUTH_TOKEN`` (+ optional
    ``STAGE0_AUTH_ROLES``) and refuses to start anonymously otherwise.
    ``runner_factory`` (Reliability P1): overrides the routed AgentRunner;
    default routes legacy/LangGraph per ``AGENT_GRAPH_RUNNER``.
    """
    # Auth validation happens BEFORE any database is opened, so a misconfigured
    # deployment fails fast without touching storage.
    resolved_auth_mode = (auth_mode or os.getenv("STAGE0_AUTH_MODE", "local-demo")).strip().lower() or "local-demo"
    token_roles: dict[str, frozenset[str]] = {}
    if resolved_auth_mode == "deployment":
        token = os.getenv("STAGE0_AUTH_TOKEN", "").strip()
        if not token:
            raise RuntimeError(
                "STAGE0_AUTH_MODE=deployment requires STAGE0_AUTH_TOKEN; "
                "refusing to start an anonymous service")
        roles = frozenset(filter(None, (role.strip() for role in
                                        os.getenv("STAGE0_AUTH_ROLES", "caregiver,ops").split(","))))
        token_roles[token] = roles
        logger.warning("deployment auth profile active; requests need the %s header", AUTH_HEADER)
    else:
        if resolved_auth_mode != "local-demo":
            raise RuntimeError(f"unknown STAGE0_AUTH_MODE: {resolved_auth_mode}")
        logger.warning("local-demo auth profile: single-patient demo principal, "
                       "no real authentication; do not expose beyond localhost")

    store = MemoryStore(db_path or os.getenv("STAGE0_DB_PATH", DEFAULT_DB))
    llm_planner_enabled = os.getenv("AGENT_LLM_PLANNER", "").strip().lower() in {"1", "true", "yes", "on"}

    def default_agent_factory() -> MedicationCoordinatorAgent:
        return MedicationCoordinatorAgent(store, llm_planner_enabled=llm_planner_enabled)

    def default_runner_factory() -> Any:
        # Reliability P1: routed runner — legacy by default, LangGraph StateGraph
        # when AGENT_GRAPH_RUNNER is enabled; in-flight runs keep their version.
        return make_runner(store, agent_factory or default_agent_factory,
                           checkpoint_path=checkpoint_path)

    worker = OutboxWorker(store, runner_factory or default_runner_factory)
    app = FastAPI(title="用药协管员 API", version="1.1.0",
                  description="Single-caregiver auditable medication coordination service "
                              "(async-first events; NOT a medical device).")
    app.state.store = store
    app.state.worker = worker
    app.state.auth_mode = resolved_auth_mode
    started_at = time.time()

    def _principal(request: Request) -> Principal:
        return _resolve_principal(request, resolved_auth_mode, token_roles)

    # ---- tracing + rate limiting ----------------------------------------

    @app.middleware("http")
    async def _trace_middleware(request: Request, call_next: Callable[[Any], Any]) -> JSONResponse:
        trace_id = request.headers.get("X-Trace-Id") or uuid.uuid4().hex[:12]
        request.state.trace_id = trace_id
        response = await call_next(request)
        response.headers["X-Trace-Id"] = trace_id
        return response

    rate_lock = threading.Lock()
    rate_window: dict[str, list[float]] = {}

    def _rate_limit(principal: Principal) -> None:
        now = time.monotonic()
        with rate_lock:
            hits = [t for t in rate_window.get(principal.user_id, []) if now - t < 60.0]
            if len(hits) >= EVENT_RATE_LIMIT_PER_MINUTE:
                raise ApiError(429, "rate_limited", "provider",
                               "event submission rate limit reached; retry after a minute")
            hits.append(now)
            rate_window[principal.user_id] = hits

    # ---- error model ----------------------------------------------------

    def _error_body(request: Request, exc: ApiError) -> dict[str, Any]:
        return {"error": {"code": exc.code, "category": exc.category,
                          "message": exc.message,
                          "trace_id": getattr(request.state, "trace_id", uuid.uuid4().hex[:12]),
                          "details": exc.details}}

    @app.exception_handler(ApiError)
    async def _api_error_handler(request: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content=_error_body(request, exc))

    @app.exception_handler(RequestValidationError)
    async def _validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(status_code=422, content={
            "error": {"code": "validation_error", "category": "validation",
                      "message": "request body failed validation",
                      "trace_id": getattr(request.state, "trace_id", uuid.uuid4().hex[:12]),
                      "details": {"errors": [
                          {"loc": [str(item) for item in err.get("loc", [])],
                           "msg": err.get("msg", "")}
                          for err in exc.errors()[:10]]}},
        })

    @app.exception_handler(Exception)
    async def _internal_error_handler(request: Request, exc: Exception) -> JSONResponse:
        # Log the sanitized signal; the response never echoes internals.
        logger.error("unhandled exception trace_id=%s exception_type=%s",
                     getattr(request.state, "trace_id", "?"), type(exc).__name__)
        return JSONResponse(status_code=500, content={
            "error": {"code": "internal_error", "category": "internal",
                      "message": f"{type(exc).__name__}",
                      "trace_id": getattr(request.state, "trace_id", uuid.uuid4().hex[:12]),
                      "details": {}},
        })

    # ---- lifecycle ------------------------------------------------------

    @app.on_event("startup")
    def _startup() -> None:
        if worker_thread:
            worker.start()

    @app.on_event("shutdown")
    def _shutdown() -> None:
        worker.stop()

    # ---- events (async-first, two-layer idempotency) --------------------

    @app.post("/v1/events", status_code=202)
    def submit_event(event_in: EventIn, request: Request,
                     idempotency_key: str = Header(..., alias="Idempotency-Key")) -> JSONResponse:
        principal = _principal(request)
        if not IDEMPOTENCY_KEY_PATTERN.match(idempotency_key):
            raise ApiError(422, "invalid_idempotency_key", "validation",
                           "Idempotency-Key must match [A-Za-z0-9_-]{1,128}")
        _rate_limit(principal)
        body = event_in.model_dump()
        request_hash = hashlib.sha256(
            json.dumps(body, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
        # Reliability P0: server-generated identity.  The turn id is the run
        # id (a uuid) — two legal keys sharing a long prefix can no longer
        # map two different events onto the same agent turn.
        event_id = uuid.uuid4().hex
        run_id = uuid.uuid4().hex
        event_key = f"api:{idempotency_key}"
        trace_id = getattr(request.state, "trace_id", uuid.uuid4().hex[:12])
        claim = store.accept_api_event(
            idempotency_key=idempotency_key, request_hash=request_hash,
            event_id=event_id, run_id=run_id,
            task_payload={"event": body, "session_id": body["session_id"],
                          "turn_id": run_id, "idempotency_key": idempotency_key,
                          "event_key": event_key, "event_id": event_id,
                          "run_id": run_id, "trace_id": trace_id})
        acceptance = {"event_key": event_key, "event_id": event_id, "run_id": run_id,
                      "status": "queued",
                      "status_url": f"/v1/events/{idempotency_key}"}
        if claim["state"] == "conflict":
            raise ApiError(422, "idempotency_key_reused", "validation",
                           "this Idempotency-Key was already used with a different payload")
        if claim["state"] == "retry_failed":
            raise ApiError(409, "previous_attempt_failed", "internal",
                           f"the previous attempt with this key failed; "
                           f"POST /v1/events/{idempotency_key}/retry to re-run it "
                           f"(do not switch keys — that would create a second event)")
        headers = {}
        if claim["state"] in {"replay_committed", "replay_in_flight"}:
            # Cross-check the outbox task: a key left in_flight by a crash
            # between task-failure and key-update must not accept silently.
            task = store.outbox_task_for(f"api-event:{idempotency_key}")
            if task is not None and task["status"] == "failed":
                raise ApiError(409, "previous_attempt_failed", "internal",
                               f"the previous attempt with this key failed; "
                               f"POST /v1/events/{idempotency_key}/retry to re-run it")
            headers["Idempotent-Replay"] = "true"
            if claim["state"] == "replay_committed":
                stored = claim["row"].get("response_json")
                if stored:
                    # Full stored body (event_key/status/response) so a replayed
                    # POST returns the SAME shape as GET /v1/events/{key}
                    # (Reliability P0 contract fix) — and the same LATEST
                    # result (Reliability P2: review convergence included).
                    body_stored = json.loads(stored)
                    if task is not None:
                        body_stored["response"] = _current_result(task)
                    acceptance = {**body_stored}
        return JSONResponse(status_code=202, content=acceptance, headers=headers or None)

    @app.get("/v1/events/{idempotency_key}")
    def event_status(idempotency_key: str, request: Request) -> JSONResponse:
        _principal(request)
        task = store.outbox_task_for(f"api-event:{idempotency_key}")
        if task is None:
            raise ApiError(404, "unknown_event", "validation", "no event for this key")
        payload = task["payload"] or {}
        event_key = payload.get("event_key", f"api:{idempotency_key}")
        if task["status"] == "done":
            return JSONResponse(status_code=200, content={
                "event_key": event_key, "status": "committed",
                "event_id": payload.get("event_id"), "run_id": payload.get("run_id"),
                "response": _current_result(task)})
        if task["status"] == "failed":
            return JSONResponse(status_code=500, content={
                "event_key": event_key, "status": "failed",
                "error_class": task.get("last_error_class"), "error": task["error"]})
        return JSONResponse(status_code=202, content={
            "event_key": event_key,
            "status": "processing" if task["status"] == "running" else "queued"})

    def _current_result(task: dict[str, Any]) -> dict[str, Any]:
        """Latest authoritative result for a committed event (Reliability P2).

        A review-resolved run's final result (workflow_runs, written by the
        run's own publish node through the same safety gates) supersedes the
        parked-turn waiting snapshot; before convergence the waiting snapshot
        stands."""
        result = dict(task["result"] or {})
        run_id = (task["payload"] or {}).get("run_id")
        if run_id:
            run = store.workflow_run_get(run_id)
            if run is not None and run.get("result"):
                if run["status"] in {"succeeded", "degraded", "failed"}:
                    final = dict(run["result"])
                    final.setdefault("run_status", run["status"])
                    final.setdefault("review_case", result.get("review_case"))
                    final.setdefault("event_id", result.get("event_id"))
                    final.setdefault("run_id", result.get("run_id"))
                    final["operation_outcomes"] = result.get("operation_outcomes", [])
                    return final
        return result

    @app.post("/v1/events/{idempotency_key}/retry")
    def retry_event(idempotency_key: str, request: Request) -> JSONResponse:
        """Explicit recovery of a failed event, keeping the original event
        identity.  effect_unknown tasks must be reconciled first — never
        blindly retried — and clients are never told to switch keys."""
        _require_role(_principal(request), "ops", "support")
        task = store.reopen_failed_outbox_task(f"api-event:{idempotency_key}")
        if task is None:
            raise ApiError(409, "retry_not_available", "validation",
                           "no failed event for this key, or the failure is "
                           "effect_unknown and requires reconciliation first")
        return JSONResponse(status_code=202, content={
            "event_key": f"api:{idempotency_key}", "status": "queued",
            "status_url": f"/v1/events/{idempotency_key}"})

    # ---- memory reads (object-scope checked; opaque ids are not authz) ---

    @app.get("/v1/memory/state")
    def memory_state(request: Request, valid_at: str | None = None,
                     known_at: str | None = None) -> dict[str, Any]:
        _authorize_scope(_principal(request), "local-demo")
        state = store.query_state(valid_at=valid_at, known_at=known_at)
        # Frontend round (2026-09-06): normalize open_conflicts rows additively
        # — parse resolution_json (kept verbatim for old clients) and attach
        # the memory ref, matching /v1/memory/conflicts row shape.
        conflicts = state.get("open_conflicts")
        if isinstance(conflicts, list):
            for row in conflicts:
                raw = row.get("resolution_json")
                if raw and "resolution" not in row:
                    try:
                        row["resolution"] = json.loads(raw)
                    except (TypeError, ValueError):
                        row["resolution"] = None
                if "ref" not in row:
                    row["ref"] = memory_ref("conflict", row.get("id"), 1)
        return state

    @app.get("/v1/memory/timeline")
    def memory_timeline(request: Request, limit: int = 100) -> list[dict[str, Any]]:
        _authorize_scope(_principal(request), "local-demo")
        return store.timeline(limit=max(1, min(limit, 500)))

    @app.get("/v1/memory/conflicts")
    def memory_conflicts(request: Request) -> list[dict[str, Any]]:
        _authorize_scope(_principal(request), "local-demo")
        return store.open_conflicts()

    @app.get("/v1/alerts")
    def alerts(request: Request) -> list[dict[str, Any]]:
        _authorize_scope(_principal(request), "local-demo")
        out = []
        for row in store.connection.execute(
            "SELECT id, memory_refs_json, source_refs_json, text FROM conclusions "
            "WHERE kind='warning' ORDER BY id"
        ).fetchall():
            out.append({
                "ref": memory_ref("conclusion", row["id"], 1),
                "memory_refs": json.loads(row["memory_refs_json"]),
                "source_refs": json.loads(row["source_refs_json"]),
                "text": row["text"],
            })
        return out

    @app.post("/v1/conflicts/{conflict_id}/actions")
    def conflict_action(conflict_id: int, action_in: ConflictActionIn,
                        request: Request) -> dict[str, Any]:
        principal = _principal(request)
        _require_role(principal, "caregiver")
        _authorize_scope(principal, "local-demo")
        try:
            return store.resolve_conflict(
                conflict_id, action=action_in.action, basis=action_in.basis,
                # The recorded actor is the authenticated principal; the body
                # actor stays a self-claimed annotation (Reliability P0).
                actor=principal.user_id, chosen_ref=action_in.chosen_ref)
        except ValueError as exc:
            raise ApiError(422, "invalid_conflict_action", "validation", str(exc)) from exc

    # ---- Reliability P2: human review loop -------------------------------

    @app.get("/v1/review-cases")
    def list_review_cases(request: Request, status: str | None = None) -> list[dict[str, Any]]:
        """Queue query scoped to the principal's role — never a global bare
        list for unauthenticated callers."""
        _require_role(_principal(request), "reviewer", "ops")
        statuses = [s.strip() for s in status.split(",")] if status else None
        return store.review_cases(statuses=statuses)

    @app.post("/v1/review-cases/{case_id}/claim")
    def claim_review_case(case_id: int, request: Request,
                          body: dict[str, Any]) -> dict[str, Any]:
        principal = _principal(request)
        _require_role(principal, "reviewer")
        _authorize_scope(principal, "local-demo")
        try:
            return store.claim_review_case(
                case_id, expected_revision=int(body.get("expected_revision", -1)),
                assignee=principal.user_id)
        except MemoryPolicyError as exc:
            raise ApiError(409, "review_claim_conflict", "validation", str(exc)) from exc

    @app.post("/v1/review-cases/{case_id}/decisions")
    def submit_review_decision(case_id: int, request: Request, body: dict[str, Any],
                               idempotency_key: str = Header(..., alias="Idempotency-Key")) -> JSONResponse:
        """Transaction ③: the structured decision + its resume task commit
        together.  No graph goto / SQL / tool names / state patches can be
        expressed here — only the five bounded actions."""
        principal = _principal(request)
        _require_role(principal, "reviewer")
        _authorize_scope(principal, "local-demo")
        if not IDEMPOTENCY_KEY_PATTERN.match(idempotency_key):
            raise ApiError(422, "invalid_idempotency_key", "validation",
                           "Idempotency-Key must match [A-Za-z0-9_-]{1,128}")
        try:
            record = store.record_review_decision(
                case_id=case_id,
                expected_revision=int(body.get("expected_revision", -1)),
                action=str(body.get("action", "")),
                payload=body.get("payload") or {},
                idempotency_key=idempotency_key,
                actor_id=principal.user_id)
        except MemoryPolicyError as exc:
            raise ApiError(409, "review_decision_conflict", "validation", str(exc)) from exc
        return JSONResponse(status_code=202, content={
            "decision_id": record["decision_id"], "status": "recorded",
            "replayed": bool(record.get("replayed"))})

    @app.post("/v1/review-cases/{case_id}/cancel")
    def cancel_review_case(case_id: int, request: Request, body: dict[str, Any]) -> dict[str, Any]:
        principal = _principal(request)
        _require_role(principal, "reviewer", "ops")
        try:
            case = store.review_case(case_id)
            if case is None:
                raise MemoryPolicyError(f"review case {case_id} does not exist")
            if case["status"] in {"resolved", "cancelled"}:
                raise MemoryPolicyError(f"review case {case_id} is already terminal")
            return store.close_review_case(
                case_id, status="cancelled",
                reason=str(body.get("reason", "")), actor=principal.user_id)
        except MemoryPolicyError as exc:
            raise ApiError(409, "review_cancel_conflict", "validation", str(exc)) from exc

    @app.get("/v1/review-cases/{case_id}/summary")
    def review_case_summary(case_id: int, request: Request) -> dict[str, Any]:
        """Exportable consultation summary ( caregiver or reviewer role)."""
        principal = _principal(request)
        _require_role(principal, "caregiver", "reviewer")
        case = store.review_case(case_id)
        if case is None or case["scope_id"] != principal.scope_id:
            raise ApiError(404, "unknown_review_case", "validation",
                           "no review case for this id")
        return {
            "case_id": case["id"], "status": case["status"], "round": case["round"],
            "reason_codes": case["reason_codes"], "created_at": case["created_at"],
            "due_at": case["due_at"],
            "summary": case["summary"],
            "decisions": [
                {"action": d["action"], "actor_id": d["actor_id"],
                 "outcome": d["outcome"], "created_at": d["created_at"]}
                for d in store.review_decisions_for(case_id)
            ],
            "notice": "本地演示环境：尚未接入真实人工服务；此摘要用于线下咨询医生/药师。",
        }

    @app.post("/v1/rechecks")
    def run_rechecks(recheck_in: RecheckIn, request: Request) -> dict[str, Any]:
        _require_role(_principal(request), "caregiver", "ops")
        return worker.agent.run_pending_rechecks(max_jobs=max(1, min(recheck_in.max_jobs, 20)))

    @app.get("/v1/health")
    def health() -> dict[str, Any]:
        version = store.connection.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()
        pending_outbox = store.connection.execute(
            "SELECT COUNT(*) FROM outbox_tasks WHERE status IN ('open','running')").fetchone()[0]
        pending_rechecks = len(store.pending_rechecks())
        return {
            "status": "ok",
            "db": str(store.db_path),
            "schema_version": version[0] if version else None,
            "pending_outbox_tasks": pending_outbox,
            "pending_rechecks": pending_rechecks,
            "llm_planner_enabled": llm_planner_enabled,
            "worker_thread": worker_thread,
            "graph_runner_enabled": worker.runner.graph_enabled,
            "auth_mode": resolved_auth_mode,
            "uptime_seconds": round(time.time() - started_at, 1),
        }

    # Frontend round (2026-09-06): read models + minimal write adapters for
    # the caregiver web app (alert/history/medication/conflict/session read
    # models, fact verify/retract, export/backup).  Legacy endpoints above
    # are untouched.
    try:
        from .read_models import register_read_model_routes
    except ImportError:  # Support ``python stage0/server.py``.
        from read_models import register_read_model_routes  # type: ignore
    register_read_model_routes(
        app, store,
        principal=_principal,
        authorize_scope=lambda p, scope: _authorize_scope(p, scope),
        require_role=_require_role,
        api_error=ApiError,
    )

    return app


def main() -> None:
    import uvicorn

    uvicorn.run("stage0.server:app", host="127.0.0.1",
                port=int(os.getenv("STAGE0_API_PORT", "8000")))


def __getattr__(name: str) -> Any:
    # Lazy module-level app: importing stage0.server (e.g. in tests) must not
    # open the live memory.db; `uvicorn stage0.server:app` resolves `app`
    # through this hook on first access.
    if name == "app":
        module_app = create_app()
        globals()["app"] = module_app
        return module_app
    raise AttributeError(name)


if __name__ == "__main__":
    main()
