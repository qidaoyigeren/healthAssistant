"""DDI extraction spike: function schema + prompt + manual-label fallback.

No model is silently invoked. With ``--llm``, an OpenAI-compatible provider key
must be set, or a ``.env`` file next to this script may provide it; otherwise the
bundled source-grounded labeled set is copied to the structured output and
accuracy is explicitly reported as not measured. TokenDance is the preferred
provider when ``TOKENDANCE_API_KEY`` is present; the older DeepSeek/OpenAI
variables remain supported for reproducibility of Stage 0/1 artifacts.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import time
from pathlib import Path

try:
    from .turn_budget import completion_call, BudgetExceeded, CURRENT
except ImportError:
    from turn_budget import completion_call, BudgetExceeded, CURRENT


ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
TOKENDANCE_BASE_URL = "https://tokendance.space/gateway/v1"
TOKENDANCE_DEFAULT_MODEL = "glm-5.3-flash"
SEVERITIES = ["contraindicated", "major", "moderate", "minor", "unknown"]
MECHANISM_TYPES = [
    "renal_clearance_reduction", "electrolyte_mediated", "pharmacodynamic_antagonism",
    "pharmacodynamic_addition", "metabolic_inhibition", "absorption_change",
    "protein_binding_displacement", "unknown",
]
EFFECT_TAGS = [
    "increased_drug_exposure", "decreased_drug_exposure", "arrhythmia", "reduced_efficacy",
    "bleeding", "gastrointestinal_injury", "myopathy_rhabdomyolysis", "hypoglycemia",
    "other_toxicity", "hyperkalemia", "enhanced_pharmacologic_effect",
]

PROMPT = """你是药品说明书信息抽取器。只根据给定的【药物相互作用】/【禁忌】/【注意事项】原文抽取。
输入会明确给出说明书药品的通用名。每条关系的 drug_a 必须是该通用名；原文中的“本品”“本药”
“本药物”都指它。每条关系输出 drug_a、drug_b、partner_class、type、severity、mechanism、
effect_tag、effect、management、source_text。类别/药物组（如“含钾药物”“CYP3A4强抑制剂”）
保留为 partner_class=true，不要猜成具体药名。禁止补充原文没有的机制或严重程度；未知类别用 unknown，
原文未提供的自由文本字段填 null。source_text 必须是能独立支持关系的最短连续原文片段。

type 只能取：renal_clearance_reduction, electrolyte_mediated, pharmacodynamic_antagonism,
pharmacodynamic_addition, metabolic_inhibition, absorption_change,
protein_binding_displacement, unknown。
severity 只能取：contraindicated, major, moderate, minor, unknown。
effect_tag 只能取：increased_drug_exposure, decreased_drug_exposure, arrhythmia,
reduced_efficacy, bleeding, gastrointestinal_injury, myopathy_rhabdomyolysis,
hypoglycemia, other_toxicity, hyperkalemia, enhanced_pharmacologic_effect。

严重度规则与评估路径相同：只有原文明确“禁忌/严禁”才用 contraindicated；明确避免/不应同用、
严重毒性、出血、心律失常、横纹肌溶解或要求密切治疗监测时用 major；要求谨慎、调整剂量或有
临床相关影响但不满足前述条件时用 moderate；证据不足用 unknown。

抽取前再检查六条通用证据门槛；它们只控制是否输出，不改变阳性关系的字段分类：
1. 适应症、成分或治疗方案中的两个药名共现不算DDI。
2. 原文未明示第二种药或药物类别时不输出；drug_b不得为空、unknown或由模型猜测。
3. 被“不影响、未见、未发现、无、没有明显影响”等否定的暴露、效应或药代变化不输出。
4. “合用安全、耐受良好、耐受性良好”等安全合用陈述不输出。
5. 同类药物过敏、交叉过敏或超敏反应是过敏禁忌，不是DDI。
6. 只有肯定断言一方改变另一方的暴露、效应、药代动力学或毒性才输出；仅提及两种药不够。
   原文明示两方禁用、不宜同用或应避免合用时，视为肯定的联合用药警示。
