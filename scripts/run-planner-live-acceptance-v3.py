"""Protocol v3 live acceptance: immutable k=3 cohort under the DEFAULT config.

Supersedes ``run-planner-live-acceptance.py``, which pinned
``PLANNER_PROVIDER_RETRIES=1`` to reproduce the 09-10 protocol and therefore
cannot report the effect of the shipped default (``0``).  This entry point:

* refuses to run without an explicit ``--enable-live``;
* prints and re-verifies the EFFECTIVE configuration (env overrides included)
  before anything is dispatched, so a stale launcher or ambient variable
  cannot silently change the batch;
* freezes source / protocol / dataset fingerprints up front;
* writes the cohort manifest BEFORE each dispatch and refuses to reuse an
  existing output directory, including partially executed cohorts;
* keeps every outcome — success, failure, rate limit, budget exhaustion.

It never tops up samples to reach a passing result.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
PROTOCOL = ROOT / 'docs/agent-capability-upgrade/closeout-2026-09-10/planner-reliability/acceptance-protocol.md'
# Switched 2026-09-11 for AVAILABILITY, not latency: the qualification probe
# found both endpoints protocol-compliant and statistically indistinguishable
# on latency (median 10.8s vs 11.8s), while the official flash endpoint was
# rate-limiting.  Results from this endpoint are NOT comparable with the
# earlier official-Zhipu cohort; that batch stays as its own record.
MODEL = 'glm-5.3-flash'
PROVIDER = 'tokendance'
BASE_URL = 'https://tokendance.space/gateway/v1'

# The batch definition is frozen here: k, the per-run envelopes, and the
# environment the child process runs under.  PLANNER_PROVIDER_RETRIES follows
# the shipped product default, which became 1 when a definitive refusal
# stopped consuming the call budget (a retry no longer starves the run).
BATCH = {
    'planned_k': 3,
    'seconds_per_run': 180,
    'calls_per_run': 8,
    'max_cycles': 12,
    'planner_provider_retries': 1,
    'planner_arg_autocorrect': 1,
    'planner_safety_rejection_limit': 2,
}
CHILD_ENV = {
    'PYTHONIOENCODING': 'utf-8',
    # Pinned so a stale ambient selector cannot silently change which endpoint
    # handles the run; effective_config() reports it back for verification.
    'LLM_PROVIDER': PROVIDER,
    'MEMORY_ENABLE_LLM': '0',
    'AGENT_MULTI_REVIEW_MODEL_ENABLED': '0',
    'PLANNER_PROVIDER_RETRIES': str(BATCH['planner_provider_retries']),
    'PLANNER_ARG_AUTOCORRECT': str(BATCH['planner_arg_autocorrect']),
    'PLANNER_SAFETY_REJECTION_LIMIT': str(BATCH['planner_safety_rejection_limit']),
    'AGENT_EVAL_MAX_CYCLES': str(BATCH['max_cycles']),
}


def fingerprint() -> str:
    """BINDING repo scope (stage0 + frontend/src + scripts/) — the widest, so
    a record citing it is comparable with any other repo-scope record."""
    sys.path.insert(0, str(ROOT))
    from stage0.source_fingerprint import SCOPE_REPO, source_fingerprint
    return source_fingerprint(SCOPE_REPO)


def effective_config() -> dict:
    """What the planner will REALLY use — resolved, not declared.

    ``LLMPlanner._provider_retry_limit`` reads the environment at call time, so
    the only trustworthy check is to import it and read the value under the
    exact environment the child will get.
    """
    sys.path.insert(0, str(ROOT))
    from stage0.agent import LLMPlanner
    from stage0.harness.default_tools import DEFAULT_TOOL_SPECS
    # Ambient values are what the operator's shell already had; the effective
    # values are what the child process will actually see after CHILD_ENV
    # overrides.  Recording both is the point: a stale export must be visible.
    from stage0.extract_ddi import assert_live_authorized, resolve_llm_config
    ambient = {key: os.environ.get(key) for key in CHILD_ENV}
    with _patched_env(CHILD_ENV):
        retries = LLMPlanner._provider_retry_limit()
        effective = {key: os.environ.get(key) for key in CHILD_ENV}
        # Resolve the endpoint the way the child will, rather than echoing the
        # constants: a stale LLM_PROVIDER or a missing key must surface here,
        # before anything is dispatched, not as a mid-batch surprise.
        resolved = resolve_llm_config()
    assert_live_authorized(resolved)
    actual = (resolved['provider'], resolved['model'], resolved['base_url'].rstrip('/'))
    if actual != (PROVIDER, MODEL, BASE_URL):
        raise RuntimeError(
            'Environment resolves %s/%s at %s, but this batch is frozen to %s/%s at %s'
            % (*actual, PROVIDER, MODEL, BASE_URL))
    return {
        'provider': resolved['provider'],
        'base_url': resolved['base_url'],
        'model': resolved['model'],
        'planner_provider_retries': retries,
        'planner_arg_autocorrect': effective['PLANNER_ARG_AUTOCORRECT'],
        'planner_tool_choice': 'required',
        'max_cycles': int(effective['AGENT_EVAL_MAX_CYCLES']),
        'seconds_per_run': BATCH['seconds_per_run'],
        'calls_per_run': BATCH['calls_per_run'],
        'planned_k': BATCH['planned_k'],
        'registered_tools': sorted(DEFAULT_TOOL_SPECS),
        'effective_env': effective,
        'ambient_overridden': {k: v for k, v in ambient.items()
                               if v is not None and v != effective[k]},
    }


class _patched_env:
    def __init__(self, updates: dict):
        self.updates = updates

    def __enter__(self):
        self.saved = {k: os.environ.get(k) for k in self.updates}
        os.environ.update(self.updates)
        return self

    def __exit__(self, *exc):
        for key, value in self.saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        return False


def load_credentials() -> None:
    try:
        from dotenv import load_dotenv
        load_dotenv(ROOT / 'stage0/.env', override=False)
    except ModuleNotFoundError:
        pass
    os.environ.update(ZHIPU_MODEL=MODEL, ZHIPU_THINKING='disabled',
                      ZHIPU_MAX_TOKENS='4096', LLM_MAX_RETRIES='0')


def smoke(out: Path, attempts: int = 4, backoff: float = 4.0) -> int:
    """One real call that proves the provider accepts the v3 request shape.

    Sends the exact serialized contract (named tools + tool_choice) and
    records the raw function name and arguments string that come back.  It
    runs no agent and asserts nothing about quality — it answers only
    "does the provider accept this request and return a tool call".
    """
    sys.path.insert(0, str(ROOT))
    from stage0.agent import RESPOND_FUNCTION, TOOL_DESCRIPTIONS
    from stage0.extract_ddi import create_llm_client, resolve_llm_config

    from stage0.extract_ddi import assert_live_authorized
    config = resolve_llm_config()
    assert_live_authorized(config)
    if config['provider'] != PROVIDER or config['model'] != MODEL \
            or config['base_url'].rstrip('/') != BASE_URL:
        raise RuntimeError(f'This batch is frozen to {PROVIDER}/{MODEL}, '
                           f'but the environment resolves {config["provider"]}/{config["model"]}')
    tools = [
        {"type": "function", "function": {"name": "rag_search",
         "description": TOOL_DESCRIPTIONS.get('rag_search', 'rag_search'),
         "parameters": {"type": "object", "properties": {
             "query": {"type": "string"},
             "gap_id": {"type": "string"},
             "expected_observation": {"type": "string"}},
             "required": ["query", "gap_id", "expected_observation"],
             "additionalProperties": False}}},
        dict(RESPOND_FUNCTION),
    ]
    client = create_llm_client(config)
    record = {'started_at': datetime.now(timezone.utc).isoformat(),
              'attempts': [],
              'request': {'model': MODEL, 'tool_choice': 'required',
                          'function_names': [t['function']['name'] for t in tools],
                          'rag_search_required': tools[0]['function']['parameters']['required']}}
    # A 429 is a refusal before execution: it costs no billed usage and tells
    # us nothing about the request shape, so a bounded retry is legitimate
    # here.  Every attempt is recorded — a rate-limited probe is evidence too.
    for attempt in range(1, attempts + 1):
        entry = {'attempt': attempt, 'at': datetime.now(timezone.utc).isoformat()}
        try:
            response = client.chat.completions.create(
                model=MODEL, temperature=0, tool_choice='required', tools=tools,
                messages=[
                    {"role": "system", "content": "你是检索规划器。必须调用一个工具，不要输出自由文本。"},
                    {"role": "user", "content": json.dumps({
                        "goal": "核查阿司匹林与布洛芬的相互作用证据",
                        "instruction": "调用 rag_search 提出一个具体的检索问题。"}, ensure_ascii=False)},
                ])
            message = response.choices[0].message
            calls = message.tool_calls or []
            entry.update({'outcome': 'response',
                          'returned_functions': [c.function.name for c in calls],
                          'raw_arguments': [c.function.arguments for c in calls],
                          'content_present': bool(message.content)})
            record['attempts'].append(entry)
            break
        except Exception as exc:  # noqa: BLE001 - recorded as evidence
            entry.update({'outcome': 'error', 'error_type': type(exc).__name__,
                          'error': str(exc)[:600],
                          'rate_limited': '429' in str(exc) or type(exc).__name__ == 'RateLimitError'})
            record['attempts'].append(entry)
            if attempt < attempts and entry['rate_limited']:
                time.sleep(backoff * attempt)
    try:
        client.close()
    except Exception:  # noqa: BLE001
        pass
    last = record['attempts'][-1]
    record['accepted_by_provider'] = last.get('outcome') == 'response'
    record['returned_functions'] = last.get('returned_functions')
    record['raw_arguments'] = last.get('raw_arguments')
    record['outcome'] = last.get('outcome')
    record['completed_at'] = datetime.now(timezone.utc).isoformat()
    target = out / 'protocol-smoke.json'
    target.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'accepted_by_provider': record['accepted_by_provider'],
                      'outcome': record['outcome'],
                      'returned_functions': record['returned_functions'],
                      'raw_arguments': record['raw_arguments'],
                      'attempts': [{k: a.get(k) for k in ('attempt', 'outcome', 'error_type', 'rate_limited')}
                                   for a in record['attempts']]},
                     ensure_ascii=False, indent=2))
    return 0 if record['accepted_by_provider'] and record['returned_functions'] else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--enable-live', action='store_true',
                        help='Required: without it no remote call is made.')
    parser.add_argument('--out', required=True)
    parser.add_argument('--smoke', action='store_true',
                        help='Protocol probe instead of the k=3 batch.')
    parser.add_argument('--smoke-backoff', type=float, default=4.0,
                        help='Seconds multiplied by attempt index between probe retries.')
    parser.add_argument('--smoke-attempts', type=int, default=4,
                        help='Bounded 429 retries for the probe (a 429 is a refusal, not a sample).')
    args = parser.parse_args()
    if not args.enable_live:
        parser.error('Remote calls disabled. Pass --enable-live explicitly.')

    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=False)  # Never overwrite, never top up.
    load_credentials()
    config = effective_config()
    (out / 'effective-config.json').write_text(
        json.dumps(config, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'effective_config': config}, ensure_ascii=False, indent=2), flush=True)

    if config['planner_provider_retries'] != BATCH['planner_provider_retries']:
        raise RuntimeError(
            f"Effective retries {config['planner_provider_retries']} != frozen "
            f"{BATCH['planner_provider_retries']}: an environment override is in play")

    if args.smoke:
        return smoke(out, attempts=args.smoke_attempts, backoff=args.smoke_backoff)

    manifest = {
        'started_at': datetime.now(timezone.utc).isoformat(),
        'protocol_version': 'propose-next-action@3',
        'planned_k': BATCH['planned_k'], 'actual_k': 0,
        'source_fingerprint': fingerprint(),
        'protocol_sha256': hashlib.sha256(PROTOCOL.read_bytes()).hexdigest(),
        'dataset_sha256': hashlib.sha256((ROOT / 'stage0/agent_evals/dev.json').read_bytes()).hexdigest(),
        'configuration': config,
        'runs': [],
    }
    target = out / 'live-cohort.json'

    def save() -> None:
        target.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')

    save()
    env = {**os.environ, **CHILD_ENV}
    for index in range(1, BATCH['planned_k'] + 1):
        if fingerprint() != manifest['source_fingerprint']:
            manifest['stopped_reason'] = 'source_changed'
            break
        manifest['actual_k'] = index
        save()  # Admission persisted before dispatch.
        cmd = [sys.executable, '-m', 'stage0.agent_evals.run_eval', '--policy', 'gap',
               '--path', 'replay', '--live', '--out', str(out / f'live-final-{index}.json')]
        try:
            run = subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True, encoding='utf-8',
                                 errors='replace', timeout=BATCH['seconds_per_run'] + 60)
            code, log = run.returncode, run.stdout + run.stderr
        except subprocess.TimeoutExpired as exc:
            code = -1
            log = 'Acceptance process timeout; retained partial output.\n' \
                  + str(exc.stdout or '') + str(exc.stderr or '')
        (out / f'live-final-{index}.log').write_text(log, encoding='utf-8')
        manifest['runs'].append({'index': index, 'exit_code': code,
                                 'artifact_present': (out / f'live-final-{index}.json').exists()})
        print(json.dumps(manifest['runs'][-1]), flush=True)
        save()
    manifest['source_unchanged'] = fingerprint() == manifest['source_fingerprint']
    manifest['completed_at'] = datetime.now(timezone.utc).isoformat()
    manifest['collection_status'] = ('complete' if len(manifest['runs']) == BATCH['planned_k']
                                     and manifest['source_unchanged'] else 'incomplete')
    manifest['quality_status'] = 'requires_metrics_and_protocol_evaluation'
    save()
    return 0 if manifest['collection_status'] == 'complete' else 1


if __name__ == '__main__':
    sys.exit(main())
