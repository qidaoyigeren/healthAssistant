"""Build easy and hard no-DDI evaluation cases from local MNBVC labels.

Easy cases remain the original 15 适应症/成分 sentences. Hard cases are sourced
from 禁忌/注意事项/药物相互作用 and deliberately contain interaction-like language
without asserting that one drug changes another drug's exposure, effect, or toxicity.
The deterministic pattern pass is conservative and stores provenance for manual audit.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import re
from pathlib import Path

from acquire import mnbvc_fields

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
SHARD = DATA / "raw" / "mnbvc_medical_output.jsonl.gz"

INTERACTION_HINTS = re.compile(
    r"合用|同时使用|同时应用|联合|增强|减弱|减少|影响|避免|同用|不宜|禁忌|相互作用|"
    r"升高|降低|增加|血药浓度|协同|拮抗|监测|调整剂量|调整|配伍|并用|联用|增效|减效|毒性"
)
HARD_PATTERNS = {
    "negated_interaction": re.compile(
        r"(?:未见|未发现|未观察到|没有|无)(?:[^。；;]{0,55})(?:相互作用|明显影响|显著影响|临床意义)"
        r"|(?:不影响|无影响于)(?:[^。；;]{0,45})(?:药代动力学|吸收|疗效|血药浓度|作用)"
    ),
    "safe_or_no_adjustment": re.compile(
        r"(?:合用|联用|同时使用|同时服用)(?:[^。；;]{0,55})(?:是安全的|安全可靠|耐受良好|耐受性良好)"
        r"|(?:安全可靠|耐受良好|耐受性良好)(?:[^。；;]{0,55})(?:合用|联用|同时使用|同时服用)"
    ),
    "same_class_non_interaction": re.compile(
        r"(?:对|有)(?:本品|本药|[^。；;]{1,30})(?:或|和|及)(?:其他)?[^。；;]{0,35}"
        r"(?:类药物|衍生物|制剂)[^。；;]{0,25}(?:过敏|超敏|变态反应)"
        r"|(?:同类|类似|其他[^。；;]{0,15}类)药物[^。；;]{0,30}(?:交叉过敏|过敏反应)"
    ),
}
HARD_SECTIONS = ("药物相互作用", "禁忌", "注意事项")
POSITIVE_DDI_CUES = re.compile(
    r"可改变[^。；;]{0,20}血药浓度|略有影响|导致[^。；;]{0,25}(?:升高|降低|毒性|出血)|"
    r"但有[^。；;]{0,40}(?:联合|合用)[^。；;]{0,30}(?:毒性|不良反应)"
)


def sentence_candidates(text: str, minimum: int = 12, maximum: int = 320) -> list[str]:
    candidates = []
    for match in re.finditer(r"[^。.!！？；;\n]+[。.!！？；;]?", re.sub(r"\s+", " ", text)):
        sentence = re.sub(r"^\s*(?:\(?\d+[\.、\)]|[（(]?\d+[）)])\s*", "", match.group(0)).strip()
        if minimum <= len(sentence) <= maximum:
            candidates.append(sentence)
    return candidates


def easy_negatives(count: int = 15) -> list[dict]:
    samples = [
        json.loads(line)
        for line in (DATA / "raw" / "mnbvc_sample_100.jsonl").read_text("utf-8").splitlines()
        if line.strip()
    ]
    picked: list[dict] = []
    seen: set[str] = set()
    for sample_index, sample in enumerate(samples):
        name = sample["drug_name"]
        if name in seen:
            continue
        chosen = section = None
        for candidate_section in ("适应症", "成分"):
            for sentence in sentence_candidates(sample.get("sections", {}).get(candidate_section, "")):
                if not INTERACTION_HINTS.search(sentence):
                    chosen, section = sentence, candidate_section
                    break
            if chosen:
                break
        if not chosen:
            continue
        picked.append({
            "case_id": f"N{len(picked) + 1:02d}",
            "difficulty": "easy",
            "negative_category": "indication_or_composition",
            "source_sample_index": sample_index,
            "source_dataset_row": sample.get("dataset_row"),
            "approval_number": sample.get("approval_number"),
            "source_url": sample.get("source_url"),
            "label_drug": name,
            "section": section,
            "text": chosen,
            "expected": {"no_ddi": True},
        })
        seen.add(name)
        if len(picked) >= count:
            break
    return picked


def _selection_key(row: dict) -> str:
    value = f"stage1-hard-negatives-v1\x1f{row['approval_number']}\x1f{row['section']}\x1f{row['text']}"
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def hard_negatives(per_category: dict[str, int] | None = None) -> list[dict]:
    requested = per_category or {
        "negated_interaction": 8,
        "safe_or_no_adjustment": 6,
        "same_class_non_interaction": 8,
    }
    candidates: dict[str, list[dict]] = {category: [] for category in requested}
    seen_approvals: set[str] = set()
    seen_text: set[str] = set()
    with gzip.open(SHARD, "rt", encoding="utf-8") as handle:
        for dataset_row, line in enumerate(handle, 1):
            fields = mnbvc_fields(json.loads(line))
            approval = fields.get("批准文号") or fields.get("编号")
            if not approval or approval in seen_approvals:
                continue
            seen_approvals.add(approval)
            drug_name = fields.get("通用名称") or fields.get("标题") or ""
            for section in HARD_SECTIONS:
                body = fields.get(section, "")
                for sentence in sentence_candidates(body):
                    normalized = re.sub(r"\s+", "", sentence)
                    if normalized in seen_text:
                        continue
                    for category, pattern in HARD_PATTERNS.items():
                        if category in {"negated_interaction", "safe_or_no_adjustment"} and section != "药物相互作用":
                            continue
                        if category in {"negated_interaction", "safe_or_no_adjustment"} and POSITIVE_DDI_CUES.search(sentence):
                            continue
                        if pattern.search(sentence):
                            candidates[category].append({
                                "difficulty": "hard",
                                "negative_category": category,
                                "source_dataset_row": dataset_row,
                                "approval_number": approval,
                                "source_url": fields.get("标题链接") or None,
                                "label_drug": drug_name,
                                "section": section,
                                "text": sentence,
                                "expected": {"no_ddi": True},
                            })
                            seen_text.add(normalized)
                            break
    selected: list[dict] = []
    selected_drugs: set[str] = set()
    for category, count in requested.items():
        ordered = sorted(candidates[category], key=_selection_key)
        diverse = [row for row in ordered if row["label_drug"] not in selected_drugs]
        remainder = [row for row in ordered if row["label_drug"] in selected_drugs]
        category_rows = (diverse + remainder)[:count]
        if len(category_rows) < count:
            raise RuntimeError(f"only {len(category_rows)} hard negatives found for {category}; need {count}")
        for row in category_rows:
            row["case_id"] = f"H{len(selected) + 1:02d}"
            selected.append(row)
            selected_drugs.add(row["label_drug"])
    return selected


def main() -> None:
    easy = easy_negatives(15)
    hard = hard_negatives()
    rows = easy + hard
    out = DATA / "eval_negative_gold.jsonl"
    out.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), "utf-8")
    summary = {
        "count": len(rows),
        "easy_count": len(easy),
        "hard_count": len(hard),
        "hard_category_counts": {
            category: sum(row["negative_category"] == category for row in hard)
            for category in HARD_PATTERNS
        },
        "unique_hard_approval_numbers": len({row["approval_number"] for row in hard}),
        "path": str(out.relative_to(ROOT)),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
