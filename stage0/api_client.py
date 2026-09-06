"""Thin API client for the Stage 10 service layer (Reliability P0 updated).

Used by ``app.py`` when ``STAGE0_API_URL`` is set (deployment form); the
default Streamlit path stays direct in-process and offline.  Event submission
is async-first: POST + poll until the outbox worker commits the turn, with a
client-generated Idempotency-Key so UI retries never double-record.

Reliability P0 contract: a committed replay returns the SAME body shape as
the status endpoint (``{"event_key","status","response"}``), and submission
keys are stable per business intent — ``SubmitKeyStore`` lets the UI reuse
the key across timeout retries and only mint a new key for a genuinely new
submission.  Failed events are recovered explicitly (``retry_event``), never
by switching keys.
"""
from __future__ import annotations

import time
import uuid
from typing import Any

import httpx


class ApiClientError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None, details: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.details = details


class SubmitKeyStore:
    """Per-session idempotency keys, stable across timeout retries.

    One business submission = one key.  ``key_for`` returns the same key
    while a submission with the same fingerprint is unresolved (pending,
    timed out, or failed); after a committed result the entry is resolved so
    a deliberate repeat of the same action becomes a NEW event with a NEW
    key.  Fingerprint = canonical content of the event, so an accidental
    double-click and an explicit retry reuse the event instead of duplicating
    it, while a real second report (after success) is recorded.
    """

    def __init__(self) -> None:
        self._entries: dict[str, dict[str, Any]] = {}

    def key_for(self, fingerprint: str) -> str:
        entry = self._entries.get(fingerprint)
        if entry is not None and not entry["resolved"]:
            return entry["key"]
        key = f"ui-{uuid.uuid4().hex}"
        self._entries[fingerprint] = {"key": key, "resolved": False, "event_key": None}
        return key

    def mark_resolved(self, fingerprint: str, *, event_key: str | None = None) -> None:
        entry = self._entries.get(fingerprint)
        if entry is not None:
            entry["resolved"] = True
            entry["event_key"] = event_key

    def state(self, fingerprint: str) -> dict[str, Any] | None:
        entry = self._entries.get(fingerprint)
        return dict(entry) if entry is not None else None


