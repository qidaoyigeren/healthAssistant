"""Local synthetic browser acceptance fixture, never opens the user's DB.

Run: python -m stage0.harness_browser_fixture (localhost:8000).
The release endpoint only exists in this fixture; no production API change.
"""
import tempfile
import threading
from pathlib import Path

import uvicorn

from stage0.graph_runner import LangGraphAgentRunner
from stage0.harness_eval import make_agent
from stage0.server import create_app


def main():
    gate = threading.Event()

    def provider(payload):
        gate.wait(timeout=180)
        observations = payload.get("observations") or []
        if not any(o.get("tool") == "memory_write" and o.get("ok") for o in observations):
            return {"decision": "tool", "tool": "memory_write", "purpose": "consolidate",
                    "arguments": {"operation": "consolidate_event"}}
        return {"decision": "respond", "rationale": "synthetic acceptance complete"}

    with tempfile.TemporaryDirectory(prefix="harness-browser-") as directory:
        base = Path(directory)
        app = create_app(db_path=base / "memory.db", auth_mode="local-demo",
                         runner_factory=lambda: LangGraphAgentRunner(
                             make_agent(app.state.store, provider=provider),
                             checkpoint_path=base / "checkpoint.db"))

        @app.post("/acceptance/release")
        def release():
            gate.set()
            return {"released": True}

        @app.post("/acceptance/pause")
        def pause():
            gate.clear()
            return {"paused": True}

        @app.get("/acceptance/counts")
        def counts():
            store = app.state.store
            with store._lock:
                return {"runs": [dict(r) for r in store.connection.execute(
                    "SELECT run_id, status FROM workflow_runs ORDER BY rowid")],
                        "medication_effects": store.connection.execute(
                            "SELECT COUNT(*) FROM episodic_memory WHERE event_type='medication_add'").fetchone()[0]}

        try:
            uvicorn.run(app, host="127.0.0.1", port=8000)
        finally:
            gate.set()
            app.state.worker.stop()
            app.state.store.close()


if __name__ == "__main__":
    main()
