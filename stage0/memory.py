"""Auditable three-layer memory for the single-patient Stage 3 demo.

The store deliberately uses SQLite rather than embeddings.  Facts are recalled
by exact keys, events are time ordered, working memory is turn scoped, and every
mutation is recorded in ``audit_log``.  Episodic salience affects ranking only;
it never deletes or hides the underlying event.

``MemoryStore.consolidate_interaction`` can use the existing OpenAI-compatible
DeepSeek client for structured fact extraction.  A deliberately small,
deterministic extractor is retained as an offline/error fallback so the demo is
reproducible without turning a model outage into memory loss.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sqlite3
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence


ROOT = Path(__file__).resolve().parent
DEFAULT_DB = ROOT / "memory.db"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _as_utc(value: str | datetime | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _iso(value: str | datetime | None) -> str:
    return _as_utc(value).isoformat(timespec="seconds")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _from_json(value: str | None, default: Any = None) -> Any:
    if value is None:
        return default
    return json.loads(value)


def _fingerprint(*parts: Any) -> str:
    return hashlib.sha256(_json(parts).encode("utf-8")).hexdigest()


def memory_ref(layer: str, item_id: int, version: int) -> str:
    return f"memory:{layer}:{item_id}@v{version}"


@dataclass(frozen=True)
class SemanticFact:
    namespace: str
    key: str
    value: Any
    salience: float = 0.7
    conflict_policy: str = "auto"  # auto | update | conflict
    valid_from: str | None = None
    source_uri: str | None = None
    # P0 provenance: how this fact reached the store (deterministic rules,
    # llm_structured, caller hint...).  Stored for audit; never used to decide
    # clinical truth.
    extraction_mode: str | None = None


class MemoryPolicyError(ValueError):
    """Raised when a write would violate a storage-layer memory policy."""


class IdempotencyKeyReused(MemoryPolicyError):
    """Raised when a client event id is replayed with a different payload."""


@dataclass(frozen=True)
class EpisodicFact:
    event_type: str
    payload: dict[str, Any]
    subject_key: str | None = None
    occurred_at: str | None = None
    salience: float = 0.6
    severity: str | None = None
    source_uri: str | None = None
    # P0: reports that could not be resolved into a current fact are kept as
    # sourced pending-verification evidence instead of being dropped.
    needs_verification: int = 0


@dataclass
class ConsolidationResult:
    semantic: list[dict[str, Any]] = field(default_factory=list)
    episodic: list[dict[str, Any]] = field(default_factory=list)
    working: list[dict[str, Any]] = field(default_factory=list)
    conflicts: list[dict[str, Any]] = field(default_factory=list)
    extraction_mode: str = "deterministic"
    extraction_error: str | None = None
    # True when this result was replayed from a committed client event id
    # instead of re-running extraction and writes.
    replayed: bool = False

    @property
    def memory_refs(self) -> list[str]:
        refs: list[str] = []
        for item in [*self.semantic, *self.episodic, *self.working]:
            if item.get("ref"):
                refs.append(item["ref"])
        return refs


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS semantic_memory (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    namespace TEXT NOT NULL,
    fact_key TEXT NOT NULL,
    value_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('active','superseded','disputed','retracted')),
    valid_from TEXT NOT NULL,
    valid_to TEXT,
    source TEXT NOT NULL,
    source_uri TEXT,
    version INTEGER NOT NULL,
    salience REAL NOT NULL CHECK(salience >= 0 AND salience <= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(namespace, fact_key, version)
);
CREATE INDEX IF NOT EXISTS idx_semantic_current
    ON semantic_memory(namespace, fact_key, status, version DESC);

CREATE TABLE IF NOT EXISTS medications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    medication_key TEXT NOT NULL,
    display_name TEXT NOT NULL,
    ingredients_json TEXT NOT NULL,
    dose TEXT,
    route TEXT,
    schedule TEXT,
    status TEXT NOT NULL CHECK(status IN ('active','stopped','superseded','disputed')),
    start_at TEXT NOT NULL,
    end_at TEXT,
    source TEXT NOT NULL,
    source_uri TEXT,
    version INTEGER NOT NULL,
    predecessor_id INTEGER REFERENCES medications(id),
    created_at TEXT NOT NULL,
    UNIQUE(medication_key, version)
);
CREATE INDEX IF NOT EXISTS idx_medications_current
    ON medications(medication_key, status, version DESC);

CREATE TABLE IF NOT EXISTS episodic_memory (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    subject_key TEXT,
    payload_json TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    source TEXT NOT NULL,
    source_uri TEXT,
    session_id TEXT NOT NULL,
    turn_id TEXT NOT NULL,
    salience REAL NOT NULL CHECK(salience >= 0 AND salience <= 1),
    severity TEXT,
    version INTEGER NOT NULL,
    fingerprint TEXT NOT NULL UNIQUE,
    parent_id INTEGER REFERENCES episodic_memory(id)
);
CREATE INDEX IF NOT EXISTS idx_episode_time ON episodic_memory(occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_episode_subject ON episodic_memory(event_type, subject_key, version DESC);

CREATE TABLE IF NOT EXISTS working_memory (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    turn_id TEXT NOT NULL,
    item_key TEXT NOT NULL,
    value_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('open','resolved','expired')),
    source TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    UNIQUE(session_id, turn_id, item_key)
);

CREATE TABLE IF NOT EXISTS conflicts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conflict_type TEXT NOT NULL,
    subject_key TEXT NOT NULL,
    left_ref TEXT NOT NULL,
    right_ref TEXT NOT NULL,
    description TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('open','resolved','dismissed')),
    resolution_json TEXT,
    source TEXT NOT NULL,
    created_at TEXT NOT NULL,
    resolved_at TEXT,
    fingerprint TEXT NOT NULL UNIQUE
);
CREATE INDEX IF NOT EXISTS idx_conflicts_open ON conflicts(status, created_at DESC);

CREATE TABLE IF NOT EXISTS interactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    turn_id TEXT NOT NULL,
    user_text TEXT NOT NULL,
    assistant_text TEXT,
    source TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(session_id, turn_id)
);

CREATE TABLE IF NOT EXISTS conclusions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    turn_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    text TEXT NOT NULL,
    memory_refs_json TEXT NOT NULL,
    source_refs_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    action TEXT NOT NULL,
    actor TEXT NOT NULL,
    target_type TEXT NOT NULL,
    target_id INTEGER,
    details_json TEXT NOT NULL,
    source TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_target ON audit_log(target_type, target_id, id);
"""


# P0 additive migration.  Columns are appended with constant defaults so that
# opening an existing schema-v3 database only extends it, never rewrites it.
P0_SCHEMA_VERSION = "3-p0"
P0_COLUMNS: dict[str, tuple[tuple[str, str], ...]] = {
    "interactions": (
        ("event_key", "TEXT"),
        ("payload_hash", "TEXT"),
        ("process_status", "TEXT NOT NULL DEFAULT 'pending'"),
        ("result_json", "TEXT"),
    ),
    "semantic_memory": (
        ("extraction_mode", "TEXT"),
        ("verification_status", "TEXT NOT NULL DEFAULT 'recorded_as_reported'"),
    ),
    "episodic_memory": (
        ("needs_verification", "INTEGER NOT NULL DEFAULT 0"),
    ),
}


def _ensure_p0_columns(connection: sqlite3.Connection) -> bool:
    """Add P0 provenance/idempotency columns when missing. Returns True if altered."""
    altered = False
    for table, columns in P0_COLUMNS.items():
        existing = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
        for name, declaration in columns:
            if name not in existing:
                connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")
                altered = True
    connection.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_interactions_event_key ON interactions(event_key)"
    )
    return altered


P1_SCHEMA_VERSION = "4-p1"

P1_TABLES = """
CREATE TABLE IF NOT EXISTS scope_revisions (
    scope_key TEXT PRIMARY KEY,
    revision INTEGER NOT NULL,
    updated_at TEXT NOT NULL
);

-- Scope dependency bookkeeping: a conclusion records the revision of every
-- collection it consumed (e.g. the full medication list at check time).  A
-- later collection change bumps the revision, which invalidates older
-- conclusions even when the new member never appeared in their citations.
INSERT OR IGNORE INTO scope_revisions(scope_key, revision, updated_at)
    VALUES('medications', 0, '1970-01-01T00:00:00+00:00');
INSERT OR IGNORE INTO scope_revisions(scope_key, revision, updated_at)
    VALUES('semantic', 0, '1970-01-01T00:00:00+00:00');

CREATE TABLE IF NOT EXISTS conflict_actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conflict_id INTEGER NOT NULL REFERENCES conflicts(id),
    action TEXT NOT NULL CHECK(action IN ('resolved','dismissed','reopened','undo')),
    basis TEXT NOT NULL,
    actor TEXT NOT NULL,
    chosen_ref TEXT,
    previous_status TEXT,
    created_at TEXT NOT NULL,
    undone_by INTEGER REFERENCES conflict_actions(id)
);
CREATE INDEX IF NOT EXISTS idx_conflict_actions_conflict ON conflict_actions(conflict_id, id);

CREATE TABLE IF NOT EXISTS dependency_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    subject_id TEXT NOT NULL DEFAULT 'default',
    task_type TEXT NOT NULL CHECK(task_type IN ('recheck_conclusion')),
    target_id INTEGER NOT NULL,
    reason TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('open','running','done','failed','cancelled')),
    attempts INTEGER NOT NULL DEFAULT 0,
    lease_token TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_dependency_tasks_status ON dependency_tasks(status, id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_dependency_tasks_open
    ON dependency_tasks(task_type, target_id) WHERE status IN ('open','running');
"""


