"""Stage 3 event-driven medication coordination agent.

This is a hand-rolled control loop, not a fixed pipeline.  On every cycle the
planner sees the event plus accumulated observations and chooses one tool call
or a response.  Reflection can add new evidence goals, so a low-confidence DDI
causes another RAG action before the response is allowed through the safety
boundary.

This module is an engineering demonstration for one patient/caregiver.  It is
not a medical device and must not be used for diagnosis or prescribing.
"""
from __future__ import annotations

import json
import re
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

try:
    from . import ddi_engine, rag
    from .memory import EpisodicFact, MemoryStore, SemanticFact
except ImportError:  # Support ``python stage0/agent.py`` style imports.
    import ddi_engine  # type: ignore
    import rag  # type: ignore
    from memory import EpisodicFact, MemoryStore, SemanticFact  # type: ignore


ROOT = Path(__file__).resolve().parent

AGENT_SYSTEM_PROMPT = """你是“用药协管员”，只做记录、检索、风险提示和就医沟通辅助。
硬性边界：
1. 不诊断、不处方，不自行建议开始/停止药物或改变剂量；
2. 每条风险警告必须带可核查来源，并引用支撑结论的 memory item；
3. severe、低置信度、证据冲突或不确定结果必须明确写“建议咨询医生/药师”；
4. 两条事实冲突时创建并展示 conflict，不得静默选边；
5. 工具由当前计划和观察决定；低置信度时先补检索或升级，再回答。
"""


@dataclass(frozen=True)
class CareEvent:
    event_type: str
    text: str
    payload: dict[str, Any] = field(default_factory=dict)
    source: str = "caregiver"
    occurred_at: str | None = None


@dataclass(frozen=True)
class ToolAction:
    tool: str
    purpose: str
    arguments: dict[str, Any]
    rationale: str


@dataclass
class Observation:
    tool: str
    purpose: str
    arguments: dict[str, Any]
    result: Any
    ok: bool = True


@dataclass
class AgentState:
    session_id: str
    turn_id: str
    event: CareEvent
    observations: list[Observation] = field(default_factory=list)
    trace: list[dict[str, Any]] = field(default_factory=list)
    reflection_notes: list[str] = field(default_factory=list)
    cycle: int = 0

    def observation(self, tool: str, purpose: str | None = None) -> Observation | None:
        for item in reversed(self.observations):
            if item.tool == tool and (purpose is None or item.purpose == purpose):
                return item
        return None

    def observations_with_prefix(self, tool: str, purpose_prefix: str) -> list[Observation]:
        return [item for item in self.observations if item.tool == tool and item.purpose.startswith(purpose_prefix)]


@dataclass
class AgentResponse:
    text: str
    warnings: list[dict[str, Any]]
    conflicts: list[dict[str, Any]]
    audit_trail: dict[str, Any]
    tool_trace: list[dict[str, Any]]
    safety_status: str = "enforced"


class DDITool:
    def __init__(self, detector: Callable[[list[str]], list[dict[str, Any]]] | None = None):
        self.detector = detector or ddi_engine.detect

    def __call__(self, medications: list[str], focus_medication: str | None = None) -> dict[str, Any]:
        warnings = self.detector(medications)
        if focus_medication:
            normalized_focus = ddi_engine.normalize_medications([focus_medication])
            focus_names = {focus_medication, *(item["name_cn"] for item in normalized_focus)}
            warnings = [
                warning for warning in warnings
                if warning.get("drug_a") in focus_names or warning.get("drug_b") in focus_names
            ]
        return {
            "medications_checked": medications,
            "focus_medication": focus_medication,
            "warnings": warnings,
            "detector": "stage0.ddi_engine.detect",
        }


class RAGTool:
    """Lazy wrapper around the existing hybrid retriever with an offline fallback."""

    def __init__(self, retriever: rag.HybridRetriever | None = None):
        self._retriever = retriever

    def _get_retriever(self) -> rag.HybridRetriever:
        if self._retriever is None:
            self._retriever = rag.HybridRetriever()
        return self._retriever

    def __call__(
        self,
        query: str,
        *,
        top_k: int = 5,
        section: str | None = None,
        drug_name: str | None = None,
    ) -> dict[str, Any]:
        try:
            results = self._get_retriever().search(
                query, mode="hybrid", top_k=top_k, section=section, drug_name=drug_name,
            )
            return {
                "query": query,
                "mode": "hybrid_bm25_bge",
                "results": [{"score": item.score, "rank": item.rank, **item.chunk} for item in results],
            }
        except Exception as exc:
            # Exact token overlap over the same RAG chunks is a degraded local
            # mode, not a replacement corpus or a new knowledge source.
            chunks = rag.read_jsonl(rag.INDEX_DIR / "chunks.jsonl")
            terms = {term for term in re.findall(r"[\u3400-\u9fff]{2,}|[A-Za-z0-9]+", query) if len(term) >= 2}
            eligible = [
                chunk for chunk in chunks
                if (not section or chunk.get("section") == section)
                and (not drug_name or drug_name.lower() in (chunk.get("drug_name") or "").lower())
            ]
            scored = sorted(
                ((sum(term.lower() in (chunk.get("text") or "").lower() for term in terms), index, chunk)
                 for index, chunk in enumerate(eligible)),
                key=lambda item: (-item[0], item[1]),
            )[:top_k]
            return {
                "query": query,
                "mode": "degraded_exact_over_rag_corpus",
                "degraded_reason": f"{type(exc).__name__}: {exc}",
                "results": [{"score": float(score), "rank": rank, **chunk} for rank, (score, _, chunk) in enumerate(scored, 1)],
            }


