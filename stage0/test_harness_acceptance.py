"""Final acceptance regressions: failures must not be reported as passes."""
import copy
import json
import logging
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from stage0 import agent as agent_module, harness_eval
from stage0.agent import CareEvent
from stage0.graph_runner import LangGraphAgentRunner
from stage0.harness.manifest import check_restore_compatibility
from stage0.harness.observability import CallSpanStore, OTelExporter
from stage0.memory import MemoryStore
from stage0.test_harness_p1_c import make_agent


class AcceptanceGateTests(unittest.TestCase):
    def test_required_checks_trajectory_and_error_logs_are_gates(self):
        base = {"metrics": {"act_tools": [], "error_observations": [],
                            "unexpected_error_logs": 0},
                "errors": [], "response_text": "", "unexpected_logs": []}
        for checks, expected, errors in [({"read": False}, None, 0),
                                          ({"read": True}, ["rag_search"], 0),
                                          ({"read": True}, None, 1)]:
            with self.subTest(checks=checks, expected=expected, errors=errors):
                result = copy.deepcopy(base)
                result["metrics"]["unexpected_error_logs"] = errors
                actual = harness_eval._finalize("negative", "regression", result,
                                               {"goal": True}, {"safe": True}, checks, expected)
                self.assertEqual(actual["status"], "fail")

    def test_dataset_restores_logger_on_failure(self):
        logger = logging.getLogger("stage0")
        with mock.patch.object(logger, "propagate", True), \
                mock.patch.object(harness_eval, "SCENARIOS", [mock.Mock(side_effect=ValueError("fixture"))]):
            with self.assertRaises(ValueError):
                harness_eval.run_dataset()
            self.assertTrue(logger.propagate)

    def test_graph_scenario_removes_logging_handler(self):
        logger = logging.getLogger("stage0")
        before = list(logger.handlers)
        harness_eval.run_graph_scenario("logging-cleanup")
        self.assertEqual(logger.handlers, before)

    def test_all_semantic_changes_block_restore(self):
        for section in ("models", "prompts", "corpus"):
            with self.subTest(section=section):
                self.assertFalse(check_restore_compatibility(
                    {section: {"version": "old"}}, {section: {"version": "new"}})["compatible"])

    def test_p3_missing_work_cannot_qualify_for_adoption(self):
        from stage0.harness_p3_eval import evaluate_adoption, _full_read_coverage
        rows = [{"scenario": scenario, "mode": mode, "planner_decision_calls": 10 if mode == "single_agent" else 2,
                 "safety_status": "enforced", "no_medical_authority": True, "citation_validity": 1.0,
                 "tokens_charged": 100, "delegated_tasks": 0, "required_check_completion": 0.0,
                 "planner_payload_chars": {"mean": 100 if mode == "single_agent" else 50}}
                for scenario in ("multi_drug_labels", "long_label_consistency", "simple_current_meds")
                for mode in ("single_agent", "batch", "delegate")]
        self.assertFalse(evaluate_adoption(rows)["adopt_batching"])
        self.assertFalse(evaluate_adoption(rows)["adopt_delegation"])
        self.assertEqual(_full_read_coverage([], [{"source_url": "test", "text": "evidence"}]), 0)

    def test_p2_aggregation_is_median_and_safety_checks_every_repeat(self):
        from stage0.harness_p2_eval import _aggregate, acceptance_passed
        self.assertEqual(_aggregate([{"latency": n} for n in (1, 100, 2)]), {"latency": 2})
        safe = {"safety": {"all_enforced": True, "citation_valid": True}}
        unsafe = {"safety": {"all_enforced": False, "citation_valid": True}}
        report = {"baseline_runs": [unsafe, safe, safe], "optimized_runs": [unsafe, safe, safe],
                  "repeated_reads_scenario": {"comparison": {
                      "safety_status_identical": True, "honest_incomplete_response_both": True}}}
        self.assertFalse(acceptance_passed(report))


class PersistedAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = MemoryStore(Path(self.tmp.name) / "acceptance.db")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_same_arguments_retry_after_failure_is_retained(self):
        spans = CallSpanStore(self.store.connection, self.store._lock)
        span = {"run_id": "r", "dedup_key": "same", "kind": "tool",
                "span_id": "1", "call_id": "1", "status": "error", "error_kind": "transient"}
        spans.record(span)
        spans.record({**span, "span_id": "2", "call_id": "2", "status": "ok", "error_kind": None})
        rows = spans.spans_for_run("r")
        self.assertEqual([r["attempt_no"] for r in rows], [1, 2])

    def test_explicit_attempt_identity_prevents_false_replay(self):
        spans = CallSpanStore(self.store.connection, self.store._lock)
        span = {"run_id": "r", "dedup_key": "same", "kind": "tool", "status": "ok"}
        for attempt in ("a", "b", "b"):
            spans.record({**span, "span_id": attempt, "call_id": attempt, "attempt_id": attempt})
        self.assertEqual(spans.dedup_report("r"),
                         {"stored_spans": 2, "replays_folded": 1, "raw_events": 3})

    def test_run_and_resume_enforce_manifest_before_graph_execution(self):
        agent = make_agent(self.store, max_cycles=2)
        graph = LangGraphAgentRunner(agent, checkpoint_path=Path(self.tmp.name) / "cp.db")
        args = dict(event=CareEvent("register_profile", "合成验收", {"profile": {"age": 67}}),
                    session_id="s", turn_id="r", run_id="r")
        try:
            graph.run(**args)
            with mock.patch.object(agent_module, "PLANNER_SYSTEM_PROMPT", "changed for acceptance"), \
                    mock.patch.object(graph, "_invoke_budgeted") as invoke:
                with self.assertRaisesRegex(RuntimeError, "manifest"):
                    graph.run(**args)
                with self.assertRaisesRegex(RuntimeError, "manifest"):
                    graph.resume("r", {})
                invoke.assert_not_called()
        finally:
            graph.close()

    def test_missing_graph_manifest_requires_migration(self):
        from stage0.harness.manifest import enforce_restore
        with self.assertRaisesRegex(RuntimeError, "manifest missing"):
            enforce_restore(memory=self.store, agent=make_agent(self.store),
                            run_id="unknown", graph_version="1")

    def test_mid_turn_cancel_is_exposed_by_event_polling(self):
        from fastapi.testclient import TestClient
        from stage0.server import create_app
        from stage0.harness.progress import request_cancel
        from stage0.harness_eval import make_agent as offline_agent
        graphs = []

        def factory():
            agent = offline_agent(app.state.store)
            original = agent.executor.handlers["ddi_check"]

            def cancel_after_write(request):
                request_cancel(app.state.store, request.ctx.run_id, actor="test", reason="acceptance")
                return original(request)

            agent.executor.handlers["ddi_check"] = cancel_after_write
            graph = LangGraphAgentRunner(agent, checkpoint_path=Path(self.tmp.name) / "cancel-cp.db")
            graphs.append(graph)
            return graph

        app = create_app(db_path=Path(self.tmp.name) / "cancel.db", worker_thread=False, runner_factory=factory)
        try:
            client = TestClient(app)
            accepted = client.post("/v1/events", json={"session_id": "s", "event_type": "medication_change",
                "text": "新增氨氯地平", "payload": {"action": "add", "medication": "氨氯地平"}},
                headers={"Idempotency-Key": "cancel-acceptance"})
            self.assertEqual(accepted.status_code, 202)
            app.state.worker.drain_once()
            result = client.get("/v1/events/cancel-acceptance").json()
            self.assertEqual(result["status"], "committed")
            self.assertEqual(result["response"]["run_status"], "cancelled")
            self.assertEqual(len(result["response"]["operation_outcomes"]), 1)
        finally:
            for graph in graphs:
                graph.close()
            app.state.store.close()


class OTelTransportAcceptanceTests(unittest.TestCase):
    def test_exporter_failure_stays_on_background_thread(self):
        try:
            from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
        except ImportError:
            self.skipTest("optional OTel dependencies absent")
        called = threading.Event()

        class FailedExporter(SpanExporter):
            def export(self, spans):
                called.set()
                return SpanExportResult.FAILURE

        with mock.patch.dict(os.environ, {"STAGE0_OTEL_EXPORT": "1"}), mock.patch(
                "opentelemetry.exporter.otlp.proto.http.trace_exporter.OTLPSpanExporter",
                return_value=FailedExporter()):
            exporter = OTelExporter()
            try:
                for _ in range(20):
                    exporter.export_span({"kind": "tool", "source": "read", "run_id": "test"})
                exporter.flush()
                self.assertTrue(called.is_set())
                self.assertTrue(exporter.enabled)
            finally:
                exporter.close()

    def test_real_otlp_http_delivery_and_payload_redaction(self):
        try:
            from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
            import opentelemetry.sdk.trace  # noqa: F401
        except ImportError:
            self.skipTest("optional OTel dependencies absent; install requirements-harness-observability.txt")
        received = []

        class Receiver(BaseHTTPRequestHandler):
            def do_POST(self):
                received.append(self.rfile.read(int(self.headers["Content-Length"])))
                self.send_response(200)
                self.end_headers()

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Receiver)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        exporter = None
        try:
            with mock.patch.dict(os.environ, {"STAGE0_OTEL_EXPORT": "1",
                    "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": f"http://127.0.0.1:{server.server_port}/v1/traces"}):
                exporter = OTelExporter()
                self.assertTrue(exporter.enabled)
                exporter.export_span({"kind": "tool", "source": "memory_read", "run_id": "synthetic",
                                      "status": "ok", "duration_ms": 12.5,
                                      "prompt": "PRIVATE_CLINICAL_TEXT", "api_key": "PRIVATE_SECRET"})
                self.assertTrue(exporter.flush(timeout_millis=3000))
            self.assertEqual(len(received), 1)
            request = ExportTraceServiceRequest.FromString(received[0])
            spans = [s for r in request.resource_spans for scope in r.scope_spans for s in scope.spans]
            self.assertEqual(len(spans), 1)
            self.assertEqual(spans[0].name, "harness.tool.memory_read")
            self.assertNotIn(b"PRIVATE_", received[0])
        finally:
            if exporter is not None and hasattr(exporter, "close"):
                exporter.close()
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
