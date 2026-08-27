"""Acquire a small, lawful Stage-0 corpus of Chinese drug instructions.

Default source: the Apache-2.0 `drug-instructions-sample-20` GitHub mirror.
This script deliberately does not crawl search pages or bypass access controls.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
RAW = DATA / "raw"
BASE = "https://raw.githubusercontent.com/lengfeng650-star/drug-instructions-sample-20/main"
MANIFEST_URL = f"{BASE}/metadata/drug_instructions_manifest.jsonl"
MNBVC_URL = "https://huggingface.co/datasets/noeatme/MNBVC/resolve/main/medical/20250624/output.jsonl.gz?download=true"
MNBVC_SHA256 = "b4928f883728ad9981dcfc371f4c332f63d22d7b45fc8f13eed2a8c012d81b2b"
USER_AGENT = "HealthAssistant-stage0-feasibility/0.1 (non-commercial research)"
TARGETS = [
    "氨氯地平", "硝苯地平", "缬沙坦", "厄贝沙坦", "美托洛尔", "氢氯噻嗪",
    "二甲双胍", "格列美脲", "阿卡波糖", "达格列净", "阿托伐他汀", "瑞舒伐他汀",
    "辛伐他汀", "非诺贝特", "阿司匹林", "氯吡格雷", "华法林", "利伐沙班",
    "硝酸甘油", "地高辛", "胺碘酮", "奥美拉唑", "左甲状腺素", "阿仑膦酸钠",
    "布洛芬", "塞来昔布", "克拉霉素", "螺内酯", "氯化钾", "西地那非",
]
SECTION_ALIASES = {"主要成份": "成分", "主要成分": "成分", "适应症": "适应症", "禁忌": "禁忌", "注意事项": "注意事项", "药物相互作用": "药物相互作用"}


def fetch(url: str, destination: Path, timeout: int = 45) -> dict:
    """Fetch one public dataset file; never retries authentication/CAPTCHA failures."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = response.read()
    except urllib.error.HTTPError as exc:
        if exc.code in {401, 403, 429}:  # do not fight access controls/rate limits
            raise RuntimeError(f"source refused request ({exc.code}); stopping: {url}") from exc
        raise
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(payload)
    return {
        "url": url,
        "path": str(destination.relative_to(ROOT)),
        "bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }


def acquire(limit: int = 20, delay: float = 1.0) -> dict:
    RAW.mkdir(parents=True, exist_ok=True)
    manifest_path = RAW / "open20_manifest.jsonl"
    events = [fetch(MANIFEST_URL, manifest_path)]
    rows = [json.loads(line) for line in manifest_path.read_text("utf-8").splitlines() if line.strip()]
    failures: list[dict] = []
    for row in rows[:limit]:
        approval = row["approval_number"]
        url = f"{BASE}/data/pdfs/{urllib.parse.quote(approval)}.pdf"
        try:
            event = fetch(url, RAW / "open20_pdfs" / f"{approval}.pdf")
            expected = row.get("pdf_sha256")
            event["checksum_matches_manifest"] = not expected or event["sha256"] == expected
            event["drug_name"] = row["drug_name"]
            events.append(event)
        except Exception as exc:  # evidence is recorded; the loop remains conservative
            failures.append({"drug_name": row["drug_name"], "url": url, "error": str(exc)})
        time.sleep(max(1.0, delay))
    result = {
        "source": "lengfeng650-star/drug-instructions-sample-20",
        "license": "Apache-2.0",
        "requested": min(limit, len(rows)),
        "downloaded_pdfs": len(events) - 1,
        "events": events,
        "failures": failures,
    }
    (DATA / "acquisition_run.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), "utf-8")
    return result


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def download_mnbvc(path: Path) -> dict:
    """Download the one public medical shard, or reuse a checksum-valid local copy."""
    if path.exists() and sha256_file(path) == MNBVC_SHA256:
        return {"url": MNBVC_URL, "path": str(path.relative_to(ROOT)), "bytes": path.stat().st_size,
                "sha256": MNBVC_SHA256, "reused": True}
    event = fetch(MNBVC_URL, path, timeout=180)
    if event["sha256"] != MNBVC_SHA256:
        raise RuntimeError("downloaded MNBVC shard checksum does not match the Hugging Face LFS object")
    event["reused"] = False
    return event


def mnbvc_fields(row: dict) -> dict[str, str]:
    fields: dict[str, str] = {}
    for paragraph in row.get("段落", []):
        try:
            column = json.loads(paragraph.get("扩展字段", "{}"))["列名"]
        except (KeyError, TypeError, json.JSONDecodeError):
            continue
        fields[column] = paragraph.get("内容", "")
    return fields


def sample_mnbvc(path: Path, sample_size: int = 100) -> dict:
    """Audit the whole medical shard and persist a deterministic unique-label sample."""
    if sha256_file(path) != MNBVC_SHA256:
        raise RuntimeError("MNBVC shard checksum does not match the Hugging Face LFS object")
    total = complete_key_sections = 0
    unique_approvals: set[str] = set()
    target_first: dict[str, dict] = {}
    general: list[dict] = []
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            row = json.loads(line)
            fields = mnbvc_fields(row)
            total += 1
            approval = fields.get("批准文号") or fields.get("编号") or f"row-{line_number}"
            if approval in unique_approvals:
                continue
            unique_approvals.add(approval)
            sections = {canonical: fields[source] for source, canonical in SECTION_ALIASES.items() if fields.get(source)}
            if all(name in sections for name in ("禁忌", "注意事项", "药物相互作用")):
                complete_key_sections += 1
            name = fields.get("通用名称") or fields.get("标题") or ""
            record = {
                "dataset_row": line_number, "drug_name": name,
                "trade_name": fields.get("商品名称") or None, "approval_number": approval,
                "manufacturer": fields.get("生产企业") or None,
                "source_url": fields.get("标题链接") or None,
                "sections": sections,
                "source_dataset": "noeatme/MNBVC medical/20250624",
                "source_dataset_license": "MIT (dataset card); underlying page rights require review",
            }
            for target in TARGETS:
                if target in name and target not in target_first:
                    target_first[target] = record
            if len(general) < sample_size * 3:
                general.append(record)
    selected: list[dict] = []
    selected_approvals: set[str] = set()
    for record in [*target_first.values(), *general]:
        if record["approval_number"] in selected_approvals:
            continue
        selected.append(record)
        selected_approvals.add(record["approval_number"])
        if len(selected) == sample_size:
            break
    output = RAW / "mnbvc_sample_100.jsonl"
    output.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in selected), "utf-8")
    result = {
        "source": "noeatme/MNBVC medical/20250624", "source_url": MNBVC_URL,
        "compressed_bytes": path.stat().st_size, "sha256": MNBVC_SHA256,
        "records": total, "unique_approval_numbers": len(unique_approvals),
        "unique_records_with_all_three_key_sections": complete_key_sections,
        "target_names_found": len(target_first), "target_names_requested": len(TARGETS),
        "sample_saved": len(selected), "sample_path": str(output.relative_to(ROOT)),
    }
    (DATA / "mnbvc_acquisition_summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), "utf-8")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=20, choices=range(1, 21))
    parser.add_argument("--delay", type=float, default=1.0, help="seconds; values below 1 are clamped")
    parser.add_argument("--mnbvc", action="store_true", help="audit downloaded MNBVC medical shard and save 100 labels")
    parser.add_argument("--download-mnbvc", action="store_true", help="download/reuse the public MNBVC medical shard, then audit it")
    parser.add_argument("--mnbvc-file", type=Path, default=RAW / "mnbvc_medical_output.jsonl.gz")
    args = parser.parse_args()
    if args.download_mnbvc:
        download = download_mnbvc(args.mnbvc_file)
        result = sample_mnbvc(args.mnbvc_file)
        result["download"] = download
    elif args.mnbvc:
        result = sample_mnbvc(args.mnbvc_file)
    else:
        result = acquire(args.limit, args.delay)
    print(json.dumps(result, ensure_ascii=False, indent=2))
