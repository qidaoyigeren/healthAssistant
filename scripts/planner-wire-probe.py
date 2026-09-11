"""Mock-transport probe: capture the EXACT serialized provider request and
verify response parsing, without any remote call.

This exists because inspecting ``prompt_payload`` or a locally built schema is
not evidence about what the provider actually receives.  The probe installs a
fake OpenAI-compatible client at the same seam the live path uses
(``create_llm_client`` -> ``client.chat.completions.create``), records the
kwargs verbatim, and replays scripted provider responses through the real
``AgentPlanner._parse_response`` / ``validate`` / ``materialize`` chain.

Offline and deterministic: no network, no credentials.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

TASK_ID = 'agent-dev-complete'


# ---------------------------------------------------------------- fake client

class FakeCompletions:
    def __init__(self, owner: 'FakeClient'):
        self.owner = owner

    def create(self, **kwargs):
        self.owner.requests.append(kwargs)
        return self.owner.next_response(kwargs)


class FakeClient:
    """Minimal stand-in for the OpenAI SDK client used by the live path."""

    def __init__(self, script):
        self.script = list(script)
        self.requests: list[dict[str, Any]] = []
        self.chat = SimpleNamespace(completions=FakeCompletions(self))
        self.timeout = 60.0

    def next_response(self, kwargs):
        item = self.script.pop(0) if self.script else {
            'content': json.dumps({'decision': 'respond', 'rationale': 'script exhausted'})}
        return make_response(item)

    def with_options(self, **kwargs):
        return self

    def close(self):
        pass


def make_response(item: dict[str, Any]):
    """Build an object shaped like an OpenAI ChatCompletion."""
    message = SimpleNamespace(content=item.get('content'), tool_calls=None)
    if 'tool' in item:
        message.tool_calls = [SimpleNamespace(
            id='call_1', type='function',
            function=SimpleNamespace(name=item['tool'],
                                     arguments=item['arguments'] if isinstance(item['arguments'], str)
                                     else json.dumps(item['arguments'], ensure_ascii=False)))]
    return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason='tool_calls')],
                           usage=SimpleNamespace(prompt_tokens=100, completion_tokens=50, total_tokens=150),
                           model='glm-4.7-flash')


# ---------------------------------------------------------------- probes

def extract_tools_schema(request: dict[str, Any]) -> dict[str, Any]:
    tools = request.get('tools') or []
    if not tools:
        return {}
    return (tools[0] or {}).get('function', {}).get('parameters', {})


def probe_request_schema(requests: list[dict[str, Any]]) -> dict[str, Any]:
    """Report what the provider actually receives on the wire."""
    report: dict[str, Any] = {'request_count': len(requests),
                              'samples': [], 'findings': []}
    for index, request in enumerate(requests):
        functions = {((t or {}).get('function') or {}).get('name'):
                     ((t or {}).get('function') or {}).get('parameters', {})
                     for t in (request.get('tools') or [])}
        sample = {
            'index': index,
            'model': request.get('model'),
            'temperature': request.get('temperature'),
            'tool_choice': request.get('tool_choice'),
            'function_names': sorted(functions),
            'messages_roles': [m.get('role') for m in (request.get('messages') or [])],
            'per_tool_required': {name: sorted(params.get('required') or [])
                                  for name, params in sorted(functions.items())},
            'per_tool_properties': {name: sorted((params.get('properties') or {}))
                                    for name, params in sorted(functions.items())},
            'rag_search_query': ((functions.get('rag_search') or {}).get('properties') or {}).get('query'),
        }
        report['samples'].append(sample)

    # Findings are about the contract, not one sample: a tool whose required
    # arguments are only in prose is exactly the defect protocol v3 fixes.
    for sample in report['samples']:
        for name, required in sample['per_tool_required'].items():
            if name in {'rag_search', 'memory_read', 'read_evidence'}:
                expect = {'rag_search': 'query', 'memory_read': 'query',
                          'read_evidence': 'evidence_id'}[name]
                if expect not in required:
                    report['findings'].append(
                        f'{name} does not declare {expect} as a required argument '
                        f'(required={required})')
        if sample['tool_choice'] not in {'required', 'auto'}:
            report['findings'].append(
                f"unexpected tool_choice shape: {sample['tool_choice']!r}")
        # The probe drives a non-terminal investigation, where respond is
        # deliberately NOT advertised (code owns termination).  Only flag a
        # missing respond on a sample that advertises no usable tool at all.
        if not sample['function_names']:
            report['findings'].append('no tool advertised at all')
    return report


def probe_response_parsing(agent_like, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Feed scripted provider responses through the real parser."""
    from stage0.agent import AgentPlanner
    planner = agent_like
    results = []
    for item in items:
        response = make_response(item)
        entry = {'input_tool': item.get('tool'), 'input_arguments': item.get('arguments')}
        try:
            parsed = planner._parse_response(response)
            entry['parsed'] = parsed
            entry['ok'] = True
        except Exception as exc:  # noqa: BLE001 - diagnostic
            entry['ok'] = False
            entry['error'] = f'{type(exc).__name__}: {exc}'
        results.append(entry)
    return results


