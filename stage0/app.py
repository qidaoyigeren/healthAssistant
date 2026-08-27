"""Interactive Stage 4 UI for the real medication coordinator agent.

Run the deterministic offline-first path with::

    streamlit run stage0/app.py

Pass ``--llm`` after Streamlit's argument separator to enable the optional
structured-memory extractor::

    streamlit run stage0/app.py -- --llm

This is a single-caregiver, single-patient engineering demo.  It is not a
medical device and does not diagnose, prescribe, or replace a clinician.
"""
from __future__ import annotations

import html
import json
import os
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

import streamlit as st

try:
    from .agent import AgentResponse, CareEvent, MedicationCoordinatorAgent
    from .memory import DEFAULT_DB, MemoryStore, memory_ref
except ImportError:  # Support ``streamlit run stage0/app.py``.
    from agent import AgentResponse, CareEvent, MedicationCoordinatorAgent  # type: ignore
    from memory import DEFAULT_DB, MemoryStore, memory_ref  # type: ignore


ROOT = Path(__file__).resolve().parent
DB_PATH = DEFAULT_DB.resolve()
LLM_ENABLED = "--llm" in sys.argv[1:]

# Keep the normal demo reproducible and network-free.  ``--llm`` only enables
# the same optional structured fact extraction exposed by ``demo.py --llm``;
# pair detection still uses persisted Stage 1/2 evidence by default.
os.environ.setdefault("DDI_ENGINE_ENABLE_LLM", "0")
os.environ.setdefault("DDI_ENGINE_ENABLE_RAG", "0")

st.set_page_config(
    page_title="用药协管员 · 可审计照护记忆",
    page_icon="🩺",
    layout="wide",
    initial_sidebar_state="expanded",
)


