"""Cross-check ingredient pairs with KEGG DDI and run the six-pair E2E spike."""
from __future__ import annotations

import argparse
import itertools
import json
import time
import urllib.request
from pathlib import Path

from normalize import build_index, load_mapping, normalize

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"


def pair_key(a: str, b: str) -> tuple[str, str]:
    return tuple(sorted((a.lower(), b.lower())))


def load_jsonl(path: Path) -> list[dict]:
    return [json.loads(x) for x in path.read_text("utf-8").splitlines() if x.strip()]


def live_kegg(kegg_a: str, kegg_b: str) -> list[dict]:
    url = f"https://rest.kegg.jp/ddi/{kegg_a}+{kegg_b}"
    request = urllib.request.Request(url, headers={"User-Agent": "HealthAssistant-stage0/0.1"})
    with urllib.request.urlopen(request, timeout=30) as response:
        text = response.read().decode("utf-8")
    time.sleep(1.0)
    rows = []
    for line in text.splitlines():
        fields = line.split("\t")
        if len(fields) >= 4:
            rows.append({"a": fields[0], "b": fields[1], "level": fields[2], "mechanism": fields[3], "url": url})
    return rows


def run(live: bool = False) -> dict:
    mappings = load_mapping()
    index = build_index(mappings)
    triples = load_jsonl(DATA / "structured" / "ddi_triples.jsonl")
    triple_index = {pair_key(x["drug_a_en"], x["drug_b_en"]): x for x in triples}
    recorded = {pair_key(x["drug_a_en"], x["drug_b_en"]): x for x in json.loads((DATA / "kegg_recorded.json").read_text("utf-8"))}
    anchors = json.loads((DATA / "known_pairs.json").read_text("utf-8"))
    results = []
    for anchor in anchors:
        left = normalize(anchor["input_a"], index)
        right = normalize(anchor["input_b"], index)
        matches = []
        for a, b in itertools.product(left["ingredients"], right["ingredients"]):
            key = pair_key(a["name_en"], b["name_en"])
            triple = triple_index.get(key)
            cross = recorded.get(key)
            if live and a.get("kegg") and b.get("kegg"):
                try:
                    live_rows = live_kegg(a["kegg"], b["kegg"])
                    cross = {"status": "matched" if live_rows else "not_found", "rows": live_rows, "checked_live": True}
                except Exception as exc:
                    cross = {"status": "error", "error": str(exc), "checked_live": True}
            if triple:
                matches.append({"ingredients": [a["name_en"], b["name_en"]], "ddi": triple, "english_crosscheck": cross})
        flagged = bool(matches)
        results.append({
            "inputs": [anchor["input_a"], anchor["input_b"]], "normalized": [left, right],
            "flagged": flagged, "expected": anchor["expected"], "correct": flagged,
            "warning": matches[0]["ddi"]["warning"] if matches else None,
            "source_text": matches[0]["ddi"]["source_text"] if matches else None,
            "source_url": matches[0]["ddi"]["source_url"] if matches else None,
            "crosscheck": matches[0]["english_crosscheck"] if matches else None,
        })
    all_crosschecks = []
    for triple in triples:
        key = pair_key(triple["drug_a_en"], triple["drug_b_en"])
        if key in recorded:
            all_crosschecks.append({
                "drug_a_en": triple["drug_a_en"], "drug_b_en": triple["drug_b_en"],
                "status": recorded[key]["status"], "level": recorded[key]["level"],
                "mechanism": recorded[key]["mechanism"], "source_url": recorded[key]["source_url"],
            })
    summary = {
        "pairs_tested": len(results), "correctly_flagged": sum(x["correct"] for x in results),
        "kegg_recorded_matches": sum(bool(x.get("crosscheck") and x["crosscheck"].get("status") == "matched") for x in results),
        "all_extracted_triples_crosschecked_in_kegg": len(all_crosschecks),
        "all_crosschecks": all_crosschecks,
        "results": results,
    }
    out = DATA / "structured" / "end_to_end.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(summary, ensure_ascii=False, indent=2), "utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", help="query KEGG REST (academic-use terms apply; 1s delay)")
    args = parser.parse_args()
    run(args.live)
