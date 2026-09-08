"""Cross-validate all 30 evaluation DDIs against KEGG by member expansion.

Fourteen cases retain their representative single-drug partner. The 16 cases
previously skipped as classes are expanded through ``ontology.py``; a class case
is corroborated when any representative member pair is returned by KEGG DDI.

KEGG REST is academic-use only. Uncached network requests are serialized with a
one-second delay, including HTTP-error responses.
"""
from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote

from ontology import CASE_CLASS_IDS, ONTOLOGY, validate as validate_ontology

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
CACHE = DATA / "kegg_ddi_cache.json"

# eval-gold case_id -> representative explicit single-drug partner.
SINGLE_PARTNER = {
    "E01": "Lithium", "E06": "Carbamazepine", "E07": "Simvastatin", "E08": "Cisapride",
    "E09": "Digoxin", "E11": "Nifedipine", "E16": "Digoxin", "E19": "Digoxin",
    "E21": "Aspirin", "E22": "Naproxen", "E23": "Cyclosporine", "E26": "Ketoconazole",
    "E28": "Amiodarone", "E29": "Warfarin",
}


def load_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text("utf-8").splitlines() if line.strip()]


def mapping_name_to_kegg() -> dict[str, str]:
    out: dict[str, str] = {}
    for row in json.loads((DATA / "mapping.json").read_text("utf-8")):
        for ingredient in row.get("ingredients", []):
            if ingredient.get("kegg"):
                out[ingredient["name_en"].lower()] = ingredient["kegg"]
    return out


def load_cache(path: Path = CACHE) -> dict:
    """Load a cache, optionally using an isolated path for a live evaluation.

    The default remains the Stage 1 cache so existing callers keep their
    behavior.  A caller that is evaluating genuinely new pairs can provide a
    separate file and avoid contaminating the regression cache.
    """
    return json.loads(path.read_text("utf-8")) if path.exists() else {}


def save_cache(cache: dict, path: Path = CACHE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cache, ensure_ascii=False, indent=2), "utf-8")


try:
    from .turn_budget import network_call
except ImportError:
    from turn_budget import network_call


def _read_url(request):
    def read(timeout):
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read().decode("utf-8")
    return network_call("kegg_http", read, timeout=30.)


def kegg_find_drug(name: str) -> str | None:
    url = "https://rest.kegg.jp/find/drug/" + quote(name)
    request = urllib.request.Request(url, headers={"User-Agent": "HealthAssistant-stage1/0.1"})
    try:
        text = _read_url(request)
    finally:
        time.sleep(1.0)
    for line in text.splitlines():
        if "\t" in line:
            return line.split("\t")[0].strip()
    return None


def kegg_ddi(kegg_a: str, kegg_b: str, cache: dict, cache_path: Path = CACHE) -> dict:
    key = " ".join(sorted([kegg_a, kegg_b]))
    if key in cache:
        return cache[key]
    url = f"https://rest.kegg.jp/ddi/{kegg_a}+{kegg_b}"
    request = urllib.request.Request(url, headers={"User-Agent": "HealthAssistant-stage1/0.1"})
    try:
        try:
            text = _read_url(request)
            rows = []
            for line in text.splitlines():
                fields = line.split("\t")
                if len(fields) >= 4:
                    rows.append({"a": fields[0], "b": fields[1], "level": fields[2], "mechanism": fields[3]})
            result = {"status": "matched" if rows else "not_found", "rows": rows, "url": url}
        except urllib.error.HTTPError as exc:
            result = {"status": "not_found" if exc.code == 404 else "error", "rows": [], "error": f"HTTP {exc.code}", "url": url}
        except Exception as exc:
            result = {"status": "error", "rows": [], "error": str(exc), "url": url}
    finally:
        time.sleep(1.0)
    cache[key] = result
    save_cache(cache, cache_path)
    return result


def resolve(name: str, name_to_kegg: dict[str, str], cache: dict, live: bool) -> str | None:
    key = name.lower()
    if key in name_to_kegg:
        return name_to_kegg[key]
    cached = cache.get(f"find:{key}")
    if cached:
        return cached
    if not live:
        return None
    kegg_id = kegg_find_drug(name)
    if kegg_id:
        cache[f"find:{key}"] = kegg_id
        save_cache(cache)
    return kegg_id


def ontology_members(case_id: str) -> list[dict]:
    members: list[dict] = []
    seen: set[str] = set()
    for class_id in CASE_CLASS_IDS.get(case_id, []):
        for item in ONTOLOGY[class_id]["members"]:
            if item["kegg"] not in seen:
                members.append({**item, "class_id": class_id})
                seen.add(item["kegg"])
    return members