STYLES = """
<style>
:root {
  --paper: #f3f7f7;
  --surface: #ffffff;
  --ink: #17324d;
  --teal: #2a7a78;
  --teal-soft: #e3f0ef;
  --saffron: #d99a32;
  --alert: #b94846;
  --slate: #627380;
  --rule: #d7e1e2;
}

.stApp {
  background: var(--paper);
  color: var(--ink);
  font-family: "Microsoft YaHei", "PingFang SC", sans-serif;
}

[data-testid="stAppViewContainer"] > .main .block-container {
  max-width: 1220px;
  padding-top: 2.2rem;
  padding-bottom: 4rem;
}

h1, h2, h3 {
  color: var(--ink) !important;
  letter-spacing: -0.025em;
}

.care-hero {
  position: relative;
  overflow: hidden;
  background: var(--surface);
  border: 1px solid var(--rule);
  border-left: 7px solid var(--teal);
  border-radius: 8px;
  padding: 1.55rem 1.75rem 1.4rem;
  margin-bottom: 1rem;
  box-shadow: 0 10px 30px rgba(23, 50, 77, 0.06);
}

.care-hero::after {
  content: "memory / evidence / action";
  position: absolute;
  right: 1.25rem;
  top: 1rem;
  color: #93a5aa;
  font: 600 0.68rem/1.2 Consolas, monospace;
  letter-spacing: 0.12em;
  text-transform: uppercase;
}

.care-eyebrow {
  color: var(--teal);
  font: 700 0.75rem/1.2 Consolas, monospace;
  letter-spacing: 0.12em;
  text-transform: uppercase;
  margin-bottom: 0.6rem;
}

.care-title {
  color: var(--ink);
  font-family: "STKaiti", "KaiTi", "Microsoft YaHei", sans-serif;
  font-size: clamp(2rem, 4vw, 3.5rem);
  font-weight: 700;
  line-height: 1.08;
  margin: 0;
}

.care-subtitle {
  color: var(--slate);
  max-width: 760px;
  line-height: 1.75;
  margin: 0.7rem 0 0;
}

.safety-strip {
  display: flex;
  flex-wrap: wrap;
  gap: 0.55rem 1.1rem;
  align-items: center;
  border-top: 1px solid var(--rule);
  margin-top: 1.15rem;
  padding-top: 0.85rem;
  color: var(--slate);
  font-size: 0.82rem;
}

.status-dot {
  display: inline-block;
  width: 0.55rem;
  height: 0.55rem;
  margin-right: 0.35rem;
  border-radius: 50%;
  background: var(--teal);
  box-shadow: 0 0 0 4px var(--teal-soft);
}

.section-kicker {
  color: var(--teal);
  font: 700 0.7rem/1.2 Consolas, monospace;
  letter-spacing: 0.11em;
  text-transform: uppercase;
  margin-bottom: -0.3rem;
}

.response-panel {
  background: var(--surface);
  border: 1px solid var(--rule);
  border-radius: 8px;
  padding: 1rem 1.15rem;
  margin: 0.35rem 0 1rem;
}

.response-panel p { margin: 0; line-height: 1.75; }

.warning-card {
  background: var(--surface);
  border: 1px solid var(--rule);
  border-left: 5px solid var(--saffron);
  border-radius: 7px;
  padding: 0.9rem 1rem;
  margin: 0.65rem 0 0.35rem;
}

.warning-card.high { border-left-color: var(--alert); }
.warning-card.low-confidence { border-style: dashed; }

.warning-pair {
  color: var(--ink);
  font-size: 1.03rem;
  font-weight: 750;
  margin-bottom: 0.45rem;
}

.badge {
  display: inline-block;
  border-radius: 999px;
  padding: 0.18rem 0.52rem;
  margin-right: 0.35rem;
  background: var(--teal-soft);
  color: var(--teal);
  font: 700 0.68rem/1.2 Consolas, monospace;
  letter-spacing: 0.04em;
  text-transform: uppercase;
}

.badge.severe { background: #f8e5e3; color: var(--alert); }
.badge.uncertain { background: #fff3dc; color: #8b5d11; }

.warning-effect {
  color: #354b5c;
  line-height: 1.65;
  margin-top: 0.6rem;
}

.audit-ref, code {
  color: #355b65 !important;
  font-family: Consolas, "SFMono-Regular", monospace !important;
  font-size: 0.76rem !important;
}

.ledger-row {
  position: relative;
  border-left: 2px solid #9fc4c2;
  padding: 0.05rem 0 1rem 1.15rem;
  margin-left: 0.35rem;
}

.ledger-row::before {
  content: "";
  position: absolute;
  left: -0.36rem;
  top: 0.2rem;
  width: 0.58rem;
  height: 0.58rem;
  border: 2px solid var(--teal);
  border-radius: 50%;
  background: var(--paper);
}

.ledger-time {
  color: var(--slate);
  font: 600 0.7rem/1.2 Consolas, monospace;
  margin-bottom: 0.22rem;
}

.ledger-main { color: var(--ink); font-weight: 650; }

.empty-ledger {
  color: var(--slate);
  border: 1px dashed #b9c9cb;
  border-radius: 7px;
  padding: 1rem;
  text-align: center;
}

[data-testid="stMetric"] {
  background: rgba(255,255,255,0.72);
  border-top: 2px solid var(--teal);
  padding: 0.8rem 0.9rem;
}

[data-testid="stSidebar"] {
  background: #eaf1f1;
  border-right: 1px solid var(--rule);
}

[data-testid="stSidebar"] .block-container { padding-top: 1.7rem; }

.stButton > button, .stFormSubmitButton > button {
  border-radius: 5px;
  border: 1px solid var(--teal);
  font-weight: 700;
}

.stButton > button:focus-visible, .stFormSubmitButton > button:focus-visible,
input:focus-visible, textarea:focus-visible, select:focus-visible {
  outline: 3px solid rgba(217, 154, 50, 0.35) !important;
  outline-offset: 2px;
}

@media (max-width: 700px) {
  [data-testid="stAppViewContainer"] > .main .block-container { padding-top: 1rem; }
  .care-hero { padding: 1.2rem; }
  .care-hero::after { display: none; }
  .care-title { font-size: 2.25rem; }
}

@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after { scroll-behavior: auto !important; transition: none !important; }
}
</style>
"""


def _escape(value: Any) -> str:
    return html.escape(str(value if value is not None else ""))


def _split_list(value: str) -> list[str]:
    normalized = value.replace("，", ",").replace("、", ",").replace(";", ",").replace("；", ",")
    return list(dict.fromkeys(item.strip() for item in normalized.split(",") if item.strip()))


