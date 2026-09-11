"""Zero-network replay of saved live planner proposals, including provider errors."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from stage0.agent import CareEvent, DDITool, MedicationCoordinatorAgent
from stage0.agent_evals.run_eval import DATA, fingerprint
from stage0.memory import MemoryStore
from stage0.test_agent_open_tasks import FixedRAG


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--directory', required=True)
    parser.add_argument('--out', required=True)
    args = parser.parse_args()
    out = Path(args.out)
    if out.exists():
        raise RuntimeError('Never overwrite acceptance evidence')
    task = json.loads(DATA.read_text(encoding='utf-8'))[0]
    rows = []
    for path in sorted(Path(args.directory).glob('live-final-*.json')):
        raw = json.loads(path.read_text(encoding='utf-8'))
        assert raw['dataset_sha256'] == hashlib.sha256(DATA.read_bytes()).hexdigest()
        assert raw['tasks'][0]['task_id'] == task['task_id']
        proposals = []
        for response in raw['tasks'][0]['observed']['responses']:
            for entry in response['tool_trace']:
                meta = entry.get('planner', {})
                if entry.get('phase') != 'plan' or not meta.get('model') or meta.get('fallback_kind') == 'circuit_break':
                    continue
                # A final deterministic response can repeat the last cached meta.
                if not proposals or meta != proposals[-1]:
                    proposals.append(meta)
        assert len(proposals) == raw['tasks'][0]['observed']['model_calls']
        consumed = []
        def provider(payload):
            proposal = proposals[len(consumed)]
            consumed.append(proposal)
            if proposal.get('proposal') is None:
                raise RuntimeError('recorded provider failure')
            return proposal['proposal']
        with tempfile.TemporaryDirectory(prefix='synthetic-closeout-replay-') as temp, patch.dict(os.environ, {
                'MEMORY_ENABLE_LLM': '0', 'AGENT_INVESTIGATION_ENABLED': '1',
                'AGENT_MULTI_REVIEW_MODEL_ENABLED': '0', 'AGENT_LLM_VERIFIER': '0'}):
            store = MemoryStore(Path(temp) / 'synthetic.db', llm_enabled=False)
            try:
                for index, med in enumerate(task['initial_state']['medications']):
                    store.apply_medication_change(action='add', name=med['name'], ingredients=[],
                        session_id='synthetic-dev', turn_id=f'seed-{index}', source='synthetic-fixture',
                        dose=med.get('dose'), occurred_at=med.get('date'))
                agent = MedicationCoordinatorAgent(store, ddi_tool=DDITool(lambda _: []),
                    rag_tool=FixedRAG(task['materials']), max_cycles=task['budget']['max_cycles'],
                    llm_planner_enabled=True, proposal_provider=provider)
                event = task['events'][0]
                response = agent.handle(CareEvent('user_message', event['text'], event.get('payload', {})),
                    session_id='synthetic-dev', turn_id='t-0', client_event_id='synthetic:t-0')
                bundle = response.answer_bundle
                row = {'raw_artifact': str(path), 'mode': 'recorded-proposal-replay', 'remote_calls': 0,
                    'proposals_consumed': len(consumed), 'proposals_recorded': len(proposals),
                    'goal_status': bundle['goal_status'], 'execution_status': bundle['execution_status'],
                    'coverage': bundle['coverage']}
                row['passed'] = (len(consumed) == len(proposals) and bundle['goal_status'] == 'completed'
                    and bundle['execution_status'] == 'degraded')
                rows.append(row)
            finally:
                store.close()
    result = {'mode': 'recorded-proposal-replay', 'remote_calls': 0,
        'source_fingerprint': fingerprint(), 'runs': rows,
        'status': 'pass' if len(rows) == 3 and all(r['passed'] for r in rows) else 'fail'}
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=True))
    return 0 if result['status'] == 'pass' else 1


if __name__ == '__main__':
    sys.exit(main())
