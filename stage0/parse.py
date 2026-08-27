"""Extract PDF/HTML/text and chunk Chinese drug-label sections."""
from __future__ import annotations

import argparse
import html
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
KEY_SECTIONS = {"成分", "适应症", "禁忌", "注意事项", "药物相互作用"}
HEADING = re.compile(
    r"(?:^|\n)\s*[【〖\[]\s*([^】〗\]\n]{1,30}?)\s*[】〗\]]\s*[:：]?",
    re.MULTILINE,
)


def pdf_to_text(path: Path) -> str:
    try:
        import pdfplumber  # type: ignore
        with pdfplumber.open(path) as pdf:
            return "\n".join((page.extract_text() or "") for page in pdf.pages)
    except ImportError:
        try:
            import fitz  # type: ignore
            with fitz.open(path) as doc:
                return "\n".join(page.get_text("text") for page in doc)
        except ImportError as exc:
            raise RuntimeError("install pdfplumber or PyMuPDF to parse PDFs") from exc


def html_to_text(path: Path) -> str:
    raw = path.read_text("utf-8", errors="replace")
    try:
        from bs4 import BeautifulSoup  # type: ignore
        return BeautifulSoup(raw, "html.parser").get_text("\n")
    except ImportError:
        raw = re.sub(r"<(script|style)\b.*?</\1>", "", raw, flags=re.I | re.S)
        return html.unescape(re.sub(r"<[^>]+>", "\n", raw))


def clean_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\u3000", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def canonical_heading(value: str) -> str:
    value = re.sub(r"\s+", "", value)
    aliases = {"主要成份": "成分", "主要成分": "成分", "相互作用": "药物相互作用", "禁忌症": "禁忌"}
    return aliases.get(value, value)


def chunk_sections(text: str) -> dict[str, str]:
    text = clean_text(text)
    matches = list(HEADING.finditer(text))
    sections: dict[str, str] = {}
    if matches and matches[0].start() > 0:
        sections["前言"] = text[: matches[0].start()].strip()
    for index, match in enumerate(matches):
        name = canonical_heading(match.group(1))
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        body = text[start:end].strip()
        if body:
            sections[name] = (sections.get(name, "") + ("\n" if name in sections else "") + body).strip()
    if not matches and text:
        sections["未分段"] = text
    return sections


def parse_path(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return pdf_to_text(path)
    if suffix in {".html", ".htm"}:
        return html_to_text(path)
    return path.read_text("utf-8", errors="replace")


def parse_corpus(raw_dir: Path, out_dir: Path, include_excerpts: bool = True) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    records, chunks, failures = [], [], []
    if include_excerpts:
        fixture = DATA / "instruction_excerpts.jsonl"
        if fixture.exists():
            for row in (json.loads(line) for line in fixture.read_text("utf-8").splitlines() if line.strip()):
                text = clean_text(row["text"])
                sections = chunk_sections(text)
                records.append({
                    "source_file": str(fixture.relative_to(ROOT)), "drug": row.get("drug"),
                    "source_url": row.get("source_url"), "is_full_label": False,
                    "text_chars": len(text), "sections": sections,
                })
        mnbvc_sample = raw_dir / "mnbvc_sample_100.jsonl"
        if mnbvc_sample.exists():
            for row in (json.loads(line) for line in mnbvc_sample.read_text("utf-8").splitlines() if line.strip()):
                sections = {canonical_heading(name): clean_text(body) for name, body in row.get("sections", {}).items() if body}
                text_chars = sum(len(body) for body in sections.values())
                records.append({
                    "source_file": str(mnbvc_sample.relative_to(ROOT)), "drug": row.get("drug_name"),
                    "approval_number": row.get("approval_number"), "source_url": row.get("source_url"),
                    "is_full_label": True, "text_chars": text_chars, "sections": sections,
                })
    for path in sorted(p for p in raw_dir.rglob("*") if p.suffix.lower() in {".pdf", ".html", ".htm", ".txt"}):
        try:
            text = clean_text(parse_path(path))
            sections = chunk_sections(text)
            record = {"source_file": str(path.relative_to(ROOT)), "text_chars": len(text), "sections": sections}
            records.append(record)
            for section, body in sections.items():
                chunks.append({"source_file": record["source_file"], "section": section, "is_key": section in KEY_SECTIONS, "text": body})
        except Exception as exc:
            failures.append({"source_file": str(path.relative_to(ROOT)), "error": str(exc)})
    # Add chunks for bundled excerpts after file processing.
    existing = {(x["source_file"], x.get("drug")) for x in records if x.get("drug")}
    for record in records:
        if record.get("drug") and (record["source_file"], record.get("drug")) in existing:
            for section, body in record["sections"].items():
                chunks.append({"source_file": record["source_file"], "drug": record["drug"], "section": section, "is_key": section in KEY_SECTIONS, "text": body})
    (out_dir / "instructions.jsonl").write_text("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in records), "utf-8")
    (out_dir / "chunks.jsonl").write_text("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in chunks), "utf-8")
    summary = {
        "documents": len(records),
        "chunks": len(chunks),
        "key_section_chunks": sum(x["is_key"] for x in chunks),
        "documents_with_禁忌": sum("禁忌" in x["sections"] for x in records),
        "documents_with_注意事项": sum("注意事项" in x["sections"] for x in records),
        "documents_with_药物相互作用": sum("药物相互作用" in x["sections"] for x in records),
        "failures": failures,
    }
    (out_dir / "parse_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), "utf-8")
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--raw", type=Path, default=DATA / "raw")
    parser.add_argument("--out", type=Path, default=DATA / "parsed")
    parser.add_argument("--no-excerpts", action="store_true", help="exclude bundled source-grounded excerpt fixtures")
    args = parser.parse_args()
    print(json.dumps(parse_corpus(args.raw, args.out, not args.no_excerpts), ensure_ascii=False, indent=2))
