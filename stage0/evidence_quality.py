"""Conservative evidence decisions and versioned, expiring generic-drug cache."""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Callable, Protocol


class FallbackPolicy:
    def __init__(self, versions, clock: Callable[[], float] = time.time, ttls=None):
        self.versions, self.clock = versions, clock
        self.ttls = ttls or {k: float(os.getenv(f'DDI_CACHE_TTL_{k.upper()}', v)) for k, v in {
            'matched': '86400', 'not_found': '900', 'provider_error': '30', 'parse_error': '60'}.items()}

    def key(self, entities):
        payload = json.dumps({'entities': sorted(entities), 'versions': self.versions}, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(payload.encode()).hexdigest()

    def read(self, entry):
        if not entry:
            return None, 'miss'
        if entry.get('versions') != self.versions:
            return None, 'version_changed_or_legacy'
        if entry.get('status') not in self.ttls:
            return None, 'unknown_status'
        expires = entry.get('expires_at')
        if not isinstance(expires, (int, float)) or self.clock() >= expires:
            return None, 'expired'
        return entry, 'hit'

    def entry(self, status, **data):
        return {**data, 'status': status, 'versions': self.versions, 'cached_epoch': self.clock(),
                'expires_at': self.clock() + max(0, self.ttls[status])}


_FILE_HASHES = {}


def file_fingerprint(path: Path):
    if not path.is_file():
        return 'unavailable'
    stat = path.stat()
    signature = (str(path.resolve()), stat.st_size, stat.st_mtime_ns)
    if signature not in _FILE_HASHES:
        h = hashlib.sha256()
        with path.open('rb') as f:
            for block in iter(lambda: f.read(1024 * 1024), b''):
                h.update(block)
        _FILE_HASHES[signature] = h.hexdigest()
    return _FILE_HASHES[signature]


class Reranker(Protocol):
    def rank(self, candidates: list, entities: list[str]) -> list: ...


class ExactCoverageReranker:
    """Optional local ordering; never admits a candidate rejected by grounding."""
    def rank(self, candidates, entities):
        return sorted(candidates, key=lambda item: (
            -sum(name in item[0].get('text', '') for name in entities),
            -int(item[0].get('section') == '药物相互作用'), item[0].get('chunk_id', '')))


def assess_claim(*, quote, text, entities, evidence_id, subject=None, required_subject=None, conditions_known=True):
    """Lexical interaction-claim screen, not a model or clinical entailment judge.

    A real citation is necessary but not sufficient. Unknown population or
    applicability always abstains. Contradiction markers concern a positive
    interaction claim only, never a medical all-clear.
    """
    result = {'status': 'insufficient', 'evidence_refs': [], 'method': 'conservative-lexical-v1',
              'conditions': {'subject': subject, 'required_subject': required_subject}, 'unresolved': []}
    if not isinstance(quote, str) or not quote or quote not in text or not evidence_id:
        result['unresolved'] = ['unreadable_or_inexact_citation']
    elif not all(entity in quote for entity in entities):
        result['unresolved'] = ['wrong_or_missing_entity']
    elif not conditions_known or (required_subject and subject != required_subject):
        result['unresolved'] = ['unknown_or_wrong_applicability']
    elif required_subject is None and re.search(r'儿童|孕妇|妊娠|哺乳|肾功能|肝功能|如果|仅在|特定人群', quote):
        result['unresolved'] = ['unstated_population_or_condition']
    elif re.search(r'未发现.{0,8}相互作用|无.{0,4}相互作用|不增加|未增加', quote):
        result.update(status='contradicted', evidence_refs=[evidence_id])
    elif re.search(r'没有证据|尚无证据|证据不足|尚不明确|尚需研究|未证实|不能证明|是否', quote):
        result['unresolved'] = ['uncertainty_or_negated_support']
    elif re.search(r'禁用|禁忌|避免.{0,8}合用|增加.{0,10}风险|合用.{0,12}(升高|增加|降低)|需.{0,5}监测', quote):
        result.update(status='supported', evidence_refs=[evidence_id])
    else:
        result['unresolved'] = ['citation_does_not_establish_claim']
    return result


def bounded_research(search, queries, *, max_queries=3, max_chunks=12, reranker=None):
    seen_queries, seen_chunks, candidates, trace = set(), set(), [], []
    reason = 'queries_exhausted'
    for query in queries:
        if len(seen_queries) >= max_queries or len(candidates) >= max_chunks:
            reason = 'budget_exhausted'
            break
        if query in seen_queries:
            reason = 'no_progress'
            break
        seen_queries.add(query)
        start = time.perf_counter()
        try:
            rows = search(query)
        except Exception as exc:
            trace.append({'query': query, 'status': 'retrieval_error', 'error_type': type(exc).__name__})
            reason = 'retrieval_error'
            break
        added = 0
        for row in rows:
            chunk = row.chunk if hasattr(row, 'chunk') else row
            identifier = chunk.get('chunk_id') or hashlib.sha256(chunk.get('text', '').encode()).hexdigest()
            if identifier not in seen_chunks and len(candidates) < max_chunks:
                candidates.append(chunk)
                seen_chunks.add(identifier)
                added += 1
        trace.append({'query': query, 'new_chunks': added, 'elapsed_ms': (time.perf_counter() - start) * 1000})
        if not added:
            reason = 'no_progress'
            break
    return {'chunks': candidates, 'trace': trace, 'termination_reason': reason,
            'coverage': 'bounded_search_only', 'queries_used': len(seen_queries)}


def replace_source(product, key, old_id, new_id, lineage, reason):
    """Explicit curator-supplied lineage; hash change alone never supersedes."""
    from .product import ProductError, SCOPE
    from .memory import utc_now
    def execute():
        records = []
        for identifier in (old_id, new_id):
            row = product.db.execute('SELECT * FROM evidence_records WHERE evidence_id=? AND (scope_id=? OR access_class=?)',
                                     (identifier, SCOPE, 'general_label')).fetchone()
            if not row or hashlib.sha256(row['content'].encode()).hexdigest() != row['content_hash']:
                raise ProductError('来源版本不可用或完整性校验失败', 409)
            records.append(row)
        if old_id == new_id or not lineage or not reason or not all(r['corpus_version'] for r in records):
            raise ProductError('必须指定不同的已知来源版本、来源谱系与替代依据')
        if records[0]['source_uri'] != records[1]['source_uri'] or not records[0]['source_uri']:
            raise ProductError('两份证据未属于同一明确来源，不能自动替代')
        relation = {'id': 'source-replacement:' + hashlib.sha256(f'{old_id}:{new_id}'.encode()).hexdigest(),
                    'old_evidence_id': old_id, 'new_evidence_id': new_id, 'lineage': lineage, 'reason': reason, 'created_at': utc_now()}
        ids = []
        for row in product.db.execute("SELECT id,source_refs_json FROM conclusions WHERE status='current'"):
            refs = json.loads(row['source_refs_json'])
            if any(isinstance(ref, dict) and ref.get('evidence_id') == old_id for ref in refs):
                ids.append(row['id'])
        relation['affected_conclusions'] = product.memory._invalidate_conclusions_tx(ids, 'explicit_source_replacement:' + relation['id'])
        product.save('source_replacement', relation)
        return relation
    return product.command(key, {'type': 'source_replacement', 'old_id': old_id, 'new_id': new_id, 'lineage': lineage, 'reason': reason}, execute)


def register_quality_routes(app, product, access, invoke, evidence_store, principal, require_role):
    from fastapi import Request
    from .product import ProductError, SCOPE
    globals()['Request'] = Request

    @app.post('/v1/evidence/assess-claim')
    def assess(request: Request, body: dict):
        access(request)
        identifier, quote = body.get('evidence_id'), body.get('quote')
        entities = body.get('entities')
        if not isinstance(entities, list) or not 1 <= len(entities) <= 4 or any(not isinstance(e, str) or not e or len(e) > 80 for e in entities):
            return invoke(lambda: (_ for _ in ()).throw(ProductError('请提供 1–4 个明确药品实体')))
        try:
            evidence_store.read(identifier, scope_id=SCOPE, offset=0, limit=1)
            with product.memory._lock:
                row = product.db.execute('SELECT content FROM evidence_records WHERE evidence_id=?', (identifier,)).fetchone()
                content = row['content']
        except Exception:
            content = ''
        return assess_claim(quote=quote, text=content, entities=entities, evidence_id=identifier,
            subject=body.get('subject'), required_subject=body.get('required_subject'), conditions_known=body.get('conditions_known') is True)

    @app.post('/v1/evidence/source-replacements')
    def replacement(request: Request, body: dict):
        access(request, True)
        require_role(principal(request), 'ops')
        return invoke(lambda: replace_source(product, body.get('key'), body.get('old_id'), body.get('new_id'), body.get('lineage'), body.get('reason')))

    @app.get('/v1/evidence/source-replacements')
    def replacements(request: Request):
        access(request)
        return {'items': product.objects('source_replacement')}
