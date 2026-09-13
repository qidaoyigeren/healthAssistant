"""Shared B1/B2 retrieval feedback. Metadata is scoped before enumeration.

No query rewriting, retry, or filter widening occurs here. Public label chunks
have no scope_id; explicitly scoped chunks are visible only in that scope.
"""
from __future__ import annotations

import hashlib
import json

from .tools import ToolSpec
from .errors import PASSTHROUGH_EXCUSES

VERSION = 'retrieval-feedback@1'
DIRECTORY_LIMIT = 32
FEEDBACK_KEYS = ('status', 'search_executed', 'applied_filters', 'invalid_fields',
                 'corpus_version', 'directory', 'scope', 'candidate_count',
                 'result_kind', 'truncation', 'error', 'query', 'retrieval_degraded', 'mode')
MESSAGES = {
    'invalid_filter': '检索条件与当前材料目录不符，尚未执行搜索。',
    'empty_filter_scope': '所选材料范围没有可供核查的内容。',
    'no_match': '已检索所选材料，暂未找到匹配内容。',
    'retrieval_error': '检索服务执行失败，尚不能判断是否存在依据。',
    'found': '已找到候选依据，需结合原文继续核查。',
}


def visible(chunk, scope_id):
    return chunk.get('scope_id') in (None, scope_id)


def snapshot(tool, scope_id):
    getter = getattr(tool, 'corpus_chunks', None)
    chunks = getter() if getter else getattr(tool, 'chunks', None)
    if chunks is None:
        raise RuntimeError('retrieval metadata unavailable')
    return [dict(c) for c in chunks if visible(c, scope_id)]


def metadata(chunks, arguments=None, *, offset=0, limit=DIRECTORY_LIMIT):
    args = arguments or {}
    sections = sorted({c['section'] for c in chunks if c.get('section')})
    drugs = sorted({c['drug_name'] for c in chunks if c.get('drug_name')})
    eligible = [c for c in chunks if matches(c, args)]
    version = 'corpus:' + hashlib.sha256(json.dumps(chunks, sort_keys=True,
        ensure_ascii=False).encode()).hexdigest()
    return {
        'corpus_version': version,
        'directory': {'sections': sections[offset:offset + limit],
                      'section_count': len(sections), 'offset': offset,
                      'omitted_sections': max(0, len(sections) - min(len(sections), offset + limit)),
                      'next_offset': offset + limit if offset + limit < len(sections) else None,
                      'drug_names': drugs[:DIRECTORY_LIMIT],
                      'omitted_drug_names': max(0, len(drugs) - DIRECTORY_LIMIT)},
        'scope': {'accessible_chunks': len(chunks), 'filtered_chunks': len(eligible),
                  'section_optional': True,
                  'without_section': 'all accessible sections, subject to other supplied filters'},
    }


def matches(chunk, args):
    return (('section' not in args or chunk.get('section') == args['section']) and
            ('drug_name' not in args or args['drug_name'].casefold() in
             (chunk.get('drug_name') or '').casefold()))


