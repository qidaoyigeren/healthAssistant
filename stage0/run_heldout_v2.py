"""Stage 9 (C1) attribution run: held-out DDI evaluation against the v2 corpus.

Run from the repository root::

    python stage0/run_heldout_v2.py

Behaviour: backs up the git-tracked v1 held-out artifacts, points the DDI
engine at ``rag_index_v2`` via ``DDI_ENGINE_RAG_INDEX_DIR``, runs
``--heldout-evaluate --live-kegg`` (fresh KEGG + RAG/LLM fallback caches, so
the recall delta is attributed to the corpus change alone), renames the new
outputs to ``*_v2`` and restores the v1 artifacts via git.  Requires network
(KEGG) and a funded provider key in ``stage0/.env``.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "stage0" / "data"
STRUCTURED = DATA / "structured"

HELDOUT_FILES = [
    STRUCTURED / "engine_heldout_metrics.json",
    STRUCTURED / "engine_heldout_predictions.jsonl",
    DATA / "heldout_ddi_pair_index.json",
    DATA / "heldout_kegg_ddi_cache.json",
    DATA / "heldout_ddi_fallback_cache.json",
]


def main() -> int:
    for path in HELDOUT_FILES:
        if not path.exists():
            print(f"missing artifact: {path}")
            return 1
    environment = dict(os.environ)
    environment["DDI_ENGINE_RAG_INDEX_DIR"] = str(STRUCTURED / "rag_index_v2")
    result = subprocess.run(
        [sys.executable, str(ROOT / "stage0" / "ddi_engine.py"),
         "--heldout-evaluate", "--live-kegg"],
        cwd=ROOT, env=environment, capture_output=True, text=True,
    )
    print(result.stdout[-4000:])
    if result.returncode != 0:
        print("STDERR:", result.stderr[-2000:])
        return result.returncode
    # Preserve the fresh v2 outputs, then restore the recorded v1 artifacts.
    renames = {
        "engine_heldout_metrics.json": "engine_heldout_v2_metrics.json",
        "engine_heldout_predictions.jsonl": "engine_heldout_v2_predictions.jsonl",
        "heldout_ddi_pair_index.json": "heldout_v2_ddi_pair_index.json",
        "heldout_kegg_ddi_cache.json": "heldout_v2_kegg_ddi_cache.json",
        "heldout_ddi_fallback_cache.json": "heldout_v2_ddi_fallback_cache.json",
    }
    for path in HELDOUT_FILES:
        target = path.with_name(renames[path.name])
        if path.exists():
            shutil.copyfile(path, target)
    subprocess.run(["git", "checkout", "--",
                    *[str(p.relative_to(ROOT)) for p in HELDOUT_FILES]],
                   cwd=ROOT, check=False)
    metrics_path = STRUCTURED / "engine_heldout_v2_metrics.json"
    if metrics_path.exists():
        metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
        summary = {key: metrics.get(key) for key in (
            "pair_precision", "pair_recall", "pair_f1",
            "severity_accuracy", "high_risk_severity_accuracy",
            "chinese_citation_coverage")}
        print("V2_HELDOUT_SUMMARY", json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
