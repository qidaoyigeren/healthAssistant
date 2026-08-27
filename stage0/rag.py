"""Local hybrid retrieval for Chinese medication-label safety sections.

The module deliberately keeps acquisition, embeddings, and retrieval local:

* ``--curate`` deterministically selects approval-number-deduplicated labels from
  the checksum-pinned MNBVC medical shard.
* ``--build`` chunks 禁忌/注意事项/药物相互作用, builds BM25 token data, embeds
  chunks with a local sentence-transformers model, and writes a FAISS index.
* ``--evaluate`` scores the frozen hand-written query set.
* ``--query`` provides a small command-line retrieval interface with metadata
  filters.

DeepSeek is chat-only and is never used by this module.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
RAW_SHARD = DATA / "raw" / "mnbvc_medical_output.jsonl.gz"
CORPUS_PATH = DATA / "rag_corpus.jsonl"
EVAL_PATH = DATA / "rag_eval_queries.jsonl"
INDEX_DIR = DATA / "structured" / "rag_index"
METRICS_PATH = DATA / "structured" / "rag_metrics.json"
MODEL_NAME = "BAAI/bge-small-zh-v1.5"
SECTIONS = ("药物相互作用", "禁忌", "注意事项")
SECTION_ALIASES = {"药物相互作用": "药物相互作用", "相互作用": "药物相互作用", "禁忌": "禁忌", "禁忌症": "禁忌", "注意事项": "注意事项"}

# Names cover common long-term cardiovascular, diabetes, lipid, respiratory,
# endocrine, psychiatric, and osteoporosis therapies. The selection is still a
# data-engineering sample, not a prevalence estimate.
CHRONIC_DRUG_TERMS = (
    "氨氯地平", "硝苯地平", "非洛地平", "缬沙坦", "厄贝沙坦", "氯沙坦", "替米沙坦",
    "贝那普利", "依那普利", "培哚普利", "卡托普利", "美托洛尔", "比索洛尔", "卡维地洛",
    "氢氯噻嗪", "呋塞米", "螺内酯", "二甲双胍", "格列美脲", "格列齐特", "格列本脲",
    "阿卡波糖", "达格列净", "恩格列净", "西格列汀", "胰岛素", "阿托伐他汀",
    "瑞舒伐他汀", "辛伐他汀", "非诺贝特", "阿司匹林", "氯吡格雷", "华法林",
    "利伐沙班", "地高辛", "胺碘酮", "奥美拉唑", "左甲状腺素", "阿仑膦酸",
    "孟鲁司特", "布地奈德", "沙美特罗", "氟替卡松", "舍曲林", "帕罗西汀",
)


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text("utf-8").splitlines() if line.strip()]


def write_jsonl(path: Path, rows: Iterable[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), "utf-8")


def mnbvc_fields(row: dict) -> dict[str, str]:
    fields: dict[str, str] = {}
    for paragraph in row.get("段落", []):
        try:
            column = json.loads(paragraph.get("扩展字段", "{}"))["列名"]
        except (KeyError, TypeError, json.JSONDecodeError):
            continue
        fields[column] = paragraph.get("内容", "")
    return fields


def stable_order_key(approval_number: str, seed: str) -> str:
    return hashlib.sha256(f"{seed}\x1f{approval_number}".encode("utf-8")).hexdigest()


def curate_corpus(
    shard: Path = RAW_SHARD,
    output: Path = CORPUS_PATH,
    sample_size: int = 1200,
    priority_limit: int = 500,
    seed: str = "stage1-rag-v1",
) -> dict:
    """Select complete labels, deduplicated by approval number.

    Up to ``priority_limit`` labels whose generic name matches a chronic-drug
    term are selected first. Remaining slots use a deterministic hash order over
    every other eligible label, preventing source-order clustering.
    """
    if sample_size < 1000:
        raise ValueError("sample_size must be at least 1000 for the Stage 1 corpus")
    unique_approvals: set[str] = set()
    priority: list[dict] = []
    diverse: list[dict] = []
    eligible = duplicate_approvals = 0
    with gzip.open(shard, "rt", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            fields = mnbvc_fields(json.loads(line))
            approval = fields.get("批准文号") or fields.get("编号")
            if not approval:
                continue
            if approval in unique_approvals:
                duplicate_approvals += 1
                continue
            unique_approvals.add(approval)
            sections = {
                canonical: re.sub(r"\s+", " ", fields[source]).strip()
                for source, canonical in SECTION_ALIASES.items()
                if fields.get(source)
            }
            # A complete three-section record avoids selection artifacts where a
            # retriever is evaluated against a label that never exposed the target section.
            if not all(sections.get(section) for section in SECTIONS):
                continue
            eligible += 1
            drug_name = fields.get("通用名称") or fields.get("标题") or ""
            matched_terms = [term for term in CHRONIC_DRUG_TERMS if term in drug_name]
            record = {
                "dataset_row": line_number,
                "drug_name": drug_name,
                "trade_name": fields.get("商品名称") or None,
                "approval_number": approval,
                "manufacturer": fields.get("生产企业") or None,
                "source_url": fields.get("标题链接") or None,
                "sections": {section: sections[section] for section in SECTIONS},
                "selection_group": "chronic_priority" if matched_terms else "diverse_hash_sample",
                "matched_priority_terms": matched_terms,
                "source_dataset": "noeatme/MNBVC medical/20250624",
                "source_dataset_license": "MIT (dataset card); underlying page rights require review",
            }
            (priority if matched_terms else diverse).append(record)
    priority.sort(key=lambda row: stable_order_key(row["approval_number"], seed + ":priority"))
    diverse.sort(key=lambda row: stable_order_key(row["approval_number"], seed + ":diverse"))
    priority_count = min(priority_limit, len(priority), sample_size)
    selected = priority[:priority_count]
    selected.extend(diverse[: sample_size - len(selected)])
    if len(selected) < sample_size:
        already = {row["approval_number"] for row in selected}
        selected.extend(row for row in priority[priority_count:] if row["approval_number"] not in already)
        selected = selected[:sample_size]
    if len(selected) < sample_size:
        raise RuntimeError(f"only {len(selected)} eligible deduplicated labels were found")
    write_jsonl(output, selected)
    summary = {
        "source": str(shard.relative_to(ROOT)),
        "output": str(output.relative_to(ROOT)),
        "selection_version": seed,
        "selection_method": "complete three-key-section labels; chronic-name priority then SHA-256-ordered diverse sample",
        "requested_labels": sample_size,
        "selected_labels": len(selected),
        "unique_approval_numbers": len({row["approval_number"] for row in selected}),
        "chronic_priority_labels": sum(row["selection_group"] == "chronic_priority" for row in selected),
        "diverse_labels": sum(row["selection_group"] == "diverse_hash_sample" for row in selected),
        "eligible_unique_complete_labels": eligible,
        "duplicate_approval_rows_skipped": duplicate_approvals,
        "sections_per_label": list(SECTIONS),
    }
    summary_path = DATA / "structured" / "rag_corpus_summary.json"
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), "utf-8")
    return summary


def _sentence_units(text: str) -> list[str]:
    units = [part.strip() for part in re.findall(r"[^。！？；;\n]+[。！？；;]?", text) if part.strip()]
    return units or [text.strip()]


def split_chunk_text(text: str, max_chars: int = 480, overlap_chars: int = 80) -> list[str]:
    """Sentence-aware bounded chunks; deterministic character fallback for long units."""
    chunks: list[str] = []
    current = ""
    for unit in _sentence_units(re.sub(r"\s+", " ", text).strip()):
        if len(unit) > max_chars:
            if current:
                chunks.append(current)
                current = ""
            step = max_chars - overlap_chars
            chunks.extend(unit[start : start + max_chars] for start in range(0, len(unit), step) if unit[start : start + max_chars])
            continue
        if current and len(current) + len(unit) > max_chars:
            chunks.append(current)
            overlap = current[-overlap_chars:] if overlap_chars else ""
            current = overlap + unit
        else:
            current += unit
    if current:
        chunks.append(current)
    return chunks


def make_chunks(corpus: list[dict]) -> list[dict]:
    chunks: list[dict] = []
    for record in corpus:
        for section in SECTIONS:
            for position, text in enumerate(split_chunk_text(record["sections"][section])):
                raw_id = f"{record['approval_number']}\x1f{section}\x1f{position}"
                chunk_id = "R" + hashlib.sha1(raw_id.encode("utf-8")).hexdigest()[:16]
                chunks.append({
                    "chunk_id": chunk_id,
                    "approval_number": record["approval_number"],
                    "drug_name": record["drug_name"],
                    "trade_name": record.get("trade_name"),
                    "section": section,
                    "chunk_index": position,
                    "text": text,
                    "source_url": record.get("source_url"),
                })
    return chunks


def jieba_tokens(text: str) -> list[str]:
    import jieba

    normalized = re.sub(r"\s+", " ", text.lower()).strip()
    return [token.strip() for token in jieba.lcut(normalized) if token.strip()]


def document_text(chunk: dict) -> str:
    return f"药品：{chunk['drug_name']}。章节：{chunk['section']}。{chunk['text']}"


def load_model(model_name: str, local_files_only: bool = False):
    from sentence_transformers import SentenceTransformer

    # HF_ENDPOINT may be set to https://hf-mirror.com by the caller when the
    # primary Hugging Face endpoint is unavailable.
    return SentenceTransformer(model_name, device="cpu", local_files_only=local_files_only)


def build_index(
    corpus_path: Path = CORPUS_PATH,
    index_dir: Path = INDEX_DIR,
    model_name: str = MODEL_NAME,
    batch_size: int = 64,
    local_files_only: bool = False,
) -> dict:
    import faiss
    import numpy as np

    corpus = read_jsonl(corpus_path)
    approvals = [row["approval_number"] for row in corpus]
    if len(approvals) != len(set(approvals)):
        raise ValueError("rag corpus is not deduplicated by approval_number")
    chunks = make_chunks(corpus)
    token_rows = [jieba_tokens(document_text(chunk)) for chunk in chunks]
    model = load_model(model_name, local_files_only=local_files_only)
    embeddings = model.encode(
        [document_text(chunk) for chunk in chunks],
        batch_size=batch_size,
        show_progress_bar=True,
        convert_to_numpy=True,
        normalize_embeddings=True,
    ).astype("float32")
    if not np.isfinite(embeddings).all():
        raise ValueError("embedding model produced non-finite values")
    index = faiss.IndexFlatIP(embeddings.shape[1])
    index.add(embeddings)
    index_dir.mkdir(parents=True, exist_ok=True)
    faiss.write_index(index, str(index_dir / "chunks.faiss"))
    write_jsonl(index_dir / "chunks.jsonl", chunks)
    write_jsonl(index_dir / "bm25_tokens.jsonl", ({"tokens": tokens} for tokens in token_rows))
    config = {
        "model": model_name,
        "embedding_dimension": int(embeddings.shape[1]),
        "normalized_embeddings": True,
        "faiss_index": "IndexFlatIP",
        "labels": len(corpus),
        "chunks": len(chunks),
        "sections": list(SECTIONS),
        "chunking": {"max_chars": 480, "overlap_chars": 80},
        "query_prefix": "为这个句子生成表示以用于检索相关文章：",
    }
    (index_dir / "config.json").write_text(json.dumps(config, ensure_ascii=False, indent=2), "utf-8")
    return config


@dataclass
class SearchResult:
    chunk: dict
    score: float
    rank: int


class HybridRetriever:
    def __init__(self, index_dir: Path = INDEX_DIR, local_files_only: bool = True):
        import faiss
        from rank_bm25 import BM25Okapi

        self.index_dir = index_dir
        self.config = json.loads((index_dir / "config.json").read_text("utf-8"))
        self.chunks = read_jsonl(index_dir / "chunks.jsonl")
        token_rows = read_jsonl(index_dir / "bm25_tokens.jsonl")
        self.bm25 = BM25Okapi([row["tokens"] for row in token_rows])
        self.index = faiss.read_index(str(index_dir / "chunks.faiss"))
        self.model = load_model(self.config["model"], local_files_only=local_files_only)

    def _eligible(self, section: str | None, approval_number: str | None, drug_name: str | None) -> set[int]:
        return {
            index for index, chunk in enumerate(self.chunks)
            if (not section or chunk["section"] == section)
            and (not approval_number or chunk["approval_number"] == approval_number)
            and (not drug_name or drug_name.lower() in chunk["drug_name"].lower())
        }

    def _bm25_order(self, query: str, eligible: set[int]) -> list[tuple[int, float]]:
        scores = self.bm25.get_scores(jieba_tokens(query))
        return sorted(((i, float(scores[i])) for i in eligible), key=lambda item: (-item[1], item[0]))

    def _vector_order(self, query: str, eligible: set[int]) -> list[tuple[int, float]]:
        vector = self.model.encode(
            [self.config["query_prefix"] + query], convert_to_numpy=True, normalize_embeddings=True
        ).astype("float32")
        scores, ids = self.index.search(vector, len(self.chunks))
        return [(int(i), float(score)) for i, score in zip(ids[0], scores[0]) if int(i) in eligible]

    def search(
        self,
        query: str,
        mode: str = "hybrid",
        top_k: int = 5,
        section: str | None = None,
        approval_number: str | None = None,
        drug_name: str | None = None,
        bm25_weight: float = 0.5,
        vector_weight: float = 0.5,
        rrf_k: int = 60,
    ) -> list[SearchResult]:
        eligible = self._eligible(section, approval_number, drug_name)
        if mode == "bm25":
            order = self._bm25_order(query, eligible)
        elif mode == "vector":
            order = self._vector_order(query, eligible)
        elif mode == "hybrid":
            bm25 = self._bm25_order(query, eligible)
            vector = self._vector_order(query, eligible)
            fused: dict[int, float] = {}
            for rank, (index, _) in enumerate(bm25, 1):
                fused[index] = fused.get(index, 0.0) + bm25_weight / (rrf_k + rank)
            for rank, (index, _) in enumerate(vector, 1):
                fused[index] = fused.get(index, 0.0) + vector_weight / (rrf_k + rank)
            order = sorted(fused.items(), key=lambda item: (-item[1], item[0]))
        else:
            raise ValueError("mode must be bm25, vector, or hybrid")
        return [SearchResult(self.chunks[index], score, rank) for rank, (index, score) in enumerate(order[:top_k], 1)]


def retrieval_metrics(retriever: HybridRetriever, queries: list[dict], mode: str, **search_kwargs: float) -> dict:
    recalls: list[float] = []
    reciprocal_ranks: list[float] = []
    records: list[dict] = []
    for query in queries:
        relevant = set(query["relevant_chunk_ids"])
        filters = query.get("filters") or {}
        # Rank the complete filtered corpus so MRR is not artificially cut at 5.
        ranked = retriever.search(query["query"], mode=mode, top_k=len(retriever.chunks), **filters, **search_kwargs)
        ids = [result.chunk["chunk_id"] for result in ranked]
        retrieved_at_5 = ids[:5]
        hits = relevant.intersection(retrieved_at_5)
        recall = len(hits) / len(relevant) if relevant else 0.0
        first_rank = next((rank for rank, chunk_id in enumerate(ids, 1) if chunk_id in relevant), None)
        reciprocal_rank = 1.0 / first_rank if first_rank else 0.0
        recalls.append(recall)
        reciprocal_ranks.append(reciprocal_rank)
        records.append({
            "query_id": query["query_id"], "recall_at_5": round(recall, 4),
            "first_relevant_rank": first_rank, "reciprocal_rank": round(reciprocal_rank, 6),
            "retrieved_at_5": retrieved_at_5,
        })
    return {
        "query_count": len(queries),
        "recall_at_5": round(sum(recalls) / len(recalls), 4) if recalls else None,
        "mrr": round(sum(reciprocal_ranks) / len(reciprocal_ranks), 4) if reciprocal_ranks else None,
        "records": records,
    }


def evaluate_retrieval(
    index_dir: Path = INDEX_DIR,
    eval_path: Path = EVAL_PATH,
    metrics_path: Path = METRICS_PATH,
    bm25_weight: float = 0.5,
    vector_weight: float = 0.5,
    rrf_k: int = 60,
) -> dict:
    queries = read_jsonl(eval_path)
    if len(queries) < 30:
        raise ValueError("retrieval evaluation requires at least 30 queries")
    retriever = HybridRetriever(index_dir=index_dir, local_files_only=True)
    kwargs = {"bm25_weight": bm25_weight, "vector_weight": vector_weight, "rrf_k": rrf_k}
    methods = {
        "bm25": retrieval_metrics(retriever, queries, "bm25"),
        "vector": retrieval_metrics(retriever, queries, "vector"),
        "hybrid": retrieval_metrics(retriever, queries, "hybrid", **kwargs),
    }
    hybrid_recall = methods["hybrid"]["recall_at_5"]
    metrics = {
        "evaluation_design": "hand-written Chinese queries with manually assigned relevant local section chunk ids",
        "query_count": len(queries),
        "corpus_labels": retriever.config["labels"],
        "index_chunks": retriever.config["chunks"],
        "model": retriever.config["model"],
        "fusion": {"method": "weighted reciprocal-rank fusion", **kwargs},
        "methods": methods,
        "acceptance": {
            "hybrid_recall_at_5_beats_bm25": hybrid_recall > methods["bm25"]["recall_at_5"],
            "hybrid_recall_at_5_beats_vector": hybrid_recall > methods["vector"]["recall_at_5"],
        },
        "limitations": [
            "Queries and relevance labels were authored by one evaluator from this fixed corpus.",
            "Metrics assess retrieval relevance, not clinical correctness or safety.",
            "MNBVC dataset-card licensing does not settle rights or freshness of underlying instruction pages.",
        ],
    }
    metrics_path.parent.mkdir(parents=True, exist_ok=True)
    metrics_path.write_text(json.dumps(metrics, ensure_ascii=False, indent=2), "utf-8")
    return metrics


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--curate", action="store_true")
    parser.add_argument("--build", action="store_true")
    parser.add_argument("--evaluate", action="store_true")
    parser.add_argument("--all", action="store_true", help="curate, build, and evaluate")
    parser.add_argument("--query")
    parser.add_argument("--mode", choices=("bm25", "vector", "hybrid"), default="hybrid")
    parser.add_argument("--section", choices=SECTIONS)
    parser.add_argument("--approval-number")
    parser.add_argument("--drug-name")
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--sample-size", type=int, default=1200)
    parser.add_argument("--priority-limit", type=int, default=500)
    parser.add_argument("--model", default=MODEL_NAME)
    parser.add_argument("--batch-size", type=int, default=64)
    parser.add_argument("--local-files-only", action="store_true")
    parser.add_argument("--bm25-weight", type=float, default=0.5)
    parser.add_argument("--vector-weight", type=float, default=0.5)
    parser.add_argument("--rrf-k", type=int, default=60)
    args = parser.parse_args()

    ran = False
    if args.curate or args.all:
        print(json.dumps(curate_corpus(sample_size=args.sample_size, priority_limit=args.priority_limit), ensure_ascii=False, indent=2))
        ran = True
    if args.build or args.all:
        print(json.dumps(build_index(model_name=args.model, batch_size=args.batch_size, local_files_only=args.local_files_only), ensure_ascii=False, indent=2))
        ran = True
    if args.evaluate or args.all:
        metrics = evaluate_retrieval(bm25_weight=args.bm25_weight, vector_weight=args.vector_weight, rrf_k=args.rrf_k)
        printable = {**metrics, "methods": {name: {k: v for k, v in values.items() if k != "records"} for name, values in metrics["methods"].items()}}
        print(json.dumps(printable, ensure_ascii=False, indent=2))
        ran = True
    if args.query:
        retriever = HybridRetriever(local_files_only=True)
        results = retriever.search(
            args.query, mode=args.mode, top_k=args.top_k, section=args.section,
            approval_number=args.approval_number, drug_name=args.drug_name,
            bm25_weight=args.bm25_weight, vector_weight=args.vector_weight, rrf_k=args.rrf_k,
        )
        print(json.dumps([{**result.chunk, "score": result.score, "rank": result.rank} for result in results], ensure_ascii=False, indent=2))
        ran = True
    if not ran:
        parser.error("choose --curate, --build, --evaluate, --all, or --query")


if __name__ == "__main__":
    main()
