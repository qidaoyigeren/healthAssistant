"""Qualify a model/provider against the v3 tool contract before switching to it.

Switching the planner model is not a config edit: protocol v3 depends on the
provider honouring ``tool_choice="required"`` and filling *named* functions with
their required nested arguments.  Those were validated against official Zhipu
glm-4.7-flash and do NOT transfer to another endpoint or gateway by assumption.

This probe sends the real serialized contract (the same named-function shape and
RESPOND_FUNCTION the agent sends) to one or more configured providers and records,
per call: latency, whether the request was accepted, which function came back,
and the raw argument string — so truncation and omissions are visible rather
than inferred.

Synthetic content only; no patient data.  Explicit --enable-live required.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Provider -> (model, base_url).  Mirrors resolve_llm_config's own defaults so
# the probe tests the endpoints the product can actually reach.
PROVIDERS = {
    'zhipu': ('glm-4.7-flash', 'https://open.bigmodel.cn/api/paas/v4'),
    'tokendance': ('glm-5.3-flash', 'https://tokendance.space/gateway/v1'),
}
KEY_ENV = {'zhipu': 'ZHIPU_API_KEY', 'tokendance': 'TOKENDANCE_API_KEY'}


def load_env() -> None:
    """Use the loader the product itself uses.

    ``python-dotenv`` is NOT installed in this venv, so every ``load_dotenv``
    call in the repo is a silent no-op; credentials actually arrive through
    ``extract_ddi._load_dotenv``, a hand-rolled parser.  Reading them any other
    way reports "not configured" while the product works fine.
    """
    from stage0.extract_ddi import _load_dotenv
    _load_dotenv()


def contract_tools():
    """The real v3 surface: named functions, required nested args, respond."""
    from stage0.agent import RESPOND_FUNCTION, TOOL_DESCRIPTIONS
    return [
        {"type": "function", "function": {
            "name": "rag_search",
            "description": TOOL_DESCRIPTIONS.get('rag_search', 'rag_search'),
            "parameters": {"type": "object", "properties": {
                "query": {"type": "string"},
                "top_k": {"type": "integer"},
                "section": {"type": "string"},
                "gap_id": {"type": "string"},
                "expected_observation": {"type": "string"}},
                "required": ["query", "gap_id", "expected_observation"],
                "additionalProperties": False}}},
        {"type": "function", "function": {
            "name": "read_evidence",
            "description": TOOL_DESCRIPTIONS.get('read_evidence', 'read_evidence'),
            "parameters": {"type": "object", "properties": {
                "evidence_id": {"type": "string"},
                "gap_id": {"type": "string"},
                "expected_observation": {"type": "string"}},
                "required": ["evidence_id", "gap_id", "expected_observation"],
                "additionalProperties": False}}},
        dict(RESPOND_FUNCTION),
    ]


MESSAGES = [
    {"role": "system", "content": "你是用药证据核查的规划器。必须调用一个工具，不要输出自由文本。"},
    {"role": "user", "content": json.dumps({
        "goal": "核查合成药甲与合成药乙的相互作用证据",
        "open_gaps": [{"gap_id": "claim:abc", "kind": "evidence_missing"}],
        "instruction": "针对 claim:abc 提出一个具体的检索问题并调用 rag_search。"},
        ensure_ascii=False)},
]


def probe(provider: str, calls: int, max_tokens: int) -> dict:
    from stage0.extract_ddi import create_llm_client

    model, base_url = PROVIDERS[provider]
    api_key = os.getenv(KEY_ENV[provider], '').strip()
    if not api_key:
        return {'provider': provider, 'skipped': f'{KEY_ENV[provider]} not configured'}
    config = {'provider': provider, 'api_key': api_key, 'base_url': base_url, 'model': model}
    client = create_llm_client(config)
    # The product disables thinking on EVERY call (llm_completion_options ->
    # extra_body.thinking).  A probe that omits it measures a different model:
    # the internal reasoning burns the completion budget and inflates latency.
    thinking = os.getenv(f'{provider.upper()}_THINKING', 'disabled').strip().lower()
    extra_body = {'thinking': {'type': thinking}} if thinking in {'enabled', 'disabled'} else None
    record = {'provider': provider, 'model': model, 'base_url': base_url,
              'max_tokens': max_tokens, 'tool_choice': 'required',
              'thinking': thinking, 'calls': []}
    try:
        for index in range(1, calls + 1):
            entry = {'call': index, 'at': datetime.now(timezone.utc).isoformat()}
            started = time.perf_counter()
            try:
                kwargs = dict(model=model, temperature=0, tool_choice='required',
                              tools=contract_tools(), messages=MESSAGES,
                              max_tokens=max_tokens)
                if extra_body:
                    kwargs['extra_body'] = extra_body
                response = client.chat.completions.create(**kwargs)
                entry['latency_ms'] = round((time.perf_counter() - started) * 1000)
                message = response.choices[0].message
                calls_out = message.tool_calls or []
                entry['outcome'] = 'response'
                entry['finish_reason'] = getattr(response.choices[0], 'finish_reason', None)
                entry['returned_functions'] = [c.function.name for c in calls_out]
                entry['raw_arguments'] = [c.function.arguments for c in calls_out]
                entry['content_present'] = bool(message.content)
                usage = getattr(response, 'usage', None)
                entry['total_tokens'] = getattr(usage, 'total_tokens', None)
                entry['accepted'] = True
            except Exception as exc:  # noqa: BLE001 - recorded as evidence
                entry['latency_ms'] = round((time.perf_counter() - started) * 1000)
                entry['outcome'] = 'error'
                entry['error_type'] = type(exc).__name__
                entry['error'] = str(exc)[:400]
                entry['accepted'] = False
            record['calls'].append(entry)
    finally:
        try:
            client.close()
        except Exception:  # noqa: BLE001
            pass
    return record


def summarize(records: list[dict]) -> list[dict]:
    summary = []
    for record in records:
        if record.get('skipped'):
            summary.append({'provider': record['provider'], 'skipped': record['skipped']})
            continue
        calls = record['calls']
        ok = [c for c in calls if c.get('accepted')]
        latencies = [c['latency_ms'] for c in ok]
        # A complete call names a permitted function and carries every required
        # argument.  Missing keys or truncated JSON both count as failures.
        complete = 0
        problems = []
        for c in ok:
            names = c.get('returned_functions') or []
            if not names:
                problems.append('no tool call returned')
                continue
            if names[0] not in {'rag_search', 'read_evidence', 'respond'}:
                problems.append(f'unknown function {names[0]}')
                continue
            try:
                args = json.loads(c['raw_arguments'][0] or '{}')
            except Exception:  # noqa: BLE001
                problems.append('arguments not parseable (truncated?)')
                continue
            if names[0] == 'rag_search' and 'query' not in args:
                problems.append('rag_search omitted query')
                continue
            complete += 1
        summary.append({
            'provider': record['provider'], 'model': record['model'],
            'calls': len(calls), 'accepted': len(ok),
            'complete_contract': complete,
            'rate_limited': sum(1 for c in calls
                                if 'RateLimit' in str(c.get('error_type', '')) or '429' in str(c.get('error', ''))),
            'latency_ms_each': latencies,
            'latency_ms_median': round(sorted(latencies)[len(latencies) // 2]) if latencies else None,
            'problems': problems,
        })
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--enable-live', action='store_true')
    parser.add_argument('--out', required=True)
    parser.add_argument('--providers', default='zhipu,tokendance')
    parser.add_argument('--calls', type=int, default=4)
    parser.add_argument('--max-tokens', type=int, default=4096,
                        help='agent_completion_options() floors the planner at 4096')
    args = parser.parse_args()
    if not args.enable_live:
        parser.error('Remote calls disabled. Pass --enable-live explicitly.')
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=False)
    load_env()
    records = [probe(name.strip(), args.calls, args.max_tokens)
               for name in args.providers.split(',') if name.strip()]
    report = {'started_at': datetime.now(timezone.utc).isoformat(),
              'note': 'synthetic content only; qualification for the v3 tool contract',
              'records': records, 'summary': summarize(records)}
    report['completed_at'] = datetime.now(timezone.utc).isoformat()
    (out / 'qualification.json').write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report['summary'], ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main())