def _runtime() -> tuple[MemoryStore, MedicationCoordinatorAgent]:
    current_mode = st.session_state.get("runtime_llm")
    if "memory_store" not in st.session_state or current_mode != LLM_ENABLED:
        old_memory = st.session_state.get("memory_store")
        if old_memory is not None:
            old_memory.close()
        memory = MemoryStore(DB_PATH, llm_enabled=LLM_ENABLED)
        st.session_state.memory_store = memory
        st.session_state.agent = MedicationCoordinatorAgent(memory)
        st.session_state.runtime_llm = LLM_ENABLED
    if "session_id" not in st.session_state:
        st.session_state.session_id = f"caregiver-ui-{uuid.uuid4().hex[:10]}"
        st.session_state.session_number = 1
    return st.session_state.memory_store, st.session_state.agent


def _reopen_session(*, clear_database: bool = False) -> None:
    memory = st.session_state.get("memory_store")
    if memory is not None:
        memory.close()
    for key in ("memory_store", "agent", "runtime_llm", "last_response", "last_event", "runtime_error"):
        st.session_state.pop(key, None)

    if clear_database:
        # Only the known Stage 3 database and its explicit SQLite sidecars may
        # be removed.  This protects every source/data artifact in ``stage0``.
        if DB_PATH.parent != ROOT:
            raise RuntimeError(f"refusing to reset unexpected database path: {DB_PATH}")
        for target in (DB_PATH, Path(f"{DB_PATH}-wal"), Path(f"{DB_PATH}-shm")):
            if target.is_file():
                target.unlink()
        st.session_state.session_number = 1
        # Streamlit can rehydrate widget values from the browser when a key is
        # reused.  Bump the UI revision so a reset visibly starts with empty
        # profile/query controls, not merely an empty database.
        st.session_state.ui_revision = int(st.session_state.get("ui_revision", 0)) + 1
        for key in list(st.session_state):
            if key.startswith("profile_") or key.startswith("med_") or key.startswith("query_"):
                st.session_state.pop(key, None)
    else:
        st.session_state.session_number = int(st.session_state.get("session_number", 1)) + 1

    st.session_state.session_id = f"caregiver-ui-{uuid.uuid4().hex[:10]}"
    st.rerun()


def _handle_event(event: CareEvent, label: str) -> None:
    _, agent = _runtime()
    try:
        with st.spinner("正在写入记忆、检查证据并执行安全边界…"):
            response = agent.handle(event, session_id=st.session_state.session_id)
        st.session_state.last_response = response
        st.session_state.last_event = label
        st.session_state.pop("runtime_error", None)
    except Exception as exc:  # Keep UI failures explicit without masking them.
        st.session_state.runtime_error = f"{type(exc).__name__}: {exc}"
    st.rerun()


def _profile_defaults(snapshot: dict[str, Any]) -> dict[str, Any]:
    defaults: dict[str, Any] = {
        "age": 0,
        "sex": "未记录",
        "weight": 0.0,
        "renal": "未记录",
        "hepatic": "未记录",
        "allergies": [],
        "diseases": [],
        "preferences": [],
    }
    for item in snapshot.get("semantic", []):
        namespace, value = item.get("namespace"), item.get("value")
        if namespace == "age":
            defaults["age"] = int(value)
        elif namespace == "sex":
            defaults["sex"] = str(value)
        elif namespace == "weight":
            defaults["weight"] = float(value)
        elif namespace == "renal_function":
            defaults["renal"] = str(value)
        elif namespace == "hepatic_function":
            defaults["hepatic"] = str(value)
        elif namespace == "allergy":
            allergen = value.get("allergen") if isinstance(value, dict) else (item.get("fact_key") or item.get("key"))
            if allergen:
                defaults["allergies"].append(str(allergen))
        elif namespace == "chronic_disease":
            name = value.get("name") if isinstance(value, dict) else (item.get("fact_key") or item.get("key"))
            if name:
                defaults["diseases"].append(str(name))
        elif namespace == "preference":
            key = item.get("fact_key") or item.get("key") or "care_notes"
            defaults["preferences"].append(f"{key}={value}")
    return defaults