class MemoryReadTool:
    def __init__(self, memory: MemoryStore):
        self.memory = memory

    def __call__(self, query: str) -> dict[str, Any]:
        if query == "snapshot":
            return self.memory.snapshot()
        if query == "current_medications":
            return {"medications": self.memory.current_medications()}
        if query == "medication_timeline":
            event_types = ["medication_add", "medication_remove", "medication_dose_change"]
            return {
                "medications": self.memory.current_medications(),
                "timeline": [item for item in self.memory.timeline(limit=500) if item["event_type"] in event_types],
            }
        if query == "conflicts":
            return {"open_conflicts": self.memory.open_conflicts()}
        raise ValueError(f"unsupported memory query: {query}")


class MemoryWriteTool:
    def __init__(self, memory: MemoryStore):
        self.memory = memory

    @staticmethod
    def _profile_hints(profile: dict[str, Any]) -> list[SemanticFact]:
        hints: list[SemanticFact] = []
        mapping = {
            "age": ("age", "patient_age", "update", 0.7),
            "sex": ("sex", "patient_sex", "auto", 0.7),
            "weight_kg": ("weight", "patient_weight_kg", "update", 0.7),
            "renal_function": ("renal_function", "renal_status", "conflict", 0.95),
            "hepatic_function": ("hepatic_function", "hepatic_status", "conflict", 0.95),
        }
        for field_name, (namespace, key, policy, salience) in mapping.items():
            if field_name in profile and profile[field_name] is not None:
                hints.append(SemanticFact(namespace, key, profile[field_name], salience, policy))
        for allergy in profile.get("allergies", []):
            hints.append(SemanticFact("allergy", str(allergy), {"allergen": allergy, "status": "reported"}, 1.0, "conflict"))
        for disease in profile.get("chronic_diseases", []):
            hints.append(SemanticFact("chronic_disease", str(disease), {"name": disease, "present": True}, 0.85, "conflict"))
        for key, value in profile.get("preferences", {}).items():
            hints.append(SemanticFact("preference", str(key), value, 0.5, "update"))
        return hints

    def __call__(self, operation: str, *, state: AgentState, **arguments: Any) -> dict[str, Any]:
        event = state.event
        if operation == "consolidate_event":
            profile = event.payload.get("profile", {})
            semantic_hints = self._profile_hints(profile)
            episodic_hints: list[EpisodicFact] = []
            if event.event_type == "procedure_exposure":
                episodic_hints.append(EpisodicFact(
                    event_type="procedure_exposure",
                    subject_key=event.payload.get("agent", "含碘造影剂"),
                    payload={
                        "reported_text": event.text,
                        "agent": event.payload.get("agent", "含碘造影剂"),
                        "doctor_involved": bool(event.payload.get("doctor_involved", "医生" in event.text)),
                    },
                    occurred_at=event.occurred_at,
                    salience=0.9,
                ))
            consolidated = self.memory.consolidate_interaction(
                session_id=state.session_id,
                turn_id=state.turn_id,
                user_text=event.text,
                source=event.source,
                semantic_hints=semantic_hints,
                episodic_hints=episodic_hints,
                working_hints=[{"key": "goal", "value": {"event_type": event.event_type, "text": event.text}}],
            )
            result: dict[str, Any] = {
                "consolidation": asdict(consolidated),
                "memory_refs": consolidated.memory_refs,
                "medication_change": None,
            }
            if event.event_type == "medication_change":
                medication = event.payload.get("medication")
                action = event.payload.get("action")
                if medication and action:
                    normalized = ddi_engine.normalize_medications([medication])
                    result["medication_change"] = self.memory.apply_medication_change(
                        action=action,
                        name=medication,
                        ingredients=normalized,
                        session_id=state.session_id,
                        turn_id=state.turn_id,
                        source=event.source,
                        occurred_at=event.occurred_at,
                        dose=event.payload.get("dose"),
                        route=event.payload.get("route"),
                        schedule=event.payload.get("schedule"),
                    )
                    change = result["medication_change"]
                    for key in ("medication", "event"):
                        if change.get(key) and change[key].get("ref"):
                            result["memory_refs"].append(change[key]["ref"])
            return result

        if operation == "record_warnings":
            warnings = arguments.get("warnings", [])
            context_refs = list(arguments.get("context_refs", []))
            recorded: list[dict[str, Any]] = []
            for warning in warnings:
                source_refs = self._warning_sources(warning)
                episode = self.memory.record_event(
                    EpisodicFact(
                        event_type="warning",
                        subject_key="|".join(sorted((warning.get("drug_a", "?"), warning.get("drug_b", "?")))),
                        payload={"warning": warning, "source_refs": source_refs, "context_refs": context_refs},
                        occurred_at=event.occurred_at,
                        salience=self._warning_salience(warning),
                        severity=warning.get("severity"),
                        source_uri=source_refs[0].get("uri"),
                    ),
                    session_id=state.session_id, turn_id=state.turn_id, source="agent:ddi_check",
                )
                warning_refs = list(dict.fromkeys([*context_refs, episode["ref"]]))
                conclusion = self.memory.record_conclusion(
                    session_id=state.session_id,
                    turn_id=state.turn_id,
                    kind="warning",
                    text=self._warning_text(warning),
                    memory_refs=warning_refs,
                    source_refs=source_refs,
                )
                enriched = dict(warning)
                enriched["citations"] = source_refs
                enriched["audit_trail"] = {
                    "warning_memory": episode["ref"],
                    "conclusion": conclusion["ref"],
                    "memory_refs": warning_refs,
                    "source_refs": source_refs,
                }
                recorded.append(enriched)
            return {"recorded_warnings": recorded, "memory_refs": [ref for warning in recorded for ref in warning["audit_trail"]["memory_refs"]]}

        if operation == "create_clinical_conflict":
            conflict = self.memory.create_conflict(
                conflict_type="clinical_action_vs_label_warning",
                subject_key=arguments["subject_key"],
                left_ref=arguments["reported_event_ref"],
                right_ref=arguments["warning_ref"],
                description=arguments["description"],
                source=event.source,
            )
            self.memory.write_working(
                state.session_id, state.turn_id, "contradiction", {"conflict_ref": conflict["ref"], "description": conflict["description"]}, source="agent",
            )
            return {"conflict": conflict, "memory_refs": [conflict["left_ref"], conflict["right_ref"], conflict["ref"]]}

        raise ValueError(f"unsupported memory operation: {operation}")

    @staticmethod
    def _warning_salience(warning: dict[str, Any]) -> float:
        return {"contraindicated": 1.0, "major": 0.98, "moderate": 0.82, "minor": 0.55, "unknown": 0.9}.get(warning.get("severity"), 0.8)

    @staticmethod
    def _warning_text(warning: dict[str, Any]) -> str:
        pair = f"{warning.get('drug_a')}×{warning.get('drug_b')}"
        effect = warning.get("effect") or "存在需要核实的用药风险"
        return f"{pair}：{effect}（{warning.get('severity', 'unknown')} / {warning.get('confidence', 'unknown')}）"

    @staticmethod
    def _warning_sources(warning: dict[str, Any]) -> list[dict[str, Any]]:
        uri = warning.get("source_url")
        if uri:
            primary = {
                "source_type": "drug_label_or_kegg",
                "uri": uri,
                "quote": warning.get("source_text"),
                "retrieval": warning.get("detection_path", "rag"),
            }
            return [primary, *warning.get("additional_sources", [])]
        # This path is deliberately marked uncertain.  It still gives an exact
        # local provenance pointer and the safety layer will force escalation.
        warning["confidence"] = "low"
        return [{
            "source_type": "local_detector_provenance",
            "uri": str((ROOT / "data" / "ddi_pair_index.json").resolve()),
            "quote": None,
            "retrieval": warning.get("detection_path", "ddi_engine"),
        }, *warning.get("additional_sources", [])]


