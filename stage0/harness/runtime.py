"""RunContext — the shared runtime contract for legacy and graph runners.

Harness P1-A.  One object carries the trusted identity and scope the executor
checks against, the run/event/operation/attempt identities, the patient
revision the turn started from, the budget handle, cancellation and trace
correlation.  Trust rules:

* the principal/scope are resolved by code (service layer or the explicit
  local-demo default), never from model output or event payload fields;
* ``checkpoint_dict()`` returns ONLY JSON-safe scalars — budget sessions,
  connections, clients and credentials never enter a checkpoint or a model
  parameter;
* ``cancelled()`` consults the active lease guard and an optional event;
  cancellation does not impersonate a tool result.
"""
from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from typing import Any

try:
    from . import turn_budget  # re-exported identity, see below
except ImportError:  # pragma: no cover - script-style import
    import turn_budget  # type: ignore


@dataclass(frozen=True)
class Principal:
    """Trusted identity.  Same shape as the service layer's Principal so the
    API principal can be passed in directly."""

    user_id: str = "local-demo-caregiver"
    roles: frozenset = frozenset({"caregiver", "ops", "reviewer"})
    scope_id: str = "local-demo"

    def has_role(self, *roles: str) -> bool:
        return bool(self.roles & set(roles))


LOCAL_DEMO_PRINCIPAL = Principal()


def principal_from(obj: Any) -> Principal:
    """Adapt the service-layer Principal (or anything with the same fields)."""
    if obj is None:
        return LOCAL_DEMO_PRINCIPAL
    if isinstance(obj, Principal):
        return obj
    return Principal(user_id=str(getattr(obj, "user_id", "unknown")),
                     roles=frozenset(getattr(obj, "roles", ()) or ()),
                     scope_id=str(getattr(obj, "scope_id", "local-demo")))


@dataclass
class RunContext:
    """Everything a tool execution or model call may rely on.  Not a secret
    holder: only ids, revisions and trace correlation live here."""

    run_id: str
    turn_id: str
    session_id: str = "local-demo"
    event_id: str | None = None
    client_event_id: str | None = None
    operation_id: str | None = None
    attempt_id: str | None = None
    principal: Principal = field(default_factory=Principal)
    # medications+semantic scope revision captured at run start; the executor
    # records it on writes so an effect can be tied to the facts it saw.
    patient_revision: int | None = None
    trace_id: str = ""
    parent_span_id: str | None = None
    # Cancellation: an optional threading.Event plus the budget lease guard
    # (set by lease_scope).  Interruption waits never touch either.
    cancel_event: threading.Event | None = field(default=None, repr=False, compare=False)

    def __post_init__(self):
        if not self.trace_id:
            self.trace_id = uuid.uuid4().hex

    # ---- capability handles ------------------------------------------------

    @property
    def budget(self):
        """The active BudgetSession for this run, if one is in scope."""
        return turn_budget.CURRENT.get()

    def cancelled(self) -> bool:
        if self.cancel_event is not None and self.cancel_event.is_set():
            return True
        session = self.budget
        if session is None:
            return False
        try:
            turn_budget.check_lease()
        except Exception:
            return True
        return bool(session.reason)

    def new_attempt_id(self) -> str:
        self.attempt_id = uuid.uuid4().hex
        return self.attempt_id

    # ---- serialization -----------------------------------------------------

    CHECKPOINT_FIELDS = ("run_id", "turn_id", "session_id", "event_id", "client_event_id",
                         "operation_id", "attempt_id", "patient_revision", "trace_id",
                         "parent_span_id")

    def checkpoint_dict(self) -> dict[str, Any]:
        """JSON-safe projection.  The principal's roles are not serialized:
        the checkpoint never becomes a credential source on resume — a resumed
        run re-resolves its principal from configuration."""
        return {key: getattr(self, key) for key in self.CHECKPOINT_FIELDS}

    @classmethod
    def from_checkpoint(cls, data: dict[str, Any], *, principal: Principal | None = None) -> "RunContext":
        payload = {key: data.get(key) for key in cls.CHECKPOINT_FIELDS if key in data}
        payload["principal"] = principal or LOCAL_DEMO_PRINCIPAL
        return cls(**payload)
