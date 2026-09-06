"""模拟审阅员工作台（Reliability P2 演示页面）。

**显著标记：本地模拟，非真实医护。** 未接入真实人工服务——本页面仅演示
工单领取、结构化决策与幂等回调的工程闭环；真实审阅需要真实身份与授权配置。

Run::

    streamlit run stage0/review_app.py
    # with the service layer:
    STAGE0_API_URL=http://127.0.0.1:8000 streamlit run stage0/review_app.py

Reviewer actions are bounded by design: five structured decisions only —
no graph goto, no SQL, no tool names, no arbitrary delivered text.
"""
from __future__ import annotations

import json
import os

import streamlit as st

try:
    from .api_client import ApiClientError, Stage0ApiClient
except ImportError:  # Support ``streamlit run stage0/review_app.py``.
    from api_client import ApiClientError, Stage0ApiClient  # type: ignore

st.set_page_config(page_title="专业审核工作台（模拟）", page_icon="🧑‍⚕️", layout="wide")

st.warning("🧪 **模拟审阅员（本地演示）**：未接入真实人工服务。本页面用于工程演示，"
           "不代表医生/药师已接单；请勿据此页面做出任何真实用药判断。", icon="⚠️")

API_BASE_URL = os.getenv("STAGE0_API_URL", "").strip()
API_CLIENT = Stage0ApiClient(API_BASE_URL) if API_BASE_URL else None

if "review_notice" not in st.session_state:
    st.session_state.review_notice = None

st.title("专业审核工作台（模拟）")


def _direct_store():
    """Direct in-process fallback (no service layer) for local demos."""
    try:
        from .memory import DEFAULT_DB, MemoryStore
    except ImportError:
        from memory import DEFAULT_DB, MemoryStore  # type: ignore
    if st.session_state.get("_direct_store") is None:
        st.session_state["_direct_store"] = MemoryStore(DEFAULT_DB)
    return st.session_state["_direct_store"]


def _store():
    return None if API_CLIENT is not None else _direct_store()


def _cases() -> list[dict]:
    if API_CLIENT is not None:
        return API_CLIENT.review_cases()
    store = _direct_store()
    # local-demo principals hold the reviewer role; the store call is the
    # same data the API would return.
    return store.review_cases()


def _summary(case_id: int) -> dict:
    if API_CLIENT is not None:
        return API_CLIENT.review_case_summary(case_id)
    store = _direct_store()
    case = store.review_case(case_id)
    decisions = store.review_decisions_for(case_id)
    return {"case_id": case_id, "status": case["status"], "round": case["round"],
            "reason_codes": case["reason_codes"], "summary": case["summary"],
            "decisions": [{"action": d["action"], "actor_id": d["actor_id"],
                           "outcome": d["outcome"]} for d in decisions]}


def _claim(case_id: int, revision: int) -> dict:
    if API_CLIENT is not None:
        return API_CLIENT.claim_review_case(case_id, expected_revision=revision)
    return _direct_store().claim_review_case(case_id, expected_revision=revision,
                                             assignee="local-demo-caregiver")


def _decide(case_id: int, action: str, revision: int, payload: dict) -> dict:
    if API_CLIENT is not None:
        return API_CLIENT.submit_review_decision(case_id, action=action,
                                                 expected_revision=revision,
                                                 payload=payload)
    import uuid
    return _direct_store().record_review_decision(
        case_id=case_id, expected_revision=revision, action=action,
        payload=payload, idempotency_key=f"rv-{uuid.uuid4().hex}",
        actor_id="local-demo-caregiver")


status_filter = st.selectbox("状态筛选", ["全部", "open", "assigned", "in_review",
                                         "waiting_user", "overdue"])
try:
    cases = _cases()
    if status_filter != "全部":
        cases = [c for c in cases if c["status"] == status_filter]
except Exception as exc:
    cases = []
    st.error(f"读取工单失败：{type(exc).__name__}")

st.caption(f"共 {len(cases)} 张工单（逾期为运营状态，超时绝不自动通过）")

for case in cases:
    with st.expander(f"工单 #{case['id']} · {case['status']} · "
                     f"第 {case['round']} 轮 · {','.join(case['reason_codes'])}"):
        summary = _summary(case["id"])
        body = summary.get("summary") or {}
        st.markdown(f"**触发原因**：{', '.join(summary.get('reason_codes') or [])}")
        st.markdown(f"**创建时间**：{summary.get('created_at')} · **SLA 截止**：{case.get('due_at')}")
        if body.get("event_text"):
            st.markdown(f"**用户报告**：{body['event_text']}")
        if body.get("response_text"):
            st.text_area("系统回复（已过安全检查）", body["response_text"],
                         height=150, key=f"resp-{case['id']}", disabled=True)
        for warning in body.get("warnings", []) or []:
            st.markdown(f"- ⚠️ {warning.get('drug_a')}×{warning.get('drug_b')}"
                        f"（{warning.get('severity')}）：{warning.get('effect')}")
        for conflict in body.get("conflicts", []) or []:
            st.markdown(f"- ⚔️ 未决矛盾 {conflict.get('ref')}")

        left, right = st.columns([1, 2])
        with left:
            if st.button("接单（CAS）", key=f"claim-{case['id']}",
                         disabled=case["status"] not in {"open", "overdue"}):
                try:
                    claimed = _claim(case["id"], expected_revision=case["revision"])
                    st.session_state.review_notice = (
                        f"已接单工单 #{case['id']}（revision {claimed['revision']}）")
                    st.rerun()
                except (ApiClientError, Exception) as exc:
                    st.error(f"接单冲突：{exc}")
        with right:
            st.download_button(
                "导出咨询摘要（JSON）",
                data=json.dumps(summary, ensure_ascii=False, indent=2),
                file_name=f"review_case_{case['id']}_summary.json", mime="application/json",
                key=f"dl-{case['id']}")

        action = st.selectbox(
            "结构化决策（仅限五类，无自由文本投放）",
            ["resolve_conflict", "confirm_reported_fact", "reject_candidate",
             "close_with_safe_guidance", "request_more_info"],
            key=f"action-{case['id']}")
        basis = st.text_input("依据说明（仅入审计，不进入用户可见文本）",
                              key=f"basis-{case['id']}")
        conflict_ref = ""
        if action == "resolve_conflict":
            conflict_ref = st.text_input("矛盾记录 ref（如 conflict:1@v1）",
                                         key=f"cref-{case['id']}")
        question = ""
        if action == "request_more_info":
            question = st.text_input("需要用户补充的问题", key=f"q-{case['id']}")
        if st.button("提交决策", key=f"decide-{case['id']}",
                     disabled=case["status"] not in {"assigned", "in_review"}):
            payload = {"basis": basis}
            if conflict_ref:
                payload["conflict_ref"] = conflict_ref
            if question:
                payload["question"] = question
            try:
                out = _decide(case["id"], action, case["revision"], payload)
                st.session_state.review_notice = (
                    f"决策已记录（{'重放幂等命中' if out.get('replayed') else '首次受理'}，"
                    f"decision {out['decision_id'][:8]}…）；恢复任务已入队")
                st.rerun()
            except (ApiClientError, Exception) as exc:
                st.error(f"决策被拒绝：{exc}")

if st.session_state.review_notice:
    st.success(st.session_state.review_notice)
if API_CLIENT is None:
    st.caption("当前为直连模式（未设置 STAGE0_API_URL）；设置后走服务层角色校验。")
