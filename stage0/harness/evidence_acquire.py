"""One model-requested, bounded read-only acquisition; no query planning.

The composite uses the existing executor for each operation, including its
permissions, EvidenceStore scope/hash checks and audit. It never approves a
claim, retries a query, selects a different direction, or writes patient facts.
"""
from __future__ import annotations

import os
from .tools import ToolSpec
from .errors import ToolExecutionError, ToolErrorKind


def enabled():
    return os.getenv('AGENT_EVIDENCE_INTERFACE', 'B1') == 'B2'


SPEC = ToolSpec(
    name='acquire_evidence',
    description=('按你指定的查询做一次只读取证：有界检索本地说明书，读取最多三条授权原文，'
                 '校验 scope 和哈希并返回原文定位、来源、版本、日期、截断与失败信息。'
                 '不改写查询，不批准结论；调查方向、冲突处理、补问与停止由你决定。'),
    argument_schema={'type': 'object', 'properties': {
        'query': {'type': 'string', 'minLength': 1},
        'top_k': {'type': 'integer', 'minimum': 1, 'maximum': 3},
        'section': {'type': 'string', 'description': '可选精确过滤，合法值来自 rag_catalog；省略时搜索所有授权章节。'}, 'drug_name': {'type': 'string'}},
        'required': ['query']},
    result_shape='dict(pages[], failures[], operations[], unfinished_checks[], no_results)',
    kind='read', required_permission='rag:search', idempotency='pure', cacheable=False)


def register(executor, evidence_store):
    def handler(request):
        top_k = request.arguments.get('top_k', 3)
        if isinstance(top_k, bool) or not isinstance(top_k, int) or not 1 <= top_k <= 3:
            raise ToolExecutionError(ToolErrorKind.INVALID_ARGUMENTS, 'top_k must be between 1 and 3')
        if not request.arguments['query'].strip():
            raise ToolExecutionError(ToolErrorKind.INVALID_ARGUMENTS, 'query must not be empty')
        operations, pages, failures = [], [], []

        def execute(tool, arguments):
            result = executor.execute(request.ctx, tool, arguments, state=request.state)
            operations.append({'tool': tool, 'arguments': arguments, 'ok': result.ok,
                               'error': result.error, 'attribution': 'deterministic_internal',
                               'evidence_refs': result.evidence_refs})
            if not result.ok:
                failures.append({'tool': tool, 'error': result.error})
            return result

        arguments = dict(request.arguments)
        arguments.setdefault('top_k', 3)
        search = execute('rag_search', arguments)
        refs = list(dict.fromkeys(search.evidence_refs)) if search.ok else []
        for ref in refs[:3]:
            read = execute('read_evidence', {'evidence_id': ref, 'offset': 0, 'limit': 2000})
            if not read.ok:
                continue
            # Metadata is only exposed AFTER the authorized read succeeds.
            meta = evidence_store.get_meta(ref) or {}
            page = dict(read.value)
            page.update(content_ref=meta.get('content_ref'), corpus_version=meta.get('corpus_version'),
                        retrieved_at=meta.get('retrieved_at'), publication_date=None,
                        publication_date_status='not_provided_by_corpus',
                        integrity='scope_and_content_hash_verified',
                        source_validation='local_corpus_provenance_only',
                        original_scope='stored_source_chunk; not the entire external document')
            pages.append(page)
            operations.append({'tool': 'verify_source_metadata', 'evidence_id': ref,
                               'ok': bool(meta.get('source_uri') and meta.get('corpus_version')),
                               'attribution': 'deterministic_internal'})
        from .retrieval import FEEDBACK_KEYS
        feedback = {k: search.value[k] for k in FEEDBACK_KEYS if search.ok and k in search.value}
        if not search.ok:
            feedback.update(status='retrieval_error', search_executed=False, error=search.error)
        if refs and not pages:
            feedback.update(status='retrieval_error', error={'kind': 'original_read_failed'})
        return {**feedback, 'query': arguments['query'], 'pages': pages, 'evidence_refs': refs,
                'result_kind': 'original_pages' if pages else 'no_original_pages',
                'failures': failures, 'operations': operations,
                'no_results': bool(search.ok and feedback.get('status') in {'no_match', 'empty_filter_scope'}),
                'search_ok': bool(search.ok and feedback.get('status') != 'retrieval_error'),
                'unread_refs': [ref for ref in refs if ref not in {p['evidence_id'] for p in pages}],
                'unfinished_checks': ['external_source_currency', 'publication_date',
                                      'patient_applicability', 'claim_support_and_conflict_review']
                    + (['remaining_original_pages'] if any(p['truncated'] for p in pages) else []),
                'conclusion_approved': False}

    executor.register(SPEC, handler)
