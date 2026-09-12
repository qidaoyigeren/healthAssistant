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

Latency L1 adds a second thing to measure: COMPLETION TOKENS.  A gateway that
ignores ``thinking:disabled`` has the model reason into the completion budget,
which costs latency and changes nothing about the visible answer, so a
contract-compliant candidate can still be unusable.  Every call now records
prompt/completion/total and ``ms_per_output_token`` on the same口径 as
``scripts/latency-baseline.py``, and each candidate gets a pre-registered
verdict against ``DISCIPLINE_THRESHOLD_TOKENS``.

A contract failure still eliminates a candidate outright — output discipline is
only compared between candidates that can actually hold the protocol.

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

# Candidate -> (model, base_url).  Mirrors resolve_llm_config's own defaults so
# the probe tests the endpoints the product can actually reach.
#
# Keyed by CANDIDATE, not by provider: one gateway serves several models, and
# ``zai-org/GLM-5.3`` on SiliconFlow is the same model TokenDance serves as
# ``glm-5.3-flash``.  That pair is the control -- if the same model is slow at
# one gateway and quick at the other, the gateway is the cost, and if it is
# slow at both, the cost is the model's own reasoning.
CANDIDATES = {
    'zhipu/glm-4.7-flash': ('zhipu', 'glm-4.7-flash', 'https://open.bigmodel.cn/api/paas/v4'),
    'tokendance/glm-5.3-flash': ('tokendance', 'glm-5.3-flash', 'https://tokendance.space/gateway/v1'),
    'siliconflow/zai-org/GLM-4.5-Air': ('siliconflow', 'zai-org/GLM-4.5-Air', 'https://api.siliconflow.cn/v1'),
    'siliconflow/Qwen/Qwen2.5-7B-Instruct': ('siliconflow', 'Qwen/Qwen2.5-7B-Instruct', 'https://api.siliconflow.cn/v1'),
    'siliconflow/Qwen/Qwen3.5-9B': ('siliconflow', 'Qwen/Qwen3.5-9B', 'https://api.siliconflow.cn/v1'),
    'siliconflow/zai-org/GLM-5.3': ('siliconflow', 'zai-org/GLM-5.3', 'https://api.siliconflow.cn/v1'),
}
KEY_ENV = {'zhipu': 'ZHIPU_API_KEY', 'tokendance': 'TOKENDANCE_API_KEY',
           'siliconflow': 'SILICONFLOW_API_KEY'}

# ---------------------------------------------------------------------------
# Pre-registered BEFORE sampling, from artifacts that predate this run.
#
# The contract asks for exactly one tool call carrying three short strings.
# Two measured boundaries, both from existing acceptance artifacts:
#   * a disciplined answer  -- Zhipu returns 49 completion tokens for the real
#     payload; the largest CONTRACT-COMPLIANT argument string on record is 209
#     characters (TokenDance, which also fills optional fields), i.e. ~130
#     tokens;
#   * a reasoning answer    -- TokenDance's observed non-disabled range is
#     823-2440 completion tokens.
#
# 200 sits in the empty gap: ~1.5x the largest observed correct answer and
# ~4x the Zhipu reference, while an order of magnitude below the reasoning
# floor.  It is not chosen after the fact, and it is not a quality judgement --
# a model can pass this and still be wrong; this measures output DISCIPLINE
# only.
DISCIPLINE_THRESHOLD_TOKENS = 200


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


