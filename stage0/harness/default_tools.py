"""Default ToolSpec registry for the medication coordinator agent.

Harness P1-A.  The argument schemas used to live as a hand-written dict in
``agent.py`` next to the prompt; they are now the ToolSpec definitions below —
the planner prompt, the guard and the executor all read the same objects, so
prompt/schema/Python parameters cannot drift.  The lenient semantics of the
canonical proposal are unchanged (extra fields ignored, rationale optional,
no provider oneOf schemas).
"""
from __future__ import annotations

from typing import Any

try:
    from .tools import ToolExecutor, ToolSpec
    from .evidence import (EvidenceStore, capture_from_ddi_warnings,
                           capture_from_rag_result)
except ImportError:  # pragma: no cover - script-style import
    from tools import ToolExecutor, ToolSpec  # type: ignore
    from evidence import EvidenceStore, capture_from_ddi_warnings, capture_from_rag_result  # type: ignore

try:
    from .errors import ToolErrorKind, ToolExecutionError
except ImportError:  # pragma: no cover - script-style import
    from errors import ToolErrorKind, ToolExecutionError  # type: ignore


MEMORY_READ_QUERIES = (
    "snapshot", "current_medications", "medication_timeline", "conflicts",
    "context_packet", "pending_rechecks",
)
MEMORY_WRITE_OPERATIONS = ("consolidate_event", "record_warnings", "create_clinical_conflict")


# The model selects a logical write only.  Warning bodies, citations, memory
# references and conflict links are executor-only fields hydrated from real
# observations; the executor rejects any model-supplied substitute.
DEFAULT_TOOL_SPECS: dict[str, ToolSpec] = {
    "ddi_check": ToolSpec(
        name="ddi_check",
        description="Run the deterministic DDI detector over the authoritative medication list.",
        argument_schema={
            "type": "object",
            "properties": {
                "medications": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                "focus_medication": {"type": "string"},
            },
            "required": ["medications"],
        },
        result_shape="dict(medications_checked, focus_medication, warnings[], detector)",
        kind="read",
        required_permission="ddi:detect",
        timeout_owner="executor",
        retry_owner="none",
        idempotency="pure",
        cacheable=True,
    ),
    "rag_search": ToolSpec(
        name="rag_search",
        description=("在本地药品说明书语料中检索候选证据（混合检索 + 确定性精确兜底）。"
                     "它推进的是 `evidence_missing` 类的开放缺口——"
                     "「核查某药的支持和反对证据」就是靠它开始。返回的是**候选**片段与"
                     "evidence_id，不是已核验的支持：摘要不等于原文，引用前必须用 "
                     "read_evidence 按 evidence_id 回读，代码才会校验来源、否定与适用条件。"),
        argument_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "top_k": {"type": "integer"},
                "section": {"type": "string", "description": "可选精确过滤，合法章节来自 rag_catalog；省略时搜索所有授权章节，其他过滤仍生效。"},
                "drug_name": {"type": "string"},
            },
            "required": ["query"],
        },
        result_shape="dict(status: invalid_filter|empty_filter_scope|no_match|retrieval_error|found, search_executed, applied_filters, directory, scope, corpus_version, results[], truncation)",
        kind="read",
        required_permission="rag:search",
        idempotency="pure",
        cacheable=False,
    ),
    "memory_read": ToolSpec(
        name="memory_read",
        description="Read one bounded view of patient memory (snapshot/medications/timeline/conflicts).",
        argument_schema={
            "type": "object",
            "properties": {"query": {"type": "string", "enum": list(MEMORY_READ_QUERIES)}},
            "required": ["query"],
        },
        result_shape="dict depending on query",
        kind="read",
        required_permission="memory:read",
        idempotency="pure",
        cacheable=True,
    ),
    "memory_write": ToolSpec(
        name="memory_write",
        description=("Persist a logical write (consolidate_event / record_warnings / "
                     "create_clinical_conflict).  Authoritative bodies and refs are injected "
                     "from real observations, never from the proposal."),
        # ``argument_schema`` describes the EXECUTOR interface (what a
        # materialized action may carry, including hydrated executor-only
        # fields); ``proposal_schema`` stays the minimal logical choice shown
        # to the model — it can only pick the operation.
        argument_schema={
            "type": "object",
            "properties": {
                "operation": {"type": "string", "enum": list(MEMORY_WRITE_OPERATIONS)},
                "warnings": {"type": "array"},
                "context_refs": {"type": "array"},
                "subject_key": {"type": "string"},
                "reported_event_ref": {"type": "string"},
                "warning_ref": {"type": "string"},
                "description": {"type": "string"},
            },
            "required": ["operation"],
        },
        result_shape="dict with operation outcome, memory_refs, receipt flags",
        kind="write",
        required_permission="memory:write",
        idempotency="receipt_keyed",
        proposal_schema={
            "type": "object",
            "properties": {"operation": {"type": "string", "enum": list(MEMORY_WRITE_OPERATIONS)}},
            "required": ["operation"],
        },
    ),
    "ask_clarification": ToolSpec(
        name="ask_clarification",
        description="Ask the caregiver a clarifying question; cannot diagnose or prescribe.",
        argument_schema={
            "type": "object",
            "properties": {"question": {"type": "string"}},
            "required": ["question"],
        },
        result_shape="dict(needs_user_input, question)",
        kind="read",
        required_permission="clarify",
        idempotency="pure",
    ),
}


