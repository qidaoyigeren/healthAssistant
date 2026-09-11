"""Canonical source fingerprints — one place that answers "which source
produced this result?".

There were five separate implementations across ``scripts/`` and ``stage0/``,
covering DIFFERENT file sets.  Comparing digests taken from two different
scopes yields a false "source changed" verdict: the live-acceptance entry
includes ``scripts/`` while ``verify-agent-closeout.py`` does not, so those two
numbers can never be equal however unchanged the tree is.  That false alarm
cost real debugging time, hence this module.

Scopes (each a superset of the previous one):

=================  ==========================================================
``runtime``        ``stage0/**/*.py`` — what the agent executes
``app``            ``runtime`` + ``frontend/src/**/*.ts``, ``*.tsx``
``repo``           ``app`` + ``scripts/*.py`` — **binding** for acceptance
=================  ==========================================================

``repo`` is the binding scope: it is the widest, so a change anywhere the
project ships is visible to it.  A record that wants to cite a comparable
fingerprint must use ``repo``; use a narrower scope only when the wider one is
genuinely irrelevant, and say which you used.

Compatibility note: the digest joins ``str(relative_path)``, which is
separator-dependent, so a digest is stable within one platform but NOT across
platforms (Windows yields ``stage0\\agent.py``).  This is retained deliberately
— normalising it would silently invalidate every fingerprint already recorded
under ``docs/``.  Changing it is a deliberate, label-worthy act, not a cleanup.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

SCOPE_RUNTIME = "runtime"
SCOPE_APP = "app"
SCOPE_REPO = "repo"
SCOPES = (SCOPE_RUNTIME, SCOPE_APP, SCOPE_REPO)

# The widest scope: cite this one when a result must be comparable.
BINDING_SCOPE = SCOPE_REPO


def scope_description(scope: str = BINDING_SCOPE) -> list[str]:
    """Human-readable file set, for recording alongside a digest."""
    return {
        SCOPE_RUNTIME: ["stage0/**/*.py"],
        SCOPE_APP: ["stage0/**/*.py", "frontend/src/**/*.ts", "frontend/src/**/*.tsx"],
        SCOPE_REPO: ["stage0/**/*.py", "frontend/src/**/*.ts",
                     "frontend/src/**/*.tsx", "scripts/*.py"],
    }[scope]


def source_files(scope: str = BINDING_SCOPE, extra: tuple = ()) -> list[Path]:
    """Deterministic, sorted file list for ``scope`` plus any ``extra`` paths.

    ``__pycache__`` is excluded defensively (it holds no ``.py`` today, so the
    filter is a no-op — it exists so a stray generated file cannot silently
    change a digest).
    """
    if scope not in SCOPES:
        raise ValueError(f"unknown fingerprint scope {scope!r}; expected one of {SCOPES}")
    paths = list((ROOT / "stage0").rglob("*.py"))
    if scope in (SCOPE_APP, SCOPE_REPO):
        paths += list((ROOT / "frontend" / "src").rglob("*.ts"))
        paths += list((ROOT / "frontend" / "src").rglob("*.tsx"))
    if scope == SCOPE_REPO:
        paths += list(ROOT.glob("scripts/*.py"))
    # ``extra`` may be given absolute or repo-relative; normalise so the
    # caller cannot silently contribute a path that relative_to(ROOT) rejects.
    for entry in extra:
        candidate = Path(entry)
        paths.append(candidate if candidate.is_absolute() else ROOT / candidate)
    return sorted(p for p in paths if p.is_file() and "__pycache__" not in str(p))


def source_fingerprint(scope: str = BINDING_SCOPE, extra: tuple = ()) -> str:
    """Single digest over the scope's contents."""
    return hashlib.sha256(
        b"".join(str(p.relative_to(ROOT)).encode() + p.read_bytes()
                 for p in source_files(scope, extra))
    ).hexdigest()


def source_fingerprint_map(scope: str = BINDING_SCOPE, extra: tuple = ()) -> dict[str, str]:
    """Per-file digests keyed by POSIX relative path (for change attribution:
    which file moved, not merely that something did)."""
    return {p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in source_files(scope, extra)}