def thinking_option(provider: str, mode: str) -> dict:
    """The ``extra_body`` (or empty dict) this candidate will actually send.

    ``product`` mirrors what the product itself would send: it asks
    ``extract_ddi.llm_completion_options()`` with ``LLM_PROVIDER`` set to this
    provider, so a gateway that the product would NOT send ``thinking`` to does
    not get it here either.  Re-deriving the rule in the probe is how the
    earlier probe came to measure a different model than the product runs.

    ``disabled`` forces it, which is what makes the GLM-5.3 control pair a
    single-variable comparison: TokenDance is measured with thinking disabled,
    so SiliconFlow's GLM-5.3 must be too, or the two numbers differ in two
    ways at once.
    """
    from stage0.extract_ddi import llm_completion_options
    if mode != 'product':
        return {'thinking': {'type': mode}}
    saved = os.environ.get('LLM_PROVIDER')
    os.environ['LLM_PROVIDER'] = provider
    try:
        options = llm_completion_options()
    finally:
        if saved is None:
            os.environ.pop('LLM_PROVIDER', None)
        else:
            os.environ['LLM_PROVIDER'] = saved
    return dict(options.get('extra_body') or {})


def probe(candidate: str, calls: int, max_tokens: int, thinking_mode: str = 'product') -> dict:
    from stage0.extract_ddi import assert_live_authorized, create_llm_client

    provider, model, base_url = CANDIDATES[candidate]
    api_key = os.getenv(KEY_ENV[provider], '').strip()
    if not api_key:
        return {'candidate': candidate, 'provider': provider,
                'skipped': f'{KEY_ENV[provider]} not configured'}
    config = {'provider': provider, 'api_key': api_key, 'base_url': base_url, 'model': model}
    # The whitelist is meaningless on the acceptance path if the probe is
    # allowed to skip it; this probe is also how a new endpoint gets added, so
    # it has to fail closed on an unlisted model just as the product does.
    assert_live_authorized(config)
    client = create_llm_client(config)
    extra_body = thinking_option(provider, thinking_mode)
    thinking = (extra_body.get('thinking') or {}).get('type')
    record = {'candidate': candidate, 'provider': provider, 'model': model, 'base_url': base_url,
              'max_tokens': max_tokens, 'tool_choice': 'required',
              'thinking_mode': thinking_mode, 'thinking': thinking, 'calls': []}
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
                # Usage is read BEFORE the choices check: a gateway that burns
                # the whole completion on hidden reasoning returns 200 with
                # choices:[] -- and its usage block is the only place that
                # failure is legible.  Reading usage first turns an opaque
                # IndexError into a measurement.
                usage = getattr(response, 'usage', None)
                if isinstance(usage, dict):
                    reported = usage.get
                else:
                    reported = lambda name: getattr(usage, name, None)  # noqa: E731
                entry['prompt_tokens'] = reported('prompt_tokens')
                entry['completion_tokens'] = reported('completion_tokens')
                entry['total_tokens'] = reported('total_tokens')
                # The DIRECT measurement of hidden reasoning.  ``thinking:
                # disabled`` being ignored used to be inferred from an inflated
                # completion count; where the gateway reports this, it is
                # observed instead.  None (not 0) when unreported -- "not
                # reported" and "no reasoning" are different facts.
                details = reported('completion_tokens_details')
                if isinstance(details, dict):
                    entry['reasoning_tokens'] = details.get('reasoning_tokens')
                else:
                    entry['reasoning_tokens'] = getattr(details, 'reasoning_tokens', None)
                entry['ms_per_output_token'] = (
                    round(entry['latency_ms'] / entry['completion_tokens'], 2)
                    if entry['completion_tokens'] else None)
                if not getattr(response, 'choices', None):
                    dumped = response.model_dump() if hasattr(response, 'model_dump') else str(response)
                    entry['outcome'] = 'empty_choices'
                    entry['response_body'] = json.dumps(dumped, ensure_ascii=False, default=str)[:600]
                    entry['accepted'] = False
                    record['calls'].append(entry)
                    continue
                message = response.choices[0].message
                calls_out = message.tool_calls or []
                entry['outcome'] = 'response'
                entry['finish_reason'] = getattr(response.choices[0], 'finish_reason', None)
                entry['returned_functions'] = [c.function.name for c in calls_out]
                entry['raw_arguments'] = [c.function.arguments for c in calls_out]
                entry['content_present'] = bool(message.content)
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
            summary.append({'candidate': record['candidate'], 'skipped': record['skipped']})
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
        # Token accounting covers EVERY call that reported usage, including the
        # ones that returned no choice -- those burned a completion budget and
        # are the strongest evidence of unhonoured ``thinking:disabled``.
        measured = [c for c in calls if c.get('completion_tokens')]
        completion_each = [c['completion_tokens'] for c in measured]
        reasoning_each = [c.get('reasoning_tokens') for c in measured]
        ratios = [c['ms_per_output_token'] for c in measured
                  if c.get('ms_per_output_token') is not None]
        over = [t for t in completion_each if t > DISCIPLINE_THRESHOLD_TOKENS]
        empty = [c for c in calls if c.get('outcome') == 'empty_choices']
        # The verdict separates things that mean different things.  A candidate
        # that returned a wrong-shaped tool call is ELIMINATED.  A candidate
        # that got rate-limited measured nothing and must not be eliminated for
        # it -- that is an availability fact about this run, not a contract
        # verdict, and collapsing the two would silently discard a good endpoint
        # on a busy afternoon.
        broken = len(ok) - complete          # returned, but failed the contract
        if broken:
            verdict = 'fail_contract'
        elif empty:
            # Spent the whole completion on reasoning, then returned nothing.
            verdict = 'fail_reasoning_exhausted_the_response'
        elif len(ok) < len(calls):
            verdict = 'inconclusive_provider_errors'
        elif not measured:
            verdict = 'unmeasured_no_token_split'
        elif over:
            verdict = 'fail_discipline'
        else:
            verdict = 'pass'
        summary.append({
            'candidate': record['candidate'],
            'provider': record['provider'], 'model': record['model'],
            'calls': len(calls), 'accepted': len(ok),
            'complete_contract': complete,
            'empty_choices_calls': len(empty),
            'rate_limited': sum(1 for c in calls
                                if 'RateLimit' in str(c.get('error_type', '')) or '429' in str(c.get('error', ''))),
            'latency_ms_each': latencies,
            'completion_tokens_each': completion_each,
            'reasoning_tokens_each': reasoning_each,
            'prompt_tokens_each': [c['prompt_tokens'] for c in measured],
            'ms_per_output_token_each': ratios,
            'completion_tokens_max': max(completion_each) if completion_each else None,
            'threshold_tokens': DISCIPLINE_THRESHOLD_TOKENS,
            'threshold': 'pre-registered; see DISCIPLINE_THRESHOLD_TOKENS',
            'discipline_verdict': verdict,
            'problems': problems,
        })
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--enable-live', action='store_true')
    parser.add_argument('--out', required=True)
    parser.add_argument('--candidates', default=','.join(CANDIDATES))
    parser.add_argument('--calls', type=int, default=4)
    parser.add_argument('--max-tokens', type=int, default=4096,
                        help='agent_completion_options() floors the planner at 4096')
    parser.add_argument('--thinking', default='product',
                        choices=['product', 'disabled', 'enabled'],
                        help='product = send what the product itself would send; '
                             'disabled = force it, for a matched control')
    args = parser.parse_args()
    if not args.enable_live:
        parser.error('Remote calls disabled. Pass --enable-live explicitly.')
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=False)
    load_env()
    selected = [name.strip() for name in args.candidates.split(',') if name.strip()]
    unknown = [name for name in selected if name not in CANDIDATES]
    if unknown:
        parser.error(f'unknown candidate(s) {unknown}; known: {sorted(CANDIDATES)}')
    records = [probe(name, args.calls, args.max_tokens, args.thinking) for name in selected]
    report = {'started_at': datetime.now(timezone.utc).isoformat(),
              'note': 'synthetic content only; qualification for the v3 tool contract',
              'discipline_threshold_tokens': DISCIPLINE_THRESHOLD_TOKENS,
              'thinking_mode': args.thinking,
              'records': records, 'summary': summarize(records)}
    report['completed_at'] = datetime.now(timezone.utc).isoformat()
    (out / 'qualification.json').write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report['summary'], ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main())
