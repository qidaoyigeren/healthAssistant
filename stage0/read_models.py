"""Read models + minimal write adapters for the caregiver web frontend.

这些端点是前端闭环所需的最小服务适配(2026-09-06 前端轮新增)。所有数据从
现有持久化记录(semantic_memory / medications / episodic_memory / conflicts /
conclusions / dependency_tasks / interactions / outbox_tasks / idempotency_keys)
和真实执行结果组装 —— 不新建权威业务数据库,不绕开 MemoryStore。

契约要点(详见 docs/frontend/api-contract.md):

* 分页统一 envelope ``{"items", "next_cursor", "total"}``;cursor 是
  ``(排序键, id)`` 的 base64 编码,保证同秒记录不遗漏。
* 预警读模型直接映射 conclusions 行;**没有结构化 severity 字段就返回
  null**,绝不做安全:从不从结论文字反推严重度。
* ``/v1/history/search`` 复用 ``memory_search.search_history``(其索引同步
  有写库副作用,在单写者进程内执行是安全的)。
* ``/v1/memory/fact-actions`` 复用 ``verify_semantic_fact`` /
  ``retract_semantic_fact``,映射真实 outcome(含 ``blocked_by_conflict``)。
* 导出/备份产物只允许按服务端产物 ID 下载(白名单文件名,拒绝路径穿越);
  JSON 导出先做在线备份快照再跨表读取,保证快照一致性。
"""
from __future__ import annotations

import base64
import json
import re
import sqlite3
import tempfile
from pathlib import Path
from typing import Any, Callable

from fastapi import Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field


class FactActionIn(BaseModel):
    """POST /v1/memory/fact-actions 请求体(事实核实/撤回)。"""

    ref: str = Field(min_length=1, max_length=128)
    action: str = Field(min_length=1, max_length=16)
    actor: str = "caregiver"  # self-claimed annotation; authz uses Principal
    basis: str = Field(min_length=1, max_length=2000)

from .backup import (
    BACKUP_DIR,
    EXPORT_DIR,
    backup_database,
    export_database,
    verify_artifact,
)

try:
    from .memory import MemoryStore, memory_ref
    from . import memory_search
except ImportError:  # pragma: no cover - script-style import
    from memory import MemoryStore, memory_ref  # type: ignore
    import memory_search  # type: ignore