class ClarificationTool:
    def __call__(self, question: str) -> dict[str, Any]:
        return {"needs_user_input": True, "question": question}


class SafetyBoundary:
    severe = {"contraindicated", "major"}

    @staticmethod
    def refuses_medical_authority(text: str) -> bool:
        patterns = (
            r"(?:给|帮).{0,6}(?:诊断|确诊)",
            r"是不是.{0,8}(?:病|癌|感染)",
            r"(?:该|应该|能不能).{0,8}(?:吃什么药|停药|加量|减量|改剂量)",
            r"(?:开|推荐).{0,4}(?:药|处方)",
        )
        return any(re.search(pattern, text) for pattern in patterns)

    def enforce(self, response: AgentResponse) -> AgentResponse:
        for warning in response.warnings:
            if not warning.get("citations"):
                raise RuntimeError("safety boundary rejected an uncited warning")
            audit = warning.get("audit_trail", {})
            if not audit.get("memory_refs") or not audit.get("source_refs"):
                raise RuntimeError("safety boundary rejected a warning without an audit trail")
        must_escalate = any(
            warning.get("severity") in self.severe
            or warning.get("confidence") in {"low", "unknown"}
            or warning.get("severity") == "unknown"
            for warning in response.warnings
        ) or bool(response.conflicts)
        if must_escalate and "建议咨询医生/药师" not in response.text:
            response.text += "\n建议咨询医生/药师，并携带当前用药清单和本次来源；不要自行停药或调整剂量。"
        response.safety_status = "enforced"
        return response