def run(live: bool) -> dict:
    ontology_status = validate_ontology()
    if not ontology_status["valid"]:
        raise ValueError(f"invalid ontology: {ontology_status['errors']}")
    name_to_kegg = mapping_name_to_kegg()
    cache = load_cache()
    gold = load_jsonl(DATA / "eval_gold_30.jsonl")
    rows: list[dict] = []
    checked_cases = corroborated_cases = 0
    checked_single = checked_class = corroborated_single = corroborated_class = 0

    for case in gold:
        case_id = case["case_id"]
        drug_a = case["expected"]["drug_a"]
        drug_b = case["expected"]["drug_b"]
        kegg_a = name_to_kegg.get(drug_a.lower())
        is_single = case_id in SINGLE_PARTNER
        members: list[dict]
        if is_single:
            partner = SINGLE_PARTNER[case_id]
            kegg_b = resolve(partner, name_to_kegg, cache, live)
            members = [{"generic_cn": None, "name_en": partner, "kegg": kegg_b, "class_id": None}] if kegg_b else []
        else:
            # A source drug can itself be a member of a broad functional class
            # (for example potassium-raising drugs). Self-pairs cannot
            # corroborate the source-label interaction and are excluded.
            members = [item for item in ontology_members(case_id) if item["kegg"] != kegg_a]

        member_results = []
        for item in members:
            if not kegg_a or not item.get("kegg"):
                continue
            result = kegg_ddi(kegg_a, item["kegg"], cache) if live else {"status": "dry_run", "rows": []}
            member_results.append({"member": item, "crosscheck": result})
        checked = bool(member_results) and (not live or any(item["crosscheck"]["status"] in {"matched", "not_found"} for item in member_results))
        corroborated = any(item["crosscheck"]["status"] == "matched" for item in member_results)
        if checked:
            checked_cases += 1
            if is_single:
                checked_single += 1
            else:
                checked_class += 1
        if corroborated:
            corroborated_cases += 1
            if is_single:
                corroborated_single += 1
            else:
                corroborated_class += 1

        first = members[0] if members else None
        rows.append({
            "case_id": case_id,
            "label_drug": case["label_drug"],
            "drug_a": drug_a,
            "drug_b": drug_b,
            "kegg_a": kegg_a,
            "partner_resolution_type": "single_drug" if is_single else "ontology_class",
            "ontology_class_ids": CASE_CLASS_IDS.get(case_id, []),
            # Backward-compatible summary fields retained for existing readers.
            "partner_resolved": first.get("name_en") if first else None,
            "kegg_b": first.get("kegg") if first else None,
            "crosscheck": next((item["crosscheck"] for item in member_results if item["crosscheck"]["status"] == "matched"), member_results[0]["crosscheck"] if member_results else None),
            "member_pairs_attempted": len(member_results),
            "member_crosschecks": member_results,
            "checked": checked,
            "corroborated": corroborated,
        })

    summary = {
        "mode": "kegg_live_ontology_expansion" if live else "kegg_dry_run_ontology_expansion",
        "total_cases": len(rows),
        "single_drug_cases": len(SINGLE_PARTNER),
        "class_partner_cases": len(CASE_CLASS_IDS),
        "ontology": ontology_status,
        "resolved_and_checked": checked_cases,
        "coverage": round(checked_cases / len(rows), 4) if rows else None,
        "kegg_matched": corroborated_cases,
        "corroboration_rate": round(corroborated_cases / checked_cases, 4) if checked_cases else None,
        "single_drug_results": {"checked": checked_single, "corroborated": corroborated_single},
        "class_partner_results": {"checked": checked_class, "corroborated": corroborated_class},
        "case_corroboration_rule": "any representative ontology member pair returned by KEGG DDI",
        "limitations": [
            "Ontology members are representative (2-5 per class), not exhaustive class definitions.",
            "KEGG DDI is standardized primarily from Japanese labels; absence is not evidence of no interaction.",
            "KEGG REST terms restrict use to academic users; uncached calls are limited to one request per second.",
        ],
        "rows": rows,
    }
    output = DATA / "structured" / "crosscheck_eval.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(summary, ensure_ascii=False, indent=2), "utf-8")
    printable = {key: value for key, value in summary.items() if key != "rows"}
    print(json.dumps(printable, ensure_ascii=False, indent=2))
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", help="query KEGG REST (academic terms; <=1 request/s)")
    args = parser.parse_args()
    run(args.live)
