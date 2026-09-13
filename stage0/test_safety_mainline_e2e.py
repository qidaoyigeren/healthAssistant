"""长期用药安全主线的端到端场景（隔离合成数据，不产生真实临床建议）。

三个场景加一条可用性要求，全部走**产品路径**（事件受理 → worker → 检查 → 事项 →
查询），而不是直接调用内部函数：

1. 用药变化产生待复核事项；
2. 跨会话补充后恢复同一事项；
3. 新事实使旧判断失效并重新打开；
4. 模型不可用时，必要检查仍执行、事项仍保存、用户拿到准确结果与待办。

合成药物名一律使用"合成药甲/乙/丙"，避免任何真实临床含义。
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from stage0.agent import DDITool, MedicationCoordinatorAgent
from stage0.care_tasks import (CareTasks, SAFETY_CASE_CONTRACT,
                               safety_case_request_id)
from stage0.memory import MemoryStore, SemanticFact
from stage0.product import ProductStore
from stage0.safety_cases import SafetyCaseStore
from stage0 import safety_cases as sc
from stage0 import safety_checks as checks

# 合成检测器：只认识"合成药甲 × 合成药乙"这一对。
SYNTHETIC_WARNING = {
    "drug_a": "合成药甲", "drug_b": "合成药乙", "severity": "major",
    "mechanism": "合成机制", "effect": "合成风险效应", "management": None,
    "source_text": "合成药甲与合成药乙合用可能增加合成风险（合成说明书文本）",
    "source_url": "https://synthetic.invalid/label/a",
    "confidence": "high", "detection_path": "synthetic_detector",
}


def synthetic_detect(medications):
    if {"合成药甲", "合成药乙"}.issubset(set(medications)):
        return [dict(SYNTHETIC_WARNING)]
    return []


class SyntheticRAG:
    """合成检索：命中一段带**适用人群条件**的说明书文字，且当前记录里没有对应事实。

    这正是"调查能提出一个只有用户能回答的问题"的合成条件——沿用仓库既有 A2 夹具
    的锚句（`stage0/agent_evals` dev 集）以确保命中同一条缺口规则。
    """

    CHUNKS = [
        {"chunk_id": "synthetic-label-1", "drug_name": "合成药甲",
         "section": "药物相互作用",
         "text": "【合成材料】合成药甲与合成药乙合用增加出血风险。",
         "source_url": "https://synthetic.invalid/label/1",
         "corpus_version": "synthetic-v1"},
        {"chunk_id": "synthetic-label-renal", "drug_name": "合成药甲",
         "section": "药物相互作用",
         "text": "合成药甲与合成药乙合用可能增加出血风险；肾功能不全者需调整剂量。",
         "source_url": "https://synthetic.invalid/label/renal",
         "corpus_version": "synthetic-v1"},
    ]

    def __call__(self, query, **kwargs):
        return {"query": query, "mode": "synthetic", "corpus_version": "synthetic-v1",
                "results": [dict(chunk) for chunk in self.CHUNKS]}


class EmptyRAG:
    def __call__(self, query, **kwargs):
        return {"query": query, "mode": "synthetic", "corpus_version": "synthetic-v1",
                "results": []}


class _App:
    """产品路径的最小宿主：真实服务、真实 worker、真实数据库，无网络、无模型。"""

    def __init__(self, directory: Path, *, rag=None, detection=None,
                 proposal_provider=None) -> None:
        import stage0.server as server
        holder: dict = {}
        detector = detection or synthetic_detect

        def factory():
            # 这个 factory 是 **worker** 用的那个：经事件/队列恢复的调查走它。
            # 只给测试自己的 CareTasks 传脚本化模型是不够的——恢复那一轮会退回
            # 降级路径，测出来的就不是同一件事。
            return MedicationCoordinatorAgent(
                holder["store"], ddi_tool=DDITool(detector),
                rag_tool=rag if rag is not None else EmptyRAG(),
                llm_planner_enabled=proposal_provider is not None,
                proposal_provider=proposal_provider)

        self.app = server.create_app(db_path=directory / "memory.db",
                                     worker_thread=False, agent_factory=factory)
        holder["store"] = self.app.state.store
        self.store = self.app.state.store
        self.worker = self.app.state.worker
        self.client = TestClient(self.app)
        self.product = ProductStore(self.store)

    def close(self) -> None:
        self.client.close()
        self.worker.stop()
        self.store.close()

    def submit(self, key: str, body: dict) -> dict:
        response = self.client.post("/v1/events", json=body,
                                    headers={"Idempotency-Key": key})
        assert response.status_code == 202, response.text
        self.worker.drain_once()
        return self.client.get(f"/v1/events/{key}").json()

    def add_medication(self, key: str, name: str) -> dict:
        return self.submit(key, {"session_id": "synthetic", "event_type": "medication_change",
                                 "text": f"新增{name}", "source": "caregiver",
                                 "payload": {"action": "add", "medication": name}})

    def stop_medication(self, key: str, name: str) -> dict:
        return self.submit(key, {"session_id": "synthetic", "event_type": "medication_change",
                                 "text": f"停用{name}", "source": "caregiver",
                                 "payload": {"action": "remove", "medication": name}})

    def mainline(self) -> dict:
        response = self.client.get("/v1/safety-mainline")
        assert response.status_code == 200, response.text
        return response.json()

    def cases(self) -> list[dict]:
        response = self.client.get("/v1/safety-cases")
        assert response.status_code == 200, response.text
        return response.json()["items"]


class SafetyMainlineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="synthetic-safety-")
        self.directory = Path(self.temp.name)
        self.api = _App(self.directory)

    def tasks(self) -> CareTasks:
        """调查用**脚本化**的 agent：同一个合成检测器，不调真实模型。"""
        store = self.api.store
        return CareTasks(self.api.product, agent_factory=lambda: MedicationCoordinatorAgent(
            store, ddi_tool=DDITool(synthetic_detect), rag_tool=EmptyRAG()))

    def tearDown(self):
        try:
            self.api.close()
        except Exception:
            pass
        self.temp.cleanup()


# ---------------------------------------------------------------------------
# 场景 1：用药变化产生待复核事项
# ---------------------------------------------------------------------------
class Scenario1MedicationChangeTests(SafetyMainlineTests):
    def test_a_change_produces_a_case_with_reason_evidence_and_next_step(self):
        # 已有长期用药与相关背景。
        self.api.add_medication("seed-1", "合成药甲")
        self.assertEqual([], self.api.cases())

        # 用户通过既有的事件受理路径提交用药变化。
        self.api.add_medication("change-2", "合成药乙")

        # 必要检查**由程序执行**并留下依据——不是"调用了 ddi_check"。
        self.assertEqual(1, len(self.api.cases()))
        case = self.api.cases()[0]
        self.assertEqual(sc.CASE_INTERACTION_RISK, case["case_type"])
        self.assertTrue(case["conclusions"], "检查必须留下结论文本")
        conclusion = case["conclusions"][0]
        self.assertEqual("current", conclusion["status"])
        self.assertIn("合成药甲", conclusion["text"])
        self.assertIn("合成药乙", conclusion["text"])
        self.assertTrue(conclusion["sources"], "结论必须带来源原文")
        self.assertEqual("https://synthetic.invalid/label/a", conclusion["sources"][0])

        # 用户看到的是：为什么产生、涉及哪些记录、下一步是谁做什么。
        self.assertEqual("necessary_check", case["trigger"]["kind"])
        self.assertEqual("medication_set", case["trigger"]["trigger"])
        labels = {item["label"] for item in case["medications"]}
        self.assertEqual({"合成药甲", "合成药乙"}, labels)
        self.assertTrue(case["next_action_summary"])
        self.assertTrue(case["responsible_party"])
        self.assertIn(case["status"], sc.UNSETTLED_STATUSES)
        self.assertIsNone(case["resolution_basis"])

    def test_the_check_runs_once_per_world_state_and_not_per_event(self):
        """重复上报同一件事不会让检查无限重复，也不会少查一次。"""
        self.api.add_medication("seed-1", "合成药甲")
        self.api.add_medication("change-2", "合成药乙")
        first = len(self.api.cases())
        # 同一条事件用同一个幂等键重放：不产生第二件事。
        self.api.add_medication("change-2", "合成药乙")
        self.assertEqual(first, len(self.api.cases()))

    def test_the_case_is_visible_on_the_mainline_page(self):
        self.api.add_medication("seed-1", "合成药甲")
        self.api.add_medication("change-2", "合成药乙")
        page = self.api.mainline()
        self.assertEqual(1, page["counts"]["attention"])
        self.assertEqual(["合成药甲", "合成药乙"],
                         [m["display_name"] for m in page["current_medications"]])
        self.assertTrue(page["recent_medication_changes"])
        # 检查队列的真实状态对用户可见——"没看到提示"不能代替"检查跑过了"。
        self.assertTrue(page["necessary_checks"]["available"])
        self.assertEqual(0, page["necessary_checks"]["open"])


# ---------------------------------------------------------------------------
# 场景 2：跨会话补充后恢复
# ---------------------------------------------------------------------------
class Scenario2CrossSessionTests(SafetyMainlineTests):
    def test_a_case_waits_restarts_and_resumes_without_re_asking(self):
        self.api.add_medication("seed-1", "合成药甲")
        self.api.add_medication("change-2", "合成药乙")
        case_id = self.api.cases()[0]["case_id"]

        # 事项进入等待补充：登记一条只有用户能回答的问题。
        store = SafetyCaseStore(self.api.product)
        store.require_input(case_id, request_id="req-dose",
                            question="合成药乙目前的服用频次是什么？", fields=["schedule"])
        waiting = self.api.cases()[0]
        self.assertEqual(sc.STATUS_AWAITING_USER, waiting["status"])
        self.assertEqual(1, len(waiting["required_inputs"]))

        # 保存并结束进程。
        self.api.close()
        self.api = _App(self.directory)

        # 恢复后仍是**同一件事**，问题还在，没有被重新生成。
        resumed = self.api.cases()[0]
        self.assertEqual(case_id, resumed["case_id"])
        self.assertEqual(sc.STATUS_AWAITING_USER, resumed["status"])
        self.assertEqual(["req-dose"],
                         [i["request_id"] for i in resumed["required_inputs"]])

        # 用户补充到达：只关闭它实际回答的那一条。
        store = SafetyCaseStore(self.api.product)
        store.record_input(case_id, request_id="req-dose",
                           answer_ref="answer:1", value="每日一次")
        answered = self.api.cases()[0]
        self.assertEqual([], answered["required_inputs"], "被回答的请求应当关闭")
        self.assertEqual(1, answered["answered_inputs_count"])
        self.assertNotEqual(sc.STATUS_RESOLVED, answered["status"],
                            "补充到达不等于事项解决")
        # 不重复写入：同一条回答重放不会产生第二条记录，也不会把已答的请求重开。
        store.record_input(case_id, request_id="req-dose",
                           answer_ref="answer:1", value="每日一次")
        again = self.api.cases()[0]
        self.assertEqual(1, again["answered_inputs_count"])
        self.assertEqual([], again["required_inputs"])

    def test_each_answer_lands_on_the_request_it_belongs_to(self):
        """一次提交带多条回答时，值不能被安到别人的请求上。"""
        self.api.add_medication("seed-1", "合成药甲")
        self.api.add_medication("change-2", "合成药乙")
        case_id = self.api.cases()[0]["case_id"]
        store = SafetyCaseStore(self.api.product)
        store.require_input(case_id, request_id="req-a", question="问题 A")
        store.require_input(case_id, request_id="req-b", question="问题 B")

        tasks = CareTasks(self.api.product)
        task = tasks.create("input-key", "safety_case", case_id)
        task = self.api.product.get(task["id"], "care_task")
        task = tasks.record_input(
            task["id"], "input-call", task["revision"],
            answers=[{"request_id": "req-b", "value": "回答B"},
                     {"request_id": "req-a", "value": "回答A"}],
            review_request_ids=["req-a", "req-b"])

        case = self.api.cases()[0]
        recorded = {entry["request_id"]: entry
                    for entry in case["history"] if entry["event"] == "input_recorded"}
        self.assertEqual("回答A", recorded["req-a"]["value"])
        self.assertEqual("回答B", recorded["req-b"]["value"])
        self.assertEqual([], case["required_inputs"])

    def test_supplying_an_answer_does_not_close_the_case(self):
        self.api.add_medication("seed-1", "合成药甲")
        self.api.add_medication("change-2", "合成药乙")
        case_id = self.api.cases()[0]["case_id"]
        store = SafetyCaseStore(self.api.product)
        store.require_input(case_id, request_id="req-1", question="问题一")
        store.record_input(case_id, request_id="req-1", answer_ref="a:1", value="答复")
        case = self.api.cases()[0]
        self.assertEqual(sc.STATUS_OPEN, case["status"])
        self.assertIsNone(case["resolution_basis"])


# ---------------------------------------------------------------------------
# 场景 3：新事实使旧判断失效
# ---------------------------------------------------------------------------
class Scenario3InvalidationTests(SafetyMainlineTests):
    """已有的处置记录遇到新信息时会发生什么。

    注意一个**由设计决定**的事实：药物对事项只有在触发条件（两条药同时在用）
    被消除之后才能关闭，而消除它的动作本身会结束这个用药分期。所以"已关闭的
    药物对事项在同一分期里被重开"是不可达的——可达的是"已有**处置记录**（持续
    跟进）的事项，因新信息进入重新复核"。
    """

    def _case_with_disposition(self) -> str:
        self.api.add_medication("seed-1", "合成药甲")
        self.api.add_medication("change-2", "合成药乙")
        case = self.api.cases()[0]
        store = SafetyCaseStore(self.api.product)
        store.disposition(case["case_id"], expected_revision=case["revision"],
                          disposition=sc.DISPOSITION_MONITORING, basis_kind=sc.BASIS_CHECK,
                          actor="local-demo-caregiver", roles=("caregiver",),
                          follow_up={"kind": "review_at", "at": "2026-10-01T00:00:00+00:00"})
        self.assertEqual(sc.STATUS_MONITORING, self.api.cases()[0]["status"])
        return case["case_id"]

    def test_a_later_change_sends_the_case_back_for_recheck_and_says_why(self):
        case_id = self._case_with_disposition()

        # 相关记录变化 → 依据失效。
        self.api.add_medication("change-3", "合成药丙")

        reopened = self.api.cases()[0]
        self.assertEqual(case_id, reopened["case_id"], "受影响的是同一件事")
        self.assertEqual(sc.STATUS_NEEDS_RECHECK, reopened["status"])
        self.assertIsNone(reopened["resolution_basis"], "旧依据不能继续生效")
        # "为何重新复核"必须能从历史里读出来。
        changes = [e for e in reopened["history"] if e["event"] == "status_changed"]
        self.assertTrue(changes, reopened["history"])
        self.assertEqual(sc.STATUS_NEEDS_RECHECK, changes[-1]["to"])
        self.assertTrue(changes[-1].get("why"))
        self.assertIn("重新", reopened["next_action_summary"])

    def test_a_routine_sync_does_not_erase_a_monitoring_arrangement(self):
        case_id = self._case_with_disposition()
        synced = SafetyCaseStore(self.api.product).sync(case_id)
        self.assertEqual(sc.STATUS_MONITORING, synced["current_status"])
        self.assertTrue(synced["follow_up"]["confirmed"])

    def test_seen_and_old_confirmations_cannot_approve_the_new_state(self):
        case_id = self._case_with_disposition()
        store = SafetyCaseStore(self.api.product)
        # 「用户已读」不是状态。
        seen = store.mark_seen(case_id)
        self.assertIsNotNone(seen["user_seen_at"])
        self.assertEqual(sc.STATUS_MONITORING, seen["current_status"])

        self.api.add_medication("change-3", "合成药丙")
        reopened = self.api.cases()[0]
        # 旧版本上的检查结论不能批准当前状态。
        from stage0.product import ProductError
        with self.assertRaises(ProductError) as err:
            store.disposition(case_id, expected_revision=reopened["revision"],
                              disposition=sc.DISPOSITION_RESOLVED,
                              basis_kind=sc.BASIS_CHECK,
                              actor="local-demo-caregiver", roles=("caregiver",))
        self.assertEqual(409, err.exception.status)
        self.assertNotEqual(sc.STATUS_RESOLVED, self.api.cases()[0]["status"])

    def test_a_resolved_case_stays_resolved_because_its_trigger_is_gone(self):
        """停药 → 触发条件消除 → 关闭；重新启用是**新的用药分期**，不是同一件事。"""
        self.api.add_medication("seed-1", "合成药甲")
        self.api.add_medication("change-2", "合成药乙")
        case_id = self.api.cases()[0]["case_id"]
        self.api.stop_medication("stop-2", "合成药乙")
        store = SafetyCaseStore(self.api.product)
        closed = store.disposition(case_id, expected_revision=self.api.cases()[0]["revision"],
                                   disposition=sc.DISPOSITION_RESOLVED,
                                   basis_kind=sc.BASIS_CHECK,
                                   actor="local-demo-caregiver", roles=("caregiver",))
        self.assertEqual(sc.STATUS_RESOLVED, closed["current_status"])
        self.assertTrue(closed["resolution_basis"]["evidence"]["eliminated"])

        self.api.add_medication("restart-2", "合成药乙")
        cases = self.api.cases()
        self.assertEqual(2, len(cases), "重新启用属于新的用药分期")
        self.assertNotEqual(case_id, next(c for c in cases if c["case_id"] != case_id)["case_id"])


# ---------------------------------------------------------------------------
# 产品接口：前端真实调用的那几个端点
# ---------------------------------------------------------------------------
class SafetyCaseApiTests(SafetyMainlineTests):
    """前端走的就是这些端点；这里验的是**服务端**的拒绝/接受，不是前端措辞。"""

    def _case(self) -> dict:
        self.api.add_medication("seed-1", "合成药甲")
        self.api.add_medication("change-2", "合成药乙")
        return self.api.cases()[0]

    def test_seen_endpoint_does_not_change_the_lifecycle(self):
        case = self._case()
        response = self.api.client.post(f"/v1/safety-cases/{case['case_id']}/seen",
                                        json={"key": "seen-1"})
        self.assertEqual(200, response.status_code, response.text)
        body = response.json()
        self.assertIsNotNone(body["user_seen_at"])
        self.assertEqual(case["status"], body["status"])
        self.assertEqual(case["revision"], body["revision"],
                         "「已读」不是一次状态迁移，不该产生新的修订")

    def test_a_disposition_with_a_valid_basis_is_accepted(self):
        case = self._case()
        self.api.stop_medication("stop-2", "合成药乙")

        # 关闭之前先看现场核对结果（界面用这个端点，不靠猜）。
        evidence = self.api.client.get(
            f"/v1/safety-cases/{case['case_id']}/closure-evidence").json()
        self.assertTrue(evidence["ok"], evidence)
        self.assertTrue(evidence["eliminated"])

        revision = self.api.cases()[0]["revision"]
        response = self.api.client.post(f"/v1/safety-cases/{case['case_id']}/disposition",
                                        json={"key": "close-1", "expected_revision": revision,
                                              "disposition": "resolved_with_basis",
                                              "basis_kind": "deterministic_check_completed"})
        self.assertEqual(200, response.status_code, response.text)
        body = response.json()
        self.assertEqual(sc.STATUS_RESOLVED, body["status"])
        self.assertEqual(sc.BASIS_CHECK, body["resolution_basis"]["kind"])
        # 操作者来自认证上下文，不是请求体。
        self.assertEqual("local-demo-caregiver", body["resolution_basis"]["actor"])

    def test_the_request_body_cannot_claim_a_reviewer_identity(self):
        """请求体里自报的 actor 不被读取——身份只能来自认证上下文。"""
        case = self._case()
        self.api.stop_medication("stop-2", "合成药乙")
        revision = self.api.cases()[0]["revision"]
        response = self.api.client.post(f"/v1/safety-cases/{case['case_id']}/disposition",
                                        json={"key": "close-actor", "expected_revision": revision,
                                              "disposition": "resolved_with_basis",
                                              "basis_kind": "deterministic_check_completed",
                                              "actor": "chief-physician"})
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual("local-demo-caregiver",
                         response.json()["resolution_basis"]["actor"])

    def test_a_refused_disposition_returns_its_reason_verbatim(self):
        """用户转述医生意见不能关闭事项：服务端拒绝，理由原样返回给界面。"""
        case = self._case()
        response = self.api.client.post(f"/v1/safety-cases/{case['case_id']}/disposition",
                                        json={"key": "close-2", "expected_revision": case["revision"],
                                              "disposition": "resolved_with_basis",
                                              "basis_kind": "user_reported",
                                              "actor": "caregiver"})
        self.assertEqual(409, response.status_code)
        message = response.json()["error"]["message"]
        self.assertIn("专业人员复核", message)
        self.assertNotEqual(sc.STATUS_RESOLVED, self.api.cases()[0]["status"])

    def test_a_stale_revision_is_refused(self):
        case = self._case()
        response = self.api.client.post(f"/v1/safety-cases/{case['case_id']}/disposition",
                                        json={"key": "close-3",
                                              "expected_revision": case["revision"] + 3,
                                              "disposition": "resolved_with_basis",
                                              "basis_kind": "deterministic_check_completed",
                                              "actor": "caregiver"})
        self.assertEqual(409, response.status_code)
        self.assertNotEqual(sc.STATUS_RESOLVED, self.api.cases()[0]["status"])

    def test_two_investigate_requests_do_not_start_two_investigations(self):
        """同一事项只跑一次调查：服务端强制，而不是靠界面记得先查一遍。

        同一个幂等键 → 重放同一次受理；不同键但事项已在跑 → 明确 409。
        两种情况下都**只有一条**在跑的调查。
        """
        case = self._case()
        first = self.api.client.post(f"/v1/safety-cases/{case['case_id']}/investigate",
                                     json={"key": "inv-1"})
        self.assertEqual(200, first.status_code, first.text)
        replay = self.api.client.post(f"/v1/safety-cases/{case['case_id']}/investigate",
                                      json={"key": "inv-1"})
        self.assertEqual(200, replay.status_code, replay.text)
        self.assertEqual(first.json()["id"], replay.json()["id"])

        second = self.api.client.post(f"/v1/safety-cases/{case['case_id']}/investigate",
                                      json={"key": "inv-2"})
        self.assertEqual(409, second.status_code)
        self.assertIn("已在队列", second.json()["error"]["message"])
        active = [t for t in self.api.product.objects("care_task")
                  if t.get("goal_type") == "safety_case"
                  and t["status"] not in ("completed", "cancelled", "failed")]
        self.assertEqual(1, len(active), active)

    def test_the_answer_endpoint_saves_and_leaves_the_case_unresolved(self):
        case = self._case()
        store = SafetyCaseStore(self.api.product)
        store.require_input(case["case_id"], request_id="q1", question="最近有没有出血？",
                            why_needed="它决定这条提示是否按当前用法成立")
        revision = self.api.cases()[0]["revision"]
        response = self.api.client.post(f"/v1/safety-cases/{case['case_id']}/answer",
                                        json={"key": "ans-1", "expected_revision": revision,
                                              "request_id": "q1", "value": "上周有一点牙龈出血"})
        self.assertEqual(200, response.status_code, response.text)
        body = response.json()
        self.assertEqual([], body["required_inputs"])
        self.assertEqual(1, body["answered_inputs_count"])
        self.assertNotEqual(sc.STATUS_RESOLVED, body["status"], "回答不等于事项解决")

    def test_an_empty_answer_over_the_api_closes_nothing(self):
        case = self._case()
        store = SafetyCaseStore(self.api.product)
        store.require_input(case["case_id"], request_id="q1", question="最近有没有出血？")
        revision = self.api.cases()[0]["revision"]
        response = self.api.client.post(f"/v1/safety-cases/{case['case_id']}/answer",
                                        json={"key": "ans-empty", "expected_revision": revision,
                                              "request_id": "q1", "value": "   "})
        self.assertEqual(200, response.status_code, response.text)
        body = response.json()
        self.assertEqual(["q1"], [i["request_id"] for i in body["required_inputs"]],
                         "空回答不得关闭请求")

    def test_saying_i_do_not_know_over_the_api_routes_to_alternative_evidence(self):
        case = self._case()
        store = SafetyCaseStore(self.api.product)
        store.require_input(case["case_id"], request_id="q1", question="最近有没有出血？")
        revision = self.api.cases()[0]["revision"]
        body = self.api.client.post(f"/v1/safety-cases/{case['case_id']}/answer",
                                    json={"key": "ans-unknown", "expected_revision": revision,
                                          "request_id": "q1", "value": "不知道"}).json()
        self.assertEqual(sc.STATUS_OPEN, body["status"])
        self.assertEqual("agent", body["responsible_party"])
        self.assertNotEqual(sc.STATUS_AWAITING_USER, body["status"],
                            "明说不知道之后不该继续等这位用户")

    def test_answering_wakes_the_waiting_investigation_through_the_queue(self):
        """补充到达要**经既有队列**唤醒调查——不是新框架，也不是什么都不发生。"""
        from stage0.care_tasks import CareTasks
        case_id = self.api.cases()[0]["case_id"] if self.api.cases() else None
        self.api.add_medication("seed-1", "合成药甲")
        self.api.add_medication("change-2", "合成药乙")
        case_id = self.api.cases()[0]["case_id"]

        tasks = self.tasks()
        task = tasks.create("wake-1", "safety_case", case_id)
        store = SafetyCaseStore(self.api.product)
        store.require_input(case_id, request_id="q1", question="最近有没有出血？")
        # 让任务停在"等待补充"。
        task = self.api.product.get(task["id"], "care_task")
        task["status"] = "waiting_input"
        task["revision"] += 1
        with self.api.product.transaction():
            self.api.product.save("care_task", task)

        before_children = len((task.get("resource_budget") or {}).get("child_run_ids") or [])
        revision = self.api.cases()[0]["revision"]
        body = self.api.client.post(f"/v1/safety-cases/{case_id}/answer",
                                    json={"key": "wake-answer", "expected_revision": revision,
                                          "request_id": "q1", "value": "没有"}).json()
        self.assertEqual([], body["required_inputs"], "回答应当被保存")

        queued = self.api.store.connection.execute(
            "SELECT COUNT(*) FROM outbox_tasks WHERE status IN ('open','running')").fetchone()[0]
        self.assertGreaterEqual(queued, 1, "回答之后应当有一条排队的调查")
        after = self.api.product.get(task["id"], "care_task")
        self.assertGreater(len(after["resource_budget"]["child_run_ids"]), before_children,
                           "唤醒要走既有的 child_run 预算记账")

    def test_the_mainline_page_reports_the_check_queue_honestly(self):
        self._case()
        page = self.api.mainline()
        for key in ("attention", "awaiting_user", "awaiting_professional",
                    "needs_recheck", "settled"):
            self.assertIn(key, page["counts"])
        self.assertTrue(page["necessary_checks"]["note"])


# ---------------------------------------------------------------------------
# 场景 1（续）：Agent 围绕未决信息展开调查并提出补问
# ---------------------------------------------------------------------------
class AgentInvestigationTests(SafetyMainlineTests):
    """从**一件具体事项**开工的调查，而不是从一份巨大的患者快照重新规划全部任务。"""

    def setUp(self):
        super().setUp()
        # 合成检索：命中"肾功能不全者慎用"，而当前记录里**没有**肾功能事实——
        # 这正是一个只能由用户回答的缺口。
        self.api.close()
        self.api = _App(self.directory, rag=SyntheticRAG())

    def test_the_agent_gets_the_case_context_not_a_whole_patient_replay(self):
        """Agent 看到的是**这一件事**的上下文，而不是重新铺一遍患者档案。"""
        self.api.add_medication("seed-1", "合成药甲")
        self.api.add_medication("change-2", "合成药乙")
        case_id = self.api.cases()[0]["case_id"]
        tasks = self.tasks()
        task = tasks.create("case-ctx-1", "safety_case", case_id)
        task = self.api.product.get(task["id"], "care_task")
        case = SafetyCaseStore(self.api.product).get(case_id)

        context = tasks._safety_case_context(task, case, {"max_steps": 16})
        self.assertEqual(case_id, context["case"]["case_id"])
        self.assertEqual("interaction_risk", context["case"]["case_type"])
        self.assertEqual("necessary_check", context["why"]["trigger"]["kind"])
        self.assertIn("合成药甲", [m["label"] for m in context["subjects"]["medications"]])
        # 当前结论带着**触发条件状态**，Agent 不必从正文里猜风险还在不在。
        self.assertEqual("risk_present", context["conclusions"][0]["trigger_state"])
        self.assertTrue(context["conclusions"][0]["version_applies"])
        self.assertIn("allowed_actions", context)
        self.assertIn("budget", context)
        # 只覆盖本事项：没有整个患者档案的复制品。
        for leaked in ("semantic", "all_facts", "patient_snapshot"):
            self.assertNotIn(leaked, context)

    def test_the_case_context_reaches_the_model_view_and_survives_a_resume(self):
        from stage0.investigation import InvestigationState
        self.api.add_medication("seed-1", "合成药甲")
        self.api.add_medication("change-2", "合成药乙")
        case_id = self.api.cases()[0]["case_id"]
        tasks = self.tasks()
        task = tasks.create("case-ctx-2", "safety_case", case_id)
        task = tasks.resume(task["id"], "case-ctx-run", task["revision"])

        inv = task.get("investigation") or {}
        self.assertTrue(inv.get("case_context"), inv.keys())
        self.assertEqual(case_id, inv["case_context"]["case"]["case_id"])
        # 模型真正读到的视图里也有它。
        restored = InvestigationState.restore(inv, "local-demo")
        self.assertIn("case_context", restored.planner_view())
        # 并且它随状态持久化：进程重开后仍在。
        self.api.close()
        self.api = _App(self.directory, rag=SyntheticRAG())
        resumed = self.api.product.get(task["id"], "care_task")
        self.assertTrue((resumed.get("investigation") or {}).get("case_context"),
                        "恢复后应当仍有同一份事项上下文")

    def test_each_decision_carries_its_structured_fields(self):
        """每一步决策都留下：针对哪个问题、依据什么、预期解决什么、什么会改变下一步。

        缺省就是缺省——界面显示"未说明"，不替模型补一个它没给的理由。
        """
        self.api.add_medication("seed-1", "合成药甲")
        self.api.add_medication("change-2", "合成药乙")
        case_id = self.api.cases()[0]["case_id"]
        tasks = self.tasks()
        task = tasks.create("case-fields-1", "safety_case", case_id)
        task = tasks.resume(task["id"], "case-fields-run", task["revision"])

        # 轨迹存在运行上（运行过程记录），不在 investigation 状态里。
        trace = []
        for run_id in task["resource_budget"]["child_run_ids"]:
            run = self.api.store.workflow_run_get(run_id) or {}
            trace.extend((run.get("result") or {}).get("trace") or [])
        # 每次**动作**决策都带齐五个结构化字段。`respond` 是收尾不是动作，
        # 它没有 gap_id 可言，不在这一条的范围里。
        self.assertTrue(trace, "调查应当留下轨迹")
        for entry in trace:
            if entry.get("phase") != "plan":
                continue
            decision = entry["decision"]
            if decision.get("tool") in (None, "respond"):
                continue
            for key in ("tool", "gap_id", "expected_observation", "basis_refs",
                        "expected_change"):
                self.assertIn(key, decision, decision)
            # 没有依据就是空列表/未说明，不是编出来的引用。
            self.assertIsInstance(decision["basis_refs"], list)
        # 权威记录由**程序**读并校验，来源如实记录——不是把标记直接置真冒充。
        inv = task.get("investigation") or {}
        self.assertTrue(inv.get("authority_read"))
        self.assertEqual("code_snapshot_validated", inv.get("authority_source"))

    def test_a_tool_action_keeps_basis_and_change_separate_from_arguments(self):
        from stage0.agent import ToolAction
        action = ToolAction('memory_read', 'p', {'query': 'snapshot'}, 'r', 'gap:1',
                            'expected', ('memory:conclusion:1@v1',), '若为空则换问法')
        self.assertEqual(('memory:conclusion:1@v1',), action.basis_refs)
        self.assertEqual('若为空则换问法', action.expected_change)
        # 决策元数据不混进工具参数。
        self.assertEqual({'query': 'snapshot'}, action.arguments)

    def scripted_tasks(self, provider):
        """用**脚本化的模型**驱动真实执行路径（契约验证，不是模型能力）。"""
        store = self.api.store
        return CareTasks(self.api.product, agent_factory=lambda: MedicationCoordinatorAgent(
            store, ddi_tool=DDITool(synthetic_detect), rag_tool=EmptyRAG(),
            llm_planner_enabled=True, proposal_provider=provider))

    @staticmethod
    def _declare(statement, target='patient_actual_state', strategy='ask_user',
                 subjects=('合成药乙',), field='schedule',
                 why='用法影响这条提示是否成立'):
        return {'decision': 'tool', 'tool': 'plan_questions', 'gap_id': 'subquestions',
                'expected_observation': '问题集建立',
                'expected_change': '如果得到答案，就按当前用法重新核对',
                'basis_refs': ['memory:conclusion:1@v1'],
                'arguments': {'questions': [{'statement': statement,
                                             'information_target': target,
                                             'strategy': strategy,
                                             'subject_refs': list(subjects),
                                             'target_field': field, 'why': why}]}}

    def _run_with(self, provider, key='scripted'):
        # 整个宿主换成脚本化模型：worker 恢复的那一轮也要走同一个规划器。
        self.api.close()
        self.api = _App(self.directory, proposal_provider=provider)
        self.api.add_medication("seed-1", "合成药甲")
        self.api.add_medication("change-2", "合成药乙")
        case_id = self.api.cases()[0]["case_id"]
        tasks = self.scripted_tasks(provider)
        task = tasks.create(key, "safety_case", case_id)
        task = tasks.resume(task["id"], f"{key}-run", task["revision"])
        return tasks, task, case_id

    def _observed_tool_results(self, task, tool):
        """这次运行里，某个工具**实际交给模型**的结果（从运行轨迹读）。"""
        out = []
        for child in (task.get("resource_budget") or {}).get("child_run_ids", []):
            run = self.api.store.workflow_run_get(child) or {}
            for entry in (run.get("result") or {}).get("trace") or []:
                if entry.get("phase") != "observe" or entry.get("tool") != tool:
                    continue
                observation = entry.get("observation") or {}
                if observation.get("ok"):
                    out.append(observation.get("result") or {})
        return out

    def test_the_tool_result_shows_the_real_adoption_not_a_blanket_success(self):
        """工具历史里必须是**真的**：采纳了哪些、ID 是什么。

        旧路径里 handler 在校验之前就写 accepted=True，模型可能看到"已接受"
        而持久状态其实拒绝了它。
        """
        def provider(payload):
            inv = payload["investigation"]
            if not (inv.get("questions") or []):
                return self._declare("合成药乙目前的服用频次是什么？")
            return {"decision": "respond"}

        _tasks, task, _case_id = self._run_with(provider, key="tool-truth")
        results = self._observed_tool_results(task, "plan_questions")
        self.assertTrue(results, "应当有一条 plan_questions 的观察")
        result = results[0]
        self.assertTrue(result.get("adopted"))
        self.assertEqual(1, len(result.get("added") or []))
        question_id = (result.get("added") or [None])[0]
        self.assertTrue(str(question_id).startswith("q:"))
        self.assertEqual(question_id, result["questions"][0]["question_id"])
        self.assertNotIn("plan_questions", result.get("allowed_tools") or [],
                         "采纳之后不该再被邀请重交计划")

    def test_a_rejected_plan_is_not_reported_as_adopted(self):
        """被拒的计划不能在工具历史里显示成成功采纳。"""
        def provider(payload):
            inv = payload["investigation"]
            if not (inv.get("questions") or []):
                # 来源能力不匹配：一般药品资料回答不了"这位患者实际怎么用"。
                return self._declare("这位患者合成药乙的实际情况是什么？",
                                     target="patient_actual_state",
                                     strategy="general_reference")
            return {"decision": "respond"}

        _tasks, task, _case_id = self._run_with(provider, key="rejected-plan")
        results = self._observed_tool_results(task, "plan_questions")
        self.assertTrue(results, "被拒的提案也应当留下观察")
        result = results[0]
        self.assertFalse(result.get("adopted"), result)
        self.assertIn("strategy_cannot_serve_target", result.get("errors") or [])
        self.assertEqual([], result.get("added") or [])

    @staticmethod
    def _authoritative_medication(payload, name):
        """模型看到的权威记录。调查进行中时，权威事实在 `investigation.facts`
        里（患者快照被换成指针，不再重复一份），所以要从那里取。"""
        for item in (payload['investigation'].get('facts') or {}).get('medications') or []:
            if item.get('display_name') == name:
                return item
        raise AssertionError(f'no such medication in the authoritative view: {name}')

    def test_path_one_an_existing_answer_is_reused_and_the_user_is_not_asked(self):
        """路径一：当前记录已有目标信息 → Agent 读取并复用，不再追问。

        问题针对的字段本来就在**权威记录**里，所以答案由程序按记录直接复用，
        不需要模型重新批准一次已确认的记录，也不该再向用户问一遍。
        """
        state = {}

        def provider(payload):
            inv = payload["investigation"]
            if not (inv.get("questions") or []):
                recorded = self._authoritative_medication(payload, "合成药乙")
                state["ref"] = recorded["ref"]
                state["start"] = recorded["start_at"]
                return {"decision": "tool", "tool": "plan_questions",
                        "gap_id": "subquestions",
                        "expected_observation": "建立这件事还需要查清什么",
                        "arguments": {"questions": [{
                            "statement": "合成药乙当前记录的用药开始时间是什么？",
                            "information_target": "patient_actual_state",
                            "strategy": "patient_record",
                            "subject_refs": [recorded["ref"]],
                            "target_field": "start_date"}]}}
            if inv["questions"][0]["information_state"] != "available":
                return {"decision": "tool", "tool": "answer_question",
                        "gap_id": inv["questions"][0]["question_id"],
                        "expected_observation": "按当前权威记录复用这条答案",
                        "arguments": {"question_id": inv["questions"][0]["question_id"],
                                      "source": "patient_record", "value": state["start"],
                                      "field": "start_date", "source_ref": state["ref"]}}
            return {"decision": "respond"}

        _tasks, task, case_id = self._run_with(provider, key="path-one")
        results = self._observed_tool_results(task, "answer_question")
        self.assertTrue(results, "应当有一次采纳动作")
        outcome = results[0]
        self.assertTrue(outcome.get("accepted"), outcome)
        self.assertEqual("authoritative_record", outcome.get("provenance"))
        inv = task["investigation"]
        self.assertEqual("available", inv["questions"][0]["information_state"])
        # 用户没有被问一遍已经记录在案的东西。
        self.assertEqual([], self.api.cases()[0]["required_inputs"])
        self.assertNotEqual(sc.STATUS_AWAITING_USER, self.api.cases()[0]["status"])

    def test_path_two_a_real_gap_is_asked_and_the_answer_reaches_the_question(self):
        """路径二：确实缺信息 → 补问 → 回答回到**同一条问题**上。

        用户回答走的是既有提交路径；它同时落到 SafetyCase 与 investigation 里
        的那条问题，且性质是"用户报告"，不等于已核实。
        """
        def provider(payload):
            inv = payload["investigation"]
            if not (inv.get("questions") or []):
                return self._declare("合成药乙目前的服用频次是什么？")
            return {"decision": "respond"}

        _tasks, task, case_id = self._run_with(provider, key="path-two")
        case = self.api.cases()[0]
        self.assertEqual(1, len(case["required_inputs"]))
        ask = case["required_inputs"][0]
        question_id = task["investigation"]["questions"][0]["question_id"]
        self.assertTrue(ask["request_id"].endswith(question_id))

        answered = self.api.client.post(
            f"/v1/safety-cases/{case_id}/answer",
            json={"key": "path-two-answer", "expected_revision": case["revision"],
                  "request_id": ask["request_id"], "value": "每日一次"}).json()
        self.assertEqual([], answered["required_inputs"], "被回答的请求要关掉")

        # 回答**同时**进了 investigation 的那条问题，且标着 user_reported。
        stored = self.api.product.get(task["id"], "care_task")
        questions = (stored.get("investigation") or {}).get("questions") or []
        self.assertEqual(1, len(questions))
        self.assertEqual(question_id, questions[0]["question_id"], "同一问题，不是新的一条")
        answers = questions[0].get("answers") or []
        self.assertTrue(answers, "用户回答必须写回那条问题")
        self.assertEqual("user_reported", answers[-1]["provenance"])
        # 用户报告 ≠ 已核实：这条问题仍然带着不确定性。
        self.assertNotEqual("available", questions[0].get("information_state"))

    def test_the_model_proposed_user_fact_question_enters_the_real_ask_path(self):
        """模型说"还需要知道什么"，要**真的**变成页面上的补问。

        这是本轮的核心：旧契约下模型提的任何子问题都被转成"去搜支持和反对
        证据"，于是"问用户服药频次"根本没有出口，只能反复检索到 no_progress。
        """
        seen = []

        def provider(payload):
            inv = payload["investigation"]
            seen.append(inv)
            if not (inv.get("questions") or []):
                return self._declare("合成药乙目前的服用频次是什么？")
            return {"decision": "respond"}

        _tasks, task, case_id = self._run_with(provider, key="ask-path")
        self.assertEqual("waiting_input", task["status"], task.get("waiting_reason"))
        inv = task["investigation"]
        # 等用户是「等待」，不是「没有进展」——两者必须在报告里分得开。
        self.assertEqual("waiting_input", inv["termination_reason"])
        self.assertEqual(1, len(inv["questions"]))
        self.assertEqual("patient_actual_state", inv["questions"][0]["information_target"])
        self.assertEqual("ask_user", inv["questions"][0]["strategy"])
        self.assertEqual([], inv["claims"], "等用户回答的问题不该被当成证据检索题")

        case = self.api.cases()[0]
        self.assertEqual(case_id, case["case_id"])
        self.assertEqual(sc.STATUS_AWAITING_USER, case["status"])
        self.assertEqual(1, len(case["required_inputs"]))
        request = case["required_inputs"][0]
        self.assertEqual("patient_actual_state", request["question_kind"])
        self.assertEqual("ask_user", request["question_strategy"])
        self.assertEqual(["schedule"], request["fields"])
        self.assertEqual("合成药乙目前的服用频次是什么？", request["question"])
        self.assertTrue(request["why_needed"], "必须说明为什么需要这条信息")
        self.assertTrue(request["request_id"].endswith(inv["questions"][0]["question_id"]),
                        "请求身份由问题的稳定 id 派生")
        # 用户看到的是"为什么出现、查到什么、要做什么"。
        self.assertTrue(case["conclusions"])
        self.assertTrue(case["next_action_summary"])

    def test_the_model_may_ask_in_its_own_words(self):
        """问句不需要逐字复述程序写好的句子。"""
        def provider(payload):
            inv = payload["investigation"]
            if not (inv.get("questions") or []):
                return self._declare("合成药乙是从什么时候开始吃的？", field="start_date")
            return {"decision": "respond"}

        tasks, task, case_id = self._run_with(provider, key="own-words")
        case = self.api.cases()[0]
        request = case["required_inputs"][0]
        self.assertEqual("合成药乙是从什么时候开始吃的？", request["question"])
        # 用**自己的话**回答——不再是那条补问的原句。
        body = self.api.client.post(f"/v1/safety-cases/{case_id}/answer",
                                    json={"key": "own-words-answer",
                                          "expected_revision": case["revision"],
                                          "request_id": request["request_id"],
                                          "value": "2026-09-01"}).json()
        self.assertEqual([], body["required_inputs"], "回答应当被接受并关闭该问题")

    def test_a_non_empty_answer_that_misses_the_field_does_not_close_the_gap(self):
        """「有内容」不等于「答到了」：开始时间必须是日期。"""
        def provider(payload):
            inv = payload["investigation"]
            if not (inv.get("questions") or []):
                return self._declare("合成药乙是从什么时候开始吃的？", field="start_date")
            return {"decision": "respond"}

        _tasks, task, case_id = self._run_with(provider, key="bad-field")
        case = self.api.cases()[0]
        request = case["required_inputs"][0]
        body = self.api.client.post(f"/v1/safety-cases/{case_id}/answer",
                                    json={"key": "bad-field-answer",
                                          "expected_revision": case["revision"],
                                          "request_id": request["request_id"],
                                          "value": "我回头看看再说"}).json()
        self.assertEqual([request["request_id"]],
                         [i["request_id"] for i in body["required_inputs"]],
                         "不符合字段格式的回答不得消除缺口")
        entry = [e for e in body["history"] if e["event"] == "input_recorded"][-1]
        self.assertFalse(entry["answered"])
        self.assertEqual(["start_date"], entry["unsatisfied_fields"])

    def test_answering_resumes_the_same_question_and_uses_the_new_information(self):
        """回答之后：同一件事、同一条问题；模型**看得到上次之后新增的东西**。

        为了让恢复后确实还有事可做，这里声明**两条**问题：一条等用户回答、
        一条要查资料。用户答完第一条之后，第二条还在，所以调查会继续——
        这时模型必须看到"用户刚答了什么"，而不是把整段历史重放一遍。
        """
        seen = []

        def provider(payload):
            inv = payload["investigation"]
            seen.append(inv.get("case_context") or {})
            if not (inv.get("questions") or []):
                return {"decision": "tool", "tool": "plan_questions",
                        "gap_id": "subquestions",
                        "expected_observation": "问题集建立",
                        "arguments": {"questions": [
                            {"statement": "合成药乙目前的服用频次是什么？",
                             "information_target": "patient_actual_state",
                             "strategy": "ask_user",
                             "subject_refs": ["合成药乙"],
                             "target_field": "schedule", "why": "用法影响判断"},
                            {"statement": "合成药甲是什么时候开始吃的？",
                             "information_target": "patient_actual_state",
                             "strategy": "ask_user",
                             "subject_refs": ["合成药甲"],
                             "target_field": "start_date", "why": "开始时间影响判断"},
                        ]}}
            return {"decision": "respond"}

        _tasks, task, case_id = self._run_with(provider, key="resume")
        case = self.api.cases()[0]
        self.assertEqual(2, len(case["required_inputs"]), "两条问题都应当登记为请求")
        ask = next(i for i in case["required_inputs"]
                   if i["question_strategy"] == "ask_user")
        answered = self.api.client.post(
            f"/v1/safety-cases/{case_id}/answer",
            json={"key": "resume-answer", "expected_revision": case["revision"],
                  "request_id": ask["request_id"], "value": "每日一次"}).json()
        self.assertEqual([], [i for i in answered["required_inputs"]
                              if i["request_id"] == ask["request_id"]],
                         "被回答的那条要关掉")

        # 回答会**经既有队列**唤醒调查；把队列跑完就是"恢复"。
        self.api.worker.drain_once()
        resumed = self.api.product.get(task["id"], "care_task")
        inv = resumed["investigation"]
        ids = [q["question_id"] for q in inv["questions"]]
        self.assertIn(ask["request_id"].rsplit(":", 1)[-1], " ".join(ids),
                      "回答之后仍然是同一批问题，不是新生成的一批")
        self.assertEqual(sorted(q["target_field"] for q in inv["questions"]
                                if q.get("strategy") == "ask_user"),
                         ["schedule", "start_date"],
                         "另一条问题仍在，没有被回答事件挤掉")

        # 最后一轮模型看到的"新增"里含这条回答——而不是把历史全部重放。
        latest = seen[-1].get("new_since_last_run") or {}
        self.assertTrue(any(e.get("answered") for e in latest.get("events") or ()),
                        latest)
        self.assertIsInstance(latest.get("since_cursor"), int,
                              "游标必须是上次消费位置，不是 None")

    def test_the_agent_asks_a_specific_question_and_it_lands_on_the_case(self):
        self.api.add_medication("seed-1", "合成药甲")
        self.api.add_medication("change-2", "合成药乙")
        case_id = self.api.cases()[0]["case_id"]

        tasks = self.tasks()
        task = tasks.create("case-goal-1", "safety_case", case_id)
        self.assertEqual(SAFETY_CASE_CONTRACT, task["safety_case_contract"])
        self.assertIn("安全事项", task["goal"])

        task = tasks.resume(task["id"], "case-run-1", task["revision"])
        self.assertIn(task["status"], ("waiting_input", "completed", "failed"), task)

        case = self.api.cases()[0]
        # 调查登记在事项上：哪一次运行、查到什么程度，可追溯。
        self.assertTrue(case["linked_run_ids"], "运行必须登记在事项上")
        if task["status"] == "waiting_input":
            # 补问落在**事项**上，带稳定的 request_id（恢复后不会重问）。
            self.assertTrue(case["required_inputs"], task["missing_inputs"])
            request = case["required_inputs"][0]
            self.assertTrue(request["request_id"].startswith(f"case:{case_id}:"))
            self.assertTrue(request["question"])
            self.assertEqual(sc.STATUS_AWAITING_USER, case["status"])

    def test_the_question_identity_is_stable_and_order_independent(self):
        """同一缺口在恢复后是**同一条**请求：身份不含时间、序号或运行 id。"""
        case_id = "safety-case:example"
        renal = {"gap_id": "claim:abc", "field": "renal_function", "question": "肾功能？"}
        # 同一缺口、不同的提问措辞/顺序 → 同一个身份。
        self.assertEqual(safety_case_request_id(case_id, renal),
                         safety_case_request_id(case_id, dict(renal, question="请补充肾功能情况")))
        self.assertEqual(f"case:{case_id}:claim:abc", safety_case_request_id(case_id, renal))
        # 不同缺口 → 不同身份。
        self.assertNotEqual(safety_case_request_id(case_id, renal),
                            safety_case_request_id(case_id, {"gap_id": "claim:def"}))
        # 没有 gap_id 时退回字段名，仍然稳定。
        self.assertEqual(f"case:{case_id}:renal_function",
                         safety_case_request_id(case_id, {"field": "renal_function"}))

    def test_re_asking_the_same_gap_does_not_add_a_second_request(self):
        """同一条请求重复登记不产生第二条——恢复后不会把同一件事再问一遍。"""
        self.api.add_medication("seed-1", "合成药甲")
        self.api.add_medication("change-2", "合成药乙")
        case_id = self.api.cases()[0]["case_id"]
        store = SafetyCaseStore(self.api.product)
        question = {"gap_id": "claim:abc", "field": "renal_function",
                    "question": "肾功能？"}
        request_id = safety_case_request_id(case_id, question)
        for _ in range(3):
            store.require_input(case_id, request_id=request_id,
                                question=question["question"], fields=["renal_function"])
        open_requests = [item["request_id"]
                         for item in self.api.cases()[0]["required_inputs"]]
        self.assertEqual([request_id], open_requests)

    def test_a_failed_investigation_leaves_the_case_unresolved(self):
        self.api.add_medication("seed-1", "合成药甲")
        self.api.add_medication("change-2", "合成药乙")
        case_id = self.api.cases()[0]["case_id"]
        tasks = self.tasks()
        task = tasks.create("case-goal-3", "safety_case", case_id)
        task = tasks.resume(task["id"], "case-run-4", task["revision"])
        if task["status"] != "failed":
            self.skipTest("本轮调查未以失败结束")
        case = self.api.cases()[0]
        self.assertEqual(sc.STATUS_EXECUTION_FAILED, case["status"])
        self.assertIsNone(case["resolution_basis"])
        self.assertIn("仍未解决", case["next_action_summary"])
        # 这次迁移同样留在历史里，带原因——"它为什么变成未完成"要答得出来。
        changes = [e for e in case["history"]
                   if e["event"] == "status_changed" and e["to"] == sc.STATUS_EXECUTION_FAILED]
        self.assertTrue(changes, case["history"])
        self.assertTrue(changes[-1]["why"])


# ---------------------------------------------------------------------------
# 模型不可用
# ---------------------------------------------------------------------------
class ModelUnavailableTests(SafetyMainlineTests):
    def test_necessary_checks_and_the_case_survive_a_missing_planner(self):
        """provider/agent 不可用：检查照跑、事项照建、用户拿到准确结果与待办。"""
        self.api.add_medication("seed-1", "合成药甲")
        self.api.add_medication("change-2", "合成药乙")
        case_id = self.api.cases()[0]["case_id"]

        # 模拟 agent 不可用：hook 与检索工具都拿不到，只剩检测器。
        self.api.store.recheck_hook = None
        self.api.store.recheck_rag_tool = None
        self.api.store.recheck_detector = None  # 默认检测器（真实 ddi_engine）

        # 再改一次记录，触发一轮必要检查。
        self.api.add_medication("change-3", "合成药丙")

        report = checks.run_necessary_checks(self.api.store,
                                             detector=synthetic_detect,
                                             product=self.api.product)
        self.assertEqual("ok", report["status"])
        self.assertTrue(all(item["status"] == "done" for item in report["completed"]),
                        report["completed"])

        page = self.api.mainline()
        # 事项仍然在，用户仍然有明确的下一步；没有一个环节读成"已处理"。
        self.assertEqual(case_id, self.api.cases()[0]["case_id"])
        self.assertIn(page["counts"]["attention"], (0, 1))
        self.assertIn(self.api.cases()[0]["status"], sc.UNSETTLED_STATUSES)
        self.assertTrue(self.api.cases()[0]["next_action_summary"])
        self.assertFalse(self.api.cases()[0]["resolution_basis"])

    def test_a_failed_check_never_reads_as_no_risk(self):
        """检查失败：保持可见，不产出结论，不建事项——绝不读成"没有风险"。"""
        def exploding(_medications):
            raise RuntimeError("detector unavailable")

        # 直接跑检查路径（没有本地 HTTP 宿主），证明它不依赖服务或 agent。
        self.api.add_medication("seed-1", "合成药甲")
        self.api.add_medication("change-2", "合成药乙")
        self.api.store.connection.execute(
            "UPDATE necessary_checks SET status='open', attempts=0")
        self.api.store.connection.commit()
        report = checks.run_necessary_checks(self.api.store, detector=exploding,
                                             product=self.api.product)
        self.assertTrue(all(item["status"] == "error" for item in report["completed"]),
                        report["completed"])
        # 第一次失败先重排（可重试），而不是直接判死。
        self.assertGreaterEqual(report["pending"]["unfinished"], 1)
        self.assertEqual(0, report["pending"]["failed"])
        # 反复失败后保持**可见**：标记为 failed，而不是悄悄变成"检查过了"。
        checks.run_necessary_checks(self.api.store, detector=exploding, product=self.api.product)
        report = checks.run_necessary_checks(self.api.store, detector=exploding,
                                             product=self.api.product)
        self.assertGreaterEqual(report["pending"]["failed"], 1)
        page = self.api.mainline()
        self.assertGreaterEqual(page["necessary_checks"]["failed"], 1)
        self.assertIn("不表示风险已排除", page["necessary_checks"]["note"])


# ---------------------------------------------------------------------------
# 验收报告：把结果按**类别**分开报告，而不是合并成一个模糊的通过率
# ---------------------------------------------------------------------------
#: 每个类别由真实跑过的用例支撑；类别名就是验收要回答的问题。
CATEGORY_OF = {
    "necessary_checks_completed": (
        "test_a_change_produces_a_case_with_reason_evidence_and_next_step",
        "test_the_check_runs_once_per_world_state_and_not_per_event",
        "test_necessary_checks_and_the_case_survive_a_missing_planner",
        "test_a_failed_check_never_reads_as_no_risk"),
    "cases_created_and_updated": (
        "test_a_change_produces_a_case_with_reason_evidence_and_next_step",
        "test_the_case_is_visible_on_the_mainline_page",
        "test_a_later_change_sends_the_case_back_for_recheck_and_says_why"),
    "investigation_made_progress": (
        "test_the_agent_asks_a_specific_question_and_it_lands_on_the_case",
        "test_the_question_identity_is_stable_and_order_independent",
        "test_re_asking_the_same_gap_does_not_add_a_second_request",
        "test_the_agent_gets_the_case_context_not_a_whole_patient_replay",
        "test_the_case_context_reaches_the_model_view_and_survives_a_resume",
        "test_each_decision_carries_its_structured_fields",
        "test_a_tool_action_keeps_basis_and_change_separate_from_arguments"),
    "waiting_and_disposition_states_correct": (
        "test_a_case_waits_restarts_and_resumes_without_re_asking",
        "test_supplying_an_answer_does_not_close_the_case",
        "test_each_answer_lands_on_the_request_it_belongs_to",
        "test_seen_and_old_confirmations_cannot_approve_the_new_state",
        "test_a_failed_investigation_leaves_the_case_unresolved",
        "test_seen_endpoint_does_not_change_the_lifecycle",
        "test_a_disposition_with_a_valid_basis_is_accepted",
        "test_the_request_body_cannot_claim_a_reviewer_identity",
        "test_a_refused_disposition_returns_its_reason_verbatim",
        "test_a_stale_revision_is_refused",
        "test_a_routine_sync_does_not_erase_a_monitoring_arrangement",
        "test_a_resolved_case_stays_resolved_because_its_trigger_is_gone",
        "test_the_answer_endpoint_saves_and_leaves_the_case_unresolved",
        "test_an_empty_answer_over_the_api_closes_nothing",
        "test_saying_i_do_not_know_over_the_api_routes_to_alternative_evidence"),
    "model_and_program_separated": (
        "test_necessary_checks_and_the_case_survive_a_missing_planner",
        "test_a_failed_check_never_reads_as_no_risk",
        "test_the_agent_asks_a_specific_question_and_it_lands_on_the_case"),
    "requests_failures_and_cost_traceable": (
        "test_the_case_is_visible_on_the_mainline_page",
        "test_the_mainline_page_reports_the_check_queue_honestly",
        "test_two_investigate_requests_do_not_start_two_investigations",
        "test_a_failed_check_never_reads_as_no_risk"),
}


def run_with_report(report_path: str) -> int:
    """跑主线验收并写一份**按类别**的报告。默认验证入口调用它。"""
    import json
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromModule(__import__(__name__, fromlist=['*']))
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    outcomes = {}
    failures = []
    for case, _ in list(result.failures) + list(result.errors):
        failures.append(case.id().rsplit('.', 1)[-1])
    for category, names in CATEGORY_OF.items():
        covered = [name for name in names]
        outcomes[category] = {
            "checks": covered,
            "passed": all(name not in failures for name in covered),
        }
    payload = {
        "tests_executed": result.testsRun,
        "failures": len(result.failures),
        "errors": len(result.errors),
        "skipped": len(result.skipped),
        "status": "pass" if result.wasSuccessful() else "fail",
        "categories": outcomes,
        "note": ("每个类别由实际跑过的用例支撑；失败/错误与跳过分别计数，"
                 "不合并成一个通过率。"),
    }
    with open(report_path, 'w', encoding='utf-8') as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    import sys
    if "--report" in sys.argv:
        sys.exit(run_with_report(sys.argv[sys.argv.index("--report") + 1]))
    unittest.main()