# Harness P1-B: the only way back to full evidence content.  The model supplies
# an evidence id it received from a capture view — never a path or URL — and
# the executor/store enforce scope, existence, hash and pagination.
READ_EVIDENCE_SPEC = ToolSpec(
    name="read_evidence",
    description=("Read back a bounded slice of previously captured evidence by id. "
                 "Only ids obtained from tool results in this run are valid; "
                 "paths/URLs are rejected."),
    argument_schema={
        "type": "object",
        "properties": {
            "evidence_id": {"type": "string"},
            "offset": {"type": "integer"},
            "limit": {"type": "integer"},
        },
        "required": ["evidence_id"],
    },
    result_shape="dict(evidence_id, content, offset, returned_chars, total_chars, truncated, source_uri, content_hash)",
    kind="read",
    required_permission="evidence:read",
    idempotency="pure",
    cacheable=True,
)


# Protocol v2: declaring an investigation's sub-questions.  This is the only
# route by which claims come into existence on the model path, so it is
# registered for every agent but advertised ONLY while an investigation's
# 'subquestions' gap is open (see agent.INVESTIGATION_ONLY_TOOLS and
# investigation.allowed_tools).  It reads nothing and writes nothing: the state
# machine adopts the questions from the observation, the executor only echoes.
ANSWER_QUESTION_SPEC = ToolSpec(
    name="answer_question",
    description=("把**已经取得的来源**落成某条问题的答案——采纳的唯一入口。"
                 "先说明在回答哪条问题（question_id），再给出候选答案与它的**引用**："
                 "source=patient_record（当前权威记录里的字段值，source_ref 写那条版本化记录）"
                 "/ evidence（本 run 回读过的原文，source_ref 写那条证据，并给出 quote 原文片段）"
                 "/ material（本 run 回读过的材料条目，source_ref 写条目 ref）。"
                 "**来源种类由服务端的真实记录解析，不由这里声明决定**：用户回答与专业意见"
                 "不是模型能声明的种类，写了会被拒绝。"
                 "服务端按来源、对象、内容版本与实际读过的片段核对**来源是否真的支持这个答案**："
                 "引用存在不等于支持答案，引文属实也不等于它陈述了这个答案，引用 A 不能用 B 的原文。"
                 "问题涉及多个对象时用 object_ref 指明这次回答的是哪一个——只答一个对象不算答完。"
                 "不要用它把问题直接设成已回答：只有核对通过的答案才算有依据，"
                 "其余如实记成候选或无依据并继续未决。"),
    argument_schema={
        "type": "object",
        "properties": {
            "question_id": {"type": "string"},
            "source": {"type": "string", "enum": [
                "patient_record", "evidence", "material"]},
            "value": {"type": "string"},
            "field": {"type": "string"},
            "quote": {"type": "string"},
            "source_ref": {"type": "string"},
            "object_ref": {"type": "string",
                           "description": "这条问题涉及的多个对象中，本次回答的是哪一个"},
            "basis_refs": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["question_id", "source", "value", "source_ref"],
    },
    result_shape=("dict(accepted, partial, answered[], still_open[], provenance, "
                  "assessment{status, reason, source_ref, locator, dependency_refs}, detail, "
                  "question[{question_id, information_state, blocking}])"),
    kind="read",
    required_permission="answer:submit",
    idempotency="pure",
)


PLAN_QUESTIONS_SPEC = ToolSpec(
    name="plan_questions",
    description=("声明本轮**还需要查清什么**——提出问题集的唯一入口。"
                 "每条问题用你自己的话写 statement，并声明两件**不同**的事："
                 "information_target（要弄清的是哪一类信息：patient_actual_state 这位患者实际是什么情况 / "
                 "material_record 材料里记的是什么 / general_reference 一般参考知识 / "
                 "professional_judgment 需要专业判断）与 strategy（这一次从哪里取："
                 "patient_record 读已有患者记录 / ask_user 问用户 / patient_material 读患者材料 / "
                 "general_reference 查一般药品资料 / professional_review 请专业复核）。"
                 "注意：一般药品资料说得了「这类药一般怎么用」，说不了「这位用户实际怎么吃」；"
                 "来源与信息类型不匹配会被拒绝并说明原因。"
                 "subject_refs 取本事项相关的药名，或**真实存在**的记忆/证据引用；"
                 "target_field 写这条问题要确定的具体字段；why 写它为什么影响当前事项。"
                 "只有 general_reference 类目标会被当作需要证据支持/反驳的问题去核查。"
                 "修订时允许改写措辞或**更换取证来源**（同一个问题不换身份）；"
                 "不得删除仍未解决的问题。"),
    argument_schema={
        "type": "object",
        "properties": {"questions": {
            "type": "array", "minItems": 1, "maxItems": 12,
            "items": {"type": "object",
                      "properties": {"statement": {"type": "string"},
                                     "information_target": {"type": "string", "enum": [
                                         "patient_actual_state", "material_record",
                                         "general_reference", "professional_judgment"]},
                                     "strategy": {"type": "string", "enum": [
                                         "patient_record", "ask_user", "patient_material",
                                         "general_reference", "professional_review"]},
                                     "subject_refs": {"type": "array", "items": {"type": "string"}},
                                     "target_field": {"type": "string"},
                                     "why": {"type": "string"},
                                     "basis_refs": {"type": "array", "items": {"type": "string"}},
                                     "entities": {"type": "array", "items": {"type": "string"}}},
                      "required": ["statement"]}}},
        "required": ["questions"],
    },
    result_shape=("dict(adopted, added[], reused[], restated[], questions[{question_id, "
                  "information_target, strategy, information_state, blocking}], allowed_tools)"),
    kind="read",
    required_permission="plan:questions",
    idempotency="pure",
)


# Protocol v2: the material index and single-item read-back.  Registered ONLY
# when a MaterialIndex is attached, so a run without materials keeps exactly
# the catalog it had — no unavailable tool is ever advertised.  Both go through
# the ordinary executor path (permission, schema, budget, audit).
LIST_MATERIALS_SPEC = ToolSpec(
    name="list_materials",
    description=("列出本轮已上传材料及其确定性差异：每条目的字段、原文定位、与当前权威记录的差异"
                 "（kind: same/changed/new/possible_duplicate/not_listed/unresolved）和未决问题。"
                 "条目是 caregiver 尚未确认的候选，不是患者事实。"),
    argument_schema={"type": "object", "properties": {}, "required": []},
    result_shape="dict(materials[{case_id, document_id, item_count, pending_count, items[]}], revision)",
    kind="read", required_permission="materials:read", idempotency="pure", cacheable=True,
)

READ_MATERIAL_ITEM_SPEC = ToolSpec(
    name="read_material_item",
    description=("按 case_id/item_id 读取单条材料的原文与定位，含 caregiver 已做的更正历史。"
                 "只有读过原文的条目才能作为报告引用。"),
    argument_schema={"type": "object", "properties": {"case_id": {"type": "string"},
                                                      "item_id": {"type": "string"}},
                     "required": ["case_id", "item_id"]},
    result_shape=("dict(case_id, item_id, fields, original_fields, corrections, locations, "
                  "kind, issues)"),
    kind="read", required_permission="materials:read", idempotency="pure", cacheable=True,
)


def register_material_tools(executor: ToolExecutor, index: Any) -> None:
    """Idempotent: attaching a newer index replaces the handlers in place
    rather than raising, so a re-attach can never half-update the catalog."""
    executor.register(LIST_MATERIALS_SPEC, lambda request: index.index(), override=True)
    executor.register(READ_MATERIAL_ITEM_SPEC,
                      lambda request: index.item(request.arguments["case_id"],
                                                 request.arguments["item_id"]),
                      override=True)


def build_default_executor(agent: Any, *, hooks: Any = None,
                           audit_sink: Any = None,
                           evidence_store: Any = None,
                           reuse: Any = None,
                           corpus_version_fn: Any = None) -> ToolExecutor:
    """Bind the five built-in tools of the coordinator agent to the executor.

    Handlers adapt the existing tool callables; code-derived enrichment (the
    patient-condition warning attachment for RAG results) moves here so it is
    part of the single shared execution path for both runners.  When an
    ``EvidenceStore`` is supplied (P1-B), retrieval results are captured as
    immutable evidence and the authorised ``read_evidence`` tool is
    registered — no runner or prompt code changes.  A ``ReuseCoordinator``
    (P2) enables controlled same-run/cross-run read reuse behind its flags.
    """
    executor = ToolExecutor(
        hooks=hooks,
        error_types={
            "MemoryPolicyError": ToolErrorKind.POLICY_VIOLATION,
            "OperationConflict": ToolErrorKind.POLICY_VIOLATION,
            "ReviewFactsMovedError": ToolErrorKind.POLICY_VIOLATION,
            "ReviewStateError": ToolErrorKind.POLICY_VIOLATION,
        },
        audit_sink=audit_sink,
        memory=getattr(agent, "memory", None),
        reuse=reuse,
        corpus_version_fn=corpus_version_fn,
    )

    def _capture_evidence(request, result, *, kind):
        if evidence_store is None or not isinstance(result, dict):
            return result
        if kind == 'rag' and result.get('status') and result['status'] != 'found':
            return result
        ctx = request.ctx
        revision = ctx.patient_revision
        if kind == "rag":
            if getattr(request.state, 'investigation', None) is not None and not result.get('corpus_version') and corpus_version_fn:
                result = dict(result, corpus_version=corpus_version_fn())
            view = capture_from_rag_result(evidence_store, result, run_id=ctx.run_id,
                                           query=str(request.arguments.get("query", "")),
                                           patient_revision=revision, scope_id=ctx.principal.scope_id)
        else:
            view = capture_from_ddi_warnings(evidence_store, result, run_id=ctx.run_id,
                                             patient_revision=revision)
        if view:
            result = dict(result)
            result["evidence"] = view
            result["evidence_refs"] = [item["evidence_id"] for item in view]
            _attach_warning_evidence_ids(result, view, kind)
        return result

    def _attach_warning_evidence_ids(result, view, kind):
        """Product P1: link each persisted warning to its captured evidence id.

        capture_from_* iterate warnings/results in order and skip the same
        entries (missing source_text / empty text), so zipping the keyed
        entries with the view reconstructs the exact warning → evidence map.
        The ids travel on the warning dicts into record_warnings → conclusion
        source_refs, making 原文 read-back addressable per warning.
        """
        if kind == "ddi":
            keyed = [w for w in result.get("warnings") or []
                     if isinstance(w, dict) and w.get("source_text")]
            for warning, item in zip(keyed, view):
                warning["evidence_id"] = item["evidence_id"]
            return
        chunks = [r for r in result.get("results") or []
                  if isinstance(r, dict) and r.get("text")]
        by_text: dict[str, str] = {}
        for chunk, item in zip(chunks, view):
            by_text.setdefault(str(chunk["text"]), str(item["evidence_id"]))
        for warning in result.get("warnings") or []:
            if isinstance(warning, dict) and warning.get("source_text") in by_text:
                warning["evidence_id"] = by_text[str(warning["source_text"])]

    for name, spec in DEFAULT_TOOL_SPECS.items():
        tool_callable = agent.tools[name]

        def handler(request, _callable=tool_callable, _name=name):
            if _name == "memory_write":
                _verify_hydrated_write(request)
                return _callable(state=request.state, **request.arguments)
            if _name == "rag_search":
                from .retrieval import search
                return _capture_evidence(request, agent._attach_condition_warnings(
                    request.state, search(_callable, request.arguments,
                        scope_id=request.ctx.principal.scope_id)), kind="rag")
            if _name == "ddi_check":
                return _capture_evidence(request, _callable(**request.arguments), kind="ddi")
            return _callable(**request.arguments)

        executor.register(spec, handler)

    from .retrieval import CATALOG_SPEC, catalog
    executor.register(CATALOG_SPEC, lambda request: catalog(agent.tools['rag_search'],
        request.ctx.principal.scope_id, request.arguments))

    def _plan_questions_handler(request):
        # **不在这里宣告成败**。采纳由 InvestigationState 在观察阶段完成
        # （executor 不改调查状态），所以这里返回的只是"我看到了什么"；真正的
        # 结果——采纳了哪些、question_id 是什么、哪些被拒——由观察阶段回填到
        # 这条 observation 上。上一版在这里就写 accepted=True，模型于是可能看到
        # 一份"已接受"而持久状态其实拒绝了它。
        return {"submitted": request.arguments.get("questions"),
                "status": "pending_adoption",
                "note": "采纳结果由调查状态在这一步给出，见同一条观察的 adopted 字段。"}

    executor.register(PLAN_QUESTIONS_SPEC, _plan_questions_handler)

    def _answer_question_handler(request):
        # 同 plan_questions：executor 不改调查状态，只回显"我看到了什么"；
        # 采纳结果由观察阶段回填到这条 observation 上。
        return {"submitted": request.arguments, "status": "pending_adoption",
                "note": "采纳结果由调查状态在这一步给出，见同一条观察的 accepted 字段。"}

    executor.register(ANSWER_QUESTION_SPEC, _answer_question_handler)

    if evidence_store is not None:
        executor.register(READ_EVIDENCE_SPEC, lambda request: evidence_store.read(
            request.arguments["evidence_id"],
            scope_id=request.ctx.principal.scope_id,
            offset=request.arguments.get("offset", 0),
            limit=request.arguments.get("limit", 2000)))
        from .evidence_acquire import enabled, register
        if enabled():
            register(executor, evidence_store)

    return executor


def _verify_hydrated_write(request: Any) -> None:
    """Defense in depth (P1-A contract 6): even a call that bypassed the
    guard's hydration cannot persist model-authored warning bodies or
    conflict links.  The check compares against the SAME single hydration
    implementation the guard uses — never a second, diverging rule.

    raises ToolExecutionError(POLICY_VIOLATION) on a mismatch."""
    arguments = request.arguments
    operation = arguments.get("operation")
    if operation not in {"record_warnings", "create_clinical_conflict"}:
        return
    state = request.state
    try:  # local import: agent layer owns the hydration rule
        from ..agent import PlannerPolicyGuard
    except ImportError:  # pragma: no cover - script-style import
        from agent import PlannerPolicyGuard  # type: ignore
    guard = PlannerPolicyGuard()
    if operation == "record_warnings":
        source = guard._warning_source(state)
        authoritative = guard._warnings_of(source)
        if list(arguments.get("warnings") or []) != list(authoritative):
            raise ToolExecutionError(
                ToolErrorKind.POLICY_VIOLATION,
                "warning bodies must be hydrated from real observations, not proposed")
        return
    # Conflict links: recompute through the guard's own derivation.
    if not guard._clinical_conflict_ready(state):
        raise ToolExecutionError(ToolErrorKind.POLICY_VIOLATION,
                                 "clinical conflict is not grounded in this turn's observations")
    authoritative = guard._clinical_conflict_arguments(state)
    for key in ("subject_key", "reported_event_ref", "warning_ref"):
        if arguments.get(key) != authoritative.get(key):
            raise ToolExecutionError(
                ToolErrorKind.POLICY_VIOLATION,
                f"conflict field {key} must come from the observed episodic record, not the proposal")