没有通过门槛的关系时返回 triples=[]。"""
TOOL = {
    "type": "function",
    "function": {
        "name": "record_ddi_triples",
        "description": "Record only DDIs explicitly supported by the supplied label excerpt.",
        "parameters": {
            "type": "object",
            "properties": {
                "triples": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "drug_a": {"type": "string"}, "drug_b": {"type": "string"},
                            "partner_class": {"type": "boolean"},
                            "type": {"type": "string", "enum": MECHANISM_TYPES},
                            "severity": {"type": "string", "enum": SEVERITIES},
                            "mechanism": {"type": ["string", "null"]},
                            "effect_tag": {"type": "string", "enum": EFFECT_TAGS},
                            "effect": {"type": ["string", "null"]},
                            "management": {"type": ["string", "null"]}, "source_text": {"type": "string"},
                        },
                        "required": ["drug_a", "drug_b", "partner_class", "type", "severity", "mechanism", "effect_tag", "effect", "management", "source_text"],
                        "additionalProperties": False,
                    },
                }
            },
            "required": ["triples"], "additionalProperties": False,
        },
    },
}

EVAL_PROMPT = """You extract exactly one drug-drug interaction from a Chinese drug-label sentence.
Use only the supplied label drug and sentence. Return English generic names or a faithful English
drug class. Do not add facts that are absent from the sentence. source_text must be an exact,
contiguous quote from the Chinese input sentence.

Use only these controlled values:
- severity: contraindicated, major, moderate, minor, unknown
- mechanism_type: renal_clearance_reduction, electrolyte_mediated,
  pharmacodynamic_antagonism, pharmacodynamic_addition, metabolic_inhibition,
  absorption_change, protein_binding_displacement, unknown
- effect_tag: increased_drug_exposure, decreased_drug_exposure, arrhythmia,
  reduced_efficacy, bleeding, gastrointestinal_injury, myopathy_rhabdomyolysis,
  hypoglycemia, other_toxicity, hyperkalemia, enhanced_pharmacologic_effect