class ReadModelError(Exception):
    """Carries HTTP status + error code; converted to ApiError by server.py."""

    def __init__(self, status_code: int, code: str, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


PAGE_LIMIT_DEFAULT = 50
PAGE_LIMIT_MAX = 200

# Artifact ids the download/verify endpoints accept — no user-supplied paths.
_ARTIFACT_ID_RE = re.compile(r"^memory-\d{8}-\d{6}\.(db|json\.gz)$")


# ---------------------------------------------------------------------------
# cursor helpers
# ---------------------------------------------------------------------------

def encode_cursor(order_key: str, row_id: int) -> str:
    raw = json.dumps({"k": order_key, "id": row_id}, ensure_ascii=False)
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")


def decode_cursor(cursor: str) -> tuple[str, int]:
    try:
        raw = json.loads(base64.urlsafe_b64decode(cursor.encode("ascii")))
        return str(raw["k"]), int(raw["id"])
    except Exception as exc:  # malformed cursor from the client
        raise ReadModelError(422, "invalid_cursor", "分页游标无效,请从头重新加载") from exc


def _page_envelope(items: list[dict[str, Any]], total: int,
                   next_cursor: str | None) -> dict[str, Any]:
    return {"items": items, "next_cursor": next_cursor, "total": total}


def _parse_limit(limit: int | None) -> int:
    if limit is None:
        return PAGE_LIMIT_DEFAULT
    return max(1, min(limit, PAGE_LIMIT_MAX))


# ---------------------------------------------------------------------------
# alert records (conclusions read model)
# ---------------------------------------------------------------------------

def _conclusion_row(row: sqlite3.Row) -> dict[str, Any]:
    out = dict(row)
    # 兼容两种输入:conclusions 原始行(memory_refs_json)与 conclusion_chain
    # 返回的已解析条目(memory_refs 已是列表)。
    if "memory_refs_json" in out:
        out["memory_refs"] = json.loads(out.pop("memory_refs_json") or "[]")
    if "source_refs_json" in out:
        out["source_refs"] = json.loads(out.pop("source_refs_json") or "[]")
    input_revision = out.pop("input_revision", None)
    if isinstance(input_revision, str):
        try:
            out["input_revision"] = json.loads(input_revision)
        except (TypeError, ValueError):
            out["input_revision"] = None
    out["ref"] = memory_ref("conclusion", out["id"], 1)
    # 诚实字段:conclusions 没有结构化 severity/confidence 列 —— 保持 null,
    # 由前端展示「未记录」。text 是后端给出的结论文字,原样保留。
    out["severity"] = None
    out["confidence"] = None
    out["evidence_available"] = len(out["source_refs"]) > 0
    return out


def alert_records(store: MemoryStore, *, status: str | None = None,
                  kind: str | None = None, limit: int | None = None,
                  cursor: str | None = None) -> dict[str, Any]:
    """完整预警列表:覆盖全部结论(含 recheck 生成的),带状态过滤。

    status ∈ current|stale|all(默认 current);kind 过滤如
    warning/condition_warning/exposure_warning。
    """
    limit = _parse_limit(limit)
    where = ["1=1"]
    params: list[Any] = []
    if status and status != "all":
        if status not in {"current", "stale"}:
            raise ReadModelError(422, "invalid_status", "status 仅支持 current/stale/all")
        where.append("status = ?")
        params.append(status)
    if kind:
        where.append("kind = ?")
        params.append(kind)
    if cursor is not None:
        key, row_id = decode_cursor(cursor)
        where.append("(created_at < ? OR (created_at = ? AND id < ?))")
        params.extend([key, key, row_id])
    where_sql = " AND ".join(where)
    conn = store.connection
    total = conn.execute(
        f"SELECT COUNT(*) FROM conclusions WHERE {where_sql}", params).fetchone()[0]
    rows = conn.execute(
        f"SELECT * FROM conclusions WHERE {where_sql} "
        f"ORDER BY created_at DESC, id DESC LIMIT ?", [*params, limit + 1]).fetchall()
    has_more = len(rows) > limit
    rows = rows[:limit]
    items = [_conclusion_row(r) for r in rows]
    next_cursor = None
    if has_more and items:
        last = rows[-1]
        next_cursor = encode_cursor(last["created_at"], last["id"])
    return _page_envelope(items, total, next_cursor)


def alert_record_detail(store: MemoryStore, alert_id: int) -> dict[str, Any]:
    row = store.connection.execute(
        "SELECT * FROM conclusions WHERE id = ?", (alert_id,)).fetchone()
    if row is None:
        raise ReadModelError(404, "unknown_alert", "没有这条预警记录")
    item = _conclusion_row(row)
    chain = store.conclusion_chain(alert_id)
    item["chain"] = {
        "versions": [_conclusion_row(v) for v in chain["versions"]],
        "current_head": chain["current_head"],
        "status": chain["status"],
        "stale_reason": chain["stale_reason"],
    }
    return item


def conclusion_history(store: MemoryStore, conclusion_id: int) -> dict[str, Any]:
    chain = store.conclusion_chain(conclusion_id)
    return {
        "versions": [_conclusion_row(v) for v in chain["versions"]],
        "current_head": chain["current_head"],
        "status": chain["status"],
        "stale_reason": chain["stale_reason"],
    }


# ---------------------------------------------------------------------------
# paged episodic history (server-side pagination, newest first)
# ---------------------------------------------------------------------------

def history_events(store: MemoryStore, *, limit: int | None = None,
                   cursor: str | None = None, event_type: str | None = None,
                   q: str | None = None,
                   occurred_from: str | None = None,
                   occurred_to: str | None = None) -> dict[str, Any]:
    """真实 episodic 全量分页(倒序);替代「升序取前 N」的旧 timeline 语义。"""
    limit = _parse_limit(limit)
    where = ["1=1"]
    params: list[Any] = []
    if event_type:
        where.append("event_type = ?")
        params.append(event_type)
    if occurred_from:
        where.append("occurred_at >= ?")
        params.append(occurred_from)
    if occurred_to:
        where.append("occurred_at <= ?")
        params.append(occurred_to)
    if q:
        where.append("(text LIKE ? OR payload_json LIKE ?)")
        like = f"%{q}%"
        params.extend([like, like])
    if cursor is not None:
        key, row_id = decode_cursor(cursor)
        where.append("(occurred_at < ? OR (occurred_at = ? AND id < ?))")
        params.extend([key, key, row_id])
    where_sql = " AND ".join(where)
    conn = store.connection
    total = conn.execute(
        f"SELECT COUNT(*) FROM episodic_memory WHERE {where_sql}", params).fetchone()[0]
    rows = conn.execute(
        f"SELECT * FROM episodic_memory WHERE {where_sql} "
        f"ORDER BY occurred_at DESC, id DESC LIMIT ?", [*params, limit + 1]).fetchall()
    has_more = len(rows) > limit
    rows = rows[:limit]
    items = [_episode_row(r) for r in rows]
    next_cursor = None
    if has_more and items:
        last = rows[-1]
        next_cursor = encode_cursor(last["occurred_at"], last["id"])
    return _page_envelope(items, total, next_cursor)


def _episode_row(row: sqlite3.Row) -> dict[str, Any]:
    out = dict(row)
    out["payload"] = json.loads(out.pop("payload_json") or "{}")
    out["ref"] = memory_ref("episodic", out["id"], out.get("version") or 1)
    return out


def event_types(store: MemoryStore) -> list[dict[str, Any]]:
    rows = store.connection.execute(
        "SELECT event_type, COUNT(*) AS count FROM episodic_memory "
        "GROUP BY event_type ORDER BY count DESC").fetchall()
    return [{"event_type": r["event_type"], "count": r["count"]} for r in rows]


# ---------------------------------------------------------------------------
# medication records (full version history incl. stopped/superseded)
# ---------------------------------------------------------------------------

def _medication_row(row: sqlite3.Row) -> dict[str, Any]:
    out = dict(row)
    out["ingredients"] = json.loads(out.pop("ingredients_json") or "[]")
    out["ref"] = memory_ref("medication", out["id"], out.get("version") or 1)
    return out


def medication_records(store: MemoryStore, *, status: str | None = None,
                       limit: int | None = None,
                       cursor: str | None = None) -> dict[str, Any]:
    """全部药物记录(含 stopped/superseded/disputed),不是当前药单过滤。"""
    limit = _parse_limit(limit)
    where = ["1=1"]
    params: list[Any] = []
    if status:
        if status not in {"active", "stopped", "superseded", "disputed", "all"}:
            raise ReadModelError(422, "invalid_status",
                                 "status 仅支持 active/stopped/superseded/disputed/all")
        if status != "all":
            where.append("status = ?")
            params.append(status)
    if cursor is not None:
        key, row_id = decode_cursor(cursor)
        where.append("(start_at < ? OR (start_at = ? AND id < ?))")
        params.extend([key, key, row_id])
    where_sql = " AND ".join(where)
    conn = store.connection
    total = conn.execute(
        f"SELECT COUNT(*) FROM medications WHERE {where_sql}", params).fetchone()[0]
    rows = conn.execute(
        f"SELECT * FROM medications WHERE {where_sql} "
        f"ORDER BY start_at DESC, id DESC LIMIT ?", [*params, limit + 1]).fetchall()
    has_more = len(rows) > limit
    rows = rows[:limit]
    items = [_medication_row(r) for r in rows]
    next_cursor = None
    if has_more and items:
        last = rows[-1]
        next_cursor = encode_cursor(last["start_at"], last["id"])
    return _page_envelope(items, total, next_cursor)


def medication_record_detail(store: MemoryStore, medication_id: int) -> dict[str, Any]:
    row = store.connection.execute(
        "SELECT * FROM medications WHERE id = ?", (medication_id,)).fetchone()
    if row is None:
        raise ReadModelError(404, "unknown_medication", "没有这条药物记录")
    item = _medication_row(row)
    # 同一 medication_key 的完整版本链(old → new),含 stopped/superseded。
    versions = store.connection.execute(
        "SELECT * FROM medications WHERE medication_key = ? ORDER BY version ASC",
        (row["medication_key"],)).fetchall()
    item["versions"] = [_medication_row(v) for v in versions]
    return item


# ---------------------------------------------------------------------------
# conflict records (all statuses) + action history
# ---------------------------------------------------------------------------

def _conflict_row(row: sqlite3.Row) -> dict[str, Any]:
    out = dict(row)
    resolution = out.pop("resolution_json", None)
    if resolution:
        try:
            out["resolution"] = json.loads(resolution)
        except (TypeError, ValueError):
            out["resolution"] = None
    else:
        out["resolution"] = None
    out["ref"] = memory_ref("conflict", out["id"], 1)
    return out


def conflict_records(store: MemoryStore, *, status: str | None = None,
                     limit: int | None = None,
                     cursor: str | None = None) -> dict[str, Any]:
    """全状态冲突查询(open/resolved/dismissed/all),供「待核实」页使用。"""
    limit = _parse_limit(limit)
    where = ["1=1"]
    params: list[Any] = []
    if status and status != "all":
        if status not in {"open", "resolved", "dismissed"}:
            raise ReadModelError(422, "invalid_status", "status 仅支持 open/resolved/dismissed/all")
        where.append("status = ?")
        params.append(status)
    if cursor is not None:
        key, row_id = decode_cursor(cursor)
        where.append("(created_at < ? OR (created_at = ? AND id < ?))")
        params.extend([key, key, row_id])
    where_sql = " AND ".join(where)
    conn = store.connection
    total = conn.execute(
        f"SELECT COUNT(*) FROM conflicts WHERE {where_sql}", params).fetchone()[0]
    rows = conn.execute(
        f"SELECT * FROM conflicts WHERE {where_sql} "
        f"ORDER BY created_at DESC, id DESC LIMIT ?", [*params, limit + 1]).fetchall()
    has_more = len(rows) > limit
    rows = rows[:limit]
    items = [_conflict_row(r) for r in rows]
    next_cursor = None
    if has_more and items:
        last = rows[-1]
        next_cursor = encode_cursor(last["created_at"], last["id"])
    return _page_envelope(items, total, next_cursor)


def conflict_record_detail(store: MemoryStore, conflict_id: int) -> dict[str, Any]:
    row = store.connection.execute(
        "SELECT * FROM conflicts WHERE id = ?", (conflict_id,)).fetchone()
    if row is None:
        raise ReadModelError(404, "unknown_conflict", "没有这条待核实记录")
    item = _conflict_row(row)
    # 两侧引用的真实记录 + 各自审计历史(同版本精确解析)。
    sides: dict[str, Any] = {}
    for side in ("left_ref", "right_ref"):
        ref = row[side]
        try:
            resolved = store.resolve_ref(ref)
            item_row = dict(resolved["row"])
            for key in list(item_row):
                if key.endswith("_json"):
                    try:
                        item_row[key[:-5]] = json.loads(item_row.pop(key) or "null")
                    except (TypeError, ValueError):
                        item_row.pop(key)
            audit = store.audit_for(ref)["audit_log"]
            sides[side] = {"ref": ref, "layer": resolved["layer"],
                           "item": item_row, "audit_log": audit}
        except (ValueError, KeyError) as exc:
            # 引用无法解析时如实标注,不构造空记录。
            sides[side] = {"ref": ref, "error": f"引用无法解析: {exc}"}
    item["sides"] = sides
    item["actions"] = [_action_row(a) for a in store.conflict_actions_for(conflict_id)]
    return item


def _action_row(row: sqlite3.Row) -> dict[str, Any]:
    return dict(row)


# ---------------------------------------------------------------------------
# memory item detail (ref resolver, whitelist layers)
# ---------------------------------------------------------------------------

_RESOLVABLE_LAYERS = {"semantic", "medication", "episodic", "conflict", "conclusion"}


def memory_item(store: MemoryStore, ref: str) -> dict[str, Any]:
    """解析 memory ref 并返回记录 + 审计日志;版本必须精确匹配。

    只接受仓库内业务层引用;文件/外部 URI 一律拒绝(任意文件读取防护)。
    """
    if not isinstance(ref, str) or not ref.startswith("memory:"):
        raise ReadModelError(422, "invalid_ref",
                             "仅支持应用内 memory:* 引用;文件或外部地址请在来源详情中查看")
    try:
        resolved = store.resolve_ref(ref)
    except ValueError as exc:
        raise ReadModelError(422, "invalid_ref", str(exc)) from exc
    if resolved["layer"] not in _RESOLVABLE_LAYERS:
        raise ReadModelError(422, "unsupported_ref_layer",
                             f"暂不支持查看 {resolved['layer']} 层记录")
    item_row = dict(resolved["row"])
    for key in list(item_row):
        if key.endswith("_json"):
            try:
                item_row[key[:-5]] = json.loads(item_row.pop(key) or "null")
            except (TypeError, ValueError):
                item_row.pop(key)
    audit = store.audit_for(ref)
    return {
        "ref": ref,
        "layer": resolved["layer"],
        "item_id": resolved["item_id"],
        "version": resolved["version"],
        "item": item_row,
        "audit_log": audit["audit_log"],
    }


# ---------------------------------------------------------------------------
# fact verify / retract (minimal write adapter)
# ---------------------------------------------------------------------------

def fact_action(store: MemoryStore, *, ref: str, action: str,
                actor: str, basis: str) -> dict[str, Any]:
    if action not in {"verify", "retract"}:
        raise ReadModelError(422, "invalid_action", "action 仅支持 verify/retract")
    if not basis.strip():
        raise ReadModelError(422, "basis_required",
                             "请填写依据/理由(不能为空)")
    try:
        if action == "verify":
            result = store.verify_semantic_fact(ref, actor=actor, basis=basis)
        else:
            result = store.retract_semantic_fact(ref, reason=basis, actor=actor)
    except ValueError as exc:
        # 未知引用格式 / 该记录不在可操作状态(如已撤回)
        raise ReadModelError(422, "invalid_fact_ref", str(exc)) from exc
    # 真实 outcome:verified / blocked_by_conflict / retracted。blocked 不是错误
    # 状态 —— 前端据此展示待核实冲突,而不是「核实成功」。
    return result


# ---------------------------------------------------------------------------
# recheck tasks
# ---------------------------------------------------------------------------

def recheck_tasks(store: MemoryStore) -> dict[str, Any]:
    """pending 与历史 dependency_tasks;真实结论引用与状态。"""
    conn = store.connection
    rows = conn.execute(
        "SELECT * FROM dependency_tasks WHERE task_type='recheck_conclusion' "
        "ORDER BY id DESC LIMIT 200").fetchall()
    pending: list[dict[str, Any]] = []
    history: list[dict[str, Any]] = []
    for raw in rows:
        task = dict(raw)
        target_id = task.get("target_id")
        conclusion = None
        if target_id:
            crow = conn.execute(
                "SELECT * FROM conclusions WHERE id = ?", (target_id,)).fetchone()
            conclusion = _conclusion_row(crow) if crow is not None else None
        task["target_conclusion"] = conclusion
        if task["status"] in {"open", "running"}:
            pending.append(task)
        else:
            history.append(task)
    return {"pending": pending, "history": history,
            "pending_count": len(pending)}


# ---------------------------------------------------------------------------
# sessions & submissions (interactions + outbox join)
# ---------------------------------------------------------------------------

def sessions_list(store: MemoryStore) -> list[dict[str, Any]]:
    rows = store.connection.execute(
        "SELECT session_id, COUNT(*) AS event_count, MIN(created_at) AS first_at, "
        "MAX(created_at) AS last_at FROM interactions "
        "GROUP BY session_id ORDER BY last_at DESC LIMIT 200").fetchall()
    return [dict(r) for r in rows]


def _submission_row(store: MemoryStore, row: sqlite3.Row) -> dict[str, Any]:
    out = dict(row)
    result = out.pop("result_json", None)
    out["response"] = json.loads(result) if result else None
    event_key = out.get("event_key")
    idem = None
    request_meta: dict[str, Any] | None = None
    outbox_status = None
    outbox_error = None
    if event_key:
        idem = event_key[4:] if event_key.startswith("api:") else None
        if idem:
            task = store.outbox_task_for(f"api-event:{idem}")
            if task is not None:
                payload = task.get("payload") or {}
                event_fields = payload.get("event") or {}
                request_meta = {
                    "event_type": event_fields.get("event_type"),
                    "text": event_fields.get("text"),
                    "occurred_at": event_fields.get("occurred_at"),
                    "source": event_fields.get("source"),
                }
                outbox_status = task.get("status")
                outbox_error = task.get("error")
                # 提交的完整持久化结果(text/warnings/conflicts/audit_trail/
                # safety_status/operation_outcomes);没有就是 null,不补造。
                if task.get("result"):
                    out["response"] = task["result"]
    out["idempotency_key"] = idem
    out["request"] = request_meta
    out["outbox_status"] = outbox_status
    out["outbox_error"] = outbox_error
    return out


def session_events(store: MemoryStore, session_id: str, *,
                   limit: int | None = None,
                   cursor: str | None = None) -> dict[str, Any]:
    """某会话的真实提交记录(交互表 + outbox 元数据 + 持久化结果)。

    没有保存的助手正文就是 null —— 不补造历史回复。
    """
    limit = _parse_limit(limit)
    where = ["session_id = ?"]
    params: list[Any] = [session_id]
    if cursor is not None:
        key, row_id = decode_cursor(cursor)
        where.append("(created_at < ? OR (created_at = ? AND id < ?))")
        params.extend([key, key, row_id])
    where_sql = " AND ".join(where)
    conn = store.connection
    total = conn.execute(
        f"SELECT COUNT(*) FROM interactions WHERE {where_sql}", params).fetchone()[0]
    rows = conn.execute(
        f"SELECT * FROM interactions WHERE {where_sql} "
        f"ORDER BY created_at DESC, id DESC LIMIT ?", [*params, limit + 1]).fetchall()
    has_more = len(rows) > limit
    rows = rows[:limit]
    items = [_submission_row(store, r) for r in rows]
    next_cursor = None
    if has_more and items:
        last = rows[-1]
        next_cursor = encode_cursor(last["created_at"], last["id"])
    return _page_envelope(items, total, next_cursor)


def turn_trace(store: MemoryStore, session_id: str, turn_id: str) -> dict[str, Any]:
    traces = store.traces_for_turn(session_id, turn_id)
    # 审计展示:已有记录原样呈现(有几分记录展示几分),不补造执行步骤。
    return {"session_id": session_id, "turn_id": turn_id,
            "traces": [{"id": t["id"], "cycle": t.get("cycle"),
                        "phase": t.get("phase"), "payload": t.get("payload"),
                        "created_at": t.get("created_at")} for t in traces]}


# ---------------------------------------------------------------------------
# overview aggregates (server-side counts — 口径明确)
# ---------------------------------------------------------------------------

def overview(store: MemoryStore) -> dict[str, Any]:
    conn = store.connection
    one = lambda sql, params=(): conn.execute(sql, params).fetchone()[0]  # noqa: E731
    last_episode = conn.execute(
        "SELECT recorded_at, occurred_at, event_type FROM episodic_memory "
        "ORDER BY recorded_at DESC, id DESC LIMIT 1").fetchone()
    return {
        "counts": {
            # 口径:medications status='active' 的当前在用条数
            "medications_active": one(
                "SELECT COUNT(*) FROM medications WHERE status='active'"),
            # 口径:全部药物版本记录(含 stopped/superseded 历史)
            "medications_records": one("SELECT COUNT(*) FROM medications"),
            # 口径:semantic facts status='active'
            "facts_active": one(
                "SELECT COUNT(*) FROM semantic_memory WHERE status='active'"),
            # 口径:valid_from 为空(生效时间不明)的 active 事实
            "facts_uncertain_time": one(
                "SELECT COUNT(*) FROM semantic_memory WHERE status='active' "
                "AND valid_from IS NULL"),
            # 口径:conclusions status='current' 的全部结论(各类 kind)
            "conclusions_current": one(
                "SELECT COUNT(*) FROM conclusions WHERE status='current'"),
            # 口径:conclusions status='stale'(失效待复查/已被替代)
            "conclusions_stale": one(
                "SELECT COUNT(*) FROM conclusions WHERE status='stale'"),
            # 口径:conflicts status='open'
            "conflicts_open": one(
                "SELECT COUNT(*) FROM conflicts WHERE status='open'"),
            # 口径:dependency_tasks 未完成的 recheck 任务
            "rechecks_pending": one(
                "SELECT COUNT(*) FROM dependency_tasks WHERE "
                "task_type='recheck_conclusion' AND status IN ('open','running')"),
            # 口径:outbox 待处理任务
            "outbox_pending": one(
                "SELECT COUNT(*) FROM outbox_tasks WHERE status IN ('open','running')"),
        },
        "last_recorded": (dict(last_episode) if last_episode else None),
    }


# ---------------------------------------------------------------------------
# exports / backups (artifact-id based; no user paths)
# ---------------------------------------------------------------------------

def _snapshot_path(store: MemoryStore) -> Path:
    """WAL-safe 在线快照(SQLite backup API),供跨表导出读取一致副本。"""
    tmp = Path(tempfile.mkstemp(prefix="stage0-snapshot-", suffix=".db")[1])
    target = sqlite3.connect(tmp)
    store.connection.backup(target)
    target.close()
    return tmp


def create_data_export(store: MemoryStore) -> dict[str, Any]:
    """全库 JSON 交换导出(.json.gz):先取在线快照再导出,保证一致性。"""
    snapshot = _snapshot_path(store)
    try:
        report = export_database(snapshot, EXPORT_DIR)
    finally:
        # Windows 上 sqlite 句柄释放可能滞后;清理失败仅留下临时文件,不影响导出。
        try:
            snapshot.unlink(missing_ok=True)
        except OSError:
            pass
    report["artifact_id"] = Path(report["export"]).name
    report["kind"] = "export"
    return report


def create_data_backup(store: MemoryStore) -> dict[str, Any]:
    report = backup_database(store.db_path, BACKUP_DIR)
    report["artifact_id"] = Path(report["backup"]).name
    report["kind"] = "backup"
    return report


def list_artifacts() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for kind, directory in (("export", EXPORT_DIR), ("backup", BACKUP_DIR)):
        if not directory.exists():
            continue
        for path in sorted(directory.iterdir(), key=lambda p: p.name, reverse=True):
            if _ARTIFACT_ID_RE.match(path.name) and path.is_file():
                out.append({"artifact_id": path.name, "kind": kind,
                            "size_bytes": path.stat().st_size,
                            "created_at": path.stat().st_mtime})
    return out


def artifact_path(artifact_id: str, kind: str | None = None) -> Path:
    """白名单产物定位:文件名固定格式,拒绝任何路径穿越。"""
    if not _ARTIFACT_ID_RE.match(artifact_id):
        raise ReadModelError(422, "invalid_artifact_id", "产物 ID 格式不正确")
    for kind_, directory in (("export", EXPORT_DIR), ("backup", BACKUP_DIR)):
        if kind is not None and kind_ != kind:
            continue
        candidate = directory / artifact_id
        if candidate.is_file():
            return candidate
    raise ReadModelError(404, "unknown_artifact", "没有这个导出/备份产物")


def artifact_verification(artifact_id: str) -> dict[str, Any]:
    path = artifact_path(artifact_id)
    return verify_artifact(path)


# ---------------------------------------------------------------------------
# route registration
# ---------------------------------------------------------------------------

def register_read_model_routes(app: Any, store: MemoryStore, *,
                               principal: Callable[[Any], Any],
                               authorize_scope: Callable[[Any], None],
                               require_role: Callable[..., None],
                               api_error: type[Exception]) -> None:
    """把读模型端点挂到 FastAPI app 上。错误统一转 ApiError。"""

    def _guard(request: Any, *, roles: tuple[str, ...] = ()) -> Any:
        p = principal(request)
        if roles:
            require_role(p, *roles)
        authorize_scope(p, "local-demo")
        return p

    def _wrap(fn: Callable[..., Any]) -> Callable[..., Any]:
        def inner(*args: Any, **kwargs: Any) -> Any:
            try:
                return fn(*args, **kwargs)
            except ReadModelError as exc:
                raise api_error(exc.status_code, exc.code, "validation", exc.message) from exc
        inner.__name__ = fn.__name__
        inner.__doc__ = fn.__doc__
        return inner

    @app.post("/v1/memory/fact-actions")
    def submit_fact_action(body: FactActionIn, request: Request) -> dict[str, Any]:
        _guard(request, roles=("caregiver", "ops"))
        # Non-idempotent write without the event pipeline's dedup guarantees:
        # a client retry is a NEW business operation (documented contract).
        # verify blocked_by_conflict returns 200 with the real outcome.
        return _wrap(fact_action)(store, ref=body.ref, action=body.action,
                                  actor=body.actor, basis=body.basis)

    @app.get("/v1/alert-records")
    def read_alert_records(request: Request, status: str | None = None,
                           kind: str | None = None, limit: int | None = None,
                           cursor: str | None = None) -> dict[str, Any]:
        _guard(request)
        return _wrap(alert_records)(store, status=status, kind=kind,
                                    limit=limit, cursor=cursor)

    @app.get("/v1/alert-records/{alert_id}")
    def read_alert_record(alert_id: int, request: Request) -> dict[str, Any]:
        _guard(request)
        return _wrap(alert_record_detail)(store, alert_id)

    @app.get("/v1/conclusions/{conclusion_id}/history")
    def read_conclusion_history(conclusion_id: int, request: Request) -> dict[str, Any]:
        _guard(request)
        return _wrap(conclusion_history)(store, conclusion_id)

    @app.get("/v1/history/events")
    def read_history_events(request: Request, limit: int | None = None,
                            cursor: str | None = None, event_type: str | None = None,
                            q: str | None = None, occurred_from: str | None = None,
                            occurred_to: str | None = None) -> dict[str, Any]:
        _guard(request)
        return _wrap(history_events)(store, limit=limit, cursor=cursor,
                                     event_type=event_type, q=q,
                                     occurred_from=occurred_from,
                                     occurred_to=occurred_to)

    @app.get("/v1/history/event-types")
    def read_event_types(request: Request) -> list[dict[str, Any]]:
        _guard(request)
        return event_types(store)

    @app.get("/v1/history/search")
    def read_history_search(request: Request, q: str = "", limit: int | None = None) -> dict[str, Any]:
        _guard(request)
        query = q.strip()
        if not query:
            return {"mode": "no_query", "results": [], "tokens": []}
        if len(query) > 200:
            raise api_error(422, "invalid_query", "validation", "搜索词过长(上限 200 字符)")
        # search_history 会同步 FTS 索引(写库副作用);单写者进程内安全。
        return memory_search.search_history(store, query,
                                            limit=max(1, min(limit or 10, 50)))

    @app.get("/v1/medication-records")
    def read_medication_records(request: Request, status: str | None = None,
                                limit: int | None = None,
                                cursor: str | None = None) -> dict[str, Any]:
        _guard(request)
        return _wrap(medication_records)(store, status=status, limit=limit, cursor=cursor)

    @app.get("/v1/medication-records/{medication_id}")
    def read_medication_record(medication_id: int, request: Request) -> dict[str, Any]:
        _guard(request)
        return _wrap(medication_record_detail)(store, medication_id)

    @app.get("/v1/conflict-records")
    def read_conflict_records(request: Request, status: str | None = None,
                              limit: int | None = None,
                              cursor: str | None = None) -> dict[str, Any]:
        _guard(request)
        return _wrap(conflict_records)(store, status=status, limit=limit, cursor=cursor)

    @app.get("/v1/conflict-records/{conflict_id}")
    def read_conflict_record(conflict_id: int, request: Request) -> dict[str, Any]:
        _guard(request)
        return _wrap(conflict_record_detail)(store, conflict_id)

    @app.get("/v1/conflicts/{conflict_id}/history")
    def read_conflict_actions(conflict_id: int, request: Request) -> list[dict[str, Any]]:
        _guard(request)
        return [_action_row(a) for a in store.conflict_actions_for(conflict_id)]

    @app.get("/v1/memory/item")
    def read_memory_item(request: Request, ref: str) -> dict[str, Any]:
        _guard(request)
        return _wrap(memory_item)(store, ref)

    @app.get("/v1/recheck-tasks")
    def read_recheck_tasks(request: Request) -> dict[str, Any]:
        _guard(request)
        return recheck_tasks(store)

    @app.get("/v1/sessions")
    def read_sessions(request: Request) -> list[dict[str, Any]]:
        _guard(request)
        return sessions_list(store)

    @app.get("/v1/sessions/{session_id}/events")
    def read_session_events(session_id: str, request: Request,
                            limit: int | None = None,
                            cursor: str | None = None) -> dict[str, Any]:
        _guard(request)
        return _wrap(session_events)(store, session_id, limit=limit, cursor=cursor)

    @app.get("/v1/sessions/{session_id}/turns/{turn_id}/trace")
    def read_turn_trace(session_id: str, turn_id: str, request: Request) -> dict[str, Any]:
        _guard(request)
        return turn_trace(store, session_id, turn_id)

    @app.get("/v1/overview")
    def read_overview(request: Request) -> dict[str, Any]:
        _guard(request)
        return overview(store)

    @app.post("/v1/data/exports")
    def create_export(request: Request) -> dict[str, Any]:
        _guard(request, roles=("caregiver", "ops"))
        return create_data_export(store)

    @app.post("/v1/data/backups")
    def create_backup(request: Request) -> dict[str, Any]:
        _guard(request, roles=("caregiver", "ops"))
        return create_data_backup(store)

    @app.get("/v1/data/artifacts")
    def read_artifacts(request: Request) -> list[dict[str, Any]]:
        _guard(request)
        return list_artifacts()

    @app.get("/v1/data/artifacts/{artifact_id}/verify")
    def verify_data_artifact(artifact_id: str, request: Request) -> dict[str, Any]:
        _guard(request)
        return _wrap(artifact_verification)(artifact_id)

    @app.get("/v1/data/artifacts/{artifact_id}/download")
    def download_data_artifact(artifact_id: str, request: Request) -> FileResponse:
        _guard(request)
        path = _wrap(artifact_path)(artifact_id)
        media = "application/gzip" if path.name.endswith(".gz") else "application/octet-stream"
        return FileResponse(path, filename=path.name, media_type=media)
