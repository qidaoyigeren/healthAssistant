"""Estimate DDI detection precision/recall on positives plus easy/hard negatives.

Unlike the Stage 0 metric, positives and negatives both use the same automatic
tool-choice decision. A positive is therefore not counted as detected merely
because a tool call was forced. These remain single-annotator data-layer metrics,
not evidence of clinical validity.
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from extract_ddi import EVAL_TOOL, create_llm_client, llm_completion_options, resolve_llm_config

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"

DETECT_PROMPT = """你是严格的药物-药物相互作用（DDI）判定器。只判断给定句子明确断言的内容；
不要根据章节名、药名共现、医学常识或可能性补充关系。只有证据充分时才调用工具，否则直接回复
“无相互作用”且不得调用工具。

按以下六条通用规则依次判断：
1. 适应症、成分或治疗方案中出现两个药名，只表示用途、组成或既往/备选治疗，不构成DDI；除非同一句
   另外明确断言一种药改变另一种药的暴露、效应、药代动力学或毒性。
2. 必须有两个可识别的药物实体：说明书药品可作为drug_a，但原句还必须明确写出第二种药或药物类别。
   疾病、症状、患者人群不是药物。若第二方缺失，不得把drug_b填为空、unknown或自行猜测，必须判阴性。
3. 否定表达具有优先权：若句子在相关范围内明确表示不影响、未见、未发现、无相互作用、没有明显改变
   或无临床相关影响，则该被否定的关系不是阳性DDI，不得把“没有变化”改写成变化。
4. 明确表示合用安全、耐受良好或耐受性良好的句子是阴性；两个药同时出现也不得据此调用工具。
5. 对某药及同类药过敏、交叉过敏或超敏反应属于患者过敏禁忌，不是一种药改变另一种药，判为阴性。
6. 阳性DDI必须有肯定证据：句子明确断言一种药改变另一种药的暴露、效应、药代动力学或毒性。
   仅列举、共现、提示咨询或资料不足不够。明确针对两方的禁用、不宜同用或应避免合用属于直接的
   联合用药警示，即使原句未写机制，也可判为阳性。

调用工具前做最终核对：第二药物/类别明确存在；变化或明确联合禁用是肯定表述；没有落入上述任何阴性规则。
任一条件不满足，就回复“无相互作用”，不要调用工具。"""


def load_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text("utf-8").splitlines() if line.strip()]


def detect(client, model: str, row: dict, delay: float) -> dict:
    user_content = f"说明书药品：{row['label_drug']}\n章节：{row.get('section', '药物相互作用')}\n句子：{row['text']}"
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            response = client.chat.completions.create(
                model=model,
                messages=[{"role": "system", "content": DETECT_PROMPT}, {"role": "user", "content": user_content}],
                tools=[EVAL_TOOL],
                tool_choice="auto",
                temperature=0,
                **llm_completion_options(),
            )
            calls = response.choices[0].message.tool_calls or []
            arguments = None
            if calls:
                try:
                    arguments = json.loads(calls[0].function.arguments)
                except (json.JSONDecodeError, TypeError):
                    arguments = calls[0].function.arguments
            return {"predicted_ddi": bool(calls), "tool_arguments": arguments, "error": None}
        except Exception as exc:
            last_error = exc
            if attempt < 2:
                time.sleep(max(1.0, delay) * (attempt + 1))
    return {"predicted_ddi": None, "tool_arguments": None, "error": str(last_error)}


def _rate(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


def summarize(records: list[dict], model: str) -> dict:
    scored = [row for row in records if row["predicted_ddi"] is not None]
    tp = sum(row["gold_ddi"] and row["predicted_ddi"] for row in scored)
    fn = sum(row["gold_ddi"] and not row["predicted_ddi"] for row in scored)
    fp = sum(not row["gold_ddi"] and row["predicted_ddi"] for row in scored)
    tn = sum(not row["gold_ddi"] and not row["predicted_ddi"] for row in scored)
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0

    negative_splits = {}
    for difficulty in ("easy", "hard"):
        rows = [row for row in scored if not row["gold_ddi"] and row["difficulty"] == difficulty]
        split_fp = sum(row["predicted_ddi"] for row in rows)
        negative_splits[difficulty] = {
            "count": len(rows),
            "false_positives": split_fp,
            "true_negatives": len(rows) - split_fp,
            "false_positive_rate": _rate(split_fp, len(rows)),
        }
    hard_category_splits = {}
    for category in sorted({row.get("negative_category") for row in scored if row.get("difficulty") == "hard"}):
        rows = [row for row in scored if row.get("negative_category") == category]
        split_fp = sum(row["predicted_ddi"] for row in rows)
        hard_category_splits[category] = {
            "count": len(rows), "false_positives": split_fp,
            "false_positive_rate": _rate(split_fp, len(rows)),
        }
    return {
        "mode": "openai_compatible_auto_detection_positive_and_negative_estimate",
        "model": model,
        "positive_sample_count": sum(row["gold_ddi"] for row in records),
        "negative_sample_count": sum(not row["gold_ddi"] for row in records),
        "successful_api_outputs": len(scored),
        "api_errors": len(records) - len(scored),
        "confusion_matrix": {"true_positives": tp, "false_negatives": fn, "false_positives": fp, "true_negatives": tn},
        "negative_splits": negative_splits,
        "hard_negative_category_splits": hard_category_splits,
        "combined_classification_estimate": {
            "precision": round(precision, 4),
            "recall": round(recall, 4),
            "f1": round(f1, 4),
        },
        "notes": [
            "Estimate only; this is not a clinical-validity claim.",
            "All 30 positives and all negatives use the same tool_choice=auto detection prompt.",
            "The 15 easy negatives are 适应症/成分; hard negatives are source-grounded key-section negations, safe-combination statements, and non-DDI same-class allergy mentions.",
            "Gold selection and labeling were performed by one evaluator and are not an independent clinical annotation.",
        ],
        "records": records,
    }


def run(delay: float = 1.0, model: str | None = None) -> dict:
    config = resolve_llm_config(model)
    model = config["model"]
    client = create_llm_client(config)
    positives = load_jsonl(DATA / "eval_gold_30.jsonl")
    negatives = load_jsonl(DATA / "eval_negative_gold.jsonl")
    cases = [
        {**row, "gold_ddi": True, "difficulty": "positive", "section": "药物相互作用"}
        for row in positives
    ] + [{**row, "gold_ddi": False} for row in negatives]
    records: list[dict] = []
    for position, row in enumerate(cases, 1):
        result = detect(client, model, row, delay)
        records.append({
            "case_id": row["case_id"], "label_drug": row["label_drug"],
            "section": row.get("section"), "difficulty": row["difficulty"],
            "negative_category": row.get("negative_category"), "text": row["text"],
            "gold_ddi": row["gold_ddi"], **result,
        })
        print(
            f"detected {position}/{len(cases)}: {row['case_id']} gold={row['gold_ddi']} predicted={result['predicted_ddi']}",
            flush=True,
        )
        if position < len(cases):
            time.sleep(max(1.0, delay))
    metrics = summarize(records, model)
    out = DATA / "structured" / "eval_negative_metrics.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(metrics, ensure_ascii=False, indent=2), "utf-8")
    print(json.dumps({key: value for key, value in metrics.items() if key != "records"}, ensure_ascii=False, indent=2))
    return metrics


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--delay", type=float, default=1.0)
    parser.add_argument("--model", default=None, help="provider model ID; defaults to the configured provider model")
    args = parser.parse_args()
    run(args.delay, args.model)
