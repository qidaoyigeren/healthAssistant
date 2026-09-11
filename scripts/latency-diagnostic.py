"""Where does planner latency actually go?

The 2026-09-11 cohort on TokenDance glm-5.3-flash met the autonomy threshold
but took 72s per turn (21.9s median per planner call, vs ~1.5s on official
Zhipu for the same protocol and the same payloads).  Latency tracked total
tokens almost linearly, but total tokens alone cannot say whether the cost is
prefill (long prompt) or decode (model writing, including hidden reasoning).

This separates the components by measuring, per call: prompt_tokens,
completion_tokens, wall latency, and the derived per-token rates.

Axes:
  prompt size    tiny probe prompt  vs  the REAL captured planner payload
  provider       official zhipu     vs  tokendance gateway
  thinking       disabled (what the product sends)  vs  omitted entirely
                 -> if omitting it changes completion_tokens, the gateway is
                    not honouring the parameter and the model is reasoning.

Payloads are captured OFFLINE by running the agent against a fake client, so
the realistic request body costs no quota.  Only the probe calls are remote.
Synthetic content only.  Explicit --enable-live required.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PROVIDERS = {
    'zhipu': ('glm-4.7-flash', 'https://open.bigmodel.cn/api/paas/v4'),
    'tokendance': ('glm-5.3-flash', 'https://tokendance.space/gateway/v1'),
}
KEY_ENV = {'zhipu': 'ZHIPU_API_KEY', 'tokendance': 'TOKENDANCE_API_KEY'}

TINY = [
    {"role": "system", "content": "你是检索规划器。必须调用一个工具，不要输出自由文本。"},
    {"role": "user", "content": "调用 rag_search 提出一个具体的中文检索问题。"},
]


# ----------------------------------------------------------- payload capture

class CapturingClient:
    def __init__(self):
        self.requests: list[dict] = []

    def _reply(self):
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(
                tool_calls=[SimpleNamespace(function=SimpleNamespace(
                    name='memory_read',
                    arguments=json.dumps({'query': 'snapshot', 'gap_id': 'authority',
                                          'expected_observation': '获得权威记录'})))],
                content=None))],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1, total_tokens=2))

    def create(self, **kwargs):
        self.requests.append(kwargs)
        return self._reply()

    # The planner calls client.chat.completions.create(**kwargs)
    @property
    def chat(self):
        return SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def with_options(self, **kwargs):
        return self

    def close(self):
        pass


def capture_payloads(cycles: int = 3) -> list[dict]:
    """Run the real agent offline and keep the exact serialized request bodies."""
    from stage0 import rag
    from stage0.agent import CareEvent, DDITool, MedicationCoordinatorAgent
    from stage0.agent_evals.run_eval import DATA
    from stage0.memory import MemoryStore

    tasks = json.loads(DATA.read_text(encoding='utf-8'))
    task = next(t for t in tasks if t['task_id'] == 'agent-dev-complete')
    client = CapturingClient()
    env = {'AGENT_INVESTIGATION_ENABLED': '1', 'AGENT_TURN_BUDGET_SECONDS': '600',
           'AGENT_TURN_TOKEN_BUDGET': '500000', 'AGENT_TURN_CALL_BUDGET': '32',
           'MEMORY_ENABLE_LLM': '0', 'AGENT_LLM_VERIFIER': '0',
           'AGENT_EVAL_MAX_CYCLES': str(cycles)}
    import os as _os
    saved = {k: _os.environ.get(k) for k in env}
    _os.environ.update(env)
    try:
        with tempfile.TemporaryDirectory(prefix='latency-capture-') as directory:
            root = Path(directory)
            chunks = task['materials']
            (root / 'chunks.jsonl').write_text(
                '\n'.join(json.dumps(c, ensure_ascii=False) for c in chunks), encoding='utf-8')

            class Retrieval:
                def __call__(self, query, **kwargs):
                    return {'query': query, 'mode': 'scripted', 'corpus_version': 'synthetic-v1',
                            'results': chunks}

            with __import__('unittest').mock.patch.object(rag, 'INDEX_DIR', root):
                store = MemoryStore(root / 'memory.db', llm_enabled=False)
                agent = MedicationCoordinatorAgent(
                    store, ddi_tool=DDITool(lambda meds: []), rag_tool=Retrieval(),
                    max_cycles=cycles, llm_planner_enabled=True,
                    llm_planner_client=client, llm_planner_model='capture')
                for i, medication in enumerate(task['initial_state']['medications']):
                    store.apply_medication_change(action='add', name=medication['name'],
                                                  ingredients=[], session_id='synthetic-dev',
                                                  turn_id=f'seed-{i}', source='synthetic-fixture',
                                                  occurred_at=medication.get('date'),
                                                  dose=medication.get('dose'))
                event = task['events'][0]
                try:
                    agent.handle(CareEvent('user_message', event['text'], event.get('payload', {})),
                                 session_id='synthetic-dev', turn_id=event.get('identity', 't-0'),
                                 client_event_id='latency-capture')
                except Exception:  # noqa: BLE001 - capture is best-effort
                    pass
                finally:
                    store.close()
    finally:
        for k, v in saved.items():
            if v is None:
                _os.environ.pop(k, None)
            else:
                _os.environ[k] = v
    return [r for r in client.requests]


# ---------------------------------------------------------------- remote probe

def probe(provider: str, messages: list[dict], tools: list, *,
          thinking: str | None, calls: int, label: str) -> dict:
    from stage0.extract_ddi import create_llm_client
    model, base_url = PROVIDERS[provider]
    api_key = os.getenv(KEY_ENV[provider], '').strip()
    if not api_key:
        return {'provider': provider, 'label': label, 'skipped': f'{KEY_ENV[provider]} unset'}
    client = create_llm_client({'provider': provider, 'api_key': api_key,
                                'base_url': base_url, 'model': model})
    record = {'provider': provider, 'model': model, 'label': label, 'thinking': thinking,
              'payload_chars': sum(len(m.get('content') or '') for m in messages),
              'calls': []}
    try:
        for index in range(1, calls + 1):
            entry = {'call': index}
            kwargs = dict(model=model, temperature=0, tool_choice='required',
                          tools=tools, messages=messages, max_tokens=4096)
            if thinking:
                kwargs['extra_body'] = {'thinking': {'type': thinking}}
            started = time.perf_counter()
            try:
                response = client.chat.completions.create(**kwargs)
                entry['latency_ms'] = round((time.perf_counter() - started) * 1000)
                usage = getattr(response, 'usage', None)
                entry['prompt_tokens'] = getattr(usage, 'prompt_tokens', None)
                entry['completion_tokens'] = getattr(usage, 'completion_tokens', None)
                entry['total_tokens'] = getattr(usage, 'total_tokens', None)
                entry['outcome'] = 'response'
            except Exception as exc:  # noqa: BLE001
                entry['latency_ms'] = round((time.perf_counter() - started) * 1000)
                entry['outcome'] = 'error'
                entry['error_type'] = type(exc).__name__
                entry['error'] = str(exc)[:200]
            record['calls'].append(entry)
    finally:
        try:
            client.close()
        except Exception:  # noqa: BLE001
            pass
    ok = [c for c in record['calls'] if c['outcome'] == 'response']
    if ok:
        record['median_latency_ms'] = sorted(c['latency_ms'] for c in ok)[len(ok) // 2]
        record['median_prompt_tokens'] = sorted(c['prompt_tokens'] for c in ok)[len(ok) // 2]
        record['median_completion_tokens'] = sorted(c['completion_tokens'] for c in ok)[len(ok) // 2]
        record['median_total_tokens'] = sorted(c['total_tokens'] for c in ok)[len(ok) // 2]
    return record


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--enable-live', action='store_true')
    parser.add_argument('--out', required=True)
    parser.add_argument('--calls', type=int, default=2)
    args = parser.parse_args()
    if not args.enable_live:
        parser.error('Remote calls disabled. Pass --enable-live explicitly.')
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=False)

    from stage0.agent import RESPOND_FUNCTION, TOOL_DESCRIPTIONS
    from stage0.extract_ddi import _load_dotenv
    _load_dotenv()

    captured = capture_payloads()
    if not captured:
        raise RuntimeError('no payload captured; cannot measure')
    tools = captured[0]['tools']
    biggest = max(captured, key=lambda r: sum(len(m.get('content') or '') for m in r['messages']))
    largest_messages = biggest['messages']

    plan = []
    for provider in ('zhipu', 'tokendance'):
        plan.append((provider, 'tiny_prompt', TINY, 'disabled'))
        plan.append((provider, 'real_payload', largest_messages, 'disabled'))
    # Does the gateway honour thinking:disabled? Compare against omitting it.
    plan.append(('tokendance', 'real_payload_encoding_default', largest_messages, None))

    records = [probe(p, msgs, tools, thinking=th, calls=args.calls, label=label)
               for p, label, msgs, th in plan]
    report = {'started_at': datetime.now(timezone.utc).isoformat(),
              'note': 'synthetic content only; latency decomposition',
              'captured_cycles': len(captured),
              'records': records,
              'completed_at': datetime.now(timezone.utc).isoformat()}
    (out / 'latency.json').write_text(json.dumps(report, ensure_ascii=False, indent=2),
                                      encoding='utf-8')
    for r in records:
        print(json.dumps({k: r.get(k) for k in
                          ('provider', 'label', 'thinking', 'payload_chars',
                           'median_latency_ms', 'median_prompt_tokens',
                           'median_completion_tokens', 'skipped')}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    sys.exit(main())
