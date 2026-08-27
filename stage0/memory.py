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


@dataclass(frozen=True)
class EpisodicFact:
    event_type: str
    payload: dict[str, Any]
    subject_key: str | None = None
    occurred_at: str | None = None
    salience: float = 0.6
    severity: str | None = None
    source_uri: str | None = None


@dataclass
class ConsolidationResult:
    semantic: list[dict[str, Any]] = field(default_factory=list)
    episodic: list[dict[str, Any]] = field(default_factory=list)
    working: list[dict[str, Any]] = field(default_factory=list)
    conflicts: list[dict[str, Any]] = field(default_factory=list)
    extraction_mode: str = "deterministic"
    extraction_error: str | None = None

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
            enabled = os.getenv("MEMORY_ENABLE_LLM", "1").strip().lower() not in {"0", "false", "no", "off"}
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
            if disease in text:
                semantic.append({"namespace": "chronic_disease", "key": disease, "value": {"present": True, "name": disease}, "salience": 0.85, "conflict_policy": "conflict"})
        allergy = re.search(r"([\u4e00-\u9fffA-Za-z0-9]{1,10})过敏", text)
        if allergy:
            allergen = allergy.group(1)
            for prefix in ("患者", "母亲", "妈妈", "我妈", "有", "对"):
                allergen = allergen.removeprefix(prefix)
            semantic.append({"namespace": "allergy", "key": allergen, "value": {"allergen": allergen, "status": "reported"}, "salience": 1.0, "conflict_policy": "conflict"})
        renal_patterns = (("肾功能轻度受损", "轻度受损"), ("轻度肾功能不全", "轻度受损"), ("肾功能不全", "受损"), ("肾功能正常", "正常"))
        for phrase, value in renal_patterns:
            if phrase in text:
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
    ):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.connection = sqlite3.connect(self.db_path, timeout=30, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.executescript(SCHEMA)
        self.connection.execute("INSERT OR REPLACE INTO schema_meta(key,value) VALUES('schema_version','3')")
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

    @staticmethod
    def _working_row(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["value"] = _from_json(item.pop("value_json"))
        item["version"] = 1
        item["ref"] = memory_ref("working", item["id"], 1)
        return item

    def write_semantic_fact(self, fact: SemanticFact, *, source: str) -> dict[str, Any]:
        if not 0 <= fact.salience <= 1:
            raise ValueError("salience must be within [0, 1]")
        now = utc_now()
        value_json = _json(fact.value)
        with self._lock, self.connection:
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
            if policy == "auto":
                policy = "conflict" if fact.namespace in SAFETY_CRITICAL_NAMESPACES else "update"
            status = "disputed" if current and policy == "conflict" else "active"
            if current:
                prior_status = "disputed" if policy == "conflict" else "superseded"
                valid_to = None if prior_status == "disputed" else now
                self.connection.execute(
                    "UPDATE semantic_memory SET status=?, valid_to=?, updated_at=? WHERE id=?",
                    (prior_status, valid_to, now, current["id"]),
                )
            cursor = self.connection.execute(
                """INSERT INTO semantic_memory(namespace,fact_key,value_json,status,valid_from,valid_to,source,source_uri,version,salience,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (fact.namespace, fact.key, value_json, status, _iso(fact.valid_from), None, source, fact.source_uri, version, fact.salience, now, now),
            )
            row = self.connection.execute("SELECT * FROM semantic_memory WHERE id=?", (cursor.lastrowid,)).fetchone()
            item = self._semantic_row(row)
            conflict = None
            if current and policy == "conflict":
                left = self._semantic_row(current)
                conflict = self.create_conflict(
                    conflict_type="semantic_fact_conflict",
                    subject_key=f"{fact.namespace}:{fact.key}",
                    left_ref=left["ref"],
                    right_ref=item["ref"],
                    description=f"同一事实存在不一致记录：{left['value']} ↔ {fact.value}；未自动选择任一方。",
                    source=source,
                )
            self._audit("insert" if current is None else policy, "semantic", item["id"], {"ref": item["ref"], "value": fact.value}, source)
            return {"outcome": "inserted" if current is None else policy, "item": item, "conflict": conflict}

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
        now_dt = _as_utc(None)
        with self._lock, self.connection:
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
        occurred_at = _iso(event.occurred_at)
        subject = event.subject_key or ""
        fingerprint = _fingerprint(event.event_type, subject, event.payload, occurred_at, source, session_id)
        with self._lock, self.connection:
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
                """INSERT INTO episodic_memory(event_type,subject_key,payload_json,occurred_at,recorded_at,source,source_uri,session_id,turn_id,salience,severity,version,fingerprint,parent_id)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (event.event_type, event.subject_key, _json(event.payload), occurred_at, utc_now(), source, event.source_uri, session_id, turn_id, event.salience, event.severity, version, fingerprint, parent_id),
            )
            row = self.connection.execute("SELECT * FROM episodic_memory WHERE id=?", (cursor.lastrowid,)).fetchone()
            item = self._episode_row(row)
            self._audit("append", "episodic", item["id"], {"ref": item["ref"], "event_type": event.event_type}, source)
            return item

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
                event = self.record_event(
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

            event_type = {"add": "medication_add", "remove": "medication_remove", "dose_change": "medication_dose_change"}[action]
            payload = {
                "action": action,
                "name": name,
                "dose": dose,
                "route": route,
                "schedule": schedule,
                "medication_ref": medication["ref"],
            }
            event = self.record_event(
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
        ordered_refs = sorted((left_ref, right_ref))
        fingerprint = _fingerprint(conflict_type, subject_key, ordered_refs)
        with self._lock, self.connection:
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
    ) -> ConsolidationResult:
        """Extract, validate, deduplicate and route facts after an interaction."""
        extracted, mode, error = self.extractor.extract(user_text)
        result = ConsolidationResult(extraction_mode=mode, extraction_error=error)
        with self._lock, self.connection:
            self.connection.execute(
                """INSERT INTO interactions(session_id,turn_id,user_text,assistant_text,source,created_at)
                   VALUES(?,?,?,?,?,?) ON CONFLICT(session_id,turn_id) DO UPDATE SET assistant_text=COALESCE(excluded.assistant_text,interactions.assistant_text)""",
                (session_id, turn_id, user_text, assistant_text, source, utc_now()),
            )
            self._audit("consolidate", "interaction", None, {"session_id": session_id, "turn_id": turn_id, "mode": mode, "error": error}, source)

        semantic_candidates: list[SemanticFact] = []
        # Explicit event payloads are primary evidence; model/fallback output
        # fills omissions.  This also preserves caller-supplied occurrence
        # times instead of replacing them with the consolidation time.
        for item in [*semantic_hints, *extracted["semantic"]]:
            semantic_candidates.append(item if isinstance(item, SemanticFact) else SemanticFact(
                namespace=item["namespace"], key=item["key"], value=item["value"],
                salience=float(item.get("salience", 0.7)), conflict_policy=item.get("conflict_policy", "auto"),
                valid_from=item.get("valid_from"), source_uri=item.get("source_uri"),
            ))
        seen_semantic: set[tuple[str, str, str]] = set()
        for fact in semantic_candidates:
            key = (fact.namespace, fact.key, _json(fact.value))
            if key in seen_semantic:
                continue
            seen_semantic.add(key)
            write = self.write_semantic_fact(fact, source=source)
            result.semantic.append(write["item"])
            if write["conflict"]:
                result.conflicts.append(write["conflict"])

        episodic_candidates: list[EpisodicFact] = []
        for item in [*episodic_hints, *extracted["episodic"]]:
            episodic_candidates.append(item if isinstance(item, EpisodicFact) else EpisodicFact(
                event_type=item["event_type"], payload=item["payload"], subject_key=item.get("subject_key"),
                occurred_at=item.get("occurred_at"), salience=float(item.get("salience", 0.6)),
                severity=item.get("severity"), source_uri=item.get("source_uri"),
            ))
        seen_episodes: set[tuple[str, str, str]] = set()
        for event in episodic_candidates:
            key = (event.event_type, event.subject_key or "", _json(event.payload))
            if key in seen_episodes:
                continue
            seen_episodes.add(key)
            result.episodic.append(self.record_event(event, session_id=session_id, turn_id=turn_id, source=source))

        for item in [*extracted["working"], *working_hints]:
            result.working.append(self.write_working(session_id, turn_id, item["key"], item["value"], source=source))
        return result

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
    ) -> dict[str, Any]:
        if not memory_refs:
            raise ValueError("a conclusion must cite at least one memory item")
        if kind == "warning" and not source_refs:
            raise ValueError("a warning conclusion must cite at least one source")
        with self._lock, self.connection:
            cursor = self.connection.execute(
                "INSERT INTO conclusions(session_id,turn_id,kind,text,memory_refs_json,source_refs_json,created_at) VALUES(?,?,?,?,?,?,?)",
                (session_id, turn_id, kind, text, _json(list(dict.fromkeys(memory_refs))), _json(list(source_refs)), utc_now()),
            )
            item = {
                "id": cursor.lastrowid,
                "ref": memory_ref("conclusion", cursor.lastrowid, 1),
                "kind": kind,
                "text": text,
                "memory_refs": list(dict.fromkeys(memory_refs)),
                "source_refs": list(source_refs),
            }
            self._audit("conclude", "conclusion", item["id"], item, "agent", actor="agent")
            return item

    def audit_for(self, ref: str) -> dict[str, Any]:
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
        audit = [dict(item) for item in self.connection.execute(
            "SELECT * FROM audit_log WHERE target_type=? AND target_id=? ORDER BY id", (layer, item_id)
        )]
        for item in audit:
            item["details"] = _from_json(item.pop("details_json"), {})
        raw = dict(row) if row else None
        if raw:
            for key in tuple(raw):
                if key.endswith("_json"):
                    raw[key.removesuffix("_json")] = _from_json(raw.pop(key))
        return {"ref": ref, "requested_version": version, "item": raw, "audit_log": audit}


def main() -> None:
    parser = argparse.ArgumentParser(description="Initialize or inspect the Stage 3 memory database")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--snapshot", action="store_true")
    args = parser.parse_args()
    with MemoryStore(args.db, llm_enabled=False) as store:
        if args.snapshot:
            print(json.dumps(store.snapshot(), ensure_ascii=False, indent=2))
        else:
            print(f"initialized schema v3: {args.db}")


if __name__ == "__main__":
    main()
