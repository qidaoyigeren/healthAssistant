"""Small auditable drug-class ontology for KEGG DDI class expansion.

Every class has 2--5 representative member drugs with KEGG D identifiers. The
members are examples for cross-validation coverage, not an exhaustive class
definition and not a clinical substitution rule.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"


def member(generic_cn: str, name_en: str, kegg: str) -> dict[str, str]:
    return {"generic_cn": generic_cn, "name_en": name_en, "kegg": kegg}


ONTOLOGY: dict[str, dict] = {
    "cardiac_glycosides": {
        "label_cn": "强心苷/洋地黄类", "aliases": ["洋地黄类", "强心苷", "digoxin class"],
        "members": [member("地高辛", "Digoxin", "D00298"), member("洋地黄毒苷", "Digitoxin", "D00297")],
    },
    "nsaids": {
        "label_cn": "非甾体抗炎药", "aliases": ["NSAIDs", "非甾体类抗炎药", "非甾体消炎药"],
        "members": [
            member("布洛芬", "Ibuprofen", "D00126"), member("萘普生", "Naproxen", "D00118"),
            member("吲哚美辛", "Indomethacin", "D00141"), member("双氯芬酸钾", "Diclofenac potassium", "D00903"),
        ],
    },
    "anticoagulants_thrombolytics": {
        "label_cn": "抗凝药或溶栓药", "aliases": ["抗凝药", "溶栓药", "anticoagulants", "thrombolytics"],
        "members": [
            member("华法林", "Warfarin", "D00564"), member("肝素钠", "Heparin sodium", "D02112"),
            member("阿替普酶", "Alteplase", "D02837"),
        ],
    },
    "corticosteroids": {
        "label_cn": "糖皮质激素", "aliases": ["皮质激素", "糖皮质激素", "corticosteroids"],
        "members": [
            member("地塞米松", "Dexamethasone", "D00292"), member("甲泼尼龙", "Methylprednisolone", "D00407"),
            member("氢化可的松", "Hydrocortisone", "D00088"),
        ],
    },
    "cyp3a4_inhibitors": {
        "label_cn": "CYP3A4抑制剂", "aliases": ["CYP3A4强抑制剂", "CYP3A4 inhibitors"],
        "members": [
            member("克拉霉素", "Clarithromycin", "D00276"), member("酮康唑", "Ketoconazole", "D00351"),
            member("伊曲康唑", "Itraconazole", "D00350"), member("利托那韦", "Ritonavir", "D00427"),
        ],
    },
    "oral_azole_antifungals": {
        "label_cn": "口服唑类抗真菌药", "aliases": ["口服咪唑类抗真菌药", "oral azole antifungals"],
        "members": [
            member("酮康唑", "Ketoconazole", "D00351"), member("伊曲康唑", "Itraconazole", "D00350"),
            member("氟康唑", "Fluconazole", "D00322"),
        ],
    },
    "hypoglycemia_potentiating_drugs": {
        "label_cn": "可增强降糖作用的药物", "aliases": ["增强降糖作用药物", "listed interacting drug classes"],
        "members": [
            member("阿司匹林", "Aspirin", "D00109"), member("保泰松", "Phenylbutazone", "D00510"),
            member("复方磺胺甲噁唑", "Sulfamethoxazole and trimethoprim", "D00285"),
        ],
    },
    "antacids_mineral_laxatives": {
        "label_cn": "抗酸剂或含矿物质泻药", "aliases": ["抗酸药", "含钙镁制剂", "mineral-containing laxatives"],
        "members": [
            member("碳酸钙", "Calcium carbonate", "D00932"), member("氢氧化镁", "Magnesium hydroxide", "D00731"),
        ],
    },
    "potassium_raising_drugs": {
        "label_cn": "升高血钾药物", "aliases": ["含钾药物", "升高血钾药物", "potassium-raising drugs"],
        "members": [
            member("氯化钾", "Potassium chloride", "D02060"), member("依那普利", "Enalapril", "D00621"),
            member("缬沙坦", "Valsartan", "D00400"), member("环孢素", "Cyclosporine", "D00184"),
        ],
    },
    "potassium_sparing_diuretics": {
        "label_cn": "保钾利尿剂", "aliases": ["保钾利尿药", "potassium-sparing diuretics"],
        "members": [
            member("螺内酯", "Spironolactone", "D00443"), member("氨苯蝶啶", "Triamterene", "D00386"),
            member("阿米洛利", "Amiloride hydrochloride", "D00649"),
        ],
    },
    "sulfonylureas": {
        "label_cn": "磺酰脲类降糖药", "aliases": ["磺酰脲类", "sulfonylureas"],
        "members": [
            member("格列美脲", "Glimepiride", "D00593"), member("格列吡嗪", "Glipizide", "D00335"),
            member("格列齐特", "Gliclazide", "D01599"), member("格列本脲", "Glibenclamide", "D00336"),
        ],
    },
    "oral_antidiabetics": {
        "label_cn": "口服降糖药", "aliases": ["口服抗糖尿病药", "oral antidiabetics"],
        "members": [
            member("二甲双胍", "Metformin", "D00944"), member("格列美脲", "Glimepiride", "D00593"),
            member("格列吡嗪", "Glipizide", "D00335"),
        ],
    },
    "glucose_lowering_drugs": {
        "label_cn": "降糖药", "aliases": ["口服降糖药或胰岛素", "glucose-lowering drugs"],
        "members": [
            member("格列美脲", "Glimepiride", "D00593"), member("二甲双胍", "Metformin", "D00944"),
            member("人胰岛素", "Insulin human", "D03230"),
        ],
    },
    "folate_antagonists": {
        "label_cn": "叶酸拮抗类抗肿瘤药", "aliases": ["甲氨蝶呤类", "antifolates"],
        "members": [member("甲氨蝶呤", "Methotrexate", "D00142"), member("培美曲塞", "Pemetrexed disodium", "D03828")],
    },
    "cyp2d6_inhibitors": {
        "label_cn": "CYP2D6抑制剂", "aliases": ["CYP2D6酶抑制剂", "CYP2D6 inhibitors"],
        "members": [
            member("奎尼丁葡萄糖酸盐", "Quinidine gluconate", "D00642"), member("帕罗西汀", "Paroxetine", "D02260"),
            member("氟西汀", "Fluoxetine", "D00326"), member("塞来昔布", "Celecoxib", "D00567"),
        ],
    },
    "coumarin_anticoagulants": {
        "label_cn": "香豆素类抗凝药", "aliases": ["香豆素衍生物", "coumarin anticoagulants"],
        "members": [member("华法林", "Warfarin", "D00564"), member("苯丙香豆素", "Phenprocoumon", "D05457")],
    },
}

# Exactly the 16 Stage 0 cases that were skipped as non-single-drug partners.
CASE_CLASS_IDS: dict[str, list[str]] = {
    "E02": ["cardiac_glycosides"],
    "E03": ["nsaids"],
    "E04": ["anticoagulants_thrombolytics"],
    "E05": ["corticosteroids"],
    "E10": ["oral_azole_antifungals"],
    "E12": ["hypoglycemia_potentiating_drugs"],
    "E13": ["antacids_mineral_laxatives"],
    "E14": ["nsaids"],
    "E15": ["potassium_raising_drugs"],
    "E17": ["anticoagulants_thrombolytics"],
    "E18": ["cardiac_glycosides", "folate_antagonists", "oral_antidiabetics"],
    "E20": ["cyp2d6_inhibitors"],
    "E24": ["potassium_raising_drugs", "potassium_sparing_diuretics"],
    "E25": ["glucose_lowering_drugs"],
    "E27": ["potassium_sparing_diuretics", "potassium_raising_drugs"],
    "E30": ["coumarin_anticoagulants"],
}


def validate() -> dict:
    errors: list[str] = []
    all_members: dict[str, dict] = {}
    for class_id, value in ONTOLOGY.items():
        members = value["members"]
        if not 2 <= len(members) <= 5:
            errors.append(f"{class_id} has {len(members)} members; expected 2-5")
        for item in members:
            if not item["kegg"].startswith("D") or len(item["kegg"]) != 6:
                errors.append(f"{class_id}: invalid KEGG id {item['kegg']}")
            all_members[item["name_en"].lower()] = item
    missing_case_classes = [class_id for ids in CASE_CLASS_IDS.values() for class_id in ids if class_id not in ONTOLOGY]
    errors.extend(f"undefined class {class_id}" for class_id in missing_case_classes)
    return {
        "valid": not errors,
        "class_count": len(ONTOLOGY),
        "unique_member_drugs": len(all_members),
        "covered_eval_class_cases": len(CASE_CLASS_IDS),
        "errors": errors,
    }


def mapping_rows() -> list[dict]:
    """Return ontology members in normalize.py's mapping-row shape."""
    unique: dict[str, dict] = {}
    for value in ONTOLOGY.values():
        for item in value["members"]:
            unique.setdefault(item["name_en"].lower(), {
                "generic_cn": item["generic_cn"], "brands": [], "aliases": [],
                "ingredients": [{"name_cn": item["generic_cn"], "name_en": item["name_en"], "kegg": item["kegg"]}],
            })
    return list(unique.values())


def extend_mapping(path: Path = DATA / "mapping.json") -> dict:
    rows = json.loads(path.read_text("utf-8"))
    existing = {
        ingredient["name_en"].lower()
        for row in rows for ingredient in row.get("ingredients", [])
    }
    additions = [
        row for row in mapping_rows()
        if row["ingredients"][0]["name_en"].lower() not in existing
    ]
    path.write_text(json.dumps(rows + additions, ensure_ascii=False, indent=2) + "\n", "utf-8")
    return {"existing_rows": len(rows), "added_rows": len(additions), "total_rows": len(rows) + len(additions)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--extend-mapping", action="store_true")
    parser.add_argument("--dump", action="store_true")
    args = parser.parse_args()
    result = validate()
    if args.extend_mapping:
        result["mapping"] = extend_mapping()
    if args.dump:
        result["ontology"] = ONTOLOGY
        result["case_class_ids"] = CASE_CLASS_IDS
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not result["valid"]:
        raise SystemExit(1)