def _ensure_p1_schema(connection: sqlite3.Connection) -> bool:
    """Create P1 scope/conflict-action/recheck-task structures. Additive only."""
    altered = False
    conclusion_columns = {
        row[1] for row in connection.execute("PRAGMA table_info(conclusions)")
    }
    for name, declaration in (
        ("status", "TEXT NOT NULL DEFAULT 'current'"),
        ("input_revision", "TEXT"),
        ("predecessor_id", "INTEGER"),
        ("superseded_by", "INTEGER"),
        ("stale_reason", "TEXT"),
    ):
        if name not in conclusion_columns:
            connection.execute(f"ALTER TABLE conclusions ADD COLUMN {name} {declaration}")
            altered = True
    connection.executescript(P1_TABLES)
    return altered


SAFETY_CRITICAL_NAMESPACES = {
    "allergy", "renal_function", "hepatic_function", "chronic_disease", "sex"
}
SAFE_UPDATE_NAMESPACES = {"age", "weight", "preference", "contact", "living_arrangement"}

EPISODIC_HALF_LIFE_DAYS = {
    "measurement": 30.0,
    "caregiver_message": 45.0,
    "procedure_exposure": 180.0,
    "medication_add": 730.0,
    "medication_remove": 730.0,
    "medication_dose_change": 730.0,
    "warning": 730.0,
    "hospitalization": 1825.0,
}


class StructuredFactExtractor:
    """LLM structured extraction with a bounded deterministic fallback."""

    SYSTEM_PROMPT = """你是单患者用药记忆的事实抽取器，不提供诊断或处方。
只抽取用户明确陈述的事实，不推断。输出一个 JSON 对象：
{"semantic":[{"namespace":"age|sex|weight|allergy|renal_function|hepatic_function|chronic_disease|preference","key":"规范键","value":任意JSON值,"salience":0到1,"conflict_policy":"auto|update|conflict"}],
 "episodic":[{"event_type":"measurement|hospitalization|procedure_exposure|caregiver_message","subject_key":"可选","payload":{},"occurred_at":"可选ISO时间","salience":0到1,"severity":"可选"}],
 "working":[{"key":"goal|intermediate|contradiction","value":任意JSON值}]}
过敏史、肝肾功能、疾病的否定或更正必须保留为候选事实并使用 conflict；不要静默覆盖。
没有事实时返回空数组。"""

    def __init__(self, enabled: bool | None = None, model: str | None = None):
        if enabled is None:
            # P0: a bare store is offline by default.  LLM extraction requires
            # the explicit MEMORY_ENABLE_LLM switch (or MemoryStore(llm_enabled=True)).
            enabled = os.getenv("MEMORY_ENABLE_LLM", "").strip().lower() in {"1", "true", "yes", "on"}
        self.enabled = enabled
        self.model = model

    def extract(self, text: str) -> tuple[dict[str, list[dict[str, Any]]], str, str | None]:
        if self.enabled:
            try:
                return self._llm_extract(text), "llm_structured", None
            except Exception as exc:  # Memory must remain available during provider failures.
                fallback = self._deterministic_extract(text)
                return fallback, "deterministic_fallback", f"{type(exc).__name__}: {exc}"
        return self._deterministic_extract(text), "deterministic", None

    def _llm_extract(self, text: str) -> dict[str, list[dict[str, Any]]]:
        try:
            from . import extract_ddi
        except ImportError:  # Support ``python stage0/memory.py``.
            import extract_ddi  # type: ignore

        config = extract_ddi.resolve_llm_config(model=self.model)
        client = extract_ddi.create_llm_client(config)
        response = client.chat.completions.create(
            model=config["model"],
            messages=[
                {"role": "system", "content": self.SYSTEM_PROMPT},
                {"role": "user", "content": text},
            ],
            response_format={"type": "json_object"},
            temperature=0,
            **extract_ddi.llm_completion_options(),
        )
        content = response.choices[0].message.content or "{}"
        return self._validate(json.loads(content))

    @staticmethod
    def _validate(payload: Any) -> dict[str, list[dict[str, Any]]]:
        if not isinstance(payload, dict):
            raise ValueError("fact extractor must return a JSON object")
        out: dict[str, list[dict[str, Any]]] = {"semantic": [], "episodic": [], "working": []}
        allowed_namespaces = {
            "age", "sex", "weight", "allergy", "renal_function", "hepatic_function",
            "chronic_disease", "preference", "contact", "living_arrangement",
        }
        for item in payload.get("semantic", []):
            if not isinstance(item, dict) or item.get("namespace") not in allowed_namespaces:
                continue
            if not isinstance(item.get("key"), str) or "value" not in item:
                continue
            item = dict(item)
            item["salience"] = min(1.0, max(0.0, float(item.get("salience", 0.7))))
            if item.get("conflict_policy") not in {"auto", "update", "conflict"}:
                item["conflict_policy"] = "auto"
            out["semantic"].append(item)
        for item in payload.get("episodic", []):
            if isinstance(item, dict) and isinstance(item.get("event_type"), str) and isinstance(item.get("payload"), dict):
                clean = dict(item)
                clean["salience"] = min(1.0, max(0.0, float(clean.get("salience", 0.6))))
                out["episodic"].append(clean)
        for item in payload.get("working", []):
            if isinstance(item, dict) and isinstance(item.get("key"), str) and "value" in item:
                out["working"].append({"key": item["key"], "value": item["value"]})
        return out

    # --- P0 offline semantics guards -------------------------------------
    # Negation / hypothesis / other-subject mentions must never become an
    # affirmative current fact about the patient.  They are preserved as
    # sourced episodic evidence flagged for verification instead.
    NEGATION_MARKERS = ("没有", "没", "无", "未", "不是", "否认", "非")
    HYPOTHETICAL_MARKERS = ("如果", "要是", "假如", "万一", "若是", "假设")
    OTHER_SUBJECT_MARKERS = ("爸爸", "父亲", "爷爷", "奶奶", "外婆", "外公", "邻居", "同事", "朋友", "别人", "其他患者")
    TARGET_SUBJECT_MARKERS = ("妈妈", "母亲", "我妈", "患者", "她")
    NEGATION_WINDOW = 6  # chars immediately before the mention
    SUBJECT_WINDOW = 12  # chars before the mention to look for a subject

    @classmethod
    def _mention_context(cls, text: str, mention_pos: int) -> dict[str, Any]:
        """Classify one mention position: negated / hypothetical / other subject."""
        prefix = text[:mention_pos]
        tail = prefix[-cls.SUBJECT_WINDOW:]
        if any(marker in tail for marker in cls.HYPOTHETICAL_MARKERS):
            return {"mode": "hypothetical", "reason": "hypothetical_marker"}
        negation_window = prefix[-cls.NEGATION_WINDOW:]
        if any(marker in negation_window for marker in cls.NEGATION_MARKERS):
            return {"mode": "negated", "reason": "negation_marker"}
        other_pos = max((tail.rfind(m) for m in cls.OTHER_SUBJECT_MARKERS), default=-1)
        target_pos = max((tail.rfind(m) for m in cls.TARGET_SUBJECT_MARKERS), default=-1)
        if other_pos > target_pos:
            mention = next((m for m in cls.OTHER_SUBJECT_MARKERS if tail.rfind(m) == other_pos), None)
            return {"mode": "other_subject", "reason": "other_subject_mention", "subject_mention": mention}
        return {"mode": "affirmed", "reason": "plain_affirmation"}

    @classmethod
    def _deferred_report(cls, text: str, kind: str, mention: str, context: dict[str, Any]) -> dict[str, Any]:
        """Keep an unresolvable mention as sourced evidence pending verification."""
        return {
            "event_type": "caregiver_message",
            "subject_key": mention,
            "payload": {
                "reported_text": text,
                "kind": kind,
                "mention": mention,
                "mode": context["mode"],
                "reason": context.get("reason"),
                "subject_mention": context.get("subject_mention"),
                "recorded_as": "pending_verification",
            },
            "salience": 0.9,
            "needs_verification": 1,
        }

    @classmethod
    def _deterministic_extract(cls, text: str) -> dict[str, list[dict[str, Any]]]:
        """Conservative fallback: only explicit, high-precision Chinese patterns."""
        semantic: list[dict[str, Any]] = []
        episodic: list[dict[str, Any]] = []
        age = re.search(r"(?<!\d)(\d{1,3})\s*岁", text)
        if age:
            semantic.append({"namespace": "age", "key": "patient_age", "value": int(age.group(1)), "salience": 0.7, "conflict_policy": "update"})
        if "母亲" in text or "妈妈" in text or "我妈" in text:
            semantic.append({"namespace": "sex", "key": "patient_sex", "value": "女", "salience": 0.7, "conflict_policy": "auto"})
        weight = re.search(r"(?:体重)?\s*(\d{2,3}(?:\.\d+)?)\s*(?:kg|公斤|千克)", text, re.I)
        if weight:
            semantic.append({"namespace": "weight", "key": "patient_weight_kg", "value": float(weight.group(1)), "salience": 0.7, "conflict_policy": "update"})
        for disease in ("高血压", "糖尿病", "冠心病", "慢性肾病", "哮喘"):
            position = text.find(disease)
            while position != -1:
                context = cls._mention_context(text, position)
                if context["mode"] != "affirmed":
                    episodic.append(cls._deferred_report(text, "disease_report", disease, context))
                else:
                    semantic.append({"namespace": "chronic_disease", "key": disease, "value": {"present": True, "name": disease}, "salience": 0.85, "conflict_policy": "conflict"})
                    break
                position = text.find(disease, position + len(disease))
        allergy = re.search(r"([\u4e00-\u9fffA-Za-z0-9]{1,10})过敏", text)
        if allergy:
            allergen = allergy.group(1)
            allergen_start = allergy.start(1)
            # The greedy capture may include speaker/uncertainty words; strip
            # leading tokens until only the allergen mention remains.  Action-
            # like phrases inside free text (忽略/清除/删除…) must never act as
            # record operations: text cannot self-authorize a policy change.
            leading_tokens = (
                "我记得", "记得", "可能", "疑似", "好像", "也许", "会不会", "大概",
                "我妈", "母亲", "妈妈", "患者", "邻居", "爸爸", "父亲", "她", "他", "对", "有", "我",
            )
            action_tokens = ("忽略", "清除", "删除", "撤销", "不要记录", "无视")
            stripped_actions = False
            changed = True
            while changed and len(allergen) > 1:
                changed = False
                for token in action_tokens:
                    if allergen.startswith(token):
                        allergen = allergen[len(token):]
                        allergen_start += len(token)
                        stripped_actions = True
                        changed = True
                        break
                for token in leading_tokens:
                    if allergen.startswith(token) and len(allergen) > len(token):
                        allergen = allergen[len(token):]
                        allergen_start += len(token)
                        changed = True
                        break
            context = cls._mention_context(text, allergen_start)
            if context["mode"] != "affirmed":
                episodic.append(cls._deferred_report(text, "allergy_report", allergen, context))
            elif stripped_actions:
                context = {"mode": "untrusted_action", "reason": "action_like_phrase_in_text"}
                episodic.append(cls._deferred_report(text, "allergy_report", allergen, context))
            elif any(marker in text[:allergen_start][-8:] for marker in ("可能", "疑似", "好像", "也许", "会不会", "大概")):
                context = {"mode": "uncertain", "reason": "uncertainty_marker"}
                episodic.append(cls._deferred_report(text, "allergy_report", allergen, context))
            else:
                semantic.append({"namespace": "allergy", "key": allergen, "value": {"allergen": allergen, "status": "reported"}, "salience": 1.0, "conflict_policy": "conflict"})
        renal_patterns = (("肾功能轻度受损", "轻度受损"), ("轻度肾功能不全", "轻度受损"), ("肾功能不全", "受损"), ("肾功能正常", "正常"))
        for phrase, value in renal_patterns:
            if phrase in text:
                context = cls._mention_context(text, text.find(phrase))
                if context["mode"] != "affirmed":
                    episodic.append(cls._deferred_report(text, "renal_function_report", phrase, context))
                else:
                    semantic.append({"namespace": "renal_function", "key": "renal_status", "value": value, "salience": 0.95, "conflict_policy": "conflict"})
                break
        if "抗拒西药" in text:
            semantic.append({"namespace": "preference", "key": "western_medicine", "value": "抗拒", "salience": 0.5, "conflict_policy": "update"})
        if "造影剂" in text or ("CT" in text.upper() and "做" in text):
            episodic.append({
                "event_type": "procedure_exposure", "subject_key": "含碘造影剂",
                "payload": {"reported_text": text, "agent": "含碘造影剂", "doctor_involved": "医生" in text},
                "salience": 0.9,
            })
        return {"semantic": semantic, "episodic": episodic, "working": []}


