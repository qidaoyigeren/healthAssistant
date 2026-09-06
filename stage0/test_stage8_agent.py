"""Stage 8 (production upgrade) agent-loop tests.

Covers the per-turn budget (wall-clock / estimated tokens / cycles) with its
explicit degraded notice, the consecutive-rejection circuit breaker, bounded
planner payloads, the veto-only response verifier (including its failure
fallbacks), and persistent turn traces.  All LLM interactions use injected
providers; the default offline path stays untouched.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
import unittest.mock as mock
from pathlib import Path

from stage0.agent import (
    AgentState,
    CareEvent,
    DDITool,
    LLMPlanner,
    MedicationCoordinatorAgent,
    Observation,
    ResponseVerifier,
)
from stage0.memory import MemoryStore


def make_store(directory) -> MemoryStore:
    return MemoryStore(Path(directory) / "memory.db", llm_enabled=False)


def _repeat_read_provider(payload):
    """A valid, never-terminating proposal loop (repeated reads are legal)."""
    return {"decision": "tool", "tool": "memory_read", "purpose": "snapshot",
            "arguments": {"query": "snapshot"}}


def _early_respond_provider(payload):
    """Always proposes respond-before-consolidation -> safety rejection."""
    return {"decision": "respond", "rationale": "too early"}


def _composer_unavailable(payload):
    """Fail composition so tests exercise the logged template fallback
    instead of making a real provider call."""
    raise RuntimeError("composer disabled in test")


class TurnBudgetTests(unittest.TestCase):
    def _agent(self, store, provider) -> MedicationCoordinatorAgent:
        return MedicationCoordinatorAgent(
            store, ddi_tool=DDITool(lambda meds: []), rag_tool=None,
            llm_planner_enabled=True, proposal_provider=provider,
            response_provider=_composer_unavailable,
        )

    def test_wall_clock_budget_degrades_explicitly(self) -> None:
        def slow_provider(payload):
            time.sleep(0.08)
            return _repeat_read_provider(payload)

        with tempfile.TemporaryDirectory() as directory:
            with make_store(directory) as store:
                agent = self._agent(store, slow_provider)
                with mock.patch.dict(os.environ, {"AGENT_TURN_BUDGET_SECONDS": "0.15"}):
                    response = agent.handle(
                        CareEvent("query_current_medications", "现在吃什么药"),
                        session_id="s", turn_id="t1")
                trace = response.tool_trace
                self.assertTrue(any(entry.get("phase") == "budget" for entry in trace))
                budget_entry = next(e for e in trace if e.get("phase") == "budget")
                self.assertEqual(budget_entry["exhausted"], "wall_clock")
                self.assertIn("结果可能不完整", response.text)
                self.assertIn("建议咨询医生/药师", response.text)

    def test_token_budget_degrades_explicitly(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with make_store(directory) as store:
                agent = self._agent(store, _repeat_read_provider)
                with mock.patch.dict(os.environ, {"AGENT_TURN_TOKEN_BUDGET": "200"}):
                    response = agent.handle(
                        CareEvent("query_current_medications", "现在吃什么药"),
                        session_id="s", turn_id="t1")
                budget_entry = next(e for e in response.tool_trace if e.get("phase") == "budget")
                self.assertEqual(budget_entry["exhausted"], "tokens")
                self.assertIn("结果可能不完整", response.text)

    def test_offline_default_turn_never_degrades(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with make_store(directory) as store:
                agent = MedicationCoordinatorAgent(store)
                response = agent.handle(
                    CareEvent("register_profile", "建立档案", {"profile": {"age": 70}}),
                    session_id="s", turn_id="t1")
                self.assertNotIn("budget", [e.get("phase") for e in response.tool_trace])
                self.assertNotIn("结果可能不完整", response.text)


class CircuitBreakerTests(unittest.TestCase):
    def test_consecutive_rejections_trip_the_breaker(self) -> None:
        calls = {"n": 0}

        def rejecting_provider(payload):
            calls["n"] += 1
            return _early_respond_provider(payload)

        with tempfile.TemporaryDirectory() as directory:
            with make_store(directory) as store:
                agent = MedicationCoordinatorAgent(
                    store, ddi_tool=DDITool(lambda meds: []), rag_tool=None,
                    llm_planner_enabled=True, proposal_provider=rejecting_provider,
                    response_provider=_composer_unavailable)
                response = agent.handle(
                    CareEvent("query_current_medications", "现在吃什么药"),
                    session_id="s", turn_id="t1")
                # The breaker tripped after the 2nd consecutive safety
                # rejection and the turn finished deterministically.
                self.assertEqual(calls["n"], 2)
                decisions = [entry.get("decision", {}).get("tool")
                             for entry in response.tool_trace if entry.get("phase") == "plan"]
                self.assertIn("circuit_break", decisions)
                breaker = next(e for e in response.tool_trace
                               if e.get("decision", {}).get("tool") == "circuit_break")
                self.assertIn("不再消耗 LLM 调用", breaker.get("note", ""))
                self.assertIn("建议咨询医生/药师", response.text)

    def test_single_rejection_still_replans_through_the_llm(self) -> None:
        calls = {"n": 0}

        def once_rejecting_provider(payload):
            calls["n"] += 1
            if calls["n"] == 1:
                return _early_respond_provider(payload)
            observations = payload.get("observations", [])
            if not any(item.get("tool") == "memory_write" for item in observations):
                return {"decision": "tool", "tool": "memory_write", "purpose": "consolidate",
                        "arguments": {"operation": "consolidate_event"}}
            return {"decision": "respond", "rationale": "saved"}

        with tempfile.TemporaryDirectory() as directory:
            with make_store(directory) as store:
                agent = MedicationCoordinatorAgent(
                    store, ddi_tool=DDITool(lambda meds: []), rag_tool=None,
                    llm_planner_enabled=True, proposal_provider=once_rejecting_provider,
                    response_provider=_composer_unavailable)
                response = agent.handle(
                    CareEvent("query_current_medications", "现在吃什么药"),
                    session_id="s", turn_id="t1")
                self.assertGreaterEqual(calls["n"], 3)  # rejected once, then LLM finished the turn
                decisions = [entry.get("decision", {}).get("tool")
                             for entry in response.tool_trace if entry.get("phase") == "plan"]
                self.assertNotIn("circuit_break", decisions)


class PayloadBoundingTests(unittest.TestCase):
    def _state_with_rag_observations(self, count: int) -> AgentState:
        state = AgentState(session_id="s", turn_id="t", event=CareEvent("medication_change", "x"))
        chunk = {"text": "字" * 480, "drug_name": "测试药", "section": "注意事项",
                 "chunk_id": "R" * 16, "score": 1.0, "rank": 1}
        for cycle in range(1, count + 1):
            state.observations.append(Observation(
                tool="rag_search", purpose=f"p{cycle}", arguments={"query": "q"},
                result={"results": [dict(chunk) for _ in range(5)], "mode": "hybrid"},
                ok=True, cycle=cycle))
        state.cycle = count
        return state

    def test_old_observations_summarized_recent_full(self) -> None:
        planner = LLMPlanner()
        state = self._state_with_rag_observations(6)
        payload = planner.prompt_payload(state)
        summarized = [item for item in payload["observations"] if "result_digest" in item]
        full = [item for item in payload["observations"] if "result_digest" not in item]
        # Current cycle plus the two before it stay full (3); earlier cycles
        # are summarized (3).
        self.assertEqual(len(summarized), 3)
        self.assertEqual(len(full), 3)
        for item in summarized:
            self.assertNotIn("result", item)
            self.assertIn("tool", item)
        for item in full:
            self.assertIn("result", item)
            for chunk in item["result"]["results"]:
                self.assertLessEqual(len(chunk["text"]), LLMPlanner.RAG_TEXT_CHARS)
        for entry in payload["recent_trace"]:
            self.assertNotIn("observation", entry)

    def test_payload_growth_is_bounded(self) -> None:
        planner = LLMPlanner()
        sizes = []
        for count in (3, 6, 9, 12):
            state = self._state_with_rag_observations(count)
            sizes.append(len(json.dumps(planner.prompt_payload(state), ensure_ascii=False, default=str)))
        late_growth = sizes[3] - sizes[1]  # 6 -> 12 observations
        # Each summarized observation adds only a digest line; without bounding
        # this delta would carry six full 5x480-char RAG results (>10k chars).
        self.assertLess(late_growth, 6 * 400)

    def test_compression_does_not_touch_state(self) -> None:
        planner = LLMPlanner()
        state = self._state_with_rag_observations(6)
        planner.prompt_payload(state)
        for observation in state.observations:
            for chunk in observation.result["results"]:
                self.assertEqual(len(chunk["text"]), 480)  # originals untouched


class VerifierTests(unittest.TestCase):
    WARNING = {
        "drug_a": "药A", "drug_b": "药B", "effect": "测试风险",
        "citations": [{"uri": "https://example.test/label"}],
        "audit_trail": {"warning_memory": "memory:episodic:1@v1",
                        "conclusion": "memory:conclusion:1@v1",
                        "memory_refs": ["memory:episodic:1@v1"]},
    }

    def _cited_text(self, tail: str) -> str:
        return ("药A 与 药B 同服存在测试风险。来源：https://example.test/label；"
                "审计：memory:episodic:1@v1\n" + tail)

    def _agent_with_verifier(self, store, provider) -> MedicationCoordinatorAgent:
        return MedicationCoordinatorAgent(
            store, verifier=ResponseVerifier(provider=provider))

    def _check(self, agent, text):
        return agent._check_response(
            text, warnings=[self.WARNING], conflicts=[], memory_refs=["memory:episodic:1@v1"],
            escalation_required=True, refusal_required=False)

    def test_semantic_false_positive_cleared_by_verifier(self) -> None:
        # "以上相互作用提示..." is not covered by the rule-layer summary
        # exemptions and gets flagged as unrecorded_or_uncited_warning — the
        # Stage 6 false-positive class the verifier adjudicates.
        text = self._cited_text("以上相互作用提示已全部列出，请以医生/药师判断为准。\n建议咨询医生/药师。")
        passing = lambda payload: json.dumps({"verdict": "pass", "findings": []})
        with tempfile.TemporaryDirectory() as directory:
            with make_store(directory) as store:
                agent = self._agent_with_verifier(store, passing)
                without = MedicationCoordinatorAgent(store)._check_response(
                    text, warnings=[self.WARNING], conflicts=[],
                    memory_refs=["memory:episodic:1@v1"],
                    escalation_required=True, refusal_required=False)
                self.assertIn("unrecorded_or_uncited_warning", without)  # rules-only rejects
                self.assertEqual(self._check(agent, text), [])           # verifier clears

    def test_hard_gates_are_never_adjudicable(self) -> None:
        text = self._cited_text("另见 memory:semantic:999@v1。\n建议咨询医生/药师。")
        passing = lambda payload: json.dumps({"verdict": "pass", "findings": []})
        with tempfile.TemporaryDirectory() as directory:
            with make_store(directory) as store:
                agent = self._agent_with_verifier(store, passing)
                problems = self._check(agent, text)
                self.assertTrue(any(p.startswith("fabricated_memory_ref") for p in problems))

    def test_verifier_reject_appends_findings(self) -> None:
        # The semantic flag brings the verifier into the loop; its reject
        # verdict must keep the rule problems and append evidence-tagged
        # findings (the additional veto power).
        text = self._cited_text("以上相互作用提示已全部列出，请以医生/药师判断为准。\n建议咨询医生/药师。")
        rejecting = lambda payload: json.dumps({
            "verdict": "reject",
            "findings": [{"type": "uncited_warning", "line": 2,
                          "excerpt": "以上相互作用提示已全部列出",
                          "rationale": "复核不通过"}]})
        with tempfile.TemporaryDirectory() as directory:
            with make_store(directory) as store:
                agent = self._agent_with_verifier(store, rejecting)
                problems = self._check(agent, text)
                self.assertIn("unrecorded_or_uncited_warning", problems)
                self.assertTrue(any(p.startswith("verifier:uncited_warning@line2") for p in problems))

    def test_verifier_failure_falls_back_to_full_rules(self) -> None:
        text = self._cited_text("以上相互作用提示已全部列出，请以医生/药师判断为准。\n建议咨询医生/药师。")
        for bad in ("垃圾输出", json.dumps({"verdict": "maybe"}),
                    json.dumps({"verdict": "reject", "findings": [
                        {"type": "uncited_warning", "line": 99, "excerpt": "不存在的行", "rationale": "x"}]})):
            with tempfile.TemporaryDirectory() as directory:
                with make_store(directory) as store:
                    agent = self._agent_with_verifier(store, lambda p: bad)
                    problems = self._check(agent, text)
                    self.assertIn("unrecorded_or_uncited_warning", problems)
                    self.assertEqual(agent._last_verifier_info["status"], "unavailable")


class TurnTraceTests(unittest.TestCase):
    def test_turn_traces_persisted_and_queryable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with make_store(directory) as store:
                agent = MedicationCoordinatorAgent(store)
                agent.handle(CareEvent("register_profile", "建立档案", {"profile": {"age": 70}}),
                             session_id="s", turn_id="t1")
                traces = store.traces_for_turn("s", "t1")
                phases = {entry["phase"] for entry in traces}
                self.assertIn("plan", phases)
                self.assertIn("observe", phases)
                self.assertIn("respond", phases)
                # Division of labour: planning traces never appear in audit_log.
                audit_actions = {row[0] for row in store.connection.execute(
                    "SELECT action FROM audit_log")}
                self.assertNotIn("conclude_planning", audit_actions)

    def test_trace_persistence_failure_never_breaks_a_turn(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with make_store(directory) as store:
                agent = MedicationCoordinatorAgent(store)
                with mock.patch.object(MemoryStore, "record_turn_trace",
                                       side_effect=RuntimeError("trace storage down")):
                    response = agent.handle(
                        CareEvent("register_profile", "建立档案", {"profile": {"age": 70}}),
                        session_id="s", turn_id="t1")
                self.assertTrue(response.text)


def measure_payload_sizes() -> dict:
    """B2 measurement artifact: old (unbounded) vs new (bounded) payload sizes
    on synthetic RAG-sized observation stacks."""
    planner = LLMPlanner()
    rows = []
    for count in (2, 4, 6, 8, 12, 16):
        state = AgentState(session_id="s", turn_id="t", event=CareEvent("medication_change", "x"))
        chunk = {"text": "字" * 480, "drug_name": "测试药", "section": "注意事项",
                 "chunk_id": "R" * 16, "score": 1.0, "rank": 1}
        for cycle in range(1, count + 1):
            state.observations.append(Observation(
                tool="rag_search", purpose=f"p{cycle}", arguments={"query": "q"},
                result={"results": [dict(chunk) for _ in range(5)], "mode": "hybrid"},
                ok=True, cycle=cycle))
        state.cycle = count
        for entry in range(count):
            state.trace.append({"phase": "observe", "cycle": entry + 1, "tool": "rag_search",
                                "purpose": "p", "ok": True, "summary": "s",
                                "observation": {"tool": "rag_search", "result": {"results": [chunk] * 5}}})
        payload = planner.prompt_payload(state)
        new_size = len(json.dumps(payload, ensure_ascii=False, default=str))
        # Reconstruct the pre-Stage-8 unbounded view for comparison.
        from dataclasses import asdict
        old_observations = [asdict(item) for item in state.observations]
        old_trace = list(state.trace)
        old_size = new_size \
            - len(json.dumps(payload["observations"], ensure_ascii=False, default=str)) \
            + len(json.dumps(old_observations, ensure_ascii=False, default=str)) \
            - len(json.dumps(payload["recent_trace"], ensure_ascii=False, default=str)) \
            + len(json.dumps(old_trace, ensure_ascii=False, default=str))
        rows.append({"observations": count, "old_chars": old_size, "new_chars": new_size,
                     "old_tokens_estimated": int(old_size / 1.5),
                     "new_tokens_estimated": int(new_size / 1.5)})
    return {
        "measurement": "synthetic RAG-sized observation stacks (5 chunks x 480 chars per cycle); "
                       "chars/1.5 token estimate; single observation per cycle",
        "rows": rows,
    }


if __name__ == "__main__":
    unittest.main()