def search(tool, arguments, *, scope_id):
    args = dict(arguments)
    value = {'contract_version': VERSION, 'query': args.get('query'),
             'applied_filters': {k: args[k] for k in ('section', 'drug_name') if k in args},
             'search_executed': False, 'results': [], 'candidate_count': 0,
             'result_kind': 'candidate_chunks', 'truncation': {'candidates_truncated': False},
             'corpus_version': None, 'directory': None, 'scope': None}
    try:
        chunks = snapshot(tool, scope_id)
        value.update(metadata(chunks, args))
        invalid = []
        if 'section' in args and args['section'] not in {c.get('section') for c in chunks}:
            invalid.append('section')
        if 'drug_name' in args and (not args['drug_name'].strip() or not any(
                args['drug_name'].casefold() in (c.get('drug_name') or '').casefold() for c in chunks)):
            invalid.append('drug_name')
        if invalid:
            return dict(value, status='invalid_filter', invalid_fields=invalid)
        if not args.get('query', '').strip():
            return dict(value, status='invalid_query', error={'kind': 'empty_query'})
        top_k = args.get('top_k', 5)
        if isinstance(top_k, bool) or not isinstance(top_k, int) or not 1 <= top_k <= 20:
            return dict(value, status='invalid_query', error={'kind': 'top_k_out_of_bounds', 'minimum': 1, 'maximum': 20})
        if not value['scope']['filtered_chunks']:
            return dict(value, status='empty_filter_scope')
        value['search_executed'] = True
        scoped = getattr(tool, 'search_in_scope', None)
        raw = scoped(args, scope_id) if scoped else tool(**args)
        # Validate returned membership before capture; a backend cannot grant
        # access through a result. Compare content as well as locator.
        allowed = {(c.get('chunk_id'), c.get('text'), c.get('source_url'), c.get('section'))
                   for c in chunks if matches(c, args)}
        candidates = [c for c in raw.get('results', []) if visible(c, scope_id) and
                      (c.get('chunk_id'), c.get('text'), c.get('source_url'), c.get('section')) in allowed]
        if raw.get('mode') == 'degraded_exact_over_rag_corpus':
            candidates = [c for c in candidates if c.get('score', 0) > 0]
        if raw.get('retrieval_failure'):
            # Preserve the existing exact fallback over the SAME query/scope.
            # Its failure signal stays visible even when it yields candidates;
            # this deterministic backend fallback is never model correction.
            value.update(retrieval_degraded=True, error={'kind': 'primary_backend_execution_failed',
                         'fallback': 'exact_same_query_and_scope', 'attribution': 'deterministic_internal'})
            if not candidates:
                return dict(value, status='retrieval_error')
        value.update({k: raw[k] for k in ('mode', 'total_matches') if k in raw})
        value.update(results=candidates[:top_k], candidate_count=len(candidates[:top_k]),
                     status='found' if candidates else 'no_match',
                     truncation={'candidates_truncated': (raw['total_matches'] > len(candidates[:top_k])
                                     if 'total_matches' in raw else None),
                                 'backend_top_k': top_k, 'total_matches': raw.get('total_matches'),
                                 'total_matches_known': 'total_matches' in raw})
        return value
    except Exception as exc:
        if type(exc).__name__ in PASSTHROUGH_EXCUSES:
            raise
        # No raw exception text: it may contain paths, foreign metadata or keys.
        return dict(value, status='retrieval_error', error={'kind': type(exc).__name__})


CATALOG_SPEC = ToolSpec(
    name='rag_catalog',
    description=('读取当前有权限访问的说明书章节目录和语料版本。section 是可选精确过滤；'
                 '省略时搜索所有授权章节，其他过滤仍生效。目录有界，next_offset 非空表示尚有遗漏。'
                 '检索状态区分无效过滤、范围无材料、查询无匹配和执行故障；目录不返回原文。'),
    argument_schema={'type': 'object', 'properties': {
        'offset': {'type': 'integer'}, 'limit': {'type': 'integer'}}, 'required': []},
    result_shape='dict(corpus_version, directory, scope)', kind='read',
    required_permission='rag:search', idempotency='pure', cacheable=False)


def catalog(tool, scope_id, args):
    offset, limit = args.get('offset', 0), args.get('limit', DIRECTORY_LIMIT)
    if isinstance(offset, bool) or isinstance(limit, bool) or offset < 0 or not 1 <= limit <= DIRECTORY_LIMIT:
        from .errors import ToolExecutionError, ToolErrorKind
        raise ToolExecutionError(ToolErrorKind.INVALID_ARGUMENTS, 'offset >= 0; limit 1..32')
    try:
        return dict(metadata(snapshot(tool, scope_id), offset=offset, limit=limit),
                    status='catalog', result_kind='metadata_only')
    except Exception as exc:
        if type(exc).__name__ in PASSTHROUGH_EXCUSES:
            raise
        return {'status': 'retrieval_error', 'error': {'kind': type(exc).__name__},
                'corpus_version': None, 'directory': None, 'scope': None}
