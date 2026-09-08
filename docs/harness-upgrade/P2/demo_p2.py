"""Harness P2 reproducible demo: progress events, cursor replay, cancellation
and no-progress detection against a temporary database.

Run:
    PYTHONPATH=. python docs/harness-upgrade/P2/demo_p2.py

Uses only synthetic cases, the deterministic offline planner and a temporary
database — no external calls, no real patient data, never touches memory.db.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
sys.stdout.reconfigure(encoding="utf-8")

from fastapi.testclient import TestClient  # noqa: E402

EVENT = {"event_type": "medication_change", "text": "新增克拉霉素",
         "payload": {"action": "add", "medication": "克拉霉素"}, "source": "caregiver"}


def main() -> int:
    from stage0.server import create_app

    with tempfile.TemporaryDirectory() as directory:
        app = create_app(db_path=Path(directory) / "memory.db", worker_thread=False)
        store = app.state.store
        worker = app.state.worker
        try:
            client = TestClient(app)

            print("== 1) 正常 run：进度游标轮询 ==")
            accepted = client.post("/v1/events", json={**EVENT, "session_id": "s1"},
                                   headers={"Idempotency-Key": "demo-progress-1"})
            run_id = accepted.json()["run_id"]
            print("POST /v1/events ->", accepted.status_code, "run_id:", run_id[:12], "…")
            print("run 行在受理时即存在：", store.workflow_run_get(run_id)["status"])
            worker.drain_once()
            page = client.get(f"/v1/runs/{run_id}/progress").json()
            print("GET progress -> run_status:", page["run_status"],
                  "latest_seq:", page["latest_seq"])
            for event in page["events"]:
                print(f"  seq={event['seq']} {event['kind']:<14} detail={event['detail']}")
            tail = client.get(f"/v1/runs/{run_id}/progress?after={page['latest_seq']}").json()
            print("游标重连（after=latest）-> 新事件数:", len(tail["events"]))

            print("\n== 2) 取消排队中的任务（不执行任何工具） ==")
            accepted2 = client.post("/v1/events", json={**EVENT, "session_id": "s1"},
                                    headers={"Idempotency-Key": "demo-cancel-1"})
            run2 = accepted2.json()["run_id"]
            cancel = client.post(f"/v1/runs/{run2}/cancel", json={"reason": "演示取消"})
            print("POST cancel ->", cancel.json()["cancel_state"])
            cancel_again = client.post(f"/v1/runs/{run2}/cancel", json={})
            print("POST cancel（幂等重复）->", cancel_again.json()["cancel_state"])
            worker.drain_once()
            done = client.get("/v1/events/demo-cancel-1").json()
            print("轮询终态 -> status:", done["status"],
                  "run_status:", done["response"]["run_status"])
            print("run 行:", store.workflow_run_get(run2)["status"],
                  "· 已写入用药记录数:",
                  store.connection.execute(
                      "SELECT COUNT(*) FROM episodic_memory WHERE event_type='medication_add'"
                  ).fetchone()[0])

            print("\n== 3) 取消终态 run（不改变历史） ==")
            late = client.post(f"/v1/runs/{run_id}/cancel", json={})
            print("POST cancel ->", late.json()["cancel_state"],
                  "· run 仍为:", store.workflow_run_get(run_id)["status"])

            print("\n== 4) 无进展检测（AGENT_NO_PROGRESS_LIMIT=2） ==")
            os.environ["AGENT_NO_PROGRESS_LIMIT"] = "2"
            try:
                from stage0.agent import CareEvent
                from stage0.graph_runner import LangGraphAgentRunner
                from stage0.harness_eval import make_agent

                def provider(payload):
                    return {"decision": "tool", "tool": "memory_read",
                            "purpose": "snapshot", "arguments": {"query": "snapshot"}}
                agent = make_agent(store, provider=provider)
                graph = LangGraphAgentRunner(agent, checkpoint_path=str(Path(directory) / "cp.db"))
                try:
                    response = graph.run(event=CareEvent(**EVENT), session_id="s",
                                         turn_id="demo-np", event_id="demo-np",
                                         run_id="demo-np")
                finally:
                    graph.close()
                acts = [e for e in response.tool_trace if e.get("phase") == "act"]
                print("脚本化重复读取规划下实际执行读取次数:", len(acts),
                      "（关闭检测时为 16 次 = max_cycles）")
                print("安全状态:", response.safety_status, "· 响应:", response.text[:60], "…")
            finally:
                os.environ.pop("AGENT_NO_PROGRESS_LIMIT", None)
            return 0
        finally:
            worker.runner.close()
            store.close()


if __name__ == "__main__":
    raise SystemExit(main())