def probe_state_sweep(store, planner=None) -> list[dict[str, Any]]:
    """Dump the *serialized* function schema at each investigation stage.

    The decisive stage is the one where ``rag_search`` is permitted — that is
    where the recorded live proposals omitted ``arguments.query``.

    ``planner`` must be the agent's real bound planner (its ``tool_schemas``
    come from the executor catalog, exactly as on the live path); a bare
    ``LLMPlanner`` falls back to the narrower default registry and would
    under-report the schema actually sent.
    """
    from stage0.agent import AgentState, CareEvent, LLMPlanner
    from stage0.investigation import InvestigationState, allowed_tools

    inv = InvestigationState('核查当前用药', 'local-demo')
    inv.sync_authority(store)
    state = AgentState('s', 't', CareEvent('user_message', '核查当前用药'), investigation=inv)
    if planner is None:
        planner = LLMPlanner(proposal_provider=lambda _: {})
    stages = []

    def capture(label: str):
        definitions = planner.tool_definitions(state)
        by_name = {d['function']['name']: d['function']['parameters'] for d in definitions}
        stages.append({
            'stage': label,
            'permitted_tools': list(allowed_tools(inv)),
            'function_names': sorted(by_name),
            'per_tool_required': {name: sorted(params.get('required') or [])
                                  for name, params in sorted(by_name.items())},
            'per_tool_properties': {name: sorted(params.get('properties') or {})
                                    for name, params in sorted(by_name.items())},
            'rag_search_query': (by_name.get('rag_search') or {}).get('properties', {}).get('query'),
            'memory_read_query': (by_name.get('memory_read') or {}).get('properties', {}).get('query'),
        })

    capture('authority_open')
    inv.authority_read = True
    inv.checks['authority'] = 'checked'
    for gap in inv.gaps:
        if gap['gap_id'] == 'authority':
            gap['status'] = 'resolved'
    inv.sync_authority(store)
    capture('claim_gaps_rag_allowed')
    inv.evidence_refs.append('ev-x')
    capture('evidence_unread')
    inv.termination_reason = 'checks_completed'
    capture('terminal')
    return stages


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', required=True, help='JSON report path')
    parser.add_argument('--cycles', type=int, default=2)
    args = parser.parse_args()

    from stage0.agent_evals import run_eval
    from stage0 import extract_ddi
    import stage0.agent as agent_module

    task = next(t for t in json.loads((ROOT / 'stage0/agent_evals/dev.json').read_text(encoding='utf-8'))
                if t['task_id'] == TASK_ID)
    tasks = [task]

    report: dict[str, Any] = {'task_id': TASK_ID, 'cycles': args.cycles,
                              'mode': 'mock_transport', 'remote_calls': 0}

    with tempfile.TemporaryDirectory(prefix='planner-wire-probe-') as directory:
        root = Path(directory)
        chunks = task['materials']
        (root / 'chunks.jsonl').write_text(
            '\n'.join(json.dumps(c, ensure_ascii=False) for c in chunks), encoding='utf-8')

        from stage0 import rag
        from stage0.memory import MemoryStore
        from stage0.agent import CareEvent, DDITool, MedicationCoordinatorAgent, RAGTool

        class Retrieval:
            def __call__(self, query, **kwargs):
                return {'query': query, 'mode': 'scripted', 'corpus_version': 'synthetic-v1',
                        'results': chunks}

        # Scripted provider replies: first a rag_search WITH arguments (the
        # behaviour under test), then a respond.
        script = [
            {'tool': 'memory_read',
             'arguments': {'query': 'snapshot'}},
            {'tool': 'rag_search',
             'arguments': {'query': '阿司匹林 布洛芬 相互作用'}},
            {'content': json.dumps({'decision': 'respond', 'rationale': 'script'}, ensure_ascii=False)},
        ]
        fake = FakeClient(script)

        env = {'AGENT_INVESTIGATION_ENABLED': '1', 'AGENT_TURN_BUDGET_SECONDS': '180',
               'AGENT_TURN_TOKEN_BUDGET': '150000', 'AGENT_TURN_CALL_BUDGET': '8',
               'LLM_MAX_RETRIES': '0', 'MEMORY_ENABLE_LLM': '0', 'AGENT_LLM_VERIFIER': '0'}
        from unittest.mock import patch
        recorded_planner = {}

        with patch.dict(os.environ, env), patch.object(rag, 'INDEX_DIR', root), \
                patch.object(extract_ddi, 'create_llm_client', lambda config: fake):
            store = MemoryStore(root / 'memory.db', llm_enabled=False)
            agent = MedicationCoordinatorAgent(
                store, ddi_tool=DDITool(lambda meds: []), rag_tool=Retrieval(),
                max_cycles=min(args.cycles, task['budget']['max_cycles']),
                llm_planner_enabled=True, llm_planner_client=fake,
                llm_planner_model='glm-4.7-flash')
            planner = getattr(agent, 'llm_planner', None) or agent.planner
            recorded_planner['planner'] = getattr(agent, 'planner', None).llm_planner if getattr(agent,'planner',None) is not None else planner
            for i, medication in enumerate(task['initial_state']['medications']):
                store.apply_medication_change(action='add', name=medication['name'], ingredients=[],
                                              session_id='synthetic-dev', turn_id=f'seed-{i}',
                                              source='synthetic-fixture',
                                              occurred_at=medication.get('date'),
                                              dose=medication.get('dose'))
            responses = []
            event = task['events'][0]
            identity = event.get('identity', 't-0')
            try:
                response = agent.handle(
                    CareEvent('user_message', event['text'], event.get('payload', {})),
                    session_id='synthetic-dev', turn_id=identity,
                    client_event_id=f'probe:{identity}')
                responses.append(response)
            finally:
                store.close()

        report['request_schema'] = probe_request_schema(fake.requests)
        sweep_store = MemoryStore(root / 'sweep.db', llm_enabled=False)
        try:
            for i, medication in enumerate(task['initial_state']['medications']):
                sweep_store.apply_medication_change(action='add', name=medication['name'], ingredients=[],
                                                    session_id='synthetic-dev', turn_id=f'sweep-{i}',
                                                    source='synthetic-fixture',
                                                    occurred_at=medication.get('date'),
                                                    dose=medication.get('dose'))
            report['state_sweep'] = probe_state_sweep(sweep_store, recorded_planner['planner'])
        finally:
            sweep_store.close()

        # Response-parsing probe against the real planner instance.
        parse_items = [
            {'tool': 'rag_search', 'arguments': {'query': 'q1'}},
            {'tool': 'rag_search', 'arguments': '{"query": "q2"}'},
            {'tool': 'rag_search', 'arguments': {}},
            {'tool': 'memory_read', 'arguments': {'query': 'snapshot'}},
            {'content': json.dumps({'decision': 'respond', 'rationale': 'done'}, ensure_ascii=False)},
        ]
        report['response_parsing'] = probe_response_parsing(recorded_planner['planner'], parse_items)

        trace = []
        for response in responses:
            for entry in getattr(response, 'tool_trace', []) or []:
                trace.append({k: v for k, v in entry.items() if k != 'observation'})
        report['tool_trace'] = trace

    out = Path(args.out).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding='utf-8')
    print(json.dumps({'out': str(out), 'requests': report['request_schema']['request_count'],
                      'findings': report['request_schema']['findings']}, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main())