Severity rubric: contraindicated only for explicit 禁忌/严禁; major for explicit avoid/do-not-use,
serious toxicity, bleeding, arrhythmia, rhabdomyolysis, or close therapeutic monitoring;
moderate for caution, dose adjustment, or clinically relevant effect without those markers.
"""

EVAL_TOOL = {
    "type": "function",
    "function": {
        "name": "record_evaluation_triple",
        "description": "Record the single DDI explicitly supported by the supplied label sentence.",
        "parameters": {
            "type": "object",
            "properties": {
                "drug_a": {"type": "string"},
                "drug_b": {"type": "string"},
                "severity": {"type": "string", "enum": ["contraindicated", "major", "moderate", "minor", "unknown"]},
                "mechanism_type": {"type": "string", "enum": [
                    "renal_clearance_reduction", "electrolyte_mediated", "pharmacodynamic_antagonism",
                    "pharmacodynamic_addition", "metabolic_inhibition", "absorption_change",
                    "protein_binding_displacement", "unknown"
                ]},
                "effect_tag": {"type": "string", "enum": [
                    "increased_drug_exposure", "decreased_drug_exposure", "arrhythmia", "reduced_efficacy",
                    "bleeding", "gastrointestinal_injury", "myopathy_rhabdomyolysis", "hypoglycemia",
                    "other_toxicity", "hyperkalemia", "enhanced_pharmacologic_effect"
                ]},
                "mechanism": {"type": ["string", "null"]},
                "effect": {"type": "string"},
                "management": {"type": ["string", "null"]},
                "source_text": {"type": "string"}
            },
            "required": ["drug_a", "drug_b", "severity", "mechanism_type", "effect_tag", "mechanism", "effect", "management", "source_text"],
            "additionalProperties": False
        }
    }
}

# Gold partner concepts are intentionally separated from model-visible prompts. Matching is
# concept-level (one accepted explicit member is sufficient for a multi-drug/class source).
PARTNER_ALIASES = {
    "E01": ["lithium", "锂"], "E02": ["digoxin", "digitalis", "洋地黄", "地高辛"], "E03": ["nsaid", "nonsteroidal", "非甾体"],
    "E04": ["anticoagul", "thrombol", "heparin", "dicoumarol", "streptokinase", "抗凝", "溶栓", "肝素", "双香豆素", "链激酶"],
    "E05": ["corticosteroid", "dexamethasone", "glucocorticoid", "糖皮质激素", "地塞米松"], "E06": ["carbamazepine", "卡马西平"],
    "E07": ["simvastatin", "lovastatin", "hmg coa", "辛伐他汀", "洛伐他汀", "还原酶抑制"], "E08": ["cisapride", "pimozide", "西沙必利", "匹莫齐特"],
    "E09": ["digoxin", "地高辛"], "E10": ["azole", "ketoconazole", "itraconazole", "miconazole", "fluconazole", "咪唑", "酮康唑", "伊曲康唑", "咪康唑", "氟康唑"],
    "E11": ["nifedipine", "硝苯地平"], "E12": ["salicyl", "sulfonamide", "phenylbutazone", "tetracycline", "mao inhibitor", "beta blocker", "chloramphenicol", "coumarin", "cyclophosphamide", "水杨酸", "磺胺", "保泰松", "四环素", "单胺氧化酶", "受体阻滞", "氯霉素", "香豆素", "环磷酰胺"],
    "E13": ["antacid", "laxative", "calcium", "magnesium", "iron", "抗酸", "导泻", "钙", "镁", "铁"], "E14": ["nsaid", "indomethacin", "nonsteroidal", "非甾体", "吲哚美辛"],
    "E15": ["potassium", "ace inhibitor", "angiotensin", "cyclosporine", "含钾", "转换酶抑制", "受体拮抗", "环孢素"], "E16": ["digoxin", "地高辛"],
    "E17": ["anticoagul", "heparin", "dicoumarol", "抗凝", "肝素", "双香豆素"], "E18": ["digoxin", "methotrexate", "antidiabetic", "hypoglycemic", "地高辛", "甲氨蝶呤", "降血糖"],
    "E19": ["digoxin", "digitalis", "地高辛", "洋地黄"], "E20": ["cyp2d6", "quinidine", "terbinafine", "paroxetine", "fluoxetine", "sertraline", "celecoxib", "propafenone", "diphenhydramine", "奎尼丁", "特比萘芬", "帕罗西汀", "氟西汀", "舍曲林", "塞来昔布", "普罗帕酮", "苯海拉明"],
    "E21": ["aspirin", "阿司匹林"], "E22": ["naproxen", "nsaid", "萘普生", "非甾体"], "E23": ["cyclosporine", "erythromycin", "gemfibrozil", "niacin", "环孢素", "红霉素", "吉非罗齐", "烟酸"],
    "E24": ["potassium sparing", "potassium containing", "含钾", "保钾"],
    "E25": ["sulfonylurea", "metformin", "insulin", "磺酰脲", "二甲双胍", "胰岛素"], "E26": ["ketoconazole", "ritonavir", "酮康唑", "利托那韦"],
    "E27": ["potassium sparing", "spironolactone", "triamterene", "amiloride", "potassium", "保钾", "螺内酯", "氨苯蝶啶", "阿米洛利", "含钾"],
    "E28": ["amiodarone", "verapamil", "diltiazem", "胺碘酮", "维拉帕米", "地尔硫卓"], "E29": ["warfarin", "华法林"],
    "E30": ["coumarin", "anticoagul", "香豆素", "抗凝"]
}

DRUG_A_ALIASES = {
    "Hydrochlorothiazide": ["hydrochlorothiazide", "氢氯噻嗪", "噻嗪类利尿剂", "厄贝沙坦氢氯噻嗪"],
    "Aspirin": ["aspirin", "阿司匹林"], "Clarithromycin": ["clarithromycin", "克拉霉素"],
    "Omeprazole": ["omeprazole", "奥美拉唑"], "Metformin": ["metformin", "二甲双胍"],
    "Glimepiride": ["glimepiride", "格列美脲"], "Alendronate": ["alendronate", "阿仑膦酸"],
    "Spironolactone": ["spironolactone", "螺内酯"], "Ibuprofen": ["ibuprofen", "布洛芬"],
    "Nifedipine": ["nifedipine", "硝苯地平"], "Metoprolol": ["metoprolol", "美托洛尔"],
    "Clopidogrel": ["clopidogrel", "氯吡格雷"], "Simvastatin": ["simvastatin", "辛伐他汀"],
    "Potassium chloride": ["potassium chloride", "氯化钾"], "Acarbose": ["acarbose", "阿卡波糖"],
    "Rivaroxaban": ["rivaroxaban", "利伐沙班"], "Valsartan": ["valsartan", "缬沙坦"],
    "Digoxin": ["digoxin", "地高辛"], "Amiodarone": ["amiodarone", "胺碘酮"],
    "Levothyroxine": ["levothyroxine", "左甲状腺素"],
}


def manual_fallback(output: Path) -> dict:
    samples = DATA / "ddi_labeled.jsonl"
    rows = [json.loads(x) for x in samples.read_text("utf-8").splitlines() if x.strip()]
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in rows), "utf-8")
    result = {"mode": "manual_labeled_fallback", "triples": len(rows), "accuracy": "not yet measured"}
    (output.parent / "extraction_summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), "utf-8")
    return result


def _load_dotenv() -> None:
    env_file = ROOT / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text("utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def resolve_llm_config(model: str | None = None, require_key: bool = True) -> dict[str, str]:
    """Resolve one OpenAI-compatible provider without exposing credentials.

    TokenDance-specific variables take precedence so a stale DeepSeek key in an
    existing local ``.env`` cannot accidentally select the old provider. Generic
    and legacy variables are retained as explicit compatibility fallbacks.
    """
    _load_dotenv()
    tokendance_key = os.getenv("TOKENDANCE_API_KEY", "").strip()
    generic_key = os.getenv("LLM_API_KEY", "").strip()
    legacy_key = (os.getenv("DEEPSEEK_API_KEY") or os.getenv("OPENAI_API_KEY") or "").strip()

    if tokendance_key:
        provider = "tokendance"
        api_key = tokendance_key
        base_url = os.getenv("TOKENDANCE_BASE_URL", TOKENDANCE_BASE_URL).strip()
        default_model = os.getenv("TOKENDANCE_MODEL", TOKENDANCE_DEFAULT_MODEL).strip()
    elif generic_key:
        provider = "openai_compatible"
        api_key = generic_key
        base_url = (os.getenv("LLM_BASE_URL") or os.getenv("OPENAI_BASE_URL") or "https://api.openai.com/v1").strip()
        default_model = (os.getenv("LLM_MODEL") or os.getenv("OPENAI_MODEL") or "gpt-4o-mini").strip()
    else:
        provider = "deepseek_legacy"
        api_key = legacy_key
        base_url = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com").strip()
        default_model = os.getenv("DEEPSEEK_MODEL", "deepseek-chat").strip()

    if require_key and not api_key:
        raise RuntimeError(
            "No LLM API key configured; set TOKENDANCE_API_KEY, LLM_API_KEY, "
            "DEEPSEEK_API_KEY, or OPENAI_API_KEY"
        )
    return {
        "provider": provider,
        "api_key": api_key,
        "base_url": base_url,
        "model": (model or default_model).strip(),
    }


def llm_completion_options() -> dict[str, object]:
    """Return provider options shared by every extraction/evaluation call."""
    config = resolve_llm_config(require_key=False)
    if config["provider"] == "tokendance":
        thinking = os.getenv("TOKENDANCE_THINKING", "disabled").strip().lower()
        max_tokens_raw = os.getenv("TOKENDANCE_MAX_TOKENS", "1024")
    else:
        thinking = os.getenv("LLM_THINKING", "").strip().lower()
        max_tokens_raw = os.getenv("LLM_MAX_TOKENS", "1024")
    try:
        max_tokens = max(128, int(max_tokens_raw))
    except ValueError as exc:
        raise RuntimeError("LLM max-token setting must be an integer") from exc
    options: dict[str, object] = {"max_tokens": max_tokens}
    if thinking in {"enabled", "disabled"}:
        options["extra_body"] = {"thinking": {"type": thinking}}
    return options


def create_llm_client(config: dict[str, str] | None = None):
    """Create the shared OpenAI-compatible client with bounded network waits."""
    config = config or resolve_llm_config()
    if config["provider"] == "tokendance":
        timeout_raw = os.getenv("TOKENDANCE_TIMEOUT_SECONDS", "60")
        retries_raw = os.getenv("TOKENDANCE_MAX_RETRIES", "0")
    else:
        timeout_raw = os.getenv("LLM_TIMEOUT_SECONDS", "60")
        retries_raw = os.getenv("LLM_MAX_RETRIES", "0")
    try:
        timeout = max(1.0, float(timeout_raw))
        max_retries = max(0, int(retries_raw))
    except ValueError as exc:
        raise RuntimeError("LLM timeout/retry settings must be numeric") from exc
    from openai import OpenAI

    return OpenAI(
        api_key=config["api_key"],
        base_url=config["base_url"],
        timeout=timeout,
        max_retries=max_retries,
    )


def _source_generic_name(label_drug: str) -> str:
    """Resolve a label/product name to a stable source-drug name.

    ``normalize`` is used when the curated mapping knows the product. For
    combinations or unmapped labels, only the formulation suffix is removed;
    the ingredient-bearing Chinese generic name remains intact.
    """
    value = re.sub(r"\s+", "", label_drug).strip()
    formulations = (
        "肠溶胶囊", "缓释胶囊", "分散片", "咀嚼片", "泡腾片", "肠溶片", "缓释片", "控释片",
        "薄膜衣片", "注射液", "口服溶液", "混悬液", "颗粒剂", "胶囊", "颗粒", "滴丸", "片剂", "片",
    )
    for suffix in formulations:
        if value.endswith(suffix):
            value = value[: -len(suffix)]
            break
    try:
        from normalize import build_index, compact, load_mapping

        exact = build_index(load_mapping()).get(compact(value))
        if exact:
            return exact["generic_cn"]
    except (ImportError, KeyError, OSError, json.JSONDecodeError):
        pass
    return value


def _production_user_content(source_drug: str, text: str) -> str:
    generic = _source_generic_name(source_drug)
    return f"说明书药品通用名：{generic}\n说明书原文：{text}"


def _canonicalize_production_triple(triple: dict, source_drug: str) -> dict:
    """Enforce source-self resolution and expose eval-compatible field names."""
    out = dict(triple)
    # Every extracted relation is anchored to the supplied label. This resolves
    # 本品/本药/本药物 deterministically even when the model emits the pronoun.
    out["drug_a"] = _source_generic_name(source_drug)
    mechanism_type = out.get("type") or out.get("mechanism_type") or "unknown"
    out["type"] = mechanism_type if mechanism_type in MECHANISM_TYPES else "unknown"
    out["mechanism_type"] = out["type"]
    if out.get("severity") not in SEVERITIES:
        out["severity"] = "unknown"
    if out.get("effect_tag") not in EFFECT_TAGS:
        out["effect_tag"] = "other_toxicity"
    return out


def _production_call(client, model: str, source_drug: str, text: str, delay: float = 1.0) -> list[dict]:
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            response = completion_call("ddi_extractor", client,
                model=model,
                messages=[
                    {"role": "system", "content": PROMPT},
                    {"role": "user", "content": _production_user_content(source_drug, text)},
                ],
                tools=[TOOL],
                tool_choice={"type": "function", "function": {"name": "record_ddi_triples"}},
                temperature=0,
                **llm_completion_options(),
            )
            calls = response.choices[0].message.tool_calls or []
            if not calls:
                # A successful response that abstains from the forced tool is
                # a model-level negative, not a transport failure. Retrying it
                # would sample repeatedly until a possible false positive
                # appears and would inflate live-evaluation recall.
                return []
            triples: list[dict] = []
            for call in calls:
                triples.extend(
                    _canonicalize_production_triple(triple, source_drug)
                    for triple in json.loads(call.function.arguments)["triples"]
                )
            # The hardened prompt explicitly defines triples=[] as the valid
            # output for a candidate that fails any of the six evidence gates.
            # Retrying that deterministic negative wastes provider calls and
            # can turn a clean result into a sampling-induced false positive.
            return triples
        except BudgetExceeded:
            raise
        except Exception as exc:
            last_error = exc
            if CURRENT.get() is not None and CURRENT.get().exhausted():
                raise
            if attempt < 2:
                time.sleep(max(1.0, delay) * (attempt + 1))
    raise RuntimeError(f"production extraction failed after three attempts: {last_error}")


def llm_extract(excerpts: Path, output: Path, model: str | None, delay: float = 1.0) -> dict:
    config = resolve_llm_config(model)
    model = config["model"]
    client = create_llm_client(config)
    out = []
    for row in (json.loads(x) for x in excerpts.read_text("utf-8").splitlines() if x.strip()):
        source_drug = row.get("drug") or row.get("label_drug") or row.get("source_drug")
        if not source_drug:
            raise ValueError("production input row is missing drug/label_drug/source_drug")
        for triple in _production_call(client, model, source_drug, row["text"], delay):
            triple.update({"source_url": row.get("source_url"), "source_drug": source_drug})
            out.append(triple)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in out), "utf-8")
    return {
        "mode": "openai_compatible_function_call",
        "provider": config["provider"],
        "model": model,
        "triples": len(out),
        "accuracy": "not yet measured (no held-out labels scored)",
    }


def _normalized(value: str | None) -> str:
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", " ", (value or "").lower()).strip()


def _same_entity(expected: str, predicted: str | None) -> bool:
    right = _normalized(predicted)
    aliases = DRUG_A_ALIASES.get(expected, [expected])
    return bool(right and any(_normalized(alias) in right or right in _normalized(alias) for alias in aliases))


def _source_span_is_valid(source: str, predicted: str | None) -> bool:
    compact_source = re.sub(r"\s+", "", source)
    compact_prediction = re.sub(r"\s+", "", predicted or "")
    return bool(compact_prediction and compact_prediction in compact_source)


def _score_record(record: dict) -> dict[str, bool]:
    prediction, expected = record.get("prediction"), record["expected"]
    if prediction is None:
        checks = {name: False for name in ("drug_a", "drug_b", "severity", "mechanism_type", "effect_tag", "source_span")}
    else:
        partner = _normalized(prediction.get("drug_b"))
        checks = {
            "drug_a": _same_entity(expected["drug_a"], prediction.get("drug_a")),
            "drug_b": any(_normalized(alias) in partner for alias in PARTNER_ALIASES[record["case_id"]]),
            "severity": prediction.get("severity") == expected["severity"],
            "mechanism_type": prediction.get("mechanism_type") == expected["mechanism_type"],
            "effect_tag": prediction.get("effect_tag") == expected["effect_tag"],
            "source_span": _source_span_is_valid(record["text"], prediction.get("source_text")),
        }
    checks["entity_pair"] = checks["drug_a"] and checks["drug_b"]
    checks["all_core"] = all(checks[name] for name in ("entity_pair", "severity", "mechanism_type", "effect_tag", "source_span"))
    return checks


def _summarize(
    predictions: list[dict],
    output: Path,
    model: str,
    metrics_path: Path | None = None,
    mode: str = "openai_compatible_function_call_blind_evaluation",
) -> dict:
    fields = ("drug_a", "drug_b", "entity_pair", "severity", "mechanism_type", "effect_tag", "source_span", "all_core")
    total = len(predictions)
    metrics = {
        "mode": mode, "model": model,
        "sample_count": total, "sample_design": "30 manually labeled positive source sentences; single annotator",
        "successful_api_outputs": sum(row["prediction"] is not None for row in predictions),
        "accuracy": {field: {"correct": sum(row["checks"][field] for row in predictions), "total": total,
                              "value": round(sum(row["checks"][field] for row in predictions) / total, 4)} for field in fields},
        "limitations": [
            "Positive-only, sentence-selected evaluation; no negatives or full-section multi-triple recall.",
            "Gold labels were created by one annotator and severity is an operational spike rubric, not a clinical standard.",
            "Entity scoring is bilingual concept matching; the model did not consistently obey the requested English-only output format.",
            "Partner scoring accepts any explicit member of a multi-drug/class source; it does not measure complete list recall."
        ],
        "predictions_path": str(output.relative_to(ROOT)),
    }
    metrics_path = metrics_path or output.parent / "eval_metrics.json"
    metrics_path.write_text(json.dumps(metrics, ensure_ascii=False, indent=2), "utf-8")
    return metrics


def rescore(output: Path, model: str) -> dict:
    predictions = [json.loads(line) for line in output.read_text("utf-8").splitlines() if line.strip()]
    for record in predictions:
        record["checks"] = _score_record(record)
    output.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in predictions), "utf-8")
    return _summarize(predictions, output, model)


def evaluate(gold_path: Path, output: Path, model: str | None, delay: float = 1.0) -> dict:
    """Blindly extract and score 30 source-grounded, manually labeled positive examples."""
    config = resolve_llm_config(model)
    model = config["model"]
    client = create_llm_client(config)
    gold = [json.loads(line) for line in gold_path.read_text("utf-8").splitlines() if line.strip()]
    sample_path = DATA / "raw" / "mnbvc_sample_100.jsonl"
    samples = [json.loads(line) for line in sample_path.read_text("utf-8").splitlines() if line.strip()]
    predictions = []
    for position, row in enumerate(gold, 1):
        user_content = f"Label drug: {row['label_drug']}\nChinese source sentence: {row['text']}"
        last_error: Exception | None = None
        prediction = None
        for attempt in range(3):
            try:
                response = completion_call("ddi_extractor", client,
                    model=model,
                    messages=[{"role": "system", "content": EVAL_PROMPT}, {"role": "user", "content": user_content}],
                    tools=[EVAL_TOOL],
                    tool_choice={"type": "function", "function": {"name": "record_evaluation_triple"}},
                    temperature=0,
                    **llm_completion_options(),
                )
                calls = response.choices[0].message.tool_calls or []
                if not calls:
                    raise ValueError("model returned no function call")
                prediction = json.loads(calls[0].function.arguments)
                break
            except Exception as exc:
                last_error = exc
                if attempt < 2:
                    time.sleep(max(1.0, delay) * (attempt + 1))
        source_record = samples[row["source_sample_index"]]
        record = {
            "case_id": row["case_id"], "label_drug": row["label_drug"], "text": row["text"],
            "source_sample_index": row["source_sample_index"],
            "approval_number": source_record.get("approval_number"), "source_url": source_record.get("source_url"),
            "expected": row["expected"], "prediction": prediction, "error": str(last_error) if prediction is None else None,
        }
        record["checks"] = _score_record(record)
        predictions.append(record)
        print(f"evaluated {position}/{len(gold)}: {row['case_id']}", flush=True)
        if position < len(gold):
            time.sleep(max(1.0, delay))
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in predictions), "utf-8")
    return _summarize(predictions, output, model)


def production_evaluate(gold_path: Path, output: Path, model: str | None, delay: float = 1.0) -> dict:
    """Run the free-text production schema on the same frozen 30 positive cases."""
    config = resolve_llm_config(model)
    model = config["model"]
    client = create_llm_client(config)
    gold = [json.loads(line) for line in gold_path.read_text("utf-8").splitlines() if line.strip()]
    samples = [
        json.loads(line)
        for line in (DATA / "raw" / "mnbvc_sample_100.jsonl").read_text("utf-8").splitlines()
        if line.strip()
    ]
    predictions: list[dict] = []
    for position, row in enumerate(gold, 1):
        prediction = None
        error = None
        try:
            triples = _production_call(client, model, row["label_drug"], row["text"], delay)
            prediction = triples[0]
        except Exception as exc:
            error = str(exc)
        source_record = samples[row["source_sample_index"]]
        record = {
            "case_id": row["case_id"], "label_drug": row["label_drug"], "text": row["text"],
            "source_sample_index": row["source_sample_index"],
            "approval_number": source_record.get("approval_number"), "source_url": source_record.get("source_url"),
            "expected": row["expected"], "prediction": prediction, "error": error,
        }
        record["checks"] = _score_record(record)
        predictions.append(record)
        print(f"production-evaluated {position}/{len(gold)}: {row['case_id']}", flush=True)
        if position < len(gold):
            time.sleep(max(1.0, delay))
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in predictions), "utf-8")
    metrics = _summarize(
        predictions,
        output,
        model,
        metrics_path=DATA / "structured" / "production_eval_metrics.json",
        mode="openai_compatible_free_text_production_path_evaluation",
    )
    metrics["alignment"] = {
        "self_reference_resolution": "drug_a deterministically canonicalized to the supplied source generic name",
        "controlled_type_field": "type is the evaluation mechanism_type enum; mechanism_type is emitted as an alias for direct scoring",
    }
    metrics["limitations"] = [
        "Positive-only, sentence-selected evaluation; detection recall is measured separately with automatic tool choice.",
        "Gold labels were created by one annotator and severity is an operational data-layer rubric, not a clinical standard.",
        "Entity scoring is bilingual concept matching after deterministic source-self canonicalization.",
        "Partner scoring accepts any explicit member of a multi-drug/class source; it does not measure complete list recall.",
    ]
    (DATA / "structured" / "production_eval_metrics.json").write_text(
        json.dumps(metrics, ensure_ascii=False, indent=2), "utf-8"
    )
    return metrics


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--llm", action="store_true")
    parser.add_argument("--evaluate", action="store_true", help="run blind 30-case labeled evaluation")
    parser.add_argument("--production-evaluate", action="store_true", help="run the free-text production path on the same 30 cases")
    parser.add_argument("--rescore", action="store_true", help="rescore saved predictions without an API call")
    parser.add_argument("--model", default=None, help="provider model ID; defaults to the configured provider model")
    parser.add_argument("--gold", type=Path, default=DATA / "eval_gold_30.jsonl")
    parser.add_argument("--delay", type=float, default=1.0, help="seconds between API requests; clamped to >=1")
    parser.add_argument("--input", type=Path, default=DATA / "instruction_excerpts.jsonl")
    parser.add_argument("--output", type=Path, default=DATA / "structured" / "ddi_triples.jsonl")
    parser.add_argument("--production-output", type=Path, default=DATA / "structured" / "production_eval_predictions_30.jsonl")
    args = parser.parse_args()
    eval_output = DATA / "structured" / "eval_predictions_30.jsonl"
    configured_model = args.model or resolve_llm_config(require_key=False)["model"]
    if args.rescore:
        result = rescore(eval_output, configured_model)
    elif args.production_evaluate:
        result = production_evaluate(args.gold, args.production_output, configured_model, args.delay)
    elif args.evaluate:
        result = evaluate(args.gold, eval_output, configured_model, args.delay)
    elif args.llm:
        result = llm_extract(args.input, args.output, configured_model, args.delay)
    else:
        result = manual_fallback(args.output)
    print(json.dumps(result, ensure_ascii=False, indent=2))
