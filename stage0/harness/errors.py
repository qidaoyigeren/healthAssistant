"""Stable tool-error taxonomy (Harness P1-A).

Contract (constraint 8 of the P1 prompt): errors returned to the model use a
stable type plus a safe summary.  Internal errors, permission errors,
insufficient evidence and empty retrieval are distinct classes; graph
interrupts, budget exhaustion and lease rejection are NEVER converted into a
retryable tool failure — they propagate unchanged to the runner.
"""
from __future__ import annotations

from enum import Enum
from typing import Any


class ToolErrorKind(str, Enum):
    INVALID_ARGUMENTS = "invalid_arguments"
    UNKNOWN_TOOL = "unknown_tool"
    PERMISSION_DENIED = "permission_denied"
    EVIDENCE_UNAVAILABLE = "evidence_unavailable"
    RETRIEVAL_EMPTY = "retrieval_empty"
    POLICY_VIOLATION = "policy_violation"
    INTERNAL_ERROR = "internal_error"
    BUDGET_EXHAUSTED = "budget_exhausted"
    CANCELLED = "cancelled"


# The model can fix these by proposing a different action; everything else is
# terminal for the tool attempt (the loop may still plan something else).
RECOVERABLE_KINDS = frozenset({
    ToolErrorKind.INVALID_ARGUMENTS,
    ToolErrorKind.UNKNOWN_TOOL,
    ToolErrorKind.RETRIEVAL_EMPTY,
})

# Propagated unchanged — never wrapped into a failed Observation.
PASSTHROUGH_EXCUSES = frozenset({"BudgetExceeded", "LeaseRejected", "GraphInterrupt",
                                 "GraphBubbleUp", "Interrupt"})


class ToolExecutionError(RuntimeError):
    """A classified, model-facing tool failure."""

    def __init__(self, kind: ToolErrorKind, message: str, *, recoverable: bool | None = None):
        if kind not in RECOVERABLE_KINDS and recoverable is None:
            recoverable = False
        self.kind = kind
        self.message = message
        self.recoverable = bool(recoverable) if recoverable is None else recoverable
        super().__init__(f"{kind.value}: {message}")

    def to_payload(self) -> dict[str, Any]:
        return {"error_kind": self.kind.value, "recoverable": self.recoverable,
                "error": self.message}

    @staticmethod
    def safe_detail(exc: BaseException) -> str:
        """Exception type + short message; never a traceback, never credentials."""
        return f"{type(exc).__name__}: {exc}"[:300]


def classify_exception(exc: BaseException, *, known_types: dict[str, ToolErrorKind] | None = None) -> ToolExecutionError:
    """Default classifier; ``known_types`` maps exception *class names* to
    kinds so the harness stays import-cycle free from the domain layer."""
    name = type(exc).__name__
    if name in PASSTHROUGH_EXCUSES:
        raise exc
    if name in (known_types or {}):
        return ToolExecutionError(known_types[name], ToolExecutionError.safe_detail(exc))
    if name in {"ValueError", "KeyError", "TypeError", "json.JSONDecodeError"}:
        # TypeError from a handler bug is a programmer error, but it is still
        # safe (and more useful) surfaced as a non-recoverable classified
        # failure instead of an unbounded string.
        kind = ToolErrorKind.INVALID_ARGUMENTS if name == "ValueError" else ToolErrorKind.INTERNAL_ERROR
        return ToolExecutionError(kind, ToolExecutionError.safe_detail(exc),
                                  recoverable=kind is ToolErrorKind.INVALID_ARGUMENTS)
    if name in {"TimeoutError", "ConnectionError"}:
        return ToolExecutionError(ToolErrorKind.INTERNAL_ERROR, ToolExecutionError.safe_detail(exc))
    return ToolExecutionError(ToolErrorKind.INTERNAL_ERROR, ToolExecutionError.safe_detail(exc))