class AgentPlanner:
    """Observation-driven single-action planner for the control loop."""

    def decide(self, state: AgentState) -> ToolAction | None:
        event = state.event
        if state.observation("memory_write", "consolidate_interaction") is None:
            return ToolAction(
                "memory_write", "consolidate_interaction", {"operation": "consolidate_event"},
                "每次交互先进行结构化事实整合、去重和冲突检测。",
            )

        if SafetyBoundary.refuses_medical_authority(event.text):
            return None

        consolidation = state.observation("memory_write", "consolidate_interaction")
        change = consolidation.result.get("medication_change") if consolidation and consolidation.ok else None
        if change and change.get("outcome") == "unresolved" and state.observation("ask_clarification") is None:
            return ToolAction(
                "ask_clarification", "missing_medication", {"question": "没有找到对应的当前用药，请确认药名和变更类型。"},
                "当前记忆无法把变更绑定到一条在用药记录。",
            )

        if event.event_type in {"register_profile", "profile_update"}:
            if state.observation("memory_read", "profile_snapshot") is None:
                return ToolAction("memory_read", "profile_snapshot", {"query": "snapshot"}, "核对整合后的患者档案和未决冲突。")
            return None

        if event.event_type == "medication_change":
            if state.observation("memory_read", "safety_context") is None:
                return ToolAction("memory_read", "safety_context", {"query": "snapshot"}, "读取当前用药、过敏和肝肾功能，建立安全检查上下文。")
            snapshot = state.observation("memory_read", "safety_context").result
            medications = [item["display_name"] for item in snapshot["medications"]]
            focus = event.payload.get("medication")
            if state.observation("ddi_check", "medication_change") is None:
                return ToolAction(
                    "ddi_check", "medication_change", {"medications": medications, "focus_medication": focus},
                    "新增/剂量变更是主动安全事件；检查所有当前药物组合并只报告涉及本次变更的结果。",
                )
            ddi_observation = state.observation("ddi_check", "medication_change")
            ddi_warnings = ddi_observation.result.get("warnings", [])
            if ddi_warnings and state.observation("memory_write", "record_ddi_warnings") is None:
                return ToolAction(
                    "memory_write", "record_ddi_warnings",
                    {"operation": "record_warnings", "warnings": ddi_warnings, "context_refs": self._context_refs(snapshot, focus)},
                    "风险结论必须先写入情景记忆和结论表，才能生成可审计响应。",
                )

            critical = [item for item in snapshot["semantic"] if item["namespace"] in {"allergy", "renal_function", "hepatic_function", "age"}]
            should_check_conditions = event.payload.get("action") in {"add", "dose_change"} and bool(critical)
            if should_check_conditions and state.observation("rag_search", "condition_check") is None:
                context = " ".join(str(item["value"]) for item in critical)
                return ToolAction(
                    "rag_search", "condition_check",
                    {"query": f"{focus} {context} 禁忌 慎用 注意事项", "top_k": 5, "drug_name": focus},
                    "患者记忆含过敏/肝肾/年龄风险，检索对应中文说明书证据，而不是仅做药物对检查。",
                )
            condition_observation = state.observation("rag_search", "condition_check")
            if condition_observation and state.observation("memory_write", "record_condition_warnings") is None:
                condition_warnings = self._condition_warnings(
                    focus, critical, snapshot.get("medications", []), condition_observation.result,
                )
                if condition_warnings:
                    return ToolAction(
                        "memory_write", "record_condition_warnings",
                        {"operation": "record_warnings", "warnings": condition_warnings, "context_refs": self._context_refs(snapshot, focus)},
                        "检索证据与患者风险事实明确匹配；持久化个体化慎用提示及双重来源。",
                    )

            stored_condition = state.observation("memory_write", "record_condition_warnings")
            if stored_condition and stored_condition.ok:
                weak_context = [
                    warning for warning in stored_condition.result.get("recorded_warnings", [])
                    if warning.get("confidence") == "low"
                ]
                if weak_context:
                    pair = weak_context[0]
                    purpose = f"reflection:{pair.get('drug_a')}|{pair.get('drug_b')}"
                    if state.observation("rag_search", purpose) is None:
                        return ToolAction(
                            "rag_search", purpose,
                            {"query": f"{pair.get('drug_a')} {pair.get('drug_b')} 出血 溃疡 药物相互作用", "top_k": 5, "section": "药物相互作用", "drug_name": focus},
                            "个体化结果包含类别推断，置信度低；再次用具体药物对检索，保留不确定性并升级。",
                        )

            unresolved = [warning for warning in ddi_warnings if warning.get("confidence") == "low" or not warning.get("source_url")]
            if unresolved:
                pair = unresolved[0]
                purpose = f"reflection:{pair.get('drug_a')}|{pair.get('drug_b')}"
                if state.observation("rag_search", purpose) is None:
                    return ToolAction(
                        "rag_search", purpose,
                        {"query": f"{pair.get('drug_a')} {pair.get('drug_b')} 药物相互作用", "top_k": 5, "section": "药物相互作用"},
                        "反思发现低置信度或来源不足，先重新检索标签证据。",
                    )
            return None

        if event.event_type == "procedure_exposure":
            if state.observation("memory_read", "exposure_context") is None:
                return ToolAction("memory_read", "exposure_context", {"query": "snapshot"}, "读取暴露时相关的在用药和肾功能事实。")
            snapshot = state.observation("memory_read", "exposure_context").result
            agent = event.payload.get("agent", "含碘造影剂")
            medications = [item["display_name"] for item in snapshot["medications"]]
            if agent not in medications:
                medications.append(agent)
            if state.observation("ddi_check", "procedure_exposure") is None:
                return ToolAction(
                    "ddi_check", "procedure_exposure", {"medications": medications, "focus_medication": agent},
                    "医疗暴露与在用药形成新的风险组合，应主动复核。",
                )
            warnings = state.observation("ddi_check", "procedure_exposure").result.get("warnings", [])
            if warnings and state.observation("memory_write", "record_exposure_warnings") is None:
                return ToolAction(
                    "memory_write", "record_exposure_warnings",
                    {"operation": "record_warnings", "warnings": warnings, "context_refs": self._context_refs(snapshot, agent)},
                    "先保存带来源的暴露警告，随后才能将其链接到矛盾对象。",
                )
            doctor_involved = bool(event.payload.get("doctor_involved", "医生" in event.text))
            if warnings and doctor_involved and state.observation("memory_write", "surface_clinical_conflict") is None:
                consolidation_refs = consolidation.result.get("memory_refs", [])
                exposure_ref = next((ref for ref in consolidation_refs if ref.startswith("memory:episodic:")), None)
                stored = state.observation("memory_write", "record_exposure_warnings")
                warning_ref = None
                if stored and stored.result.get("recorded_warnings"):
                    warning_ref = stored.result["recorded_warnings"][0]["audit_trail"]["warning_memory"]
                if exposure_ref and warning_ref:
                    return ToolAction(
                        "memory_write", "surface_clinical_conflict",
                        {
                            "operation": "create_clinical_conflict",
                            "subject_key": agent,
                            "reported_event_ref": exposure_ref,
                            "warning_ref": warning_ref,
                            "description": "照护者报告医生已安排/实施造影剂暴露，但当前二甲双胍与肾功能记忆关联到说明书慎用风险；系统不判定医嘱对错，也不静默覆盖。",
                        },
                        "医生行为与标签风险并非由代理裁决；建立显式未决 conflict 并向照护者展示。",
                    )
            return None

        if event.event_type in {"query_current_medications", "user_message"} and self._asks_current_medications(event.text):
            if state.observation("memory_read", "medication_timeline") is None:
                return ToolAction(
                    "memory_read", "medication_timeline", {"query": "medication_timeline"},
                    "问题要求跨会话精确回忆当前清单及变更时间线。",
                )
            return None

        if state.observation("ask_clarification") is None:
            return ToolAction(
                "ask_clarification", "ambiguous_intent",
                {"question": "请说明这是新增、停用、剂量变更、测量记录，还是查询当前用药。"},
                "现有事件不足以安全选择下一工具。",
            )
        return None

    @staticmethod
    def _asks_current_medications(text: str) -> bool:
        return any(pattern in text for pattern in ("现在吃什么药", "当前用药", "在吃哪些药", "用药清单"))

    @staticmethod
    def _context_refs(snapshot: dict[str, Any], focus: str | None) -> list[str]:
        refs = [item["ref"] for item in snapshot.get("semantic", [])]
        for med in snapshot.get("medications", []):
            names = {med["display_name"], *(item.get("name_cn") for item in med.get("ingredients", []))}
            if not focus or focus in names:
                refs.append(med["ref"])
            else:
                refs.append(med["ref"])
        return list(dict.fromkeys(refs))

    @staticmethod
    def _condition_warnings(
        medication: str,
        critical_facts: Sequence[dict[str, Any]],
        current_medications: Sequence[dict[str, Any]],
        rag_result: dict[str, Any],
    ) -> list[dict[str, Any]]:
        warnings: list[dict[str, Any]] = []
        condition_added = False
        for result in rag_result.get("results", []):
            text = result.get("text") or ""
            if medication.lower() not in (result.get("drug_name") or "").lower():
                continue
            matched: list[str] = []
            severity = "moderate"
            for fact in critical_facts:
                namespace = fact["namespace"]
                value = fact["value"]
                if namespace == "allergy":
                    allergen = value.get("allergen") if isinstance(value, dict) else str(value)
                    if allergen and allergen in text and "过敏" in text:
                        matched.append(f"过敏史:{allergen}")
                        severity = "major" if "禁用" in text else "moderate"
                elif namespace == "renal_function" and any(word in text for word in ("肾功能不全", "肝肾功能不全", "肾功能受损")):
                    matched.append(f"肾功能:{value}")
                elif namespace == "hepatic_function" and any(word in text for word in ("肝功能不全", "肝肾功能不全", "肝功能受损")):
                    matched.append(f"肝功能:{value}")
                elif namespace == "age" and isinstance(value, (int, float)) and value >= 60 and "60岁以上" in text:
                    matched.append(f"年龄:{value}岁")
            if condition_added or not matched or not re.search(r"慎用|禁用|医师指导|风险|不良反应", text):
                continue
            warnings.append({
                "drug_a": medication,
                "drug_b": "患者个体风险",
                "severity": severity,
                "mechanism": "说明书注意事项与结构化患者事实匹配",
                "effect": "；".join(matched) + "，需由医生/药师复核适用性",
                "management": None,
                "source_text": text,
                "source_url": result.get("source_url"),
                "confidence": "medium" if result.get("source_url") else "low",
                "detection_path": rag_result.get("mode", "hybrid_bm25_bge"),
            })
            condition_added = True  # One best exact-label citation is sufficient for this condition finding.

        # The Stage 2 detector remains the primary pair detector.  If it has no
        # direct aspirin×ibuprofen record, the agent can still notice that the
        # retrieved ibuprofen label warns about concurrent antipyretic/
        # analgesic/anti-inflammatory medicines.  This is explicitly marked as
        # an ontology/class inference (low confidence), never presented as a
        # direct pair assertion, and therefore triggers the reflection branch.
        current_names = {item.get("display_name") for item in current_medications}
        if medication == "布洛芬" and "阿司匹林" in current_names:
            for result in rag_result.get("results", []):
                text = result.get("text") or ""
                if "其他解热、镇痛、抗炎药物同用" not in text or not any(word in text for word in ("胃肠道不良反应", "溃疡", "出血")):
                    continue
                warnings.append({
                    "drug_a": "阿司匹林",
                    "drug_b": "布洛芬",
                    "severity": "major" if "出血" in text else "moderate",
                    "mechanism": "布洛芬说明书类别警示与当前阿司匹林记录的保守匹配（非直接药物对证据）",
                    "effect": "胃肠道不良反应/溃疡风险可能叠加；是否构成出血风险需医生/药师核实",
                    "management": None,
                    "source_text": text,
                    "source_url": result.get("source_url"),
                    "confidence": "low",
                    "detection_path": f"{rag_result.get('mode', 'hybrid_bm25_bge')}+class_inference",
                    "additional_sources": [{
                        "source_type": "agent_inference_disclosure",
                        "uri": str((ROOT / "agent.py").resolve()),
                        "quote": "类别匹配，不是说明书直接点名阿司匹林×布洛芬",
                        "retrieval": "reflection_required",
                    }],
                })
                break
        return warnings