def _stored_warning_records(memory: MemoryStore) -> list[dict[str, Any]]:
    """Join warning episodes to conclusions for a read-only UI audit view."""
    conclusions: list[dict[str, Any]] = []
    for row in memory.connection.execute(
        "SELECT id,memory_refs_json,source_refs_json,text FROM conclusions WHERE kind='warning' ORDER BY id"
    ).fetchall():
        conclusions.append({
            "ref": memory_ref("conclusion", row["id"], 1),
            "memory_refs": json.loads(row["memory_refs_json"]),
            "source_refs": json.loads(row["source_refs_json"]),
            "text": row["text"],
        })

    records: list[dict[str, Any]] = []
    for episode in memory.timeline(limit=500):
        if episode.get("event_type") != "warning":
            continue
        payload = episode.get("payload") or {}
        warning = dict(payload.get("warning") or {})
        conclusion = next(
            (item for item in conclusions if episode["ref"] in item["memory_refs"]),
            None,
        )
        warning["citations"] = payload.get("source_refs") or (conclusion or {}).get("source_refs", [])
        warning["audit_trail"] = {
            "warning_memory": episode["ref"],
            "conclusion": (conclusion or {}).get("ref"),
            "memory_refs": (conclusion or {}).get("memory_refs", payload.get("context_refs", [])),
            "source_refs": warning["citations"],
        }
        warning["occurred_at"] = episode.get("occurred_at")
        records.append(warning)
    return list(reversed(records))


