"""Immutable RunManifest (Harness P1-C).

One manifest per run, written at run start and never rewritten:

* code revision (``git rev-parse``), a dirty flag and a hash of the working
  diff — noted as dirty, never presented as clean;
* graph/state schema versions, planner/composer/verifier model configs,
  prompt/schema/policy hashes, corpus/index/retrieval configuration, run
  limits, feature flags and dependency versions;
* NO credentials, api keys or tokens — configuration hashes only.

Version recording is NOT semantic locking: ``check_restore_compatibility``
states what a restore must satisfy — load the same resource versions or run
an explicit compatible migration.  Old manifests missing a field read back
as ``unknown``, never as today's value.
"""
from __future__ import annotations

import hashlib
import json
import os
import logging
import subprocess
import sqlite3
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from functools import lru_cache
from typing import Any

MANIFEST_DDL = """
CREATE TABLE IF NOT EXISTS run_manifests (
    run_id TEXT PRIMARY KEY,
    manifest_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _sha(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, ensure_ascii=False, sort_keys=True,
                                     default=str).encode("utf-8")).hexdigest()[:16]


def _git(args: list[str], cwd: Path | None = None) -> str | None:
    try:
        result = subprocess.run(["git", *args], capture_output=True, text=True,
                                encoding="utf-8", errors="replace",
                                timeout=5, cwd=str(cwd) if cwd else None)
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return None


def _safe(fn, default=None):
    try:
        return fn()
    except Exception:
        return default


def _package_version(name: str) -> str:
    try:
        from importlib.metadata import version
        return version(name)
    except Exception:
        return "unknown"


@lru_cache(maxsize=512)
def _file_hash(path: str, size: int, mtime_ns: int) -> str:
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def fingerprint(path: Path) -> str:
    """Fingerprint actual resources; absence is explicit, never hash(null)."""
    try:
        stat = path.stat()
        return _file_hash(str(path.resolve()), stat.st_size, stat.st_mtime_ns)
    except FileNotFoundError:
        return "missing"
    except OSError:
        return "unknown"


@dataclass
class RunManifest:
    run_id: str
    created_at: str
    code: dict[str, Any]
    graph: dict[str, Any]
    models: dict[str, Any]
    prompts: dict[str, Any]
    policy: dict[str, Any]
    corpus: dict[str, Any]
    limits: dict[str, Any]
    feature_flags: dict[str, Any]
    dependencies: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True)


def build_manifest(*, run_id: str, agent, graph_version: str,
                   state_schema_version: str | None = None,
                   max_cycles: int = 16, review_enabled: bool | None = None) -> RunManifest:
    """Capture the full version context of one run.  Best-effort: every
    capture failure degrades to ``unknown`` instead of failing the run."""
    repo_root = Path(__file__).resolve().parents[2]

    revision = _git(["rev-parse", "HEAD"], repo_root)
    dirty = False
    diff_hash: str | None = None
    if revision is not None:
        status = _git(["status", "--porcelain"], repo_root) or ""
        dirty = bool(status.strip())
        if dirty:
            diff_hash = _sha(_git(["diff"], repo_root) or "")
    code = {"revision": revision or "unknown", "dirty": dirty,
            "diff_hash": diff_hash, "note": "dirty 表示工作区存在未提交修改"}
    code["source_tree_hash"] = _sha({str(p.relative_to(repo_root)): fingerprint(p)
                                      for p in sorted((repo_root / "stage0").rglob("*.py"))})

    # Model configuration — model ids only, never api keys.
    planner = getattr(agent, "planner", None)
    llm_planner = getattr(planner, "llm_planner", None)
    composer = getattr(agent, "response_composer", None)
    verifier = getattr(agent, "verifier", None)
    models = {
        "planner_mode": "hybrid" if getattr(planner, "enabled", False) else "deterministic",
        "planner_model": getattr(llm_planner, "model", None) or getattr(planner, "model", None),
        "composer_enabled": composer is not None,
        "composer_model": getattr(composer, "model", None),
        "verifier_enabled": verifier is not None,
        "verifier_model": getattr(verifier, "model", None),
    }

    # Prompt/schema/policy hashes — the exact objects the run used.
    try:
        from .default_tools import DEFAULT_TOOL_SPECS
        from .tools import PERMISSION_ROLES
        from .. import agent as agent_module
    except ImportError:  # pragma: no cover
        from default_tools import DEFAULT_TOOL_SPECS  # type: ignore
        from tools import PERMISSION_ROLES  # type: ignore
        import agent as agent_module  # type: ignore
    # Harness P3: prefer the agent's LIVE executor registry (which includes
    # the flag-gated P3 tools) over the static default registry.
    live_specs = getattr(getattr(agent, "executor", None), "specs", None) or DEFAULT_TOOL_SPECS
    prompts = {
        "planner_system_prompt": _sha(agent_module.PLANNER_SYSTEM_PROMPT),
        "response_system_prompt": _sha(agent_module.RESPONSE_SYSTEM_PROMPT),
        "verifier_system_prompt": _sha(agent_module.VERIFIER_SYSTEM_PROMPT),
        "canonical_proposal_schema": _sha(agent_module.CANONICAL_PROPOSAL_SCHEMA),
        "tool_catalog": _sha({name: spec.model_schema for name, spec in live_specs.items()}),
        "tool_executor_specs": _sha({name: f"{spec.kind}:{spec.required_permission}"
                                     for name, spec in live_specs.items()}),
    }

    policy = {
        "safety_rejection_limit": os.getenv("PLANNER_SAFETY_REJECTION_LIMIT", "2"),
        "permission_roles": {k: sorted(v) for k, v in PERMISSION_ROLES.items()},
        "review_enabled": (os.getenv("STAGE0_REVIEW_ENABLED", "").lower() in {"1", "true", "yes", "on"}
                           if review_enabled is None else review_enabled),
        "read_cache": os.getenv("STAGE0_READ_CACHE", ""),
        "run_reuse": os.getenv("STAGE0_RUN_REUSE", ""),
        "no_progress_limit": os.getenv("AGENT_NO_PROGRESS_LIMIT", "0"),
    }

    # Harness P3: the delegation/batching experiment configuration is part of
    # the immutable manifest — worker roles, their fixed toolsets and limits,
    # and the honest worker kind (deterministic pipeline, no model).
    try:
        from . import delegation as _delegation
        policy["delegation"] = {
            "enabled": _delegation.delegation_enabled(),
            "batch_read": os.getenv("STAGE0_READ_BATCH", ""),
            "worker_kind": _delegation.delegation_limits()["worker_kind"],
            "worker_model": _delegation.delegation_limits()["worker_model"],
            "roles": {name: {"tools": sorted(role["tools"]), "label": role["label"]}
                      for name, role in _delegation.WORKER_ROLES.items()},
            "limits": {k: v for k, v in _delegation.delegation_limits().items()
                       if k not in {"worker_kind", "worker_model"}},
        }
    except Exception:
        policy["delegation"] = "unknown"

    from .. import ddi_engine
    rag_dir = Path(os.getenv("DDI_ENGINE_RAG_INDEX_DIR") or agent_module.rag.INDEX_DIR)
    pair_path = ddi_engine._pair_index_path()
    corpus = {"rag_index_dir": str(rag_dir),
              "rag_index_override_env": os.getenv("DDI_ENGINE_RAG_INDEX_DIR"),
              "rag_index_files": {name: fingerprint(rag_dir / name) for name in
                                  ("config.json", "chunks.jsonl", "bm25_tokens.jsonl", "chunks.faiss")},
              "ddi_pair_index_path": str(pair_path), "ddi_pair_index": fingerprint(pair_path)}

    from ..turn_budget import TurnBudget
    limits = asdict(TurnBudget.from_env(max_cycles))

    feature_flags = {
        "AGENT_GRAPH_RUNNER": os.getenv("AGENT_GRAPH_RUNNER", ""),
        "STAGE0_REVIEW_ENABLED": os.getenv("STAGE0_REVIEW_ENABLED", ""),
        "STAGE0_OTEL_EXPORT": os.getenv("STAGE0_OTEL_EXPORT", ""),
        "STAGE0_DELEGATED_WORKERS": os.getenv("STAGE0_DELEGATED_WORKERS", ""),
        "STAGE0_READ_BATCH": os.getenv("STAGE0_READ_BATCH", ""),
    }

    dependencies = {name: _package_version(name) for name in
                    ("langgraph", "langchain-core", "openai", "pydantic",
                     "langgraph-checkpoint-sqlite")}

    return RunManifest(
        run_id=run_id, created_at=_now(), code=code, graph={
            "graph_version": graph_version, "state_schema_version": state_schema_version},
        models=models, prompts=prompts, policy=policy, corpus=corpus,
        limits=limits, feature_flags=feature_flags, dependencies=dependencies)


class ManifestStore:
    def __init__(self, connection: sqlite3.Connection, lock=None):
        self.connection = connection
        self._lock = lock or threading.RLock()
        with self._lock:
            self.connection.executescript(MANIFEST_DDL)
            self.connection.commit()

    def save(self, manifest: RunManifest) -> None:
        """Immutable save: an existing manifest is never overwritten."""
        with self._lock, self.connection:
            self.connection.execute(
                "INSERT OR IGNORE INTO run_manifests(run_id,manifest_json,created_at) VALUES(?,?,?)",
                (manifest.run_id, manifest.to_json(), manifest.created_at))

    def get(self, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self.connection.execute(
                "SELECT manifest_json FROM run_manifests WHERE run_id=?", (run_id,)).fetchone()
        return json.loads(row["manifest_json"]) if row else None


UNKNOWN = "unknown"

# Sections where a difference changes how a replayed run must be interpreted.
SEMANTIC_SECTIONS = ("graph", "models", "prompts", "policy", "limits", "corpus")


def manifest_diff(manifest_a: dict[str, Any], manifest_b: dict[str, Any]) -> dict[str, Any]:
    """Point out the manifest differences between two runs (or a run and the
    current configuration).  Fields missing from the older manifest report as
    ``unknown`` — never backfilled with current values."""
    differences: list[dict[str, Any]] = []
    for section in SEMANTIC_SECTIONS:
        a = manifest_a.get(section) or {}
        b = manifest_b.get(section) or {}
        for key in sorted(set(a) | set(b)):
            value_a, value_b = a.get(key, UNKNOWN), b.get(key, UNKNOWN)
            if value_a != value_b:
                differences.append({"section": section, "field": key,
                                    "manifest_a": value_a, "manifest_b": value_b})
    return {"differences": differences, "identical": not differences}


def check_restore_compatibility(manifest: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    """Restore rule (P1-C): a run resumes only with the same resource
    versions or after an explicit compatible migration; otherwise it must
    fail/park as pending, never silently reinterpret."""
    diff = manifest_diff(manifest, current)
    blocking = diff["differences"]
    return {
        "compatible": not blocking,
        "migration_required": bool(blocking),
        "differences": diff["differences"],
        "rule": "恢复必须加载相同资源版本或执行显式兼容迁移；旧 manifest 缺字段按 unknown 处理",
    }


class ManifestCompatibilityError(RuntimeError):
    """Permanent restore rejection; requires resource rollback or explicit migration."""


def enforce_restore(*, memory, agent, run_id: str, graph_version: str,
                    state_schema_version: str | None = None, review_enabled: bool = False) -> None:
    stored = ManifestStore(memory.connection, memory._lock).get(run_id)
    if stored is None:
        if graph_version != "legacy":
            raise ManifestCompatibilityError("manifest missing; restore requires explicit provenance migration")
        # Pre-manifest runs cannot claim reproducibility. P0 separately seals
        # missing usage ledgers. Do not backfill history with current values.
        logger = logging.getLogger(__name__)
        logger.warning("manifest unavailable for legacy run; provenance unknown run_id=%s", run_id)
        return
    current = build_manifest(run_id=run_id, agent=agent, graph_version=graph_version,
                             state_schema_version=state_schema_version,
                             max_cycles=agent.max_cycles, review_enabled=review_enabled).to_dict()
    # Budget limits belong to the original run. Changing env must not refill
    # that ledger or prevent P0's conservative recovery/termination.
    current["limits"] = stored.get("limits", {})
    result = check_restore_compatibility(stored, current)
    if not result["compatible"]:
        fields = ", ".join(f"{d['section']}.{d['field']}" for d in result["differences"])
        raise ManifestCompatibilityError(f"manifest incompatible; explicit migration required: {fields}")
