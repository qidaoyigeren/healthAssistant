"""Readable two-session Stage 3 caregiver demonstration.

By default the demo reuses ``stage0/memory.db``.  Pass ``--reset`` for the
canonical scripted transcript, ``--llm`` for structured memory extraction, or
``--llm-planner`` for the separately opt-in hybrid planner.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

try:
    from .agent import CareEvent, MedicationCoordinatorAgent
    from .memory import DEFAULT_DB, MemoryStore
except ImportError:
    from agent import CareEvent, MedicationCoordinatorAgent  # type: ignore
    from memory import DEFAULT_DB, MemoryStore  # type: ignore


def banner(title: str) -> None:
    print(f"\n{'=' * 18} {title} {'=' * 18}")


def show_turn(label: str, response: Any, *, verbose_tools: bool = False) -> None:
    print(f"\n照护者：{label}")
    acts = [item for item in response.tool_trace if item.get("phase") == "act"]
    loop = " → ".join(f"{item['tool']}[{item['purpose']}]" for item in acts) or "respond"
    print(f"代理循环：plan → {loop} → respond")
    print(f"用药协管员：{response.text}")
    print(
        "审计摘要：",
        json.dumps(
            {
                "memory_refs": response.audit_trail.get("memory_refs", []),
                "source_count": len(response.audit_trail.get("source_refs", [])),
                "conflict_refs": [item["ref"] for item in response.conflicts],
                "safety_status": response.safety_status,
            },
            ensure_ascii=False,
        ),
    )
    if verbose_tools:
        print(json.dumps(response.tool_trace, ensure_ascii=False, indent=2))


def reset_database(path: Path) -> None:
    # Explicit, non-recursive targets only.  SQLite may leave these two sidecars
    # after an interrupted prior demo.
    for target in (path, Path(f"{path}-wal"), Path(f"{path}-shm")):
        if target.is_file():
            target.unlink()


def run_demo(
    db_path: Path,
    *,
    reset: bool,
    llm: bool,
    verbose_tools: bool,
    llm_planner: bool = False,
) -> None:
    if reset:
        reset_database(db_path.resolve())

    # The scripted run uses persisted Stage 1/2 evidence only.  ``rag_search``
    # remains the real local hybrid BM25+BGE tool; these flags merely prevent
    # the DDI engine from launching new fallback extraction for unrelated pairs.
    os.environ.setdefault("DDI_ENGINE_ENABLE_LLM", "0")
    os.environ.setdefault("DDI_ENGINE_ENABLE_RAG", "0")

    banner("SESSION 1 / 主动管理")
    first_warning: dict[str, Any] | None = None
    with MemoryStore(db_path, llm_enabled=llm) as memory:
        agent = MedicationCoordinatorAgent(memory, llm_planner_enabled=llm_planner)
        session_id = "caregiver-session-1"

        profile_text = "登记我母亲：72岁，女，高血压和糖尿病，磺胺过敏，肾功能轻度受损，平时有些抗拒西药。"
        response = agent.handle(
            CareEvent(
                "register_profile",
                profile_text,
                {
                    "profile": {
                        "age": 72,
                        "sex": "女",
                        "allergies": ["磺胺"],
                        "renal_function": "轻度受损",
                        "chronic_diseases": ["高血压", "糖尿病"],
                        "preferences": {"western_medicine": "抗拒"},
                    }
                },
                occurred_at="2026-08-01T09:00:00+08:00",
            ),
            session_id=session_id,
            turn_id="s1-profile",
        )
        show_turn(profile_text, response, verbose_tools=verbose_tools)

        for index, (medication, date) in enumerate(
            (("氨氯地平", "2026-08-01T09:10:00+08:00"), ("二甲双胍", "2026-08-01T09:20:00+08:00"), ("阿司匹林", "2026-08-01T09:30:00+08:00")),
            1,
        ):
            text = f"新增{medication}。"
            response = agent.handle(
                CareEvent("medication_change", text, {"action": "add", "medication": medication}, occurred_at=date),
                session_id=session_id,
                turn_id=f"s1-base-med-{index}",
            )
            show_turn(text, response, verbose_tools=verbose_tools)

        text = "今天新增克拉霉素。"
        response = agent.handle(
            CareEvent(
                "medication_change", text,
                {"action": "add", "medication": "克拉霉素"},
                occurred_at="2026-08-27T10:00:00+08:00",
            ),
            session_id=session_id,
            turn_id="s1-add-clarithromycin",
        )
        show_turn(text, response, verbose_tools=verbose_tools)
        if response.warnings:
            first_warning = response.warnings[0]

        text = "又新增布洛芬。"
        response = agent.handle(
            CareEvent(
                "medication_change", text,
                {"action": "add", "medication": "布洛芬"},
                occurred_at="2026-08-27T10:10:00+08:00",
            ),
            session_id=session_id,
            turn_id="s1-add-ibuprofen",
        )
        show_turn(text, response, verbose_tools=verbose_tools)
        if first_warning is None and response.warnings:
            first_warning = response.warnings[0]

        text = "医生上个月让做的CT用了造影剂。"
        response = agent.handle(
            CareEvent(
                "procedure_exposure", text,
                {"agent": "含碘造影剂", "doctor_involved": True},
                occurred_at="2026-07-15T14:00:00+08:00",
            ),
            session_id=session_id,
            turn_id="s1-contrast-exposure",
        )
        show_turn(text, response, verbose_tools=verbose_tools)

        banner("SALIENCE / DECAY 示例")
        episodes = memory.retrieve_episodic(limit=6, as_of="2026-08-27T12:00:00+08:00")
        for item in episodes:
            print(
                f"{item['ref']} type={item['event_type']} salience={item['salience']:.2f} "
                f"age_days={item['age_days']:.1f} retrieval_weight={item['retrieval_weight']:.4f}"
            )

    # The first connection is closed here.  A new store and agent prove that
    # session 2 reads SQLite state, not Python objects from session 1.
    banner("SESSION 2 / 重新打开数据库")
    with MemoryStore(db_path, llm_enabled=llm) as memory:
        second_agent = MedicationCoordinatorAgent(memory, llm_planner_enabled=llm_planner)
        text = "我妈现在吃什么药？"
        response = second_agent.handle(
            CareEvent("query_current_medications", text),
            session_id="caregiver-session-2",
            turn_id="s2-current-medications",
        )
        show_turn(text, response, verbose_tools=verbose_tools)

        banner("一条警告的完整审计链")
        if first_warning:
            trail = first_warning["audit_trail"]
            print("警告：", f"{first_warning['drug_a']}×{first_warning['drug_b']}")
            print("来源：", json.dumps(first_warning["citations"], ensure_ascii=False, indent=2))
            print("支撑 memory refs：", json.dumps(trail["memory_refs"], ensure_ascii=False, indent=2))
            print("警告事件审计：", json.dumps(memory.audit_for(trail["warning_memory"]), ensure_ascii=False, indent=2))
            print("结论审计：", json.dumps(memory.audit_for(trail["conclusion"]), ensure_ascii=False, indent=2))
        else:
            print("本次运行没有产生可展示的警告。")

        banner("未决冲突")
        print(json.dumps(memory.open_conflicts(), ensure_ascii=False, indent=2))
        print(f"\nSQLite memory: {db_path.resolve()}")


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Run the Stage 3 medication coordinator demo")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--reset", action="store_true", help="remove only the selected SQLite DB and its sidecars before the demo")
    parser.add_argument("--llm", action="store_true", help="enable DeepSeek structured fact extraction")
    parser.add_argument("--llm-planner", action="store_true", help="opt in to the Stage 6 agentic loop: the LLM decides every cycle and composes responses; deterministic code enforces safety only")
    parser.add_argument("--verbose-tools", action="store_true")
    args = parser.parse_args()
    run_demo(
        args.db,
        reset=args.reset,
        llm=args.llm,
        verbose_tools=args.verbose_tools,
        llm_planner=args.llm_planner,
    )


if __name__ == "__main__":
    main()
