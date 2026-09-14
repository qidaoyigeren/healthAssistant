"""浏览器验收用的隔离宿主：真实服务、真实数据库、**不调模型**。

它做的事只有两件：

1. 用**产品路径**（事件受理 → worker → 必要检查）建出一件真实的安全事项；
2. 跑一次真实调查：由规划器通过 ``plan_questions`` 提出一条问题，经执行器与
   校验器被采用，再落成事项上的补问——**不是**预先塞进数据库的一行。

只有措辞是脚本化的，机制一步没省。

浏览器随后走的就是普通用户走的那条路：打开页面、回答、看到变化。
临时库，不碰 `stage0/memory.db`，合成药名没有临床含义。

    python -m stage0.safety_browser_fixture --port 8000
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

from .agent import DDITool, MedicationCoordinatorAgent
from .memory import MemoryStore
from .care_tasks import CareTasks
from .product import ProductStore
from .safety_cases import SafetyCaseStore

WARNING = {
    "drug_a": "合成药甲", "drug_b": "合成药乙", "severity": "major", "mechanism": "合成机制",
    "effect": "合成风险效应", "management": None,
    "source_text": "合成药甲与合成药乙合用可能增加合成风险（合成说明书文本）",
    "source_url": "https://synthetic.invalid/label/a", "confidence": "high",
    "detection_path": "synthetic_detector",
}

QUESTION = "合成药乙目前的服用频次是什么？"
QUESTION_WHY = "这条信息决定这次相互作用提示是否按当前用法成立。"


def synthetic_detect(medications):
    return [dict(WARNING)] if {"合成药甲", "合成药乙"}.issubset(set(medications)) else []


class EmptyRAG:
    def __call__(self, query, **kwargs):
        return {"query": query, "mode": "synthetic", "corpus_version": "synthetic-v1",
                "results": []}


def plan_provider(payload):
    """脚本化的规划器：用**真实执行路径**声明一条问题。

    关键在"真实"：这条问题不是预先塞进数据库的，而是像模型一样通过
    ``plan_questions`` 提案、经执行器与校验器、由调查状态采用，再落成页面上的
    补问。脚本化的是**措辞**，不是机制。
    """
    investigation = payload.get("investigation") or {}
    if investigation.get("questions"):
        return {"decision": "respond"}
    return {"decision": "tool", "tool": "plan_questions", "gap_id": "subquestions",
            "expected_observation": "建立这件事还需要查清什么",
            "expected_change": "若用户给出频次，就按当前用法重新核对这条提示",
            "arguments": {"questions": [{
                "statement": QUESTION,
                # 要弄清的是**这位患者实际怎么用**——不是"资料一般怎么说"。
                "information_target": "patient_actual_state",
                "strategy": "ask_user",
                "subject_refs": ["合成药乙"],
                "target_field": "schedule",
                "why": QUESTION_WHY,
            }]}}


def scripted_agent(store):
    return MedicationCoordinatorAgent(
        store, ddi_tool=DDITool(synthetic_detect), rag_tool=EmptyRAG(),
        llm_planner_enabled=True, proposal_provider=plan_provider)


def build_app(db_path: Path):
    import stage0.server as server

    app = server.create_app(
        db_path=db_path, worker_thread=False,
        # worker 与同步路径必须用**同一个**脚本化 agent：否则"恢复那一轮"会
        # 退回降级路径，浏览器看到的就不是同一条链路。
        agent_factory=lambda: scripted_agent(app.state.store))

    @app.post("/__fixture/drain")
    def _drain():
        """**验收专用**：把 outbox 里排队的任务跑掉。

        产品里这件事由后台 worker 线程做；验收关掉那个线程是为了确定性，于是需要
        一个显式的推进口。它只存在于这个合成后端——`stage0/server.py` 上没有这条
        路由，产品路径不会多出一个"手动跑任务"的入口。
        """
        return {"drained": len(app.state.worker.drain_once(max_tasks=5))}

    return app


def seed(app) -> dict:
    """用产品路径建事项：两次用药变化，必要检查随即执行。"""
    store: MemoryStore = app.state.store
    worker = app.state.worker
    product = ProductStore(store)
    for index, name in enumerate(("合成药甲", "合成药乙")):
        key = f"browser-seed-{index}"
        store.accept_api_event(
            idempotency_key=key,
            request_hash=f"hash-{index}",
            event_id=f"ev-{index}", run_id=f"run-{index}",
            task_payload={
                "event": {"session_id": "browser", "event_type": "medication_change",
                          "text": f"新增{name}", "source": "caregiver",
                          "payload": {"action": "add", "medication": name}},
                "session_id": "browser", "turn_id": f"run-{index}",
                "idempotency_key": key, "event_key": f"api:{key}",
                "event_id": f"ev-{index}", "run_id": f"run-{index}", "trace_id": f"trace-{index}"})
        worker.drain_once()

    cases = SafetyCaseStore(product).objects()
    if not cases:
        raise SystemExit("fixture 没能建出安全事项——先检查必要检查是否执行")
    case = cases[0]

    # 让 Agent 走**真实调查路径**提出这条问题，而不是把问题直接塞进事项。
    tasks = CareTasks(product, agent_factory=lambda: scripted_agent(store))
    task = tasks.create("browser-investigate", "safety_case", case["id"])
    tasks.resume(task["id"], "browser-investigate-run", task["revision"])
    worker.drain_once()

    pending = SafetyCaseStore(product).get(case["id"])["required_inputs"]
    if not pending:
        raise SystemExit("调查没能产出待补充的问题——检查 plan_questions 路径是否通畅")
    ask = pending[0]
    return {"case_id": case["id"],
            "request_id": ask["request_id"],
            "question": ask["question"],
            "question_kind": ask.get("question_kind"),
            "question_strategy": ask.get("question_strategy"),
            "fields": ask.get("fields") or [],
            "url": f"http://127.0.0.1:5173/safety/{case['id']}"}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--db", default=None, help="默认用临时库；给出路径则复用")
    args = parser.parse_args()

    import uvicorn
    if args.db:
        db_path = Path(args.db)
        temporary = None
    else:
        temporary = tempfile.TemporaryDirectory(prefix="safety-browser-")
        db_path = Path(temporary.name) / "memory.db"

    app = build_app(db_path)
    info = seed(app)
    print("FIXTURE_READY " + json.dumps(info, ensure_ascii=False), flush=True)
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
    if temporary is not None:
        temporary.cleanup()
    return 0


if __name__ == "__main__":
    sys.exit(main())
