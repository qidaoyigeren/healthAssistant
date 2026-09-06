"""Runtime two-path drug-drug interaction (DDI) detector.

The public entry point is ``detect(medication_list: list[str]) -> list[warning]``.
Inputs are normalized to active ingredients, including every ingredient in a
combination product, before unordered ingredient pairs are enumerated.

Operational severity rubric (detector policy, not clinical validation):

* ``contraindicated``: the Chinese source explicitly says 禁忌/严禁/禁用, or
  KEGG marks the pair CI.
* ``major``: the source says 避免/不宜同用, or describes serious toxicity such
  as bleeding, arrhythmia, rhabdomyolysis, lactic acidosis, hyperkalemia,
  acute renal toxicity, or an equivalently serious outcome.
* ``moderate``: the source calls for caution, dose adjustment or monitoring, or
  documents a clinically relevant but milder effect.
* ``minor``: the source describes a clinically negligible interaction.
* ``unknown``: no usable severity evidence is present.

KEGG CI/P is an anchor signal rather than the sole evidence source: CI fixes the
final level at ``contraindicated``; P is disambiguated by grounded label text or
the hardened extractor, and otherwise defaults conservatively to ``moderate``.
This module is detector infrastructure only. It is not a clinical decision
support system and its evaluation is not clinical validation.

Runtime controls are environment variables so the public function keeps the
single required signature. ``DDI_ENGINE_LIVE_KEGG=1`` enables cache-missing KEGG
requests (the reused client enforces <=1 request/second). ``DDI_ENGINE_ENABLE_LLM``
and ``DDI_ENGINE_ENABLE_RAG`` can disable new fallback work; previously extracted
grounded evidence remains available in offline mode.

``evaluate()`` is deliberately a regression replay over the persisted Stage
0/1 evidence and sets ``generalization_claim`` to false. ``evaluate_heldout()``
is a separate live evaluator: it audits pairs against the shared caches and
Stage 1 scope, then uses ``data/heldout_*`` cache paths so fresh KEGG/RAG/LLM
results cannot inflate the regression numbers.
"""
from __future__ import annotations

import argparse
import itertools
import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

# The Stage 0 files are intentionally usable both as ``python stage0/foo.py``
# and as ``import stage0.foo`` from the repository root.  The existing Stage 0
# modules use top-level imports internally, so expose their directory before
# importing them rather than duplicating or rewriting those modules.
ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import crosscheck_eval
import extract_ddi
import normalize as normalizer
import ontology
import rag

DATA = ROOT / "data"
STRUCTURED = DATA / "structured"
PAIR_INDEX_PATH = DATA / "ddi_pair_index.json"
FALLBACK_CACHE_PATH = DATA / "ddi_fallback_cache.json"
EVAL_CASES_PATH = DATA / "engine_eval_cases.jsonl"
EVAL_METRICS_PATH = STRUCTURED / "engine_eval_metrics.json"
REGRESSION_METRICS_PATH = STRUCTURED / "engine_regression_metrics.json"
HELDOUT_CASES_PATH = DATA / "engine_heldout_cases.jsonl"
HELDOUT_METRICS_PATH = STRUCTURED / "engine_heldout_metrics.json"
HELDOUT_PAIR_INDEX_PATH = DATA / "heldout_ddi_pair_index.json"
HELDOUT_KEGG_CACHE_PATH = DATA / "heldout_kegg_ddi_cache.json"
HELDOUT_FALLBACK_CACHE_PATH = DATA / "heldout_ddi_fallback_cache.json"

SEVERITIES = ("contraindicated", "major", "moderate", "minor", "unknown")
SEVERITY_RANK = {value: len(SEVERITIES) - index for index, value in enumerate(SEVERITIES)}
CHINESE_RE = re.compile(r"[\u3400-\u9fff]")
KEGG_PAIR_RE = re.compile(r"^D\d{5} D\d{5}$")

_MAPPING_ROWS: list[dict] | None = None
_NORMALIZE_INDEX: dict[str, dict] | None = None
_CATALOG: dict[str, Any] | None = None
_PAIR_INDEX: dict | None = None
_PAIR_INDEX_PATH_LOADED: Path | None = None
_EVIDENCE_INDEX: dict[tuple[str, str], list[dict]] | None = None
_RETRIEVER: rag.HybridRetriever | None = None


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text("utf-8").splitlines() if line.strip()]