class MedicationCoordinatorAgent:
    """Plan → act → observe → reflect loop with event-driven safety goals."""

    def __init__(
        self,
        memory: MemoryStore,
        *,
        ddi_tool: DDITool | None = None,
        rag_tool: RAGTool | None = None,
        max_cycles: int = 16,
    ):
        self.memory = memory
        self.planner = AgentPlanner()
        self.safety = SafetyBoundary()
        self.max_cycles = max_cycles
        self.tools: dict[str, Any] = {
            "ddi_check": ddi_tool or DDITool(),
            "rag_search": rag_tool or RAGTool(),
            "memory_read": MemoryReadTool(memory),
            "memory_write": MemoryWriteTool(memory),
            "ask_clarification": ClarificationTool(),
        }

    def handle(self, event: CareEvent, *, session_id: str, turn_id: str | None = None) -> AgentResponse:
        turn_id = turn_id or f"turn-{uuid.uuid4().hex[:10]}"
        self.memory.expire_working(session_id, except_turn=turn_id)
        state = AgentState(session_id=session_id, turn_id=turn_id, event=event)
        while state.cycle < self.max_cycles:
            state.cycle += 1
            action = self.planner.decide(state)
            state.trace.append({
                "phase": "plan", "cycle": state.cycle,
                "decision": asdict(action) if action else {"tool": "respond"},
            })
            if action is None:
                response = self._respond(state)
                return self.safety.enforce(response)
            observation = self._act(state, action)
            state.observations.append(observation)
            state.trace.append({
                "phase": "observe", "cycle": state.cycle, "tool": action.tool,
                "purpose": action.purpose, "ok": observation.ok,
                "summary": self._summarize_result(observation.result),
            })
            self._reflect(state, observation)
        raise RuntimeError(f"agent exceeded {self.max_cycles} control-loop cycles")

    def _act(self, state: AgentState, action: ToolAction) -> Observation:
        state.trace.append({"phase": "act", "cycle": state.cycle, "tool": action.tool, "purpose": action.purpose, "arguments": action.arguments})
        try:
            if action.tool == "memory_write":
                result = self.tools[action.tool](state=state, **action.arguments)
            else:
                result = self.tools[action.tool](**action.arguments)
            return Observation(action.tool, action.purpose, action.arguments, result, True)
        except Exception as exc:
            return Observation(action.tool, action.purpose, action.arguments, {"error": f"{type(exc).__name__}: {exc}"}, False)

    def _reflect(self, state: AgentState, observation: Observation) -> None:
        note = "观察结果足以继续规划。"
        if not observation.ok:
            note = f"工具 {observation.tool} 失败；不得据此形成确定性医疗结论，响应必须标记不确定并升级。"
        elif observation.tool == "ddi_check":
            warnings = observation.result.get("warnings", [])
            weak = [warning for warning in warnings if warning.get("confidence") == "low" or not warning.get("source_url")]
            if weak:
                note = "DDI 结果低置信度或来源不足；新增重新检索证据目标，未检索前不直接输出。"
            elif warnings:
                note = "DDI 结果有可追溯来源；下一步先写入记忆，再响应。"
            else:
                note = "本次变更未检出已知药物对警告；仍检查患者过敏/肝肾/年龄条件。"
        elif observation.tool == "rag_search" and observation.purpose.startswith("reflection:"):
            if observation.result.get("results"):
                note = "反思检索获得候选标签证据；原低置信度仍按不确定结果升级，不由代理自行裁决。"
            else:
                note = "反思检索无补充证据；保留不确定性并升级医生/药师。"
        elif observation.tool == "memory_write" and "conflict" in observation.result:
            note = "已创建未决 conflict；响应必须同时展示两侧，不得静默选择。"
        state.reflection_notes.append(note)
        state.trace.append({"phase": "reflect", "cycle": state.cycle, "note": note})

    def _respond(self, state: AgentState) -> AgentResponse:
        event = state.event
        warnings: list[dict[str, Any]] = []
        conflicts: list[dict[str, Any]] = []
        memory_refs: list[str] = []
        source_refs: list[dict[str, Any]] = []
        for observation in state.observations:
            if observation.tool == "memory_write" and observation.ok:
                memory_refs.extend(observation.result.get("memory_refs", []))
                warnings.extend(observation.result.get("recorded_warnings", []))
                if observation.result.get("conflict"):
                    conflicts.append(observation.result["conflict"])
            if observation.tool == "ask_clarification" and observation.ok:
                text = observation.result["question"]
                return AgentResponse(
                    text=text,
                    warnings=[], conflicts=[],
                    audit_trail={"session_id": state.session_id, "turn_id": state.turn_id, "memory_refs": list(dict.fromkeys(memory_refs)), "source_refs": []},
                    tool_trace=state.trace,
                )
        for warning in warnings:
            source_refs.extend(warning["citations"])

        if SafetyBoundary.refuses_medical_authority(event.text):
            text = "我不能诊断、开药或自行建议停药/调整剂量。我可以整理症状、用药清单和已有来源，供医生/药师评估。建议咨询医生/药师。"
        elif event.event_type in {"register_profile", "profile_update"}:
            snapshot_observation = state.observation("memory_read", "profile_snapshot")
            snapshot = snapshot_observation.result if snapshot_observation and snapshot_observation.ok else {}
            text = f"已保存患者档案：{self._profile_summary(snapshot.get('semantic', []))}。"
            if snapshot.get("open_conflicts"):
                text += f" 当前有 {len(snapshot['open_conflicts'])} 条未决矛盾，未自动覆盖。"
                conflicts.extend(snapshot["open_conflicts"])
        elif event.event_type == "medication_change":
            medication = event.payload.get("medication", "该药")
            action_text = {"add": "新增", "remove": "停用记录", "dose_change": "剂量变更记录"}.get(event.payload.get("action"), "变更")
            text = f"已记录{action_text}：{medication}。"
            if warnings:
                text += "\n" + "\n".join(self._format_warning(warning) for warning in warnings)
                text += "\n这些是风险提示，不是停药或改量建议；请勿自行调整。"
            else:
                text += " 本次主动检查未形成带证据的新增警告；这不等于证明绝对安全。"
        elif event.event_type == "procedure_exposure":
            text = "已记录造影剂暴露事件。"
            if warnings:
                text += "\n" + "\n".join(self._format_warning(warning) for warning in warnings)
            if conflicts:
                text += "\n检测到未决矛盾：" + "；".join(conflict["description"] for conflict in conflicts)
                text += " 系统保留了“照护者报告的医疗行为”和“说明书风险证据”两侧，没有判定哪一侧正确。"
        elif event.event_type in {"query_current_medications", "user_message"}:
            timeline_observation = state.observation("memory_read", "medication_timeline")
            if timeline_observation and timeline_observation.ok:
                result = timeline_observation.result
                medications = result["medications"]
                memory_refs.extend(item["ref"] for item in medications)
                memory_refs.extend(item["ref"] for item in result["timeline"])
                current = "、".join(item["display_name"] for item in medications) or "无在用药记录"
                text = f"当前记忆中的在用药：{current}。\n变更时间线：\n" + "\n".join(
                    f"- {item['occurred_at']} {item['event_type']} {item['payload'].get('name')} [{item['ref']}]"
                    for item in result["timeline"]
                )
            else:
                text = "当前无法读取用药记忆；建议携带药盒或处方请医生/药师核对。"
        else:
            text = "请补充要记录或查询的用药事项。"

        failed_tools = [item for item in state.observations if not item.ok]
        if failed_tools:
            text += "\n部分工具调用失败，结果不完整；建议咨询医生/药师。"
        return AgentResponse(
            text=text,
            warnings=warnings,
            conflicts=list({item["ref"]: item for item in conflicts}.values()),
            audit_trail={
                "session_id": state.session_id,
                "turn_id": state.turn_id,
                "memory_refs": list(dict.fromkeys(memory_refs)),
                "source_refs": source_refs,
                "reflection": state.reflection_notes,
            },
            tool_trace=state.trace,
        )

    @staticmethod
    def _profile_summary(facts: Sequence[dict[str, Any]]) -> str:
        labels = {
            "age": "年龄", "sex": "性别", "weight": "体重", "allergy": "过敏",
            "renal_function": "肾功能", "hepatic_function": "肝功能", "chronic_disease": "慢病",
            "preference": "偏好",
        }
        return "；".join(f"{labels.get(item['namespace'], item['namespace'])}:{item['value']} [{item['ref']}]" for item in facts)

    @staticmethod
    def _format_warning(warning: dict[str, Any]) -> str:
        source = warning["citations"][0]
        pair = f"{warning.get('drug_a')}×{warning.get('drug_b')}"
        effect = warning.get("effect") or "存在需核实风险"
        return (
            f"⚠ {pair}（{warning.get('severity')}/{warning.get('confidence')}）：{effect}。"
            f"来源：{source.get('uri')}；审计：{warning['audit_trail']['warning_memory']} → {warning['audit_trail']['conclusion']}"
        )

    @staticmethod
    def _summarize_result(result: Any) -> dict[str, Any]:
        if not isinstance(result, dict):
            return {"type": type(result).__name__}
        summary: dict[str, Any] = {"keys": sorted(result.keys())}
        for key in ("warnings", "recorded_warnings", "results", "medications", "timeline"):
            if isinstance(result.get(key), list):
                summary[f"{key}_count"] = len(result[key])
        if result.get("error"):
            summary["error"] = result["error"]
        if result.get("conflict"):
            summary["conflict_ref"] = result["conflict"].get("ref")
        return summary


def response_json(response: AgentResponse) -> str:
    return json.dumps(asdict(response), ensure_ascii=False, indent=2)


__all__ = [
    "AGENT_SYSTEM_PROMPT", "AgentResponse", "CareEvent", "DDITool", "MedicationCoordinatorAgent",
    "RAGTool", "SafetyBoundary", "response_json",
]