class Stage0ApiClient:
    def __init__(self, base_url: str, *, timeout: float = 30.0):
        self.base_url = base_url.rstrip("/")
        self._client = httpx.Client(base_url=self.base_url, timeout=timeout)
        self.submit_keys = SubmitKeyStore()

    # ---- events ---------------------------------------------------------

    @staticmethod
    def fingerprint_event(event: dict[str, Any], *, session_id: str) -> str:
        """Canonical fingerprint of one business submission (Reliability P0).

        Session-scoped on purpose: the same content reported after a NEW
        session is a new report, not a retry."""
        import json
        return json.dumps({"session_id": session_id, "event": event},
                          sort_keys=True, ensure_ascii=False)

    def submit_event(self, event: dict[str, Any], *, session_id: str,
                     idempotency_key: str | None = None,
                     poll_timeout: float = 60.0,
                     poll_interval: float = 0.3) -> dict[str, Any]:
        """Submit one CareEvent and poll until the worker commits it.

        Returns the committed body (``{"event_key","status","response"}``)
        with the full ``response`` — identical for a first commit and a
        replayed POST (both now come from the same published result).
        Raises ApiClientError on validation conflicts, failed turns, or
        polling timeout.  On timeout the caller should RETRY with the same
        key (see ``SubmitKeyStore``), not submit a new event.
        """
        fingerprint = self.fingerprint_event(event, session_id=session_id)
        key = idempotency_key or self.submit_keys.key_for(fingerprint)
        body = {**event, "session_id": session_id}
        acceptance = self._request("POST", "/v1/events", json=body,
                                   headers={"Idempotency-Key": key})
        if acceptance.get("status") == "committed" and "response" in acceptance:
            self.submit_keys.mark_resolved(fingerprint, event_key=acceptance.get("event_key"))
            return self._normalize(acceptance)
        deadline = time.time() + poll_timeout
        while time.time() < deadline:
            status = self._request("GET", f"/v1/events/{key}")
            if status.get("status") == "committed":
                self.submit_keys.mark_resolved(fingerprint, event_key=status.get("event_key"))
                return self._normalize(status)
            if status.get("status") == "failed":
                raise ApiClientError(
                    f"event processing failed: {status.get('error')}",
                    status_code=500, details=status)
            time.sleep(poll_interval)
        # Timeout: the key stays unresolved, so the next submit attempt with
        # the same content reuses this key and the same event.
        raise ApiClientError("event processing timed out; the same submission "
                             "will be retried with the same idempotency key",
                             status_code=None,
                             details={"event_key": acceptance.get("event_key"),
                                      "status_url": acceptance.get("status_url")})

    def retry_event(self, idempotency_key: str) -> dict[str, Any]:
        """Explicit recovery of a failed event — keeps the original event
        identity (Reliability P0).  Requires an ops/support principal."""
        return self._request("POST", f"/v1/events/{idempotency_key}/retry")

    # ---- human review loop (Reliability P2) -------------------------------

    def review_cases(self, *, status: str | None = None) -> list[dict[str, Any]]:
        params = {"status": status} if status else None
        return self._request("GET", "/v1/review-cases", params=params)

    def review_case_summary(self, case_id: int) -> dict[str, Any]:
        return self._request("GET", f"/v1/review-cases/{case_id}/summary")

    def claim_review_case(self, case_id: int, *, expected_revision: int) -> dict[str, Any]:
        return self._request("POST", f"/v1/review-cases/{case_id}/claim",
                             json={"expected_revision": expected_revision})

    def submit_review_decision(self, case_id: int, *, action: str,
                               expected_revision: int,
                               payload: dict[str, Any] | None = None,
                               idempotency_key: str | None = None) -> dict[str, Any]:
        """Submit one structured decision.  The client generates the
        Idempotency-Key when not supplied, so a retry of the same submit
        (timeout, double callback) acts exactly once."""
        key = idempotency_key or f"rv-{uuid.uuid4().hex}"
        body = {"action": action, "payload": payload or {},
                "expected_revision": expected_revision}
        out = self._request("POST", f"/v1/review-cases/{case_id}/decisions",
                            json=body, headers={"Idempotency-Key": key})
        out["idempotency_key"] = key
        return out

    def cancel_review_case(self, case_id: int, *, reason: str) -> dict[str, Any]:
        return self._request("POST", f"/v1/review-cases/{case_id}/cancel",
                             json={"reason": reason})

    @staticmethod
    def _normalize(body: dict[str, Any]) -> dict[str, Any]:
        # Older v1 servers may return a committed acceptance without the
        # full response; surface that honestly instead of inventing one.
        out = {"event_key": body.get("event_key"), "status": body.get("status"),
               "response": body.get("response")}
        if out["response"] is None:
            out["response"] = {}
        return out

    # ---- reads ----------------------------------------------------------

    def memory_state(self, *, valid_at: str | None = None,
                     known_at: str | None = None) -> dict[str, Any]:
        params = {name: value for name, value in
                  (("valid_at", valid_at), ("known_at", known_at)) if value}
        return self._request("GET", "/v1/memory/state", params=params)

    def timeline(self, limit: int = 100) -> list[dict[str, Any]]:
        return self._request("GET", "/v1/memory/timeline", params={"limit": limit})

    def conflicts(self) -> list[dict[str, Any]]:
        return self._request("GET", "/v1/memory/conflicts")

    def alerts(self) -> list[dict[str, Any]]:
        return self._request("GET", "/v1/alerts")

    def health(self) -> dict[str, Any]:
        return self._request("GET", "/v1/health")

    # ---- internals ------------------------------------------------------

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        response = self._client.request(method, path, **kwargs)
        if response.status_code >= 400:
            detail = response.json().get("error", {}) if response.headers.get(
                "content-type", "").startswith("application/json") else {}
            raise ApiClientError(
                f"{method} {path} -> {response.status_code}: "
                f"{detail.get('code', response.text[:200])}",
                status_code=response.status_code, details=detail)
        return response.json()

    def close(self) -> None:
        self._client.close()
