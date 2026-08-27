"""Normalize Chinese brand/generic names to one or more active ingredients."""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MAPPING_PATH = ROOT / "data" / "mapping.json"


def compact(name: str) -> str:
    name = re.sub(r"[\s®™·]", "", name.strip().lower())
    name = re.sub(r"(片|胶囊|缓释片|控释片|肠溶片|注射液|颗粒|钠片|钙片)$", "", name)
    return name


def load_mapping(path: Path = MAPPING_PATH) -> list[dict]:
    return json.loads(path.read_text("utf-8"))


def build_index(rows: list[dict]) -> dict[str, dict]:
    index: dict[str, dict] = {}
    for row in rows:
        for name in [row["generic_cn"], *row.get("brands", []), *row.get("aliases", [])]:
            index[compact(name)] = row
    return index


def normalize(name: str, index: dict[str, dict]) -> dict:
    key = compact(name)
    row = index.get(key)
    if not row:
        # Conservative containment fallback, longest alias wins.
        choices = [(len(alias), value) for alias, value in index.items() if alias and alias in key]
        row = max(choices, default=(0, None), key=lambda x: x[0])[1]
    if not row:
        return {"input": name, "matched": False, "generic_cn": None, "ingredients": []}
    return {"input": name, "matched": True, "generic_cn": row["generic_cn"], "ingredients": row["ingredients"]}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("names", nargs="*")
    parser.add_argument("--input", type=Path, help="UTF-8 file, one name per line")
    parser.add_argument("--output", type=Path, default=ROOT / "data" / "structured" / "normalized.jsonl")
    args = parser.parse_args()
    names = list(args.names)
    if args.input:
        names.extend(x.strip() for x in args.input.read_text("utf-8").splitlines() if x.strip())
    if not names:
        names = ["倍他乐克", "拜阿司匹灵", "复方利血平氨苯蝶啶胶囊", "硝酸甘油", "万艾可"]
    index = build_index(load_mapping())
    results = [normalize(name, index) for name in names]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in results), "utf-8")
    print(json.dumps(results, ensure_ascii=False, indent=2))
