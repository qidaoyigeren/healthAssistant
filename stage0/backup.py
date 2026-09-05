"""Backup, export, restore and verification for the family memory database.

This is a single-family medication history: losing ``memory.db`` loses the
history.  The module deliberately keeps everything local and auditable —
online SQLite backups (WAL-safe), a JSON interchange export, restore drills
and integrity verification.  It makes no cloud-compliance claims.

Usage::

    python -m stage0.backup --backup                    # backups/memory-<ts>.db + sidecars
    python -m stage0.backup --export                    # exports/memory-<ts>.json.gz
    python -m stage0.backup --restore <file> --to <db>  # refuse to overwrite
    python -m stage0.backup --verify <file>             # integrity + sidecar match
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
DEFAULT_DB = ROOT / "memory.db"
BACKUP_DIR = ROOT / "backups"
EXPORT_DIR = ROOT / "exports"

# Virtual tables (history_fts) and their shadow tables are excluded from JSON
# export; the index is rebuilt from episodic rows on import instead.
_EXPORT_SKIP_PREFIXES = ("sqlite_", "history_fts")

EXPORT_FORMAT = "healthassistant-memory-export-v1"


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _user_tables(connection: sqlite3.Connection) -> list[str]:
    return [
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' AND name NOT LIKE 'history_fts%'"
        )
    ]


def _table_counts(connection: sqlite3.Connection) -> dict[str, int]:
    return {
        table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in _user_tables(connection)
    }


def _schema_version(connection: sqlite3.Connection) -> str | None:
    row = connection.execute(
        "SELECT value FROM schema_meta WHERE key='schema_version'"
    ).fetchone()
    return row[0] if row else None


def _write_sidecar(artifact: Path, payload: dict[str, Any]) -> Path:
    sidecar = artifact.with_suffix(artifact.suffix + ".counts.json")
    sidecar.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    checksum = artifact.with_suffix(artifact.suffix + ".sha256")
    checksum.write_text(f"{_sha256_file(artifact)}  {artifact.name}\n", encoding="utf-8")
    return sidecar


def backup_database(db_path: Path = DEFAULT_DB, out_dir: Path = BACKUP_DIR) -> dict[str, Any]:
    """Online backup via the sqlite3 backup API (safe while WAL writes happen)."""
    if not Path(db_path).exists():
        raise FileNotFoundError(f"database not found: {db_path}")
    out_dir.mkdir(parents=True, exist_ok=True)
    dest = out_dir / f"memory-{_timestamp()}.db"
    source = sqlite3.connect(db_path)
    target = sqlite3.connect(dest)
    with target:
        source.backup(target)
    source.close()
    target.close()
    check = sqlite3.connect(dest)
    try:
        counts = _table_counts(check)
        version = _schema_version(check)
    finally:
        check.close()
    report = {
        "backup": str(dest),
        "source": str(db_path),
        "schema_version": version,
        "row_counts": counts,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    _write_sidecar(dest, report)
    return report


def export_database(db_path: Path = DEFAULT_DB, out_dir: Path = EXPORT_DIR) -> dict[str, Any]:
    """Dump every user table to a gzipped JSON interchange file."""
    if not Path(db_path).exists():
        raise FileNotFoundError(f"database not found: {db_path}")
    out_dir.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    tables = _user_tables(connection)
    payload: dict[str, Any] = {
        "format": EXPORT_FORMAT,
        "schema_version": _schema_version(connection),
        "exported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": str(db_path),
        "tables": {
            table: [dict(row) for row in connection.execute(f"SELECT * FROM {table}")]
            for table in tables
        },
    }
    connection.close()
    dest = out_dir / f"memory-{_timestamp()}.json.gz"
    with gzip.open(dest, "wt", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False)
    report = {
        "export": str(dest),
        "schema_version": payload["schema_version"],
        "row_counts": {table: len(rows) for table, rows in payload["tables"].items()},
        "created_at": payload["exported_at"],
    }
    _write_sidecar(dest, report)
    return report


def restore_database(source: Path, target: Path) -> dict[str, Any]:
    """Restore an artifact into ``target`` (must not already exist).

    ``.db`` artifacts are restored through the SQLite backup API after an
    integrity check; ``.json.gz`` artifacts rebuild the schema first (via
    MemoryStore's own migration path) and insert rows with foreign keys
    disabled, then verify referential integrity before returning.
    """
    target = Path(target)
    if target.exists():
        raise FileExistsError(f"refusing to overwrite existing file: {target}")
    source = Path(source)
    if not source.exists():
        raise FileNotFoundError(f"artifact not found: {source}")
    target.parent.mkdir(parents=True, exist_ok=True)
    if source.suffix == ".gz":
        return _import_json_export(source, target)
    integrity = _verify_sqlite(source)
    if integrity["integrity_check"] != "ok":
        raise RuntimeError(f"artifact failed integrity check: {source}")
    src = sqlite3.connect(source)
    dst = sqlite3.connect(target)
    with dst:
        src.backup(dst)
    src.close()
    dst.close()
    check = sqlite3.connect(target)
    try:
        counts = _table_counts(check)
    finally:
        check.close()
    return {
        "restored": str(target),
        "source": str(source),
        "row_counts": counts,
    }


def _import_json_export(source: Path, target: Path) -> dict[str, Any]:
    with gzip.open(source, "rt", encoding="utf-8") as handle:
        payload = json.load(handle)
    if payload.get("format") != EXPORT_FORMAT:
        raise RuntimeError(f"unknown export format: {payload.get('format')!r}")
    try:
        from .memory import MemoryStore
    except ImportError:  # Support ``python stage0/backup.py``.
        from memory import MemoryStore  # type: ignore
    store = MemoryStore(target, llm_enabled=False)
    store.close()  # schema + migrations applied
    connection = sqlite3.connect(target)
    connection.execute("PRAGMA foreign_keys=OFF")
    try:
        with connection:
            for table, rows in payload.get("tables", {}).items():
                if not rows:
                    continue
                columns = list(rows[0].keys())
                # OR REPLACE: the fresh target already carries the schema_meta
                # rows its own migrations wrote; the export's versions win.
                connection.executemany(
                    f"INSERT OR REPLACE INTO {table} ({','.join(columns)}) "
                    f"VALUES ({','.join('?' for _ in columns)})",
                    ([row[column] for column in columns] for row in rows),
                )
        problems = connection.execute("PRAGMA foreign_key_check").fetchall()
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        counts = _table_counts(connection)
    finally:
        connection.close()
    if problems or integrity != "ok":
        target.unlink(missing_ok=True)
        raise RuntimeError(
            f"import failed validation (integrity={integrity}, fk_problems={len(problems)}); "
            "target removed"
        )
    # Rebuild the episodic FTS index from the imported rows.
    store = MemoryStore(target, llm_enabled=False)
    try:
        from .memory_search import sync_history_index
    except ImportError:
        from memory_search import sync_history_index  # type: ignore
    synced = sync_history_index(store)
    store.close()
    return {"restored": str(target), "source": str(source), "row_counts": counts, "fts_synced": synced}


def _verify_sqlite(path: Path) -> dict[str, Any]:
    connection = sqlite3.connect(path)
    report = {
        "integrity_check": connection.execute("PRAGMA integrity_check").fetchone()[0],
        "quick_check": connection.execute("PRAGMA quick_check").fetchone()[0],
        "foreign_key_violations": len(connection.execute("PRAGMA foreign_key_check").fetchall()),
        "row_counts": _table_counts(connection),
        "schema_version": _schema_version(connection),
    }
    connection.close()
    return report


def verify_artifact(path: Path) -> dict[str, Any]:
    """Verify one artifact: integrity checks plus sidecar checksum/count match."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"artifact not found: {path}")
    report: dict[str, Any] = {"artifact": str(path)}
    checksum_path = path.with_suffix(path.suffix + ".sha256")
    if checksum_path.exists():
        expected = checksum_path.read_text("utf-8").split()[0]
        report["sha256_matches"] = _sha256_file(path) == expected
    counts_path = path.with_suffix(path.suffix + ".counts.json")
    if path.suffix == ".gz":
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            payload = json.load(handle)
        report["format"] = payload.get("format")
        report["schema_version"] = payload.get("schema_version")
        report["row_counts"] = {t: len(r) for t, r in payload.get("tables", {}).items()}
        report["integrity_check"] = "ok" if payload.get("format") == EXPORT_FORMAT else "unknown_format"
        if counts_path.exists():
            expected_counts = json.loads(counts_path.read_text("utf-8")).get("row_counts", {})
            report["counts_match_sidecar"] = report["row_counts"] == expected_counts
        return report
    report.update(_verify_sqlite(path))
    if counts_path.exists():
        expected_counts = json.loads(counts_path.read_text("utf-8")).get("row_counts", {})
        report["counts_match_sidecar"] = report["row_counts"] == expected_counts
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Backup / export / restore / verify the memory database")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--backup", action="store_true")
    parser.add_argument("--export", action="store_true")
    parser.add_argument("--restore", type=Path, default=None, help="artifact to restore (.db or .json.gz)")
    parser.add_argument("--to", type=Path, default=None, help="target path for --restore (must not exist)")
    parser.add_argument("--verify", type=Path, default=None, help="artifact to verify")
    parser.add_argument("--out", type=Path, default=None, help="output directory for --backup/--export")
    args = parser.parse_args()
    if args.backup:
        print(json.dumps(backup_database(args.db, args.out or BACKUP_DIR), ensure_ascii=False, indent=2))
    elif args.export:
        print(json.dumps(export_database(args.db, args.out or EXPORT_DIR), ensure_ascii=False, indent=2))
    elif args.restore:
        if args.to is None:
            parser.error("--restore requires --to <target-path>")
        print(json.dumps(restore_database(args.restore, args.to), ensure_ascii=False, indent=2))
    elif args.verify:
        print(json.dumps(verify_artifact(args.verify), ensure_ascii=False, indent=2))
    else:
        parser.error("choose --backup, --export, --restore <file> --to <path>, or --verify <file>")


if __name__ == "__main__":
    main()