def _write_json(path: Path, value: Any) -> None:
    """Write a small runtime cache atomically."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", "utf-8")
    temporary.replace(path)


def _enabled(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off", ""}


def _configured_path(env_name: str, default: Path) -> Path:
    """Resolve a cache path at call time so an eval can use an isolated store."""
    raw = os.getenv(env_name)
    return Path(raw).expanduser() if raw else default


def _pair_index_path() -> Path:
    return _configured_path("DDI_ENGINE_PAIR_INDEX_PATH", PAIR_INDEX_PATH)


def _kegg_cache_path() -> Path:
    return _configured_path("DDI_ENGINE_KEGG_CACHE_PATH", crosscheck_eval.CACHE)


def _fallback_cache_path() -> Path:
    return _configured_path("DDI_ENGINE_FALLBACK_CACHE_PATH", FALLBACK_CACHE_PATH)


def _mapping_rows() -> list[dict]:
    """Reuse normalize + ontology while tolerating a not-yet-extended mapping."""
    global _MAPPING_ROWS
    if _MAPPING_ROWS is None:
        rows = normalizer.load_mapping()
        seen = {
            (ingredient.get("kegg"), ingredient.get("name_en", "").lower())
            for row in rows for ingredient in row.get("ingredients", [])
        }
        for row in ontology.mapping_rows():
            ingredient = row["ingredients"][0]
            key = (ingredient.get("kegg"), ingredient.get("name_en", "").lower())
            if key not in seen:
                rows.append(row)
                seen.add(key)
        _MAPPING_ROWS = rows
    return _MAPPING_ROWS


def _normalize_index() -> dict[str, dict]:
    global _NORMALIZE_INDEX
    if _NORMALIZE_INDEX is None:
        _NORMALIZE_INDEX = normalizer.build_index(_mapping_rows())
    return _NORMALIZE_INDEX


def _ingredient_key(ingredient: dict) -> str:
    return ingredient.get("kegg") or normalizer.compact(ingredient["name_cn"])


def _pair_name_key(a: dict | str, b: dict | str) -> tuple[str, str]:
    left = a if isinstance(a, str) else a["name_cn"]
    right = b if isinstance(b, str) else b["name_cn"]
    return tuple(sorted((left, right)))


def _catalog() -> dict[str, Any]:
    global _CATALOG
    if _CATALOG is not None:
        return _CATALOG
    by_en: dict[str, dict] = {}
    by_cn: dict[str, dict] = {}
    by_kegg: dict[str, dict] = {}
    aliases: dict[str, set[str]] = defaultdict(set)
    for row in _mapping_rows():
        row_aliases = [row.get("generic_cn", ""), *row.get("brands", []), *row.get("aliases", [])]
        for ingredient in row.get("ingredients", []):
            item = {
                "name_cn": ingredient["name_cn"],
                "name_en": ingredient.get("name_en"),
                "kegg": ingredient.get("kegg"),
            }
            by_cn[normalizer.compact(item["name_cn"])] = item
            if item.get("name_en"):
                by_en[item["name_en"].lower()] = item
            if item.get("kegg"):
                by_kegg[item["kegg"]] = item
            aliases[item["name_cn"]].update(x for x in row_aliases if x)
            aliases[item["name_cn"]].add(item["name_cn"])
            if item.get("name_en"):
                aliases[item["name_cn"]].add(item["name_en"])
    _CATALOG = {"by_en": by_en, "by_cn": by_cn, "by_kegg": by_kegg, "aliases": aliases}
    return _CATALOG


def _resolve_concept(name: str | None) -> dict | None:
    if not name:
        return None
    catalog = _catalog()
    lowered = name.strip().lower()
    if lowered in catalog["by_en"]:
        return catalog["by_en"][lowered]
    compacted = normalizer.compact(name)
    if compacted in catalog["by_cn"]:
        return catalog["by_cn"][compacted]
    result = normalizer.normalize(name, _normalize_index())
    if result["matched"] and len(result["ingredients"]) == 1:
        ingredient = result["ingredients"][0]
        return {
            "name_cn": ingredient["name_cn"], "name_en": ingredient.get("name_en"),
            "kegg": ingredient.get("kegg"),
        }
    return None


def normalize_medications(medication_list: list[str]) -> list[dict]:
    """Normalize and deduplicate all active ingredients in input order."""
    if not isinstance(medication_list, list) or any(not isinstance(item, str) for item in medication_list):
        raise TypeError("medication_list must be list[str]")
    unique: dict[str, dict] = {}
    for input_name in medication_list:
        if not input_name.strip():
            continue
        result = normalizer.normalize(input_name, _normalize_index())
        for ingredient in result["ingredients"]:
            item = {
                "name_cn": ingredient["name_cn"],
                "name_en": ingredient.get("name_en"),
                "kegg": ingredient.get("kegg"),
                "input_names": [input_name],
            }
            key = _ingredient_key(item)
            if key in unique:
                if input_name not in unique[key]["input_names"]:
                    unique[key]["input_names"].append(input_name)
            else:
                unique[key] = item
    return list(unique.values())


def build_pair_index(path: Path | None = None, cache_path: Path | None = None) -> dict:
    """Persist every KEGG pair already present in the selected cache.

    With no arguments this preserves the shared Stage 1 index.  The optional
    paths are used by the held-out evaluator to build an empty, isolated index
    before any live KEGG requests are made.
    """
    path = path or _pair_index_path()
    cache_path = cache_path or _kegg_cache_path()
    catalog = _catalog()
    cache = crosscheck_eval.load_cache(cache_path)
    entries: dict[str, dict] = {}
    checked_not_found: list[str] = []
    for key, result in sorted(cache.items()):
        if not KEGG_PAIR_RE.fullmatch(key) or not isinstance(result, dict):
            continue
        if result.get("status") != "matched":
            checked_not_found.append(key)
            continue
        kegg_a, kegg_b = key.split()
        rows = result.get("rows") or []
        entries[key] = {
            "drug_a": catalog["by_kegg"].get(kegg_a, {"kegg": kegg_a}),
            "drug_b": catalog["by_kegg"].get(kegg_b, {"kegg": kegg_b}),
            "levels": sorted({row.get("level") for row in rows if row.get("level")}),
            "mechanisms": sorted({row.get("mechanism") for row in rows if row.get("mechanism")}),
            "source_url": result.get("url") or f"https://rest.kegg.jp/ddi/{kegg_a}+{kegg_b}",
        }
    universe = sorted(
        ({"name_cn": item["name_cn"], "name_en": item.get("name_en"), "kegg": item["kegg"]}
         for item in catalog["by_kegg"].values()),
        key=lambda item: item["kegg"],
    )
    output = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": "data/kegg_ddi_cache.json via crosscheck_eval.kegg_ddi",
        "universe_size": len(universe),
        "universe": universe,
        "cached_pair_count": len(entries) + len(checked_not_found),
        "matched_pair_count": len(entries),
        "checked_not_found_count": len(checked_not_found),
        "entries": entries,
        "checked_not_found": checked_not_found,
    }
    _write_json(path, output)
    global _PAIR_INDEX, _PAIR_INDEX_PATH_LOADED
    _PAIR_INDEX = output
    _PAIR_INDEX_PATH_LOADED = path.resolve()
    return output


def _load_pair_index() -> dict:
    global _PAIR_INDEX, _PAIR_INDEX_PATH_LOADED
    path = _pair_index_path()
    resolved_path = path.resolve()
    if _PAIR_INDEX is None or _PAIR_INDEX_PATH_LOADED != resolved_path:
        if path.exists():
            _PAIR_INDEX = json.loads(path.read_text("utf-8"))
        else:
            _PAIR_INDEX = build_pair_index(path=path, cache_path=_kegg_cache_path())
        _PAIR_INDEX_PATH_LOADED = resolved_path
    return _PAIR_INDEX


def _kegg_lookup(a: dict, b: dict) -> dict | None:
    if not a.get("kegg") or not b.get("kegg"):
        return None
    key = " ".join(sorted((a["kegg"], b["kegg"])))
    index = _load_pair_index()
    if key in index["entries"]:
        return index["entries"][key]
    if key in set(index.get("checked_not_found", [])) or not _enabled("DDI_ENGINE_LIVE_KEGG", False):
        return None
    cache_path = _kegg_cache_path()
    result = crosscheck_eval.kegg_ddi(
        a["kegg"], b["kegg"], crosscheck_eval.load_cache(cache_path), cache_path=cache_path,
    )
    # Rebuild from the selected cache, including a negative result.  In live
    # held-out mode this is the isolated cache, never the Stage 1 replay cache.
    index = build_pair_index(path=_pair_index_path(), cache_path=cache_path)
    return index["entries"].get(key) if result.get("status") == "matched" else None


def _infer_text_severity(text: str, supplied: str | None = None) -> str:
    """Apply the module rubric to a Chinese evidence span."""
    compact = re.sub(r"\s+", "", text or "")
    if re.search(r"禁忌|严禁|禁用", compact):
        return "contraindicated"
    major_markers = (
        r"不宜.{0,8}(同用|合用)|避免.{0,8}(同用|合用|使用)|不应.{0,8}(同用|合用)|不建议.{0,8}(同用|合用)",
        r"横纹肌溶解|乳酸酸中毒|心律失常|心率失常|室性心动过速|室颤|出血|胃肠潜血",
        r"高钾血症|严重低血压|严重心动过缓|急性肾功能衰竭|肾毒性|明显中毒|药物中毒",
        r"增强.{0,12}抗凝|抗凝.{0,12}增强|会引起.{0,12}(血药|血).{0,6}浓度升高",
    )
    if any(re.search(pattern, compact) for pattern in major_markers):
        return "major"
    if re.search(r"临床上可忽略|无临床意义|轻微且无需处理", compact):
        return "minor"
    if re.search(r"谨慎|慎用|小心|监测|调整|减量|减少.{0,8}剂量|风险|增加|升高|降低|减弱|增强|影响|延长", compact):
        # A validated extractor severity is allowed to preserve a stronger
        # label when the same span also contains generic monitoring language.
        if supplied in {"contraindicated", "major"}:
            return supplied
        return "moderate"
    return supplied if supplied in SEVERITIES else "unknown"


def _effect_from_tag(tag: str | None) -> str | None:
    return {
        "increased_drug_exposure": "药物暴露或血药浓度升高",
        "decreased_drug_exposure": "药物暴露或血药浓度降低",
        "arrhythmia": "心律失常风险增加",
        "reduced_efficacy": "疗效减弱",
        "bleeding": "出血风险增加",
        "gastrointestinal_injury": "胃肠道损伤风险增加",
        "myopathy_rhabdomyolysis": "肌病或横纹肌溶解风险增加",
        "hypoglycemia": "低血糖风险增加",
        "other_toxicity": "毒性风险增加",
        "hyperkalemia": "高钾血症风险增加",
        "enhanced_pharmacologic_effect": "药理作用增强",
    }.get(tag or "")


def _evidence_record(source: dict, a: dict, b: dict, origin: str, *, curated: bool = False) -> dict:
    text = source.get("source_text") or source.get("text") or ""
    supplied = source.get("severity")
    severity = supplied if curated and supplied in SEVERITIES else _infer_text_severity(text, supplied)
    return {
        "drug_a": a["name_cn"],
        "drug_b": b["name_cn"],
        "severity": severity,
        "mechanism": source.get("mechanism"),
        "effect": source.get("effect") or _effect_from_tag(source.get("effect_tag")),
        "management": source.get("management"),
        "source_text": text,
        "source_url": source.get("source_url"),
        "origin": origin,
        "curated": curated,
    }


def _class_members(class_ids: Iterable[str]) -> list[dict]:
    out: dict[str, dict] = {}
    for class_id in class_ids:
        for member in ontology.ONTOLOGY[class_id]["members"]:
            item = _resolve_concept(member["name_en"])
            if item is None:
                item = {
                    "name_cn": member["generic_cn"],
                    "name_en": member["name_en"],
                    "kegg": member.get("kegg"),
                }
            out[_ingredient_key(item)] = item
    return list(out.values())


_CLASS_HINTS: dict[str, tuple[str, ...]] = {
    "cardiac_glycosides": ("洋地黄", "强心苷", "digoxin class", "cardiac glycoside"),
    "nsaids": ("非甾体", "nsaid", "nonsteroidal"),
    "anticoagulants_thrombolytics": ("抗凝", "溶栓", "anticoagul", "thrombol"),
    "corticosteroids": ("糖皮质激素", "皮质激素", "corticosteroid", "glucocorticoid"),
    "oral_azole_antifungals": ("咪唑类抗真菌", "口服咪唑", "azole", "咪康唑"),
    "hypoglycemia_potentiating_drugs": ("水杨酸", "保泰松", "磺胺类", "增强降糖", "listed interacting"),
    "antacids_mineral_laxatives": ("抗酸", "导泻", "含钙镁", "antacid", "laxative", "mineral"),
    "potassium_raising_drugs": ("含钾", "钾补充", "升高血钾", "potassium", "potassium-raising"),
    "potassium_sparing_diuretics": ("保钾", "potassium-sparing"),
    "sulfonylureas": ("磺酰脲", "sulfonylurea"),
    "oral_antidiabetics": ("口服降糖", "口服抗糖尿病", "oral antidiabetic"),
    "glucose_lowering_drugs": ("降糖", "胰岛素", "glucose-lowering", "hypoglycemic"),
    "folate_antagonists": ("叶酸拮抗", "甲氨蝶呤", "antifolate"),
    "cyp2d6_inhibitors": ("cyp2d6",),
    "coumarin_anticoagulants": ("香豆素", "双香豆素", "coumarin"),
}


def _class_ids_from_text(value: str | None) -> list[str]:
    compact = re.sub(r"\s+", "", (value or "").lower())
    if not compact:
        return []
    return [
        class_id for class_id, hints in _CLASS_HINTS.items()
        if any(re.sub(r"\s+", "", hint.lower()) in compact for hint in hints)
    ]


def _manual_partner_members(row: dict) -> list[dict]:
    if not row.get("partner_class"):
        item = _resolve_concept(row.get("drug_b_en")) or _resolve_concept(row.get("drug_b"))
        return [item] if item else []
    names = " ".join(str(row.get(key) or "") for key in ("drug_b", "drug_b_en"))
    if "造影剂" in names or "contrast" in names.lower():
        item = _resolve_concept("含碘造影剂")
        return [item] if item else []
    class_ids = _class_ids_from_text(names)
    if class_ids:
        return _class_members(class_ids)
    item = _resolve_concept(row.get("drug_b_en")) or _resolve_concept(row.get("drug_b"))
    return [item] if item else []


def _build_evidence_index() -> dict[tuple[str, str], list[dict]]:
    """Index grounded Stage 1 outputs as the warm fallback-result cache."""
    index: dict[tuple[str, str], list[dict]] = defaultdict(list)
    # The twelve hand-audited records are preferred when present.
    for row in _read_jsonl(DATA / "ddi_labeled.jsonl"):
        source = _resolve_concept(row.get("drug_a_en")) or _resolve_concept(row.get("drug_a"))
        if not source:
            continue
        for partner in _manual_partner_members(row):
            if _ingredient_key(source) == _ingredient_key(partner):
                continue
            evidence = _evidence_record(row, source, partner, "curated_label_cache", curated=True)
            index[_pair_name_key(source, partner)].append(evidence)

    # These are prior calls through extract_ddi.PROMPT/TOOL, preserving all six
    # hardened evidence gates. Class partners are expanded only by ontology.py.
    for row in _read_jsonl(STRUCTURED / "production_eval_predictions_30.jsonl"):
        prediction = row.get("prediction")
        if not isinstance(prediction, dict) or not prediction:
            continue
        source = _resolve_concept((row.get("expected") or {}).get("drug_a"))
        if not source:
            continue
        case_id = row.get("case_id")
        if case_id in ontology.CASE_CLASS_IDS:
            partners = _class_members(ontology.CASE_CLASS_IDS[case_id])
        else:
            partner_name = crosscheck_eval.SINGLE_PARTNER.get(case_id, "")
            partner = _resolve_concept(partner_name)
            partners = [partner] if partner else []
        source_row = {**prediction, "text": row.get("text"), "source_url": row.get("source_url")}
        for partner in partners:
            if _ingredient_key(source) == _ingredient_key(partner):
                continue
            evidence = _evidence_record(source_row, source, partner, "hardened_llm_cache")
            index[_pair_name_key(source, partner)].append(evidence)
    return index


def _evidence_index() -> dict[tuple[str, str], list[dict]]:
    global _EVIDENCE_INDEX
    if _EVIDENCE_INDEX is None:
        _EVIDENCE_INDEX = _build_evidence_index()
    return _EVIDENCE_INDEX


def _aliases(ingredient: dict) -> set[str]:
    values = set(_catalog()["aliases"].get(ingredient["name_cn"], set()))
    values.add(ingredient["name_cn"])
    if ingredient.get("name_en"):
        values.add(ingredient["name_en"])
    return {re.sub(r"\s+", "", value).lower() for value in values if len(value.strip()) >= 2}


def _mentions(text: str, ingredient: dict) -> bool:
    compact = re.sub(r"\s+", "", text).lower()
    return any(alias in compact for alias in _aliases(ingredient))


def _load_fallback_cache() -> dict:
    path = _fallback_cache_path()
    if not path.exists():
        return {"schema_version": 1, "pairs": {}}
    try:
        value = json.loads(path.read_text("utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"schema_version": 1, "pairs": {}}
    if not isinstance(value, dict) or not isinstance(value.get("pairs"), dict):
        return {"schema_version": 1, "pairs": {}}
    return value


def _get_retriever() -> rag.HybridRetriever:
    global _RETRIEVER
    if _RETRIEVER is None:
        # Stage 9 (C1): allow attribution runs to point at an alternate index
        # (e.g. the theophylline-fixed v2 corpus) without touching the v1
        # artifacts the recorded baselines were measured against.
        index_dir = os.getenv("DDI_ENGINE_RAG_INDEX_DIR")
        if index_dir:
            _RETRIEVER = rag.HybridRetriever(index_dir=Path(index_dir), local_files_only=True)
        else:
            _RETRIEVER = rag.HybridRetriever(local_files_only=True)
    return _RETRIEVER


def _rag_candidates(a: dict, b: dict) -> list[tuple[dict, dict, dict]]:
    """Return (chunk, source ingredient, partner ingredient) candidates."""
    if not _enabled("DDI_ENGINE_ENABLE_RAG", True):
        return []
    try:
        retriever = _get_retriever()
        results = []
        # Searching in both orientations helps when only one drug is used as
        # the label title in the curated corpus.  The final mention checks are
        # still deterministic and prevent a merely semantically similar chunk
        # from being sent to the extractor.
        for query in (
            f"{a['name_cn']} {b['name_cn']} 药物相互作用 禁忌 注意事项",
            f"{b['name_cn']} {a['name_cn']} 药物相互作用 禁忌 注意事项",
        ):
            results.extend(retriever.search(query, mode="hybrid", top_k=12))
    except Exception:
        # Optional local model/index failures must not disable structured hits.
        return []
    candidates: list[tuple[dict, dict, dict]] = []
    seen: set[str] = set()
    for result in results:
        chunk = result.chunk
        if chunk["chunk_id"] in seen:
            continue
        seen.add(chunk["chunk_id"])
        label = chunk.get("drug_name", "")
        text = chunk.get("text", "")
        if _mentions(label, a) and _mentions(text, b):
            candidates.append((chunk, a, b))
        elif _mentions(label, b) and _mentions(text, a):
            candidates.append((chunk, b, a))
    # Two independently retrieved exact-mention chunks balance evidence
    # redundancy against live-provider cost. Additional chunks were dominated
    # by duplicate label versions in the held-out pilot.
    return candidates[:2]


def _partner_claim_supported(source_text: str, partner: dict) -> bool:
    """Require the quoted Chinese text to name the ingredient or its class."""
    if _mentions(source_text, partner):
        return True
    for class_id, definition in ontology.ONTOLOGY.items():
        if not any(
            member.get("kegg") == partner.get("kegg")
            or normalizer.compact(member.get("generic_cn", "")) == normalizer.compact(partner.get("name_cn", ""))
            for member in definition["members"]
        ):
            continue
        terms = (
            definition.get("label_cn", ""),
            *definition.get("aliases", []),
            *_CLASS_HINTS.get(class_id, ()),
        )
        compact = re.sub(r"\s+", "", source_text).lower()
        if any(re.sub(r"\s+", "", term.lower()) in compact for term in terms if term):
            return True
    return False


def _live_fallback(a: dict, b: dict) -> list[dict]:
    key = " | ".join(_pair_name_key(a, b))
    cache = _load_fallback_cache()
    cached = cache["pairs"].get(key)
    if isinstance(cached, dict) and cached:
        if cached.get("status") == "matched":
            return cached.get("evidence", [])
        if cached.get("status") in {"not_found", "error"}:
            return []
    candidates = _rag_candidates(a, b)
    if not candidates or not _enabled("DDI_ENGINE_ENABLE_LLM", True):
        return []
    try:
        config = extract_ddi.resolve_llm_config()
    except RuntimeError:
        return []
    try:
        client = extract_ddi.create_llm_client(config)
    except Exception:
        return []
    evidence: list[dict] = []
    attempted_chunks: list[str] = []
    errors: list[str] = []
    for chunk, source, partner in candidates:
        attempted_chunks.append(chunk["chunk_id"])
        try:
            triples = extract_ddi._production_call(
                client, config["model"],
                chunk["drug_name"], chunk["text"], delay=1.0,
            )
        except Exception as exc:
            errors.append(f"{type(exc).__name__}: {exc}")
            continue
        for triple in triples:
            if not isinstance(triple, dict):
                continue
            quote = triple.get("source_text") or ""
            if not isinstance(quote, str):
                continue
            if quote not in chunk["text"] or not _partner_claim_supported(quote, partner):
                continue
            source_row = {**triple, "source_url": chunk.get("source_url")}
            evidence.append(_evidence_record(source_row, source, partner, "rag+llm"))
    cache["pairs"][key] = {
        "status": "matched" if evidence else ("error" if errors else "not_found"),
        "evidence": evidence,
        "attempted_chunk_ids": attempted_chunks,
        "errors": errors,
        "cached_at": datetime.now(timezone.utc).isoformat(),
        "prompt": "extract_ddi.PROMPT (Stage 1b hardened six-gate prompt)",
    }
    _write_json(_fallback_cache_path(), cache)
    return evidence


def _fallback_lookup(a: dict, b: dict) -> list[dict]:
    warm = _evidence_index().get(_pair_name_key(a, b), [])
    return warm or _live_fallback(a, b)


def _select_evidence(evidence: list[dict]) -> dict | None:
    if not evidence:
        return None
    return sorted(
        evidence,
        key=lambda item: (
            not item.get("curated", False),
            not bool(item.get("source_url")),
            -SEVERITY_RANK.get(item.get("severity", "unknown"), 0),
            len(item.get("source_text") or ""),
        ),
    )[0]


def _evidence_severity_conflict(evidence: list[dict]) -> bool:
    severities = {
        item.get("severity")
        for item in evidence
        if item.get("severity") in {"contraindicated", "major", "moderate", "minor"}
    }
    return len(severities) > 1


def _merge_warning(a: dict, b: dict, kegg: dict | None, evidence_rows: list[dict]) -> dict | None:
    evidence = _select_evidence(evidence_rows)
    if not kegg and not evidence:
        return None
    levels = set(kegg.get("levels", [])) if kegg else set()
    evidence_severity = evidence.get("severity", "unknown") if evidence else "unknown"
    conflict = _evidence_severity_conflict(evidence_rows)
    if "CI" in levels:
        severity = "contraindicated"
        conflict = conflict or bool(evidence and evidence_severity != "contraindicated")
    elif "P" in levels:
        severity = evidence_severity if evidence_severity in {"contraindicated", "major", "moderate"} else "moderate"
        conflict = conflict or evidence_severity == "minor"
    else:
        severity = evidence_severity
    if kegg and evidence:
        confidence = "low" if conflict or evidence_severity == "unknown" else "high"
        detection_path = "kegg+fallback_cache" if evidence.get("origin") != "rag+llm" else "kegg+rag+llm"
    elif kegg:
        confidence = "medium"
        detection_path = "kegg"
    else:
        confidence = "low" if conflict or evidence_severity == "unknown" else "medium"
        detection_path = "fallback_cache" if evidence.get("origin") != "rag+llm" else "rag+llm"
    mechanisms = kegg.get("mechanisms", []) if kegg else []
    kegg_mechanism = next((item for item in mechanisms if item and item != "unclassified"), None)
    if kegg_mechanism is None:
        kegg_mechanism = next((item for item in mechanisms if item), None)
    cited_evidence = bool(evidence and evidence.get("source_text") and evidence.get("source_url"))
    return {
        "drug_a": a["name_cn"],
        "drug_b": b["name_cn"],
        "severity": severity,
        "mechanism": (evidence or {}).get("mechanism") or kegg_mechanism,
        "effect": (evidence or {}).get("effect"),
        "management": (evidence or {}).get("management"),
        # Keep the quote and URL paired.  A KEGG URL must not be presented as
        # the citation for a Chinese label quote that has no label URL.
        "source_text": (evidence or {}).get("source_text") if cited_evidence else None,
        "source_url": (evidence or {}).get("source_url") or (kegg or {}).get("source_url"),
        "confidence": confidence,
        "detection_path": detection_path,
    }


def detect(medication_list: list[str]) -> list[dict]:
    """Return ranked, cited warnings for normalized active-ingredient pairs."""
    ingredients = normalize_medications(medication_list)
    warnings: list[dict] = []
    for a, b in itertools.combinations(ingredients, 2):
        if _ingredient_key(a) == _ingredient_key(b):
            continue
        warning = _merge_warning(a, b, _kegg_lookup(a, b), _fallback_lookup(a, b))
        if warning:
            warnings.append(warning)
    warnings.sort(
        key=lambda item: (
            -SEVERITY_RANK.get(item["severity"], 0),
            {"high": 0, "medium": 1, "low": 2}.get(item["confidence"], 3),
            item["drug_a"], item["drug_b"],
        )
    )
    return warnings


def evaluate(
    cases_path: Path = EVAL_CASES_PATH,
    metrics_path: Path = EVAL_METRICS_PATH,
) -> dict:
    """Run the frozen scenario regression set with network/model calls disabled."""
    cases = _read_jsonl(cases_path)
    if len(cases) < 24:
        raise ValueError("engine evaluation requires at least 24 medication lists")
    previous = {name: os.environ.get(name) for name in (
        "DDI_ENGINE_LIVE_KEGG", "DDI_ENGINE_ENABLE_LLM", "DDI_ENGINE_ENABLE_RAG",
    )}
    os.environ.update({
        "DDI_ENGINE_LIVE_KEGG": "0", "DDI_ENGINE_ENABLE_LLM": "0", "DDI_ENGINE_ENABLE_RAG": "0",
    })
    records: list[dict] = []
    try:
        for case in cases:
            warnings = detect(case["medications"])
            records.append({"case_id": case["case_id"], "warnings": warnings})
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    tp = fp = fn = severity_correct = 0
    flagged_gold = 0
    citation_count = 0
    path_counts: dict[str, int] = defaultdict(int)
    unique_fast: set[tuple[str, str]] = set()
    unique_fallback: set[tuple[str, str]] = set()
    per_case: list[dict] = []
    clean_total = clean_correct = 0
    severe_required = {
        _pair_name_key("华法林", "阿司匹林"),
        _pair_name_key("克拉霉素", "辛伐他汀"),
        _pair_name_key("二甲双胍", "碘造影剂（药物类别）"),
        _pair_name_key("硝酸甘油", "西地那非"),
        _pair_name_key("螺内酯", "氯化钾"),
        _pair_name_key("地高辛", "胺碘酮"),
    }
    severe_seen: set[tuple[str, str]] = set()
    for case, record in zip(cases, records):
        gold = {_pair_name_key(item["drug_a"], item["drug_b"]): item for item in case.get("ground_truth", [])}
        predicted = {_pair_name_key(item["drug_a"], item["drug_b"]): item for item in record["warnings"]}
        common = gold.keys() & predicted.keys()
        tp += len(common)
        fp += len(predicted.keys() - gold.keys())
        fn += len(gold.keys() - predicted.keys())
        flagged_gold += len(common)
        severity_correct += sum(predicted[key]["severity"] == gold[key]["severity"] for key in common)
        for warning in record["warnings"]:
            has_chinese_quote = bool(CHINESE_RE.search(warning.get("source_text") or ""))
            citation_count += int(has_chinese_quote and bool(warning.get("source_url")))
            path_counts[warning["detection_path"]] += 1
            pair = _pair_name_key(warning["drug_a"], warning["drug_b"])
            if "kegg" in warning["detection_path"]:
                unique_fast.add(pair)
            if "fallback" in warning["detection_path"] or "rag+llm" in warning["detection_path"]:
                unique_fallback.add(pair)
            if pair in severe_required:
                severe_seen.add(pair)
        if not gold:
            clean_total += 1
            clean_correct += int(not predicted)
        per_case.append({
            "case_id": case["case_id"],
            "gold_pairs": len(gold), "predicted_pairs": len(predicted),
            "true_positive_pairs": len(common),
            "false_positive_pairs": len(predicted.keys() - gold.keys()),
            "missed_pairs": [list(pair) for pair in sorted(gold.keys() - predicted.keys())],
        })
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    warning_count = sum(len(record["warnings"]) for record in records)
    metrics = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "evaluation_kind": "regression_replay",
        "generalization_claim": False,
        "evaluation_design": "scenario-level regression over normalized ingredient pairs; gold authored from Stage 0/1 grounded label evidence",
        "case_count": len(cases),
        "medications_per_case": {
            "min": min(len(case["medications"]) for case in cases),
            "max": max(len(case["medications"]) for case in cases),
        },
        "gold_positive_pair_occurrences": tp + fn,
        "warning_occurrences": warning_count,
        "pair_detection": {
            "true_positive": tp, "false_positive": fp, "false_negative": fn,
            "precision": round(precision, 4), "recall": round(recall, 4), "f1": round(f1, 4),
        },
        "severity_accuracy_on_flagged_gold_pairs": {
            "correct": severity_correct, "total": flagged_gold,
            "value": round(severity_correct / flagged_gold, 4) if flagged_gold else None,
        },
        "citation_coverage": {
            "definition": "warning has a Chinese source_text span and a source_url",
            "covered": citation_count, "total": warning_count,
            "value": round(citation_count / warning_count, 4) if warning_count else None,
        },
        "path_usage": {
            "warning_occurrences_by_detection_path": dict(sorted(path_counts.items())),
            "fast_path_warning_occurrences": sum(count for path, count in path_counts.items() if "kegg" in path),
            "fallback_path_warning_occurrences": sum(count for path, count in path_counts.items() if "fallback" in path or "rag+llm" in path),
            "unique_fast_path_pairs": len(unique_fast),
            "unique_fallback_path_pairs": len(unique_fallback),
            "fast_path_pair_count": len(unique_fast),
            "fallback_path_pair_count": len(unique_fallback),
        },
        "clean_lists": {"total": clean_total, "returned_no_warning": clean_correct},
        "required_severe_pair_coverage": {
            "covered": len(severe_seen), "total": len(severe_required),
            "all_flagged": severe_seen == severe_required,
            "missing": [list(pair) for pair in sorted(severe_required - severe_seen)],
        },
        "acceptance": {
            "scope": "regression_only; do not interpret as held-out detection accuracy",
            "pair_precision_gte_0_85": precision >= 0.85,
            "pair_recall_gte_0_90": recall >= 0.90,
            "severity_accuracy_gte_0_80": bool(flagged_gold and severity_correct / flagged_gold >= 0.80),
            "citation_coverage_gte_0_90": bool(warning_count and citation_count / warning_count >= 0.90),
            "all_required_severe_pairs_flagged": severe_seen == severe_required,
        },
        "per_case": per_case,
        "limitations": [
            "This is detector regression testing, not clinical validation or evidence of patient-outcome safety.",
            "Cases reuse the Stage 0/1 scoped label-evidence universe and are not an independent external benchmark.",
            "A single annotator assigned the operational severities; class expansion uses the small ontology, not exhaustive pharmacology.",
            "Evaluation disables new network and model calls, measuring the reproducible persisted-index and warm-cache path.",
        ],
    }
    _write_json(metrics_path, metrics)
    # Keep the legacy path for reproducibility, while giving the artifact the
    # name it deserves in reports and interviews.
    _write_json(REGRESSION_METRICS_PATH, metrics)
    predictions_path = STRUCTURED / "engine_eval_predictions.jsonl"
    predictions_path.write_text(
        "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records), "utf-8"
    )
    return metrics


def _case_candidate_pairs(case: dict) -> set[tuple[str, str]]:
    """Return normalized ingredient pairs represented by one eval list."""
    ingredients = normalize_medications(case.get("medications", []))
    return {
        _pair_name_key(a, b)
        for a, b in itertools.combinations(ingredients, 2)
        if _ingredient_key(a) != _ingredient_key(b)
    }


def _pair_kegg_key(pair: tuple[str, str]) -> str | None:
    concepts = [_resolve_concept(name) for name in pair]
    if not all(concept and concept.get("kegg") for concept in concepts):
        return None
    return " ".join(sorted(concept["kegg"] for concept in concepts if concept))


def _stage1_scoped_pairs() -> set[tuple[str, str]]:
    """Collect the pair universe that a held-out audit must exclude."""
    pairs = set(_evidence_index().keys())
    for path in (EVAL_CASES_PATH, DATA / "eval_gold_30.jsonl"):
        for row in _read_jsonl(path):
            for item in row.get("ground_truth", []):
                pairs.add(_pair_name_key(item["drug_a"], item["drug_b"]))
            expected = row.get("expected") or {}
            if expected.get("drug_a") and expected.get("drug_b"):
                left = _resolve_concept(expected["drug_a"])
                right = _resolve_concept(expected["drug_b"])
                if left and right:
                    pairs.add(_pair_name_key(left, right))
    try:
        known_pairs = json.loads((DATA / "known_pairs.json").read_text("utf-8"))
    except (OSError, json.JSONDecodeError):
        known_pairs = []
    if not isinstance(known_pairs, list):
        known_pairs = []
    for row in known_pairs:
        left = _resolve_concept(row.get("input_a"))
        right = _resolve_concept(row.get("input_b"))
        if left and right:
            pairs.add(_pair_name_key(left, right))
    return pairs


def _shared_kegg_pair_keys() -> set[str]:
    cache = crosscheck_eval.load_cache(crosscheck_eval.CACHE)
    return {key for key in cache if KEGG_PAIR_RE.fullmatch(key)}


def _shared_fallback_pair_keys() -> set[tuple[str, str]]:
    path = FALLBACK_CACHE_PATH
    if not path.exists():
        return set()
    try:
        value = json.loads(path.read_text("utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    return {
        _pair_name_key(*key.split(" | "))
        for key in (value.get("pairs") or {})
        if isinstance(key, str) and " | " in key
    }


def _heldout_leakage_audit(cases: list[dict]) -> dict:
    """Reject held-out pairs already represented by Stage 0/1 evidence.

    The audit is deliberately stricter than merely checking the old gold
    labels: a pair is excluded if it was in the shared KEGG cache (including a
    cached negative), the fallback cache, the old regression gold, or the
    Stage 1 grounded evidence index.  A pair made only from ontology members
    is also rejected so the set tests new coverage rather than class replay.
    """
    scoped = _stage1_scoped_pairs()
    shared_kegg = _shared_kegg_pair_keys()
    shared_fallback = _shared_fallback_pair_keys()
    ontology_ids = {
        member["kegg"]
        for definition in ontology.ONTOLOGY.values()
        for member in definition["members"]
    }
    pair_details: dict[tuple[str, str], dict] = {}
    for case in cases:
        for pair in _case_candidate_pairs(case):
            concepts = [_resolve_concept(name) for name in pair]
            kegg_key = _pair_kegg_key(pair)
            detail = pair_details.setdefault(pair, {
                "pair": list(pair),
                "kegg_key": kegg_key,
                "in_shared_kegg_cache": bool(kegg_key and kegg_key in shared_kegg),
                "in_shared_fallback_cache": pair in shared_fallback,
                "in_stage1_evidence": pair in scoped,
                "both_members_in_ontology": bool(
                    all(concept and concept.get("kegg") in ontology_ids for concept in concepts)
                ),
            })
            # Keep this assignment explicit in case a pair appears first in a
            # case before a later normalization pass supplies its KEGG id.
            detail["kegg_key"] = kegg_key
    violations = [
        detail for detail in pair_details.values()
        if detail["in_shared_kegg_cache"]
        or detail["in_shared_fallback_cache"]
        or detail["in_stage1_evidence"]
        or detail["both_members_in_ontology"]
    ]
    return {
        "passed": not violations,
        "checked_case_count": len(cases),
        "checked_pair_count": len(pair_details),
        "stage1_scoped_pair_count": len(scoped),
        "shared_kegg_pair_count": len(shared_kegg),
        "shared_fallback_pair_count": len(shared_fallback),
        "violations": sorted(violations, key=lambda item: item["pair"]),
        "pairs": sorted(pair_details.values(), key=lambda item: item["pair"]),
    }


def _heldout_trace() -> dict:
    kegg_cache = crosscheck_eval.load_cache(HELDOUT_KEGG_CACHE_PATH)
    kegg_statuses = defaultdict(int)
    for value in kegg_cache.values():
        if isinstance(value, dict) and value.get("status"):
            kegg_statuses[value["status"]] += 1
    fallback = _load_json_object(HELDOUT_FALLBACK_CACHE_PATH)
    fallback_pairs = fallback.get("pairs", {}) if isinstance(fallback, dict) else {}
    fallback_statuses = defaultdict(int)
    attempted_chunks = 0
    for value in fallback_pairs.values():
        if not isinstance(value, dict):
            continue
        if value.get("status"):
            fallback_statuses[value["status"]] += 1
        attempted_chunks += len(value.get("attempted_chunk_ids") or [])
    llm_config = extract_ddi.resolve_llm_config(require_key=False)
    completion_options = extract_ddi.llm_completion_options()
    thinking = (completion_options.get("extra_body") or {}).get("thinking", {}).get("type")
    return {
        "kegg_cache_path": str(HELDOUT_KEGG_CACHE_PATH.relative_to(ROOT)),
        "kegg_pair_cache_entries": sum(kegg_statuses.values()),
        "kegg_statuses": dict(sorted(kegg_statuses.items())),
        "fallback_cache_path": str(HELDOUT_FALLBACK_CACHE_PATH.relative_to(ROOT)),
        "fallback_pair_cache_entries": len(fallback_pairs),
        "fallback_statuses": dict(sorted(fallback_statuses.items())),
        "fallback_attempted_rag_chunks": attempted_chunks,
        "llm_call_status": (
            "provider_error" if fallback_statuses.get("error")
            else "completed" if attempted_chunks else "not_needed"
        ),
        "llm_provider": llm_config["provider"],
        "llm_model": llm_config["model"],
        "llm_controls": {
            "thinking": thinking,
            "max_tokens": completion_options.get("max_tokens"),
            "request_timeout_seconds": float(os.getenv("TOKENDANCE_TIMEOUT_SECONDS", "60")),
            "sdk_max_retries": int(os.getenv("TOKENDANCE_MAX_RETRIES", "0")),
        },
        "model_key_available": bool(llm_config["api_key"]),
    }


def _load_json_object(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text("utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def evaluate_heldout(
    cases_path: Path = HELDOUT_CASES_PATH,
    metrics_path: Path = HELDOUT_METRICS_PATH,
    reset_caches: bool = True,
) -> dict:
    """Run a fresh live evaluation against pairs outside the Stage 0/1 scope.

    By default this evaluator resets only the three held-out cache artifacts,
    enables KEGG/RAG/LLM, and restores the process environment afterward. A
    bounded-provider-timeout interruption may resume those same isolated
    caches by passing ``reset_caches=False``. It refuses
    to run if its own leakage audit finds a pair in the shared regression
    universe, making accidental cache replay a hard failure rather than a
    silent metric inflation.
    """
    cases = _read_jsonl(cases_path)
    if len(cases) < 24:
        raise ValueError("held-out evaluation requires at least 24 medication lists")
    medication_counts = [len(case.get("medications", [])) for case in cases]
    if min(medication_counts) < 3 or max(medication_counts) > 6:
        raise ValueError("held-out lists must contain 3-6 medications")
    audit = _heldout_leakage_audit(cases)
    if not audit["passed"]:
        raise ValueError("held-out leakage audit failed: " + json.dumps(audit["violations"], ensure_ascii=False))

    previous_env = {name: os.environ.get(name) for name in (
        "DDI_ENGINE_LIVE_KEGG", "DDI_ENGINE_ENABLE_LLM", "DDI_ENGINE_ENABLE_RAG",
        "DDI_ENGINE_PAIR_INDEX_PATH", "DDI_ENGINE_KEGG_CACHE_PATH", "DDI_ENGINE_FALLBACK_CACHE_PATH",
    )}
    global _PAIR_INDEX, _PAIR_INDEX_PATH_LOADED
    previous_pair_index = _PAIR_INDEX
    previous_pair_index_path = _PAIR_INDEX_PATH_LOADED
    records: list[dict] = []
    try:
        if reset_caches:
            _write_json(HELDOUT_KEGG_CACHE_PATH, {})
            _write_json(HELDOUT_FALLBACK_CACHE_PATH, {"schema_version": 1, "pairs": {}})
        else:
            if not HELDOUT_KEGG_CACHE_PATH.exists():
                _write_json(HELDOUT_KEGG_CACHE_PATH, {})
            if not HELDOUT_FALLBACK_CACHE_PATH.exists():
                _write_json(HELDOUT_FALLBACK_CACHE_PATH, {"schema_version": 1, "pairs": {}})
            # Resume only stable results. Transport/provider errors are
            # transient observations, not negative DDI evidence, so retry them
            # under the current bounded client settings.
            kegg_cache = crosscheck_eval.load_cache(HELDOUT_KEGG_CACHE_PATH)
            stable_kegg = {
                key: value for key, value in kegg_cache.items()
                if not isinstance(value, dict) or value.get("status") != "error"
            }
            if len(stable_kegg) != len(kegg_cache):
                _write_json(HELDOUT_KEGG_CACHE_PATH, stable_kegg)
            fallback_cache = _load_json_object(HELDOUT_FALLBACK_CACHE_PATH)
            fallback_pairs = fallback_cache.get("pairs", {}) if isinstance(fallback_cache, dict) else {}
            stable_fallback = {
                key: value for key, value in fallback_pairs.items()
                if not isinstance(value, dict) or value.get("status") != "error"
            }
            if len(stable_fallback) != len(fallback_pairs):
                fallback_cache["pairs"] = stable_fallback
                _write_json(HELDOUT_FALLBACK_CACHE_PATH, fallback_cache)
        os.environ.update({
            "DDI_ENGINE_LIVE_KEGG": "1",
            "DDI_ENGINE_ENABLE_LLM": "1",
            "DDI_ENGINE_ENABLE_RAG": "1",
            "DDI_ENGINE_PAIR_INDEX_PATH": str(HELDOUT_PAIR_INDEX_PATH),
            "DDI_ENGINE_KEGG_CACHE_PATH": str(HELDOUT_KEGG_CACHE_PATH),
            "DDI_ENGINE_FALLBACK_CACHE_PATH": str(HELDOUT_FALLBACK_CACHE_PATH),
        })
        build_pair_index(path=HELDOUT_PAIR_INDEX_PATH, cache_path=HELDOUT_KEGG_CACHE_PATH)
        for case in cases:
            warnings = detect(case["medications"])
            records.append({"case_id": case["case_id"], "warnings": warnings})
    finally:
        _PAIR_INDEX = previous_pair_index
        _PAIR_INDEX_PATH_LOADED = previous_pair_index_path
        for name, value in previous_env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    tp = fp = fn = severity_correct = 0
    flagged_gold = 0
    citation_count = 0
    path_counts: dict[str, int] = defaultdict(int)
    unique_fast: set[tuple[str, str]] = set()
    unique_fallback: set[tuple[str, str]] = set()
    per_case: list[dict] = []
    clean_total = clean_correct = 0
    high_risk_gold = high_risk_correct = 0
    for case, record in zip(cases, records):
        gold = {_pair_name_key(item["drug_a"], item["drug_b"]): item for item in case.get("ground_truth", [])}
        predicted = {_pair_name_key(item["drug_a"], item["drug_b"]): item for item in record["warnings"]}
        common = gold.keys() & predicted.keys()
        tp += len(common)
        fp += len(predicted.keys() - gold.keys())
        fn += len(gold.keys() - predicted.keys())
        flagged_gold += len(common)
        severity_correct += sum(predicted[key]["severity"] == gold[key]["severity"] for key in common)
        for key in common:
            if gold[key]["severity"] in {"contraindicated", "major"}:
                high_risk_gold += 1
                high_risk_correct += int(predicted[key]["severity"] == gold[key]["severity"])
        for warning in record["warnings"]:
            has_chinese_quote = bool(CHINESE_RE.search(warning.get("source_text") or ""))
            citation_count += int(has_chinese_quote and bool(warning.get("source_url")))
            path_counts[warning["detection_path"]] += 1
            pair = _pair_name_key(warning["drug_a"], warning["drug_b"])
            if "kegg" in warning["detection_path"]:
                unique_fast.add(pair)
            if "fallback" in warning["detection_path"] or "rag+llm" in warning["detection_path"]:
                unique_fallback.add(pair)
        if not gold:
            clean_total += 1
            clean_correct += int(not predicted)
        per_case.append({
            "case_id": case["case_id"],
            "gold_pairs": len(gold), "predicted_pairs": len(predicted),
            "true_positive_pairs": len(common),
            "false_positive_pairs": len(predicted.keys() - gold.keys()),
            "missed_pairs": [list(pair) for pair in sorted(gold.keys() - predicted.keys())],
        })
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    warning_count = sum(len(record["warnings"]) for record in records)
    trace = _heldout_trace()
    composite_completed = trace["llm_call_status"] in {"completed", "not_needed"}
    metrics = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "evaluation_kind": "heldout_live_generalization",
        "evaluation_status": "complete" if composite_completed else "partial_provider_failure",
        "generalization_claim": composite_completed,
        "evaluation_design": "live scenario evaluation over pairs rejected by a pre-run Stage 0/1 leakage audit; KEGG/RAG/LLM enabled with isolated caches",
        "case_count": len(cases),
        "medications_per_case": {"min": min(medication_counts), "max": max(medication_counts)},
        "unique_heldout_pair_count": audit["checked_pair_count"],
        "gold_positive_pair_occurrences": tp + fn,
        "warning_occurrences": warning_count,
        "pair_detection": {
            "true_positive": tp, "false_positive": fp, "false_negative": fn,
            "precision": round(precision, 4), "recall": round(recall, 4), "f1": round(f1, 4),
        },
        "severity_accuracy_on_flagged_gold_pairs": {
            "correct": severity_correct, "total": flagged_gold,
            "value": round(severity_correct / flagged_gold, 4) if flagged_gold else None,
        },
        "high_risk_severity_accuracy": {
            "definition": "contraindicated or major gold pairs that were flagged",
            "correct": high_risk_correct, "total": high_risk_gold,
            "value": round(high_risk_correct / high_risk_gold, 4) if high_risk_gold else None,
        },
        "citation_coverage": {
            "definition": "warning has a Chinese source_text span and a source_url",
            "covered": citation_count, "total": warning_count,
            "value": round(citation_count / warning_count, 4) if warning_count else None,
        },
        "path_usage": {
            "warning_occurrences_by_detection_path": dict(sorted(path_counts.items())),
            "fast_path_warning_occurrences": sum(count for path, count in path_counts.items() if "kegg" in path),
            "fallback_path_warning_occurrences": sum(count for path, count in path_counts.items() if "fallback" in path or "rag+llm" in path),
            "unique_fast_path_pairs": len(unique_fast),
            "unique_fallback_path_pairs": len(unique_fallback),
            "fast_path_pair_count": len(unique_fast),
            "fallback_path_pair_count": len(unique_fallback),
        },
        "live_execution": {
            "kegg_enabled": True, "rag_enabled": True, "llm_enabled": True,
            "isolated_caches": True,
            "resumed_from_current_isolated_cache": not reset_caches,
            "full_live_composite_path_completed": composite_completed,
            "trace": trace,
        },
        "leakage_audit": audit,
        "clean_lists": {"total": clean_total, "returned_no_warning": clean_correct},
        "acceptance": {
            "scope": "held-out estimate; not clinical validation",
            "leakage_audit_passed": audit["passed"],
            "live_execution_completed": True,
            "full_live_composite_path_completed": composite_completed,
            "pair_precision_gte_0_85": precision >= 0.85,
            "pair_recall_gte_0_90": recall >= 0.90,
            "severity_accuracy_gte_0_80": bool(flagged_gold and severity_correct / flagged_gold >= 0.80),
            "high_risk_severity_accuracy_gte_0_95": bool(high_risk_gold and high_risk_correct / high_risk_gold >= 0.95),
            "citation_coverage_gte_0_90": bool(warning_count and citation_count / warning_count >= 0.90),
        },
        "per_case": per_case,
        "limitations": [
            "This is a small live generalization estimate, not clinical validation or evidence of patient-outcome safety.",
            "Positive labels were manually authored from cited label excerpts where available; the KEGG-only atorvastatin-voriconazole case has no local Chinese label citation.",
            "Negative controls are scenario-level controls, not proof that no interaction exists under every dose, route, genotype, or patient condition.",
            "The local RAG corpus is a curated MNBVC-derived label sample; live model results can vary with provider availability and model version.",
        ],
    }
    _write_json(metrics_path, metrics)
    predictions_path = STRUCTURED / "engine_heldout_predictions.jsonl"
    predictions_path.write_text(
        "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records), "utf-8"
    )
    return metrics


def main() -> None:
    parser = argparse.ArgumentParser(description="Stage 2 runtime DDI detector")
    parser.add_argument("medications", nargs="*")
    parser.add_argument("--build-index", action="store_true", help="rebuild data/ddi_pair_index.json from the KEGG cache")
    parser.add_argument("--evaluate", action="store_true", help="run data/engine_eval_cases.jsonl")
    parser.add_argument("--heldout-evaluate", action="store_true", help="run the isolated live held-out evaluation")
    parser.add_argument("--resume-heldout", action="store_true", help="resume the current isolated held-out caches")
    parser.add_argument("--live-kegg", action="store_true", help="allow cache-missing KEGG requests during this run")
    parser.add_argument("--no-llm", action="store_true", help="do not make new LLM provider calls")
    args = parser.parse_args()
    if args.live_kegg:
        os.environ["DDI_ENGINE_LIVE_KEGG"] = "1"
    if args.no_llm:
        os.environ["DDI_ENGINE_ENABLE_LLM"] = "0"
    output: Any = None
    if args.build_index:
        output = build_pair_index()
    if args.evaluate:
        output = evaluate()
    if args.heldout_evaluate:
        output = evaluate_heldout(reset_caches=not args.resume_heldout)
    if args.medications:
        output = detect(args.medications)
    if output is None:
        output = detect(["可迈丁", "拜阿司匹灵", "可达龙", "兰尼"])
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