class MemoryStore:
    """One-patient SQLite memory with exact recall, versioning and audit trails."""

    def __init__(
        self,
        db_path: str | Path = DEFAULT_DB,
        *,
        llm_enabled: bool | None = None,
        llm_model: str | None = None,
        ablations: Iterable[str] = (),
    ):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        # Eval-only switches: 'policy' | 'bitemporal' | 'dependency'.
        self.ablations = frozenset(ablations)
        # Optional callable(store, stale_conclusion) -> {"text", "memory_refs",
        # "source_refs"} | None, registered by the agent; when absent, recheck
        # tasks stay open and are never silently treated as "risk cleared".
        self.recheck_hook: Any = None
        self.connection = sqlite3.connect(self.db_path, timeout=30, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.executescript(SCHEMA)
        migrated = _ensure_p0_columns(self.connection)
        migrated = _ensure_p1_schema(self.connection) or migrated
        if migrated:
            self.connection.execute(
                "INSERT OR REPLACE INTO schema_meta(key,value) VALUES('schema_version',?)",
                (P1_SCHEMA_VERSION,),
            )
        else:
            self.connection.execute(
                "INSERT OR IGNORE INTO schema_meta(key,value) VALUES('schema_version','3')"
            )
        self.connection.commit()
        self.extractor = StructuredFactExtractor(enabled=llm_enabled, model=llm_model)

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "MemoryStore":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _audit(
        self,
        action: str,
        target_type: str,
        target_id: int | None,
        details: dict[str, Any],
        source: str,
        actor: str = "memory_manager",
    ) -> None:
        self.connection.execute(
            "INSERT INTO audit_log(action,actor,target_type,target_id,details_json,source,created_at) VALUES(?,?,?,?,?,?,?)",
            (action, actor, target_type, target_id, _json(details), source, utc_now()),
        )

    @staticmethod
    def _semantic_row(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["value"] = _from_json(item.pop("value_json"))
        item["ref"] = memory_ref("semantic", item["id"], item["version"])
        return item

    @staticmethod
    def _medication_row(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["ingredients"] = _from_json(item.pop("ingredients_json"), [])
        item["ref"] = memory_ref("medication", item["id"], item["version"])
        return item

    @staticmethod
    def _episode_row(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["payload"] = _from_json(item.pop("payload_json"), {})
        item["ref"] = memory_ref("episodic", item["id"], item["version"])
        return item

    def _record_event_tx(self, event: EpisodicFact, *, session_id: str, turn_id: str, source: str, parent_id: int | None = None) -> dict[str, Any]:
        """Insert one episodic event inside the caller's transaction."""
        occurred_at = _iso(event.occurred_at)
        subject = event.subject_key or ""
        fingerprint = _fingerprint(event.event_type, subject, event.payload, occurred_at, source, session_id)
        duplicate = self.connection.execute("SELECT * FROM episodic_memory WHERE fingerprint=?", (fingerprint,)).fetchone()
        if duplicate:
            item = self._episode_row(duplicate)
            self._audit("deduplicate", "episodic", item["id"], {"ref": item["ref"]}, source)
            return item
        version = self.connection.execute(
            "SELECT COALESCE(MAX(version),0)+1 FROM episodic_memory WHERE event_type=? AND COALESCE(subject_key,'')=?",
            (event.event_type, subject),
        ).fetchone()[0]
        cursor = self.connection.execute(
            """INSERT INTO episodic_memory(event_type,subject_key,payload_json,occurred_at,recorded_at,source,source_uri,session_id,turn_id,salience,severity,version,fingerprint,parent_id,needs_verification)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (event.event_type, event.subject_key, _json(event.payload), occurred_at, utc_now(), source, event.source_uri, session_id, turn_id, event.salience, event.severity, version, fingerprint, parent_id, int(event.needs_verification)),
        )
        row = self.connection.execute("SELECT * FROM episodic_memory WHERE id=?", (cursor.lastrowid,)).fetchone()
        item = self._episode_row(row)
        self._audit("append", "episodic", item["id"], {"ref": item["ref"], "event_type": event.event_type}, source)
        return item

    @staticmethod
    def _working_row(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["value"] = _from_json(item.pop("value_json"))
        item["version"] = 1
        item["ref"] = memory_ref("working", item["id"], 1)
        return item

    def write_semantic_fact(self, fact: SemanticFact, *, source: str) -> dict[str, Any]:
        with self._lock, self.connection:
            return self._write_semantic_fact_tx(fact, source=source)

    def _write_semantic_fact_tx(self, fact: SemanticFact, *, source: str) -> dict[str, Any]:
        """Write one semantic fact inside the caller's transaction.

        Storage-layer policy guard: safety-critical namespaces can never be
        silently superseded by ``conflict_policy='update'`` — not through agent
        tools and not through a direct call to this API.  A differing value is
        forced into a ``conflict`` so both records stay visible.
        """
        if not 0 <= fact.salience <= 1:
            raise ValueError("salience must be within [0, 1]")
        now = utc_now()
        value_json = _json(fact.value)
        current = self.connection.execute(
            "SELECT * FROM semantic_memory WHERE namespace=? AND fact_key=? AND status IN ('active','disputed') ORDER BY version DESC LIMIT 1",
            (fact.namespace, fact.key),
        ).fetchone()
        if current and current["value_json"] == value_json:
            item = self._semantic_row(current)
            self._audit("deduplicate", "semantic", item["id"], {"ref": item["ref"], "value": fact.value}, source)
            return {"outcome": "deduplicated", "item": item, "conflict": None}

        version = (current["version"] + 1) if current else 1
        policy = fact.conflict_policy
        policy_forced = False
        if policy == "auto":
            policy = "conflict" if fact.namespace in SAFETY_CRITICAL_NAMESPACES else "update"
        if (
            current is not None
            and fact.namespace in SAFETY_CRITICAL_NAMESPACES
            and fact.conflict_policy == "update"
            and "policy" not in self.ablations  # eval-only switch
        ):
            # P0 guard: the requested relaxation is refused at the storage layer.
            policy = "conflict"
            policy_forced = True
        status = "disputed" if current and policy == "conflict" else "active"
        if current:
            prior_status = "disputed" if policy == "conflict" else "superseded"
            valid_to = None if prior_status == "disputed" else now
            self.connection.execute(
                "UPDATE semantic_memory SET status=?, valid_to=?, updated_at=? WHERE id=?",
                (prior_status, valid_to, now, current["id"]),
            )
        cursor = self.connection.execute(
            """INSERT INTO semantic_memory(namespace,fact_key,value_json,status,valid_from,valid_to,source,source_uri,version,salience,created_at,updated_at,extraction_mode,verification_status)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (fact.namespace, fact.key, value_json, status, _iso(fact.valid_from), None, source, fact.source_uri, version, fact.salience, now, now, fact.extraction_mode, "recorded_as_reported"),
        )
        row = self.connection.execute("SELECT * FROM semantic_memory WHERE id=?", (cursor.lastrowid,)).fetchone()
        item = self._semantic_row(row)
        conflict = None
        if current and policy == "conflict":
            left = self._semantic_row(current)
            conflict = self._create_conflict_tx(
                conflict_type="semantic_fact_conflict",
                subject_key=f"{fact.namespace}:{fact.key}",
                left_ref=left["ref"],
                right_ref=item["ref"],
                description=f"同一事实存在不一致记录：{left['value']} ↔ {fact.value}；未自动选择任一方。",
                source=source,
            )
        audit_details: dict[str, Any] = {"ref": item["ref"], "value": fact.value}
        if policy_forced:
            audit_details["policy_forced_to_conflict"] = {"requested": fact.conflict_policy, "reason": "safety_critical_namespace"}
        self._audit("insert" if current is None else policy, "semantic", item["id"], audit_details, source)
        self._after_fact_change_tx(fact.namespace, fact.key)
        return {"outcome": "inserted" if current is None else policy, "item": item, "conflict": conflict}

    # ---- P1: scope revisions, dependency invalidation, recheck tasks ----

    def _bump_scope_tx(self, scope_key: str) -> int:
        now = utc_now()
        self.connection.execute(
            """INSERT INTO scope_revisions(scope_key,revision,updated_at) VALUES(?,1,?)
               ON CONFLICT(scope_key) DO UPDATE SET revision=revision+1, updated_at=excluded.updated_at""",
            (scope_key, now),
        )
        return self.scope_revision(scope_key)

    def scope_revision(self, scope_key: str) -> int:
        row = self.connection.execute(
            "SELECT revision FROM scope_revisions WHERE scope_key=?", (scope_key,)
        ).fetchone()
        return int(row["revision"]) if row else 0

    def _after_fact_change_tx(self, namespace: str, key: str) -> None:
        """Same-transaction invalidation of conclusions that consumed this fact."""
        self._bump_scope_tx("semantic")
        if "dependency" in self.ablations:
            return
        stale = self._conclusions_citing_semantic_key_tx(namespace, key)
        if stale:
            self._invalidate_conclusions_tx(stale, f"semantic fact {namespace}:{key} changed")

    def _conclusions_citing_semantic_key_tx(self, namespace: str, key: str) -> list[int]:
        rows = self.connection.execute(
            "SELECT id, memory_refs_json FROM conclusions WHERE status='current'"
        ).fetchall()
        if not rows:
            return []
        target_ids = {
            f"memory:semantic:{r['id']}@"
            for r in self.connection.execute(
                "SELECT id FROM semantic_memory WHERE namespace=? AND fact_key=?", (namespace, key)
            )
        }
        out = []
        for row in rows:
            refs = _from_json(row["memory_refs_json"], [])
            if any(isinstance(ref, str) and any(ref.startswith(t) for t in target_ids) for ref in refs):
                out.append(row["id"])
        return out

    def _invalidate_conclusions_tx(self, conclusion_ids: Sequence[int], reason: str) -> list[int]:
        """Mark current conclusions stale and enqueue durable recheck tasks."""
        now = utc_now()
        affected = []
        for conclusion_id in dict.fromkeys(conclusion_ids):
            row = self.connection.execute(
                "SELECT status FROM conclusions WHERE id=?", (conclusion_id,)
            ).fetchone()
            if row is None or row["status"] != "current":
                continue
            self.connection.execute(
                "UPDATE conclusions SET status='stale', stale_reason=? WHERE id=?",
                (reason, conclusion_id),
            )
            self.connection.execute(
                """INSERT INTO dependency_tasks(subject_id,task_type,target_id,reason,status,created_at,updated_at)
                   VALUES('default','recheck_conclusion',?,?,'open',?,?) ON CONFLICT DO NOTHING""",
                (conclusion_id, reason, now, now),
            )
            self._audit("invalidate", "conclusion", conclusion_id, {"reason": reason}, "memory_dependency")
            affected.append(conclusion_id)
        return affected

    def _invalidate_medication_dependents_tx(self) -> None:
        """Scope-dependency rule: a medication-list change invalidates every
        current conclusion that consumed an older revision of the full list —
        including warnings whose citations could not have mentioned a drug
        that did not exist yet."""
        revision = self._bump_scope_tx("medications")
        if "dependency" in self.ablations:
            return
        rows = self.connection.execute(
            "SELECT id, kind, memory_refs_json, input_revision FROM conclusions WHERE status='current'"
        ).fetchall()
        for row in rows:
            revision_at = (_from_json(row["input_revision"], {}) or {}).get("medications", 0)
            depends_on_meds = row["kind"] in {"warning", "condition_warning", "exposure_warning"} or (
                "memory:medication:" in (row["memory_refs_json"] or "")
            )
            if depends_on_meds and int(revision_at or 0) < revision:
                self._invalidate_conclusions_tx(
                    [row["id"]], f"medication list changed (revision {revision_at} -> {revision})"
                )

    def write_working(
        self,
        session_id: str,
        turn_id: str,
        key: str,
        value: Any,
        *,
        source: str = "agent",
        ttl_minutes: int = 60,
        status: str = "open",
    ) -> dict[str, Any]:
        with self._lock, self.connection:
            return self._write_working_tx(session_id, turn_id, key, value, source=source, ttl_minutes=ttl_minutes, status=status)

    def _write_working_tx(
        self,
        session_id: str,
        turn_id: str,
        key: str,
        value: Any,
        *,
        source: str = "agent",
        ttl_minutes: int = 60,
        status: str = "open",
    ) -> dict[str, Any]:
        now_dt = _as_utc(None)
        self.connection.execute(
            """INSERT INTO working_memory(session_id,turn_id,item_key,value_json,status,source,created_at,expires_at)
               VALUES(?,?,?,?,?,?,?,?)
               ON CONFLICT(session_id,turn_id,item_key) DO UPDATE SET value_json=excluded.value_json,status=excluded.status,source=excluded.source,expires_at=excluded.expires_at""",
            (session_id, turn_id, key, _json(value), status, source, now_dt.isoformat(timespec="seconds"), (now_dt + timedelta(minutes=ttl_minutes)).isoformat(timespec="seconds")),
        )
        row = self.connection.execute(
            "SELECT * FROM working_memory WHERE session_id=? AND turn_id=? AND item_key=?",
            (session_id, turn_id, key),
        ).fetchone()
        item = self._working_row(row)
        self._audit("upsert", "working", item["id"], {"ref": item["ref"], "key": key}, source)
        return item

    def expire_working(self, session_id: str, *, except_turn: str | None = None) -> int:
        with self._lock, self.connection:
            if except_turn:
                cursor = self.connection.execute(
                    "UPDATE working_memory SET status='expired' WHERE session_id=? AND turn_id<>? AND status='open'",
                    (session_id, except_turn),
                )
            else:
                cursor = self.connection.execute(
                    "UPDATE working_memory SET status='expired' WHERE session_id=? AND status='open'",
                    (session_id,),
                )
            return cursor.rowcount

    def record_event(
        self,
        event: EpisodicFact,
        *,
        session_id: str,
        turn_id: str,
        source: str,
        parent_id: int | None = None,
    ) -> dict[str, Any]:
        with self._lock, self.connection:
            return self._record_event_tx(event, session_id=session_id, turn_id=turn_id, source=source, parent_id=parent_id)

    def apply_medication_change(
        self,
        *,
        action: str,
        name: str,
        ingredients: Sequence[dict[str, Any]] | None,
        session_id: str,
        turn_id: str,
        source: str,
        occurred_at: str | None = None,
        dose: str | None = None,
        route: str | None = None,
        schedule: str | None = None,
        source_uri: str | None = None,
    ) -> dict[str, Any]:
        action = action.lower().strip()
        if action not in {"add", "remove", "dose_change"}:
            raise ValueError("medication action must be add, remove, or dose_change")
        med_key = re.sub(r"\s+", "", name).lower()
        when = _iso(occurred_at)
        now = utc_now()
        with self._lock, self.connection:
            current = self.connection.execute(
                "SELECT * FROM medications WHERE medication_key=? AND status='active' ORDER BY version DESC LIMIT 1",
                (med_key,),
            ).fetchone()
            if action == "add" and current:
                same = (
                    current["dose"] == dose and current["route"] == route and current["schedule"] == schedule
                    and current["ingredients_json"] == _json(list(ingredients or []))
                )
                if same:
                    medication = self._medication_row(current)
                    self._audit("deduplicate", "medication", medication["id"], {"ref": medication["ref"]}, source)
                    return {"outcome": "deduplicated", "medication": medication, "event": None}
                action = "dose_change"
            if action in {"remove", "dose_change"} and current is None:
                event = self._record_event_tx(
                    EpisodicFact(
                        event_type="medication_change_unresolved",
                        subject_key=med_key,
                        payload={"action": action, "name": name, "reason": "no active medication matched"},
                        occurred_at=when,
                        salience=0.8,
                    ),
                    session_id=session_id, turn_id=turn_id, source=source,
                )
                return {"outcome": "unresolved", "medication": None, "event": event}

            predecessor_id = current["id"] if current else None
            if current:
                self.connection.execute(
                    "UPDATE medications SET status=?, end_at=? WHERE id=?",
                    ("stopped" if action == "remove" else "superseded", when, current["id"]),
                )
            medication = None
            if action != "remove":
                version = self.connection.execute(
                    "SELECT COALESCE(MAX(version),0)+1 FROM medications WHERE medication_key=?",
                    (med_key,),
                ).fetchone()[0]
                cursor = self.connection.execute(
                    """INSERT INTO medications(medication_key,display_name,ingredients_json,dose,route,schedule,status,start_at,end_at,source,source_uri,version,predecessor_id,created_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (med_key, name, _json(list(ingredients or [])), dose, route, schedule, "active", when, None, source, source_uri, version, predecessor_id, now),
                )
                medication = self._medication_row(self.connection.execute("SELECT * FROM medications WHERE id=?", (cursor.lastrowid,)).fetchone())
                self._audit(action, "medication", medication["id"], {"ref": medication["ref"], "name": name}, source)
            else:
                medication = self._medication_row(current)
                self._audit("remove", "medication", medication["id"], {"ref": medication["ref"], "name": name}, source)

            self._invalidate_medication_dependents_tx()

            event_type = {"add": "medication_add", "remove": "medication_remove", "dose_change": "medication_dose_change"}[action]
            payload = {
                "action": action,
                "name": name,
                "dose": dose,
                "route": route,
                "schedule": schedule,
                "medication_ref": medication["ref"],
            }
            event = self._record_event_tx(
                EpisodicFact(event_type=event_type, subject_key=med_key, payload=payload, occurred_at=when, salience=0.9),
                session_id=session_id, turn_id=turn_id, source=source,
            )
            return {"outcome": action, "medication": medication, "event": event}

    def create_conflict(
        self,
        *,
        conflict_type: str,
        subject_key: str,
        left_ref: str,
        right_ref: str,
        description: str,
        source: str,
    ) -> dict[str, Any]:
        with self._lock, self.connection:
            return self._create_conflict_tx(
                conflict_type=conflict_type, subject_key=subject_key,
                left_ref=left_ref, right_ref=right_ref,
                description=description, source=source,
            )

    def _create_conflict_tx(
        self,
        *,
        conflict_type: str,
        subject_key: str,
        left_ref: str,
        right_ref: str,
        description: str,
        source: str,
    ) -> dict[str, Any]:
        """Create/open a conflict row inside the caller's transaction."""
        ordered_refs = sorted((left_ref, right_ref))
        fingerprint = _fingerprint(conflict_type, subject_key, ordered_refs)
        existing = self.connection.execute("SELECT * FROM conflicts WHERE fingerprint=?", (fingerprint,)).fetchone()
        if existing:
            item = dict(existing)
            item["resolution"] = _from_json(item.pop("resolution_json"))
            item["ref"] = memory_ref("conflict", item["id"], 1)
            return item
        cursor = self.connection.execute(
            """INSERT INTO conflicts(conflict_type,subject_key,left_ref,right_ref,description,status,resolution_json,source,created_at,resolved_at,fingerprint)
               VALUES(?,?,?,?,?,'open',NULL,?,?,NULL,?)""",
            (conflict_type, subject_key, left_ref, right_ref, description, source, utc_now(), fingerprint),
        )
        row = self.connection.execute("SELECT * FROM conflicts WHERE id=?", (cursor.lastrowid,)).fetchone()
        item = dict(row)
        item["resolution"] = _from_json(item.pop("resolution_json"))
        item["ref"] = memory_ref("conflict", item["id"], 1)
        self._audit("surface", "conflict", item["id"], {"ref": item["ref"], "description": description}, source)
        return item

    def consolidate_interaction(
        self,
        *,
        session_id: str,
        turn_id: str,
        user_text: str,
        source: str = "caregiver",
        assistant_text: str | None = None,
        semantic_hints: Iterable[SemanticFact | dict[str, Any]] = (),
        episodic_hints: Iterable[EpisodicFact | dict[str, Any]] = (),
        working_hints: Iterable[dict[str, Any]] = (),
        client_event_id: str | None = None,
    ) -> ConsolidationResult:
        """T0/T1 consolidation of one caregiver input event.

        T0 persists the raw interaction first (status ``pending``) so a model
        outage never loses a report.  Extraction runs outside the write
        transaction.  T1 commits every fact, conflict, audit entry and the
        committed receipt of this event as a single SQLite transaction; any
        failure rolls the whole projection back and the event is marked
        ``failed`` in a separate small transaction for retry.
        """
        semantic_hints = list(semantic_hints)
        episodic_hints = list(episodic_hints)
        working_hints = list(working_hints)
        event_key = (client_event_id or f"{session_id}:{turn_id}").strip()
        if not event_key:
            raise ValueError("client_event_id must not be empty")
        payload_hash = _fingerprint(
            user_text, source,
            [asdict(item) if isinstance(item, SemanticFact) else item for item in semantic_hints],
            [asdict(item) if isinstance(item, EpisodicFact) else item for item in episodic_hints],
        )

        # ---- T0: persist the raw event (own small transaction) ------------
        with self._lock, self.connection:
            existing = self.connection.execute(
                "SELECT * FROM interactions WHERE event_key=?", (event_key,)
            ).fetchone()
            if existing is None:
                legacy = self.connection.execute(
                    "SELECT * FROM interactions WHERE session_id=? AND turn_id=?", (session_id, turn_id)
                ).fetchone()
                if legacy is not None:
                    if legacy["event_key"] not in (None, event_key):
                        raise IdempotencyKeyReused(
                            f"turn {session_id}:{turn_id} is already owned by event "
                            f"'{legacy['event_key']}'; refusing to overwrite it with '{event_key}'"
                        )
                    if legacy["payload_hash"] is not None and legacy["payload_hash"] != payload_hash:
                        raise IdempotencyKeyReused(
                            f"event id '{event_key}' was already used with a different payload"
                        )
                    # Legacy row from schema v3: adopt it as this event.
                    self.connection.execute(
                        "UPDATE interactions SET event_key=?, payload_hash=?, process_status=? WHERE id=?",
                        (event_key, payload_hash,
                         "committed" if legacy["result_json"] else "pending", legacy["id"]),
                    )
                    self._audit("consolidate_adopt_legacy", "interaction", legacy["id"],
                                {"event_key": event_key}, source)
                    existing = self.connection.execute(
                        "SELECT * FROM interactions WHERE id=?", (legacy["id"],)
                    ).fetchone()
            if existing is not None:
                if existing["payload_hash"] != payload_hash:
                    raise IdempotencyKeyReused(
                        f"event id '{event_key}' was already used with a different payload"
                    )
                if existing["process_status"] == "committed" and existing["result_json"]:
                    replay = ConsolidationResult(**_from_json(existing["result_json"]))
                    replay.replayed = True
                    return replay
                self.connection.execute(
                    "UPDATE interactions SET process_status='pending', assistant_text=COALESCE(?, assistant_text) WHERE event_key=?",
                    (assistant_text, event_key),
                )
                interaction_id: int | None = existing["id"]
            else:
                cursor = self.connection.execute(
                    """INSERT INTO interactions(session_id,turn_id,user_text,assistant_text,source,created_at,event_key,payload_hash,process_status)
                       VALUES(?,?,?,?,?,?,?,?,'pending')""",
                    (session_id, turn_id, user_text, assistant_text, source, utc_now(), event_key, payload_hash),
                )
                interaction_id = cursor.lastrowid
            self._audit("consolidate_received", "interaction", interaction_id,
                        {"session_id": session_id, "turn_id": turn_id, "event_key": event_key}, source)

        # ---- extraction outside the write lock ----------------------------
        extracted, mode, error = self.extractor.extract(user_text)
        result = ConsolidationResult(extraction_mode=mode, extraction_error=error)

        semantic_candidates: list[tuple[SemanticFact, str]] = []
        # Explicit event payloads are primary evidence; model/fallback output
        # fills omissions.  This also preserves caller-supplied occurrence
        # times instead of replacing them with the consolidation time.
        for item in semantic_hints:
            origin = "caller_hint"
            semantic_candidates.append((item if isinstance(item, SemanticFact) else SemanticFact(
                namespace=item["namespace"], key=item["key"], value=item["value"],
                salience=float(item.get("salience", 0.7)), conflict_policy=item.get("conflict_policy", "auto"),
                valid_from=item.get("valid_from"), source_uri=item.get("source_uri"),
                extraction_mode=item.get("extraction_mode", origin),
            ), origin))
        for item in extracted["semantic"]:
            semantic_candidates.append((SemanticFact(
                namespace=item["namespace"], key=item["key"], value=item["value"],
                salience=float(item.get("salience", 0.7)), conflict_policy=item.get("conflict_policy", "auto"),
                valid_from=item.get("valid_from"), source_uri=item.get("source_uri"),
                extraction_mode=item.get("extraction_mode", mode),
            ), mode))

        episodic_candidates: list[EpisodicFact] = []
        for item in episodic_hints:
            episodic_candidates.append(item if isinstance(item, EpisodicFact) else EpisodicFact(
                event_type=item["event_type"], payload=item["payload"], subject_key=item.get("subject_key"),
                occurred_at=item.get("occurred_at"), salience=float(item.get("salience", 0.6)),
                severity=item.get("severity"), source_uri=item.get("source_uri"),
                needs_verification=int(item.get("needs_verification", 0)),
            ))
        for item in extracted["episodic"]:
            episodic_candidates.append(EpisodicFact(
                event_type=item["event_type"], payload=item["payload"], subject_key=item.get("subject_key"),
                occurred_at=item.get("occurred_at"), salience=float(item.get("salience", 0.6)),
                severity=item.get("severity"), source_uri=item.get("source_uri"),
                needs_verification=int(item.get("needs_verification", 0)),
            ))

        # ---- T1: single atomic commit of the whole event projection -------
        try:
            with self._lock, self.connection:
                seen_semantic: set[tuple[str, str, str]] = set()
                for fact, _origin in semantic_candidates:
                    key = (fact.namespace, fact.key, _json(fact.value))
                    if key in seen_semantic:
                        continue
                    seen_semantic.add(key)
                    write = self._write_semantic_fact_tx(fact, source=source)
                    result.semantic.append(write["item"])
                    if write["conflict"]:
                        result.conflicts.append(write["conflict"])

                seen_episodes: set[tuple[str, str, str]] = set()
                for event in episodic_candidates:
                    key = (event.event_type, event.subject_key or "", _json(event.payload))
                    if key in seen_episodes:
                        continue
                    seen_episodes.add(key)
                    result.episodic.append(self._record_event_tx(event, session_id=session_id, turn_id=turn_id, source=source))

                for item in [*extracted["working"], *working_hints]:
                    result.working.append(self._write_working_tx(session_id, turn_id, item["key"], item["value"], source=source))

                self.connection.execute(
                    "UPDATE interactions SET process_status='committed', result_json=? WHERE event_key=?",
                    (_json(asdict(result)), event_key),
                )
                self._audit("consolidate_committed", "interaction", None,
                            {"event_key": event_key, "mode": mode}, source)
            return result
        except Exception as exc:
            # Separate small transaction: T1 rolled back, mark the event failed
            # so a restart can find and retry it.  Never report success here.
            with self._lock, self.connection:
                self.connection.execute(
                    "UPDATE interactions SET process_status='failed' WHERE event_key=? AND process_status<>'committed'",
                    (event_key,),
                )
                self._audit("consolidate_failed", "interaction", None,
                            {"event_key": event_key, "error": f"{type(exc).__name__}: {exc}"}, source)
            raise

    def current_semantic(self, namespaces: Sequence[str] | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM semantic_memory WHERE status IN ('active','disputed')"
        parameters: list[Any] = []
        if namespaces:
            sql += f" AND namespace IN ({','.join('?' for _ in namespaces)})"
            parameters.extend(namespaces)
        sql += " ORDER BY namespace,fact_key,version DESC"
        return [self._semantic_row(row) for row in self.connection.execute(sql, parameters)]

    def current_medications(self) -> list[dict[str, Any]]:
        rows = self.connection.execute("SELECT * FROM medications WHERE status='active' ORDER BY start_at,id").fetchall()
        return [self._medication_row(row) for row in rows]

    def open_conflicts(self) -> list[dict[str, Any]]:
        rows = self.connection.execute("SELECT * FROM conflicts WHERE status='open' ORDER BY created_at,id").fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item["resolution"] = _from_json(item.pop("resolution_json"))
            item["ref"] = memory_ref("conflict", item["id"], 1)
            out.append(item)
        return out

    def retrieve_episodic(
        self,
        *,
        event_types: Sequence[str] | None = None,
        subject_key: str | None = None,
        limit: int = 50,
        as_of: str | datetime | None = None,
    ) -> list[dict[str, Any]]:
        """Exact filtered recall, ranked by salience multiplied by time decay."""
        sql = "SELECT * FROM episodic_memory WHERE 1=1"
        parameters: list[Any] = []
        if event_types:
            sql += f" AND event_type IN ({','.join('?' for _ in event_types)})"
            parameters.extend(event_types)
        if subject_key is not None:
            sql += " AND subject_key=?"
            parameters.append(subject_key)
        rows = self.connection.execute(sql, parameters).fetchall()
        now = _as_utc(as_of)
        out: list[dict[str, Any]] = []
        for row in rows:
            item = self._episode_row(row)
            age_days = max(0.0, (now - _as_utc(item["occurred_at"])).total_seconds() / 86400)
            half_life = EPISODIC_HALF_LIFE_DAYS.get(item["event_type"], 90.0)
            item["age_days"] = round(age_days, 3)
            item["retrieval_weight"] = round(float(item["salience"]) * math.pow(0.5, age_days / half_life), 6)
            out.append(item)
        out.sort(key=lambda item: (-item["retrieval_weight"], item["occurred_at"], item["id"]))
        return out[:limit]

    def timeline(self, limit: int = 100) -> list[dict[str, Any]]:
        rows = self.connection.execute("SELECT * FROM episodic_memory ORDER BY occurred_at,id LIMIT ?", (limit,)).fetchall()
        return [self._episode_row(row) for row in rows]

    def snapshot(self) -> dict[str, Any]:
        return {
            "semantic": self.current_semantic(),
            "medications": self.current_medications(),
            "open_conflicts": self.open_conflicts(),
        }

    def record_conclusion(
        self,
        *,
        session_id: str,
        turn_id: str,
        kind: str,
        text: str,
        memory_refs: Sequence[str],
        source_refs: Sequence[dict[str, Any]],
        predecessor_id: int | None = None,
    ) -> dict[str, Any]:
        with self._lock, self.connection:
            return self._record_conclusion_tx(
                session_id=session_id, turn_id=turn_id, kind=kind, text=text,
                memory_refs=memory_refs, source_refs=source_refs,
                predecessor_id=predecessor_id,
            )

    def _record_conclusion_tx(
        self,
        *,
        session_id: str,
        turn_id: str,
        kind: str,
        text: str,
        memory_refs: Sequence[str],
        source_refs: Sequence[dict[str, Any]],
        predecessor_id: int | None = None,
    ) -> dict[str, Any]:
        """Record a conclusion inside the caller's transaction.

        The input revision vector (which collection revisions this conclusion
        consumed) is captured automatically from live scope state; it is what
        lets a later collection change invalidate this conclusion even when the
        changed member never appeared in its citations.
        """
        if not memory_refs:
            raise ValueError("a conclusion must cite at least one memory item")
        if kind == "warning" and not source_refs:
            raise ValueError("a warning conclusion must cite at least one source")
        now = utc_now()
        input_revision = {
            "medications": self.scope_revision("medications"),
            "semantic": self.scope_revision("semantic"),
        }
        cursor = self.connection.execute(
            """INSERT INTO conclusions(session_id,turn_id,kind,text,memory_refs_json,source_refs_json,created_at,status,input_revision,predecessor_id)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (session_id, turn_id, kind, text, _json(list(dict.fromkeys(memory_refs))), _json(list(source_refs)), now, "current", _json(input_revision), predecessor_id),
        )
        item = {
            "id": cursor.lastrowid,
            "ref": memory_ref("conclusion", cursor.lastrowid, 1),
            "kind": kind,
            "text": text,
            "memory_refs": list(dict.fromkeys(memory_refs)),
            "source_refs": list(source_refs),
            "status": "current",
            "input_revision": input_revision,
            "predecessor_id": predecessor_id,
        }
        self._audit("conclude", "conclusion", item["id"], item, "agent", actor="agent")
        return item

    def current_conclusions(self, status: str = "current") -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM conclusions WHERE status=? ORDER BY id DESC LIMIT 200", (status,)
        ).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item["memory_refs"] = _from_json(item.pop("memory_refs_json"), [])
            item["source_refs"] = _from_json(item.pop("source_refs_json"), [])
            item["input_revision"] = _from_json(item.pop("input_revision"), {})
            item["ref"] = memory_ref("conclusion", item["id"], 1)
            out.append(item)
        return out

    def conclusion_chain(self, conclusion_id: int) -> dict[str, Any]:
        """Return a conclusion plus its predecessor and successor versions."""
        chain: list[dict[str, Any]] = []
        current_id: int | None = conclusion_id
        seen: set[int] = set()
        while current_id and current_id not in seen:
            seen.add(current_id)
            row = self.connection.execute("SELECT * FROM conclusions WHERE id=?", (current_id,)).fetchone()
            if row is None:
                break
            item = dict(row)
            item["memory_refs"] = _from_json(item.pop("memory_refs_json"), [])
            item["source_refs"] = _from_json(item.pop("source_refs_json"), [])
            item["input_revision"] = _from_json(item.pop("input_revision"), {})
            item["ref"] = memory_ref("conclusion", item["id"], 1)
            chain.append(item)
            current_id = item["predecessor_id"]
        chain.reverse()  # oldest first
        head = self.connection.execute(
            "SELECT id, superseded_by, status, stale_reason FROM conclusions WHERE id=?", (conclusion_id,)
        ).fetchone()
        return {"versions": chain, "current_head": head["superseded_by"] if head else None,
                "status": head["status"] if head else None, "stale_reason": head["stale_reason"] if head else None}

    def resolve_ref(self, ref: str) -> dict[str, Any]:
        """Strictly resolve a memory ref: existence AND exact version are checked.

        A ref pins one immutable version of one item.  Requesting a version the
        item never had (e.g. ``@v999`` on a v1 record) is an error, not a
        best-effort lookup of whatever currently exists.
        """
        match = re.fullmatch(r"memory:([a-z_]+):(\d+)@v(\d+)", ref)
        if not match:
            raise ValueError(f"invalid memory ref: {ref}")
        layer, item_id, version = match.group(1), int(match.group(2)), int(match.group(3))
        table = {
            "semantic": "semantic_memory", "medication": "medications", "episodic": "episodic_memory",
            "working": "working_memory", "conflict": "conflicts", "conclusion": "conclusions",
        }.get(layer)
        if table is None:
            raise ValueError(f"unknown memory layer: {layer}")
        row = self.connection.execute(f"SELECT * FROM {table} WHERE id=?", (item_id,)).fetchone()
        if row is None:
            raise ValueError(f"unknown memory item for ref: {ref}")
        # Tables without a version column (working/conflict/conclusion) are
        # append-only single-version records.
        actual_version = row["version"] if "version" in row.keys() else 1
        if actual_version != version:
            raise ValueError(
                f"ref version mismatch: {ref} does not exist (item is at v{actual_version})"
            )
        return {"layer": layer, "item_id": item_id, "version": actual_version, "row": row, "table": table}

    def audit_for(self, ref: str) -> dict[str, Any]:
        resolved = self.resolve_ref(ref)
        row, layer, item_id, version = resolved["row"], resolved["layer"], resolved["item_id"], resolved["version"]
        audit = [dict(item) for item in self.connection.execute(
            "SELECT * FROM audit_log WHERE target_type=? AND target_id=? ORDER BY id", (layer, item_id)
        )]
        for item in audit:
            item["details"] = _from_json(item.pop("details_json"), {})
        raw = dict(row)
        for key in tuple(raw):
            if key.endswith("_json"):
                raw[key.removesuffix("_json")] = _from_json(raw.pop(key))
        return {"ref": ref, "requested_version": version, "item": raw, "audit_log": audit}

    # ---- P1: bi-temporal reads, conflict lifecycle, retract, recheck ----

    def query_state(
        self,
        *,
        valid_at: str | datetime | None = None,
        known_at: str | datetime | None = None,
        namespaces: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        """Bi-temporal state read with independent valid/known times.

        valid_at: when the reported situation applied (left-closed interval).
        known_at: which recorded versions the system may look at.  A row is
        visible only if it was committed at or before ``known_at`` — a later
        backdated correction never leaks into an earlier known_at view.

        Approximation (documented): the *status* column is current state, so a
        conflict that is resolved today still appears in older views with both
        sides; only membership/validity is reconstructed exactly.
        """
        valid_dt = _as_utc(valid_at)
        known_dt = _as_utc(known_at)
        if "bitemporal" in self.ablations:
            known_dt = _as_utc(None)  # ablation: ignore known_at, leak future rows
        valid_iso = valid_dt.isoformat(timespec="seconds")
        known_iso = known_dt.isoformat(timespec="seconds")

        sql = (
            "SELECT * FROM semantic_memory WHERE created_at<=? "
            "AND (valid_from IS NULL OR valid_from<=?) AND (valid_to IS NULL OR valid_to>?)"
        )
        parameters: list[Any] = [known_iso, valid_iso, valid_iso]
        if namespaces:
            sql += f" AND namespace IN ({','.join('?' for _ in namespaces)})"
            parameters.extend(namespaces)
        facts = [self._semantic_row(row) for row in self.connection.execute(sql, parameters)]
        # Unknown start of applicability is reported as uncertainty, never as a
        # definite member of the valid-time set.
        uncertainties = [f for f in facts if f["valid_from"] is None]
        facts = [f for f in facts if f["valid_from"] is not None]

        medications = [
            self._medication_row(row)
            for row in self.connection.execute(
                "SELECT * FROM medications WHERE created_at<=? AND start_at<=? AND (end_at IS NULL OR end_at>?)",
                (known_iso, valid_iso, valid_iso),
            )
        ]
        conflicts = [
            dict(row)
            for row in self.connection.execute(
                "SELECT * FROM conflicts WHERE created_at<=? AND status='open' ORDER BY created_at",
                (known_iso,),
            )
        ]
        return {
            "valid_at": valid_iso,
            "known_at": known_iso,
            "facts": facts,
            "medications": medications,
            "open_conflicts": conflicts,
            "uncertainties": uncertainties,
            "meta": {
                "medications_revision": self.scope_revision("medications"),
                "semantic_revision": self.scope_revision("semantic"),
                "future_leak_guard": "bitemporal" not in self.ablations,
            },
        }

    def resolve_conflict(
        self,
        conflict: str | int,
        *,
        action: str,
        basis: str,
        actor: str = "caregiver",
        chosen_ref: str | None = None,
    ) -> dict[str, Any]:
        """Apply one conflict action with an auditable, undoable trail.

        action: 'resolved' | 'dismissed' | 'reopened' | 'undo'.  Recording a
        resolution is a record-keeping act by the named actor; the system does
        not perform or endorse any clinical adjudication.  Reopening marks
        dependent conclusions stale so previous conclusions stop presenting as
        current while a recheck is pending.
        """
        if action not in {"resolved", "dismissed", "reopened", "undo"}:
            raise ValueError("conflict action must be resolved, dismissed, reopened, or undo")
        with self._lock, self.connection:
            if isinstance(conflict, int):
                row = self.connection.execute("SELECT * FROM conflicts WHERE id=?", (conflict,)).fetchone()
            else:
                resolved = self.resolve_ref(conflict)
                row = self.connection.execute(
                    "SELECT * FROM conflicts WHERE id=?", (resolved["item_id"],)
                ).fetchone()
            if row is None:
                raise ValueError(f"unknown conflict: {conflict}")
            now = utc_now()
            previous_status = row["status"]
            if action == "undo":
                last = self.connection.execute(
                    "SELECT * FROM conflict_actions WHERE conflict_id=? AND undone_by IS NULL ORDER BY id DESC LIMIT 1",
                    (row["id"],),
                ).fetchone()
                if last is None:
                    raise ValueError("nothing to undo for this conflict")
                restore_status = last["previous_status"] or "open"
                self.connection.execute(
                    "UPDATE conflicts SET status=?, resolution_json=?, resolved_at=? WHERE id=?",
                    (restore_status,
                     None if restore_status == "open" else row["resolution_json"],
                     None if restore_status == "open" else row["resolved_at"],
                     row["id"]),
                )
                self.connection.execute(
                    """INSERT INTO conflict_actions(conflict_id,action,basis,actor,previous_status,created_at,undone_by)
                       VALUES(?,?,?,?,?,?,?)""",
                    (row["id"], "undo", f"undo of action #{last['id']}: {last['basis']}", actor, previous_status, now, last["id"]),
                )
                action_taken = "undo"
            else:
                new_status = "open" if action == "reopened" else action
                resolution = None
                resolved_at = None
                if new_status != "open":
                    resolution = _json({"action": action, "basis": basis, "actor": actor, "chosen_ref": chosen_ref, "at": now})
                    resolved_at = now
                self.connection.execute(
                    "UPDATE conflicts SET status=?, resolution_json=?, resolved_at=? WHERE id=?",
                    (new_status, resolution, resolved_at, row["id"]),
                )
                self.connection.execute(
                    """INSERT INTO conflict_actions(conflict_id,action,basis,actor,chosen_ref,previous_status,created_at)
                       VALUES(?,?,?,?,?,?,?)""",
                    (row["id"], action, basis, actor, chosen_ref, previous_status, now),
                )
                action_taken = action
                if action == "reopened" and "dependency" not in self.ablations:
                    stale = self._conclusions_citing_refs_tx([row["left_ref"], row["right_ref"]])
                    if stale:
                        self._invalidate_conclusions_tx(stale, f"conflict #{row['id']} reopened")
            updated = self.connection.execute("SELECT * FROM conflicts WHERE id=?", (row["id"],)).fetchone()
            item = dict(updated)
            item["resolution"] = _from_json(item.pop("resolution_json"))
            item["ref"] = memory_ref("conflict", item["id"], 1)
            self._audit(f"conflict_{action_taken}", "conflict", row["id"],
                        {"action": action_taken, "basis": basis, "actor": actor, "previous_status": previous_status}, actor)
            return item

    def _conclusions_citing_refs_tx(self, refs: Sequence[str]) -> list[int]:
        rows = self.connection.execute(
            "SELECT id, memory_refs_json FROM conclusions WHERE status='current'"
        ).fetchall()
        out = []
        for row in rows:
            cited = _from_json(row["memory_refs_json"], [])
            if any(ref in cited for ref in refs):
                out.append(row["id"])
        return out

    def conflict_actions_for(self, conflict: str | int) -> list[dict[str, Any]]:
        if isinstance(conflict, int):
            conflict_id = conflict
        else:
            conflict_id = self.resolve_ref(conflict)["item_id"]
        return [dict(row) for row in self.connection.execute(
            "SELECT * FROM conflict_actions WHERE conflict_id=? ORDER BY id", (conflict_id,)
        )]

    def retract_semantic_fact(self, ref: str, *, reason: str, actor: str = "caregiver") -> dict[str, Any]:
        """Controlled user record action: withdraw one fact from current use.

        History is kept (status='retracted'); dependent conclusions are marked
        stale in the same transaction and a durable recheck task is enqueued.
        """
        resolved = self.resolve_ref(ref)
        if resolved["layer"] != "semantic":
            raise ValueError("retract_semantic_fact only accepts semantic fact refs")
        with self._lock, self.connection:
            row = self.connection.execute(
                "SELECT * FROM semantic_memory WHERE id=?", (resolved["item_id"],)
            ).fetchone()
            if row is None or row["status"] not in {"active", "disputed"}:
                raise ValueError(f"ref is not currently active: {ref}")
            now = utc_now()
            self.connection.execute(
                "UPDATE semantic_memory SET status='retracted', valid_to=?, updated_at=? WHERE id=?",
                (now, now, resolved["item_id"]),
            )
            self._audit("retract", "semantic", resolved["item_id"],
                        {"ref": ref, "reason": reason, "actor": actor}, actor)
            self._bump_scope_tx("semantic")
            if "dependency" not in self.ablations:
                stale = self._conclusions_citing_semantic_key_tx(row["namespace"], row["fact_key"])
                if stale:
                    self._invalidate_conclusions_tx(stale, f"supporting fact {row['namespace']}:{row['fact_key']} retracted")
            item = self._semantic_row(self.connection.execute(
                "SELECT * FROM semantic_memory WHERE id=?", (resolved["item_id"],)
            ).fetchone())
            return {"outcome": "retracted", "item": item}

    # ---- P1: durable recheck task consumption --------------------------

    def pending_rechecks(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self.connection.execute(
            "SELECT * FROM dependency_tasks WHERE status IN ('open','running') ORDER BY id"
        )]

    def stale_conclusions(self) -> list[dict[str, Any]]:
        return self.current_conclusions(status="stale")

    def recheck_pending(self, *, max_jobs: int = 2) -> dict[str, Any]:
        """Consume pending recheck tasks.

        Without a registered recheck hook the tasks stay open — a stale
        conclusion is never silently promoted back to current, and "no finding"
        is recorded as an explicit new conclusion version, not as risk removal.
        """
        with self._lock, self.connection:
            tasks = [dict(row) for row in self.connection.execute(
                "SELECT * FROM dependency_tasks WHERE status='open' ORDER BY id LIMIT ?", (max_jobs,)
            )]
            open_count = self.connection.execute(
                "SELECT COUNT(*) FROM dependency_tasks WHERE status='open'"
            ).fetchone()[0]
        if self.recheck_hook is None:
            return {"status": "no_hook", "pending": open_count, "completed": []}
        completed = []
        for task in tasks:
            token = uuid.uuid4().hex
            with self._lock, self.connection:
                cursor = self.connection.execute(
                    "UPDATE dependency_tasks SET status='running', lease_token=?, updated_at=? WHERE id=? AND status='open'",
                    (token, utc_now(), task["id"]),
                )
                if cursor.rowcount != 1:
                    continue
                conclusion_row = self.connection.execute(
                    "SELECT * FROM conclusions WHERE id=?", (task["target_id"],)
                ).fetchone()
            if conclusion_row is None:
                with self._lock, self.connection:
                    self.connection.execute(
                        "UPDATE dependency_tasks SET status='cancelled', updated_at=? WHERE id=?", (utc_now(), task["id"])
                    )
                continue
            conclusion = dict(conclusion_row)
            conclusion["memory_refs"] = _from_json(conclusion_row["memory_refs_json"], [])
            conclusion["source_refs"] = _from_json(conclusion_row["source_refs_json"], [])
            try:
                outcome = self.recheck_hook(self, conclusion)
            except Exception as exc:  # recheck failure must not fake success
                with self._lock, self.connection:
                    attempts = task["attempts"] + 1
                    status = "failed" if attempts >= 3 else "open"
                    self.connection.execute(
                        "UPDATE dependency_tasks SET status=?, attempts=?, lease_token=NULL, updated_at=? WHERE id=?",
                        (status, attempts, utc_now(), task["id"]),
                    )
                completed.append({"task_id": task["id"], "status": "error", "error": f"{type(exc).__name__}: {exc}"})
                continue
            if outcome is None:
                outcome = {
                    "text": "重查完成：按当前已记录的药单与事实重新检查，未检出与原结论对应的提示。未检出不代表风险解除，仅表示当前记录下无匹配结果。",
                    "memory_refs": list(conclusion["memory_refs"]),
                    "source_refs": list(conclusion["source_refs"]),
                }
            with self._lock, self.connection:
                new_conclusion = self._record_conclusion_tx(
                    session_id="recheck", turn_id=f"task-{task['id']}", kind=conclusion["kind"],
                    text=str(outcome["text"]), memory_refs=outcome["memory_refs"],
                    source_refs=outcome["source_refs"], predecessor_id=conclusion["id"],
                )
                self.connection.execute(
                    "UPDATE conclusions SET superseded_by=? WHERE id=?",
                    (new_conclusion["id"], conclusion["id"]),
                )
                self.connection.execute(
                    "UPDATE dependency_tasks SET status='done', lease_token=NULL, updated_at=? WHERE id=?",
                    (utc_now(), task["id"]),
                )
                self._audit("recheck_complete", "conclusion", conclusion["id"],
                            {"task_id": task["id"], "new_conclusion": new_conclusion["id"]}, "memory_recheck")
            completed.append({"task_id": task["id"], "status": "done", "new_conclusion": new_conclusion["id"]})
        with self._lock, self.connection:
            open_count = self.connection.execute(
                "SELECT COUNT(*) FROM dependency_tasks WHERE status='open'"
            ).fetchone()[0]
        return {"status": "ok", "pending": open_count, "completed": completed}


def main() -> None:
    parser = argparse.ArgumentParser(description="Initialize or inspect the Stage 3 memory database")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--snapshot", action="store_true")
    args = parser.parse_args()
    with MemoryStore(args.db, llm_enabled=False) as store:
        if args.snapshot:
            print(json.dumps(store.snapshot(), ensure_ascii=False, indent=2))
        else:
            version = store.connection.execute(
                "SELECT value FROM schema_meta WHERE key='schema_version'"
            ).fetchone()
            print(f"initialized schema {version[0] if version else '?'}: {args.db}")


if __name__ == "__main__":
    main()