def _render_warning(warning: dict[str, Any], *, persisted: bool = False) -> None:
    severity = str(warning.get("severity") or "unknown")
    confidence = str(warning.get("confidence") or "unknown")
    high = severity in {"contraindicated", "major"}
    low = confidence in {"low", "unknown"}
    classes = "warning-card" + (" high" if high else "") + (" low-confidence" if low else "")
    severity_class = "severe" if high else ""
    confidence_class = "uncertain" if low else ""
    when = f" · {_escape(warning.get('occurred_at'))}" if persisted and warning.get("occurred_at") else ""
    st.markdown(
        f"""
        <div class="{classes}">
          <div class="warning-pair">{_escape(warning.get('drug_a', '?'))} × {_escape(warning.get('drug_b', '?'))}{when}</div>
          <span class="badge {severity_class}">{_escape(severity)}</span>
          <span class="badge {confidence_class}">confidence · {_escape(confidence)}</span>
          <div class="warning-effect">{_escape(warning.get('effect') or '存在需要核实的用药风险')}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    trail = warning.get("audit_trail") or {}
    citations = warning.get("citations") or trail.get("source_refs") or []
    with st.expander("查看来源与审计链"):
        if citations:
            for index, citation in enumerate(citations, 1):
                uri = citation.get("uri") or "未提供 URI"
                st.markdown(f"**来源 {index}** · `{citation.get('source_type', 'source')}`")
                st.code(str(uri), language=None)
                if citation.get("quote"):
                    st.caption(f"原文摘录：{citation['quote']}")
        else:
            st.warning("这条记录没有可显示的来源；不得据此形成确定性结论。")
        st.markdown("**审计引用**")
        refs = list(dict.fromkeys([
            *trail.get("memory_refs", []),
            *([trail.get("warning_memory")] if trail.get("warning_memory") else []),
            *([trail.get("conclusion")] if trail.get("conclusion") else []),
        ]))
        st.code("\n".join(refs) if refs else "无审计引用", language=None)
        if low:
            st.warning("低置信度/类别推断已升级：建议咨询医生或药师，不要自行停药或改量。")


def _render_response(response: AgentResponse | None, label: str | None) -> None:
    st.markdown('<div class="section-kicker">Latest agent turn</div>', unsafe_allow_html=True)
    st.subheader("本次协管结果")
    if response is None:
        st.markdown(
            '<div class="empty-ledger">先登记档案或新增一条用药记录。主动警告会在这里出现。</div>',
            unsafe_allow_html=True,
        )
        return
    if label:
        st.caption(f"照护者事件：{label}")
    st.markdown(
        f'<div class="response-panel"><p>{_escape(response.text).replace(chr(10), "<br>")}</p></div>',
        unsafe_allow_html=True,
    )
    if response.warnings:
        st.markdown("#### 主动风险提示")
        for warning in response.warnings:
            _render_warning(warning)
    elif response.safety_status == "enforced":
        st.caption("安全边界已执行；本次没有形成带证据的新增警告。未检出不等于绝对安全。")
    if response.conflicts:
        st.markdown("#### 本次未决冲突")
        for conflict in response.conflicts:
            st.warning(f"{conflict.get('description')}  [{conflict.get('ref')}]")
    with st.expander("本次代理循环与审计摘要"):
        acts = [item for item in response.tool_trace if item.get("phase") == "act"]
        st.write(" → ".join(f"{item['tool']}[{item['purpose']}]" for item in acts) or "respond")
        st.json({
            "safety_status": response.safety_status,
            "memory_refs": response.audit_trail.get("memory_refs", []),
            "source_refs": response.audit_trail.get("source_refs", []),
            "reflection": response.audit_trail.get("reflection", []),
        })


def _render_sidebar() -> None:
    with st.sidebar:
        st.markdown("## 用药协管员")
        st.caption("单照护者 · 单患者 · 本地演示")
        st.divider()
        mode_label = "LLM 结构化抽取" if LLM_ENABLED else "确定性离线路径"
        st.markdown(f"<span class='status-dot'></span> **{mode_label}**", unsafe_allow_html=True)
        st.caption("DDI 检查默认只使用已持久化的本地证据，不发起网络请求。")
        st.code(f"session {st.session_state.get('session_number', 1)}\n{st.session_state.session_id}", language=None)
        if st.button("开启新会话（保留记忆）", use_container_width=True):
            _reopen_session(clear_database=False)
        st.divider()
        st.markdown("**重置演示**")
        reset_confirmed = st.checkbox(
            "我确认清空 stage0/memory.db",
            key=f"reset_confirmed_{st.session_state.get('ui_revision', 0)}",
        )
        if st.button("清空并重新开始", disabled=not reset_confirmed, use_container_width=True):
            _reopen_session(clear_database=True)
        st.caption("仅删除 memory.db 及 SQLite sidecar；不会改动代码、数据或评估结果。")
        st.divider()
        st.caption("非医疗器械 · 不诊断 · 不处方 · 不建议自行停药或改量")


memory, _agent = _runtime()
if "ui_revision" not in st.session_state:
    st.session_state.ui_revision = 0
snapshot = memory.snapshot()
warning_records = _stored_warning_records(memory)
medication_events = [
    item for item in memory.timeline(limit=500)
    if item.get("event_type") in {"medication_add", "medication_remove", "medication_dose_change"}
]

st.markdown(STYLES, unsafe_allow_html=True)
_render_sidebar()

mode_copy = "LLM 结构化抽取已启用" if LLM_ENABLED else "默认离线、确定性运行"
st.markdown(
    f"""
    <section class="care-hero">
      <div class="care-eyebrow">Auditable medication coordination</div>
      <h1 class="care-title">把每一次用药变化，写成可核查的照护记忆</h1>
      <p class="care-subtitle">记住父母健康史、主动预警用药冲突、全程可审计的“用药协管员”。记录事实、检查来源、保留冲突；不替代医生作出医疗决定。</p>
      <div class="safety-strip">
        <span><i class="status-dot"></i>{_escape(mode_copy)}</span>
        <span>SQLite 精确记忆</span>
        <span>真实 DDI detector</span>
        <span>安全边界已启用</span>
      </div>
    </section>
    """,
    unsafe_allow_html=True,
)

metric_cols = st.columns(4)
metric_cols[0].metric("当前用药", len(snapshot.get("medications", [])), help="SQLite 中 status=active 的精确记录")
metric_cols[1].metric("累计警告", len(warning_records), help="已写入情景记忆的 warning 事件")
metric_cols[2].metric("未决冲突", len(snapshot.get("open_conflicts", [])), help="系统不会静默选择冲突的一侧")
metric_cols[3].metric("记忆会话", st.session_state.get("session_number", 1), help="新会话会重开 SQLite 与代理，但保留数据库")

if st.session_state.get("runtime_error"):
    st.error(f"代理执行失败：{st.session_state.runtime_error}")

left, right = st.columns([1.03, 0.97], gap="large")
with left:
    st.markdown('<div class="section-kicker">Care events</div>', unsafe_allow_html=True)
    st.subheader("照护操作台")
    profile_tab, medication_tab, exposure_tab, query_tab = st.tabs(["患者档案", "用药变更", "CT 造影", "记忆查询"])

    with profile_tab:
        defaults = _profile_defaults(snapshot)
        revision = st.session_state.get("ui_revision", 0)
        with st.form(f"profile_form_{revision}"):
            c1, c2, c3 = st.columns(3)
            age = c1.number_input("年龄", min_value=0, max_value=130, value=defaults["age"], step=1, key=f"profile_age_{revision}")
            sex_options = list(dict.fromkeys([defaults["sex"], "未记录", "女", "男", "其他"]))
            sex = c2.selectbox("性别", sex_options, key=f"profile_sex_{revision}")
            weight = c3.number_input("体重（kg）", min_value=0.0, max_value=300.0, value=defaults["weight"], step=0.5, key=f"profile_weight_{revision}")
            c4, c5 = st.columns(2)
            renal_options = list(dict.fromkeys([defaults["renal"], "未记录", "正常", "轻度受损", "中度受损", "重度受损"]))
            hepatic_options = list(dict.fromkeys([defaults["hepatic"], "未记录", "正常", "轻度受损", "中度受损", "重度受损"]))
            renal = c4.selectbox("肾功能", renal_options, key=f"profile_renal_{revision}")
            hepatic = c5.selectbox("肝功能", hepatic_options, key=f"profile_hepatic_{revision}")
            allergies = st.text_input("过敏史", value="、".join(defaults["allergies"]), placeholder="如：磺胺、青霉素", key=f"profile_allergies_{revision}")
            diseases = st.text_input("慢性病", value="、".join(defaults["diseases"]), placeholder="如：高血压、糖尿病", key=f"profile_diseases_{revision}")
            preferences = st.text_area("照护偏好", value="；".join(defaults["preferences"]), placeholder="如：希望用大字版用药清单", key=f"profile_preferences_{revision}")
            profile_submitted = st.form_submit_button("保存患者档案", use_container_width=True)
        st.caption("安全关键事实按版本保存；新旧记录冲突时会显式标记，不会静默覆盖。")
        if profile_submitted:
            profile: dict[str, Any] = {
                "allergies": _split_list(allergies),
                "chronic_diseases": _split_list(diseases),
            }
            if age:
                profile["age"] = int(age)
            if sex != "未记录":
                profile["sex"] = sex
            if weight:
                profile["weight_kg"] = float(weight)
            if renal != "未记录":
                profile["renal_function"] = renal
            if hepatic != "未记录":
                profile["hepatic_function"] = hepatic
            if preferences.strip():
                profile["preferences"] = {"care_notes": preferences.strip()}
            _handle_event(
                CareEvent("profile_update" if snapshot.get("semantic") else "register_profile", "保存患者档案。", {"profile": profile}),
                "保存患者档案",
            )

    with medication_tab:
        add_tab, remove_tab = st.tabs(["新增药物", "停用记录"])
        with add_tab:
            with st.form("add_medication_form", clear_on_submit=True):
                medication_name = st.text_input("药名（商品名或通用名）", placeholder="如：氨氯地平 / 克拉霉素", key="med_add_name")
                c1, c2, c3 = st.columns(3)
                dose = c1.text_input("剂量（可选）", placeholder="5 mg", key="med_add_dose")
                route = c2.text_input("给药途径（可选）", placeholder="口服", key="med_add_route")
                schedule = c3.text_input("频次（可选）", placeholder="每日一次", key="med_add_schedule")
                add_submitted = st.form_submit_button("新增并主动检查", use_container_width=True)
            if add_submitted:
                if medication_name.strip():
                    payload = {"action": "add", "medication": medication_name.strip()}
                    if dose.strip():
                        payload["dose"] = dose.strip()
                    if route.strip():
                        payload["route"] = route.strip()
                    if schedule.strip():
                        payload["schedule"] = schedule.strip()
                    _handle_event(
                        CareEvent("medication_change", f"新增{medication_name.strip()}。", payload),
                        f"新增 {medication_name.strip()}",
                    )
                else:
                    st.warning("请输入药名后再检查。")
        with remove_tab:
            active_names = [item["display_name"] for item in snapshot.get("medications", [])]
            with st.form("remove_medication_form"):
                selected_medication = st.selectbox(
                    "选择要记录停用的药物",
                    active_names if active_names else ["暂无在用药"],
                    disabled=not active_names,
                    key="med_remove_name",
                )
                remove_submitted = st.form_submit_button("记录停用", disabled=not active_names, use_container_width=True)
            if remove_submitted and active_names:
                _handle_event(
                    CareEvent("medication_change", f"记录停用{selected_medication}。", {"action": "remove", "medication": selected_medication}),
                    f"停用 {selected_medication}",
                )

    with exposure_tab:
        st.write("记录已发生的含碘造影剂暴露。系统会把医疗行为与说明书风险两侧都保留下来。")
        with st.form("contrast_form"):
            exposure_text = st.text_area(
                "照护者报告",
                value="医生上个月让做的 CT 用了造影剂。",
                key="contrast_text",
            )
            contrast_submitted = st.form_submit_button("记录暴露并检查冲突", use_container_width=True)
        if contrast_submitted:
            _handle_event(
                CareEvent(
                    "procedure_exposure",
                    exposure_text.strip() or "报告含碘造影剂暴露。",
                    {"agent": "含碘造影剂", "doctor_involved": "医生" in exposure_text},
                ),
                "报告 CT 含碘造影剂暴露",
            )

    with query_tab:
        with st.form("query_form", clear_on_submit=False):
            query = st.text_input("向照护记忆提问", value="现在吃什么药", key="query_text")
            query_submitted = st.form_submit_button("从 memory.db 精确查询", use_container_width=True)
        st.caption("试试：现在吃什么药。回答包含当前清单与跨会话变更时间线。")
        if query_submitted and query.strip():
            _handle_event(CareEvent("user_message", query.strip()), query.strip())

with right:
    _render_response(st.session_state.get("last_response"), st.session_state.get("last_event"))

st.divider()
warnings_tab, memory_tab, profile_view_tab = st.tabs(["风险预警", "记忆账本", "档案与边界"])

with warnings_tab:
    st.markdown('<div class="section-kicker">Evidence ledger</div>', unsafe_allow_html=True)
    st.subheader("已持久化的风险预警")
    st.caption("每条记录同时展示严重度、置信度、来源与 memory → conclusion 审计链。")
    if warning_records:
        for item in warning_records:
            _render_warning(item, persisted=True)
    else:
        st.markdown(
            '<div class="empty-ledger">暂无已持久化警告。新增药物后，真实代理会主动调用 DDI 检查。</div>',
            unsafe_allow_html=True,
        )

with memory_tab:
    med_col, timeline_col, conflict_col = st.columns([0.9, 1.15, 0.95], gap="large")
    with med_col:
        st.markdown("### 当前用药")
        if snapshot.get("medications"):
            for item in snapshot["medications"]:
                ingredients = "、".join(
                    ingredient.get("name_cn", "?") for ingredient in item.get("ingredients", [])
                )
                st.markdown(f"**{item['display_name']}**")
                st.caption(f"成分：{ingredients or '未标准化'}")
                details = " · ".join(part for part in (item.get("dose"), item.get("route"), item.get("schedule")) if part)
                if details:
                    st.caption(details)
                st.code(item["ref"], language=None)
        else:
            st.caption("暂无在用药记录。")
    with timeline_col:
        st.markdown("### 变更时间线")
        if medication_events:
            event_labels = {
                "medication_add": "新增",
                "medication_remove": "停用",
                "medication_dose_change": "剂量变更",
            }
            for item in medication_events:
                payload = item.get("payload") or {}
                st.markdown(
                    f"""
                    <div class="ledger-row">
                      <div class="ledger-time">{_escape(item.get('occurred_at'))}</div>
                      <div class="ledger-main">{_escape(event_labels.get(item.get('event_type'), item.get('event_type')))} · {_escape(payload.get('name') or item.get('subject_key'))}</div>
                      <div class="audit-ref">{_escape(item.get('ref'))}</div>
                    </div>
                    """,
                    unsafe_allow_html=True,
                )
        else:
            st.caption("新增、停用或剂量变更后，这里会形成不可丢失的事件序列。")
    with conflict_col:
        st.markdown("### 未决冲突")
        if snapshot.get("open_conflicts"):
            for conflict in snapshot["open_conflicts"]:
                with st.expander(conflict.get("subject_key") or "未决冲突"):
                    st.write(conflict.get("description"))
                    st.code(
                        "\n".join(filter(None, [conflict.get("left_ref"), conflict.get("right_ref"), conflict.get("ref")])),
                        language=None,
                    )
        else:
            st.caption("暂无未决冲突。系统不会为了给出流畅答案而静默选边。")

with profile_view_tab:
    p1, p2 = st.columns([1.2, 0.8], gap="large")
    with p1:
        st.markdown("### 结构化患者事实")
        if snapshot.get("semantic"):
            for item in snapshot["semantic"]:
                st.markdown(f"**{item.get('namespace')} · {item.get('key')}**")
                st.write(item.get("value"))
                st.code(item.get("ref"), language=None)
        else:
            st.caption("尚未登记患者档案。")
    with p2:
        st.markdown("### 安全边界")
        st.info(
            "本系统只做记录、检索、风险提示和就医沟通辅助。它不诊断、不处方，"
            "不会建议自行开始/停止药物或改变剂量。严重、低置信度和冲突结果会升级给医生/药师。"
        )
        st.caption(f"SQLite：{DB_PATH}")
