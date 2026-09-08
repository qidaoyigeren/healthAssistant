"""Product P1 browser-acceptance fixture: synthetic, isolated, localhost-only.

Run: python -m stage0.product_p1_browser_fixture  (127.0.0.1:8000)

Seeds (via the REAL event pipeline, deterministic scripted detector):
  1. add 氨氯地平, 2. add 克拉霉素 → one DDI warning whose citation carries a
  readable evidence_id (backed by the synthetic label text below).

Never opens the user's database; temporary directory only.
"""
import argparse
import json
import tempfile
import threading
import time
import urllib.request
import uuid
from pathlib import Path

import uvicorn

from stage0.agent import DDITool, MedicationCoordinatorAgent
from stage0.server import create_app


def fake_detect(medications):
    warnings = []
    if {"氨氯地平", "克拉霉素"}.issubset(medications):
        warnings.append({
            "drug_a": "氨氯地平", "drug_b": "克拉霉素", "severity": "moderate",
            "mechanism": "CYP3A4", "effect": "降压作用增强，可能出现低血压",
            "management": None,
            "source_text": ("SYNTHETIC-LABEL（合成评测文本）：与CYP3A4抑制剂克拉霉素合用时，"
                            "氨氯地平暴露量增加，需监测血压与不良反应。"),
            "source_url": "https://synthetic.example/label/amlodipine-clarithromycin",
            "confidence": "medium", "detection_path": "synthetic_browser_detector",
        })
    return warnings


class EmptyRAG:
    def __call__(self, query: str, **_: object) -> dict:
        return {"query": query, "mode": "synthetic", "results": []}


def _post_event(key: str, body: dict, deadline_s: float = 60.0, *, port: int = 8000) -> None:
    """Submit through the real /v1/events pipeline and wait for commit."""
    deadline = time.time() + deadline_s
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/v1/events",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "Idempotency-Key": key},
        method="POST")
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                assert response.status == 202, response.status
            break
        except Exception:
            time.sleep(0.3)
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/v1/events/{key}", timeout=5) as response:
                payload = json.loads(response.read().decode("utf-8"))
            if payload.get("status") == "committed":
                return
        except Exception:
            pass
        time.sleep(0.3)
    raise RuntimeError(f"seed event {key} did not commit in time")


def _seed(port: int = 8000) -> None:
    time.sleep(1.0)  # let uvicorn bind first
    _post_event("p1-seed-amlo", {
        "event_type": "medication_change", "text": "新增氨氯地平",
        "payload": {"action": "add", "medication": "氨氯地平", "dose": "5mg"},
        "session_id": "browser-p1", "source": "caregiver", "occurred_at": None,
    }, port=port)
    _post_event("p1-seed-clari", {
        "event_type": "medication_change", "text": "新增克拉霉素",
        "payload": {"action": "add", "medication": "克拉霉素", "dose": "250mg"},
        "session_id": "browser-p1", "source": "caregiver", "occurred_at": None,
    }, port=port)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8000)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="product-p1-browser-") as directory:
        base = Path(directory)

        def agent_factory():
            return MedicationCoordinatorAgent(
                app.state.store, ddi_tool=DDITool(fake_detect), rag_tool=EmptyRAG())

        app = create_app(db_path=base / "memory.db", auth_mode="local-demo",
                         agent_factory=agent_factory)
        @app.get('/v1/acceptance/product-fixture')
        @app.get('/acceptance/product-fixture')
        def fixture_identity():
            return {'fixture': 'product-p1-synthetic', 'temporary': True, 'port': args.port}

        threading.Thread(target=_seed, args=(args.port,), daemon=True).start()
        try:
            uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
        finally:
            app.state.worker.stop()
            app.state.store.close()


if __name__ == "__main__":
    main()
