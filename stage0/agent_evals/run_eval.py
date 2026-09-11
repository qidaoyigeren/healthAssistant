"""Run with python -m stage0.agent_evals.run_eval --policy baseline|gap --path replay|tools.

No remote calls in either default path. `tools` executes the real local RAG
adapter over a supplied synthetic corpus; replay fixes retrieval results.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[2]
DATA = Path(__file__).with_name('dev.json')
FROZEN = ROOT / 'docs/agent-capability-upgrade/A0/baseline-source.zip'


def fingerprint():
    files = sorted(p for p in (ROOT / 'stage0').rglob('*.py') if '__pycache__' not in str(p))
    return hashlib.sha256(b''.join(str(p.relative_to(ROOT)).encode() + p.read_bytes() for p in files)).hexdigest()


def freeze():
    if FROZEN.exists():
        raise RuntimeError('Frozen baseline already exists; never overwrite history')
    FROZEN.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(FROZEN, 'w', zipfile.ZIP_DEFLATED) as archive:
        for path in sorted((ROOT / 'stage0').rglob('*.py')):
            archive.write(path, path.relative_to(ROOT))
    manifest = {'revision': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
                'source_fingerprint': fingerprint(), 'archive_sha256': hashlib.sha256(FROZEN.read_bytes()).hexdigest(),
                'dataset_sha256': hashlib.sha256(DATA.read_bytes()).hexdigest(),
                'policy': 'pre-A1 working source, including pre-existing uncommitted fixes',
                'scope': 'synthetic-development', 'llm': False,
                'config': {'max_cycles': 12, 'wall_clock_seconds': 120, 'call_budget': 32, 'token_budget': 150000}}
    (FROZEN.parent / 'baseline-config.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')


def score(task, observed):
    """Independent outcome rubric; no unique action ordering."""
    inv = observed.get('investigation') or {}
    questions = inv.get('questions', [])
    asked = {q['field'] for q in questions}
    needed = set(task['expected_gaps'])
    failures = []
    if not needed.issubset(asked):
        failures.append('necessary_question_missing')
    if asked - needed:
        failures.append('unnecessary_question')
    if 'bounded_report' in task['required_artifacts'] and not inv:
        failures.append('missing_bounded_report')
    if inv and inv.get('termination_reason') not in task['allowed_terminal_reasons']:
        failures.append('unexpected_terminal_reason')
    supported = [c for c in inv.get('claims', []) if c['status'] == 'supported']
    if task.get('forbid_supported') and supported:
        failures.append('unsupported_claim')
    if observed.get('duplicate_effects', 0):
        failures.append('duplicate_effect')
    if observed.get('error'):
        failures.append('execution_error')
    if task.get('force_fail'):
        failures.append('intentional_negative_control')
    return {'passed': not failures, 'failures': failures,
            'necessary_question_recall': {'numerator': len(needed & asked), 'denominator': len(needed)},
            'invalid_questions': len(asked - needed),
            'supported_claim_ratio': {'numerator': sum(bool(c.get('supporting_evidence')) for c in supported), 'denominator': len(supported)},
            'false_completion': bool(task.get('forbid_supported') and observed.get('goal_status') == 'completed'),
            'recovery_success': observed.get('recovery_success'), 'duplicate_effects': observed.get('duplicate_effects', 0)}


def run_task(task, policy, path, live=False):
    from stage0.agent import CareEvent, DDITool, MedicationCoordinatorAgent, RAGTool
    from stage0.memory import MemoryStore
    from stage0 import rag
    from stage0.harness.progress import cancel_event_for
    started = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix='synthetic-agent-eval-') as directory:
        root = Path(directory)
        chunks = task['materials']
        (root / 'chunks.jsonl').write_text('\n'.join(json.dumps(c, ensure_ascii=False) for c in chunks), encoding='utf-8')
        class Retrieval:
            def __call__(self, query, **kwargs):
                if task.get('fault') == 'tool_timeout':
                    raise TimeoutError('synthetic tool timeout')
                return {'query': query, 'mode': 'scripted', 'corpus_version': 'synthetic-v1', 'results': chunks}
        class LocalOnlyRAG(RAGTool):
            def __call__(self, query, **kwargs):
                if task.get('fault') == 'tool_timeout':
                    raise TimeoutError('synthetic tool timeout')
                return super().__call__(query, **kwargs)
            def _get_retriever(self):
                raise RuntimeError('explicit local exact retrieval; embeddings disabled')
        def provider(payload):
            raise RuntimeError('synthetic provider 429 rate_limit')
        env = {'AGENT_INVESTIGATION_ENABLED': '1' if policy == 'gap' else '0',
               'AGENT_TURN_BUDGET_SECONDS': '180' if live else '120', 'AGENT_TURN_TOKEN_BUDGET': '150000',
               'AGENT_TURN_CALL_BUDGET': '8' if live else '32', 'LLM_MAX_RETRIES': '0',
               'MEMORY_ENABLE_LLM': '0', 'AGENT_LLM_VERIFIER': '0'}
        with patch.dict(os.environ, env), patch.object(rag, 'INDEX_DIR', root):
            store = MemoryStore(root / 'memory.db', llm_enabled=False)
            client = None
            config = None
            if live:
                from stage0.extract_ddi import (assert_live_authorized, create_llm_client,
                                                resolve_llm_config)
                config = resolve_llm_config()
                assert_live_authorized(config)
                client = create_llm_client(config)
            progress_samples = []
            def make():
                # A5 ablation hook: an env override may tighten (never loosen)
                # the per-task cycle budget to measure budget sensitivity.
                budget_cycles = task['budget']['max_cycles']
                override = os.getenv('AGENT_EVAL_MAX_CYCLES')
                if override:
                    budget_cycles = min(budget_cycles, int(override))
                agent = MedicationCoordinatorAgent(store, ddi_tool=DDITool(lambda meds: []),
                    rag_tool=Retrieval() if path == 'replay' else LocalOnlyRAG(), max_cycles=budget_cycles,
                    llm_planner_enabled=live or task.get('fault') == 'model_rate_limit',
                    llm_planner_client=client, llm_planner_model=config['model'] if config else None,
                    proposal_provider=None if live else provider, response_provider=provider if task.get('fault') == 'model_rate_limit' else None)
                if hasattr(agent, 'progress_store'):
                    emit = agent.progress_store.emit
                    def measured_emit(run_id, kind, **kwargs):
                        event_id = emit(run_id, kind, **kwargs)
                        if event_id:
                            progress_samples.append({'event_id': event_id, 'kind': kind,
                                'elapsed_ms': round((time.perf_counter() - started) * 1000, 3)})
                        return event_id
                    agent.progress_store.emit = measured_emit
                return agent
            agent = make()
            for i, medication in enumerate(task['initial_state']['medications']):
                store.apply_medication_change(action='add', name=medication['name'], ingredients=[],
                    session_id='synthetic-dev', turn_id=f'seed-{i}', source='synthetic-fixture',
                    occurred_at=medication.get('date'), dose=medication.get('dose'))
            responses = []
            try:
                for i, event in enumerate(task['events']):
                    if event.get('restart'):
                        store.close()
                        store = MemoryStore(root / 'memory.db', llm_enabled=False)
                        agent = make()
                        continue
                    identity = event.get('identity', f't-{i}')
                    if task.get('fault') == 'cancel':
                        cancel_event_for(identity).set()
                    response = agent.handle(CareEvent('user_message', event['text'], event.get('payload', {})),
                        session_id='synthetic-dev', turn_id=identity, client_event_id=f'synthetic:{identity}')
                    responses.append(asdict(response))
                last = responses[-1]
                inv = (last.get('answer_bundle') or {}).get('investigation')
                trace = [e for r in responses for e in r['tool_trace']]
                outcome = {'investigation': inv, 'responses': responses,
                    'execution_status': (last.get('answer_bundle') or {}).get('execution_status', 'finished'),
                    'goal_status': (last.get('answer_bundle') or {}).get('goal_status', 'unknown'),
                    'answer_status': (last.get('answer_bundle') or {}).get('answer_status', 'template'),
                    'tool_calls': sum(e.get('phase') == 'act' for e in trace),
                    'model_calls': sum(bool((e.get('planner') or {}).get('model')) for e in trace),
                    'actual_tokens': 0, 'unknown_usage': 0, 'mode': 'scripted' if task.get('fault') == 'model_rate_limit' else 'deterministic',
                    'degradation_reasons': list(dict.fromkeys(reason for r in responses
                        if (reason := (r.get('answer_bundle') or {}).get('coverage', {}).get('degraded_reason')
                            or r['audit_trail'].get('response_fallback_reason')))),
                    'recovery_success': None, 'duplicate_effects': 0}
                # Domain receipt count is an actual DB observation, not a planner claim.
                receipts = [dict(r) for r in store.connection.execute('SELECT scope_id,operation_id FROM operation_receipts')]
                outcome['receipts'] = receipts
                outcome['duplicate_effects'] = len(receipts) - len({(r['scope_id'], r['operation_id']) for r in receipts})
            except Exception as exc:
                outcome = {'error': f'{type(exc).__name__}: {exc}', 'responses': responses}
            finally:
                attempts = [dict(r) for r in store.connection.execute('SELECT * FROM llm_attempts')]
                outcome['usage_ledger'] = attempts
                outcome['model_calls'] = len(attempts)
                outcome['actual_tokens'] = sum(r.get('usage_tokens') or 0 for r in attempts) if live else 0
                outcome['unknown_usage'] = sum(r.get('usage_tokens') is None for r in attempts) if live else 0
                outcome['scripted_provider_attempts'] = len(attempts) if not live else 0
                outcome['mode'] = 'llm' if live else outcome.get('mode', 'deterministic')
                outcome['config'] = {k: config[k] for k in ('provider', 'model', 'base_url')} if config else {'provider': 'none'}
                outcome['budgets'] = [dict(r) for r in store.connection.execute('SELECT run_id,budget_json FROM workflow_runs')]
                outcome['progress_samples'] = progress_samples
                outcome['first_backend_progress_ms'] = progress_samples[0]['elapsed_ms'] if progress_samples else None
                store.close()
                if client:
                    client.close()
                for event in task['events']:
                    if event.get('identity'):
                        cancel_event_for(event['identity']).clear()
    outcome['latency_ms'] = round((time.perf_counter() - started) * 1000, 2)
    return {'task_id': task['task_id'], 'family_id': task['family_id'], 'score': score(task, outcome), 'observed': outcome}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--policy', choices=['baseline', 'gap'], default='gap')
    parser.add_argument('--path', choices=['replay', 'tools'], default='replay')
    parser.add_argument('--out', required=True)
    parser.add_argument('--freeze', action='store_true')
    parser.add_argument('--negative-control', action='store_true')
    parser.add_argument('--live', action='store_true', help='Explicit opt-in: one task, 180s/8 calls, allowlisted endpoint only')
    parser.add_argument('--frozen-child', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.live:
        if Path(args.out).exists():
            raise RuntimeError('Live result exists: preserve it; do not repeat sampling')
        # Credentials load through extract_ddi's own parser (python-dotenv is
        # not installed, so load_dotenv here would be a silent no-op).
        from stage0.extract_ddi import _load_dotenv, resolve_llm_config
        _load_dotenv()
        # Pin the completion options for whichever provider actually resolved.
        # Hardcoding ZHIPU_* would leave the real provider reading stale
        # defaults; thinking must be disabled either way, or the model spends
        # the completion budget on internal reasoning and the call truncates.
        active = resolve_llm_config(require_key=False)['provider']
        prefix = {'zhipu': 'ZHIPU', 'tokendance': 'TOKENDANCE'}.get(active)
        if prefix:
            os.environ[f'{prefix}_THINKING'] = 'disabled'
            os.environ.setdefault(f'{prefix}_MAX_TOKENS', '4096')
        os.environ['LLM_MAX_RETRIES'] = '0'
    if args.freeze:
        freeze()
    if args.policy == 'baseline' and not args.frozen_child:
        with tempfile.TemporaryDirectory(prefix='agent-baseline-source-') as directory:
            with zipfile.ZipFile(FROZEN) as archive:
                archive.extractall(directory)
            target = Path(directory) / 'stage0/agent_evals'
            target.mkdir(exist_ok=True)
            # Evaluation protocol stays common to both versions; application source is frozen.
            import shutil
            for file in Path(__file__).parent.glob('*'):
                if file.is_file():
                    shutil.copy2(file, target / file.name)
            command = [sys.executable, '-m', 'stage0.agent_evals.run_eval', '--frozen-child', '--policy', 'baseline',
                       '--path', args.path, '--out', str(Path(args.out).resolve())]
            if args.negative_control:
                command.append('--negative-control')
            if args.live:
                command.append('--live')
            return subprocess.call(command, cwd=directory)
    tasks = json.loads(DATA.read_text(encoding='utf-8'))
    if args.live:
        tasks = tasks[:1]
    if args.negative_control:
        tasks = [dict(tasks[0], force_fail=True)]
    results = [run_task(task, args.policy, args.path, live=args.live) for task in tasks]
    from stage0.agent import PLANNER_PROTOCOL_VERSION
    report = {'protocol': 'agent-eval@1', 'planner_protocol_version': PLANNER_PROTOCOL_VERSION,
        'dataset': 'author-synthetic-dev@1', 'policy': args.policy, 'path': args.path,
        'source_fingerprint': fingerprint(), 'dataset_sha256': hashlib.sha256(DATA.read_bytes()).hexdigest(),
        'engineering_status': 'pass' if results and all(r['score']['passed'] for r in results) else 'fail',
        'real_model_quality': 'unavailable', 'provider_availability': 'unavailable', 'independent_held_out': 'unavailable',
        'summary': {'total': len(results), 'passed': sum(r['score']['passed'] for r in results)}, 'tasks': results}
    if args.live:
        attempts = [a for r in results for a in r['observed']['usage_ledger']]
        report['provider_availability'] = {'attempts': len(attempts), 'actual_responses': sum(a.get('usage_tokens') is not None for a in attempts),
            'unknown_usage': sum(a.get('usage_tokens') is None for a in attempts)}
        report['real_model_quality'] = 'single_exposed_dev_task_only'
        report['llm_answer_success'] = all(r['observed'].get('answer_status') == 'llm_validated' for r in results)
        report['repeat_protocol'] = {'planned_k': 1, 'actual_k': len(results), 'per_run_seconds': 180, 'per_run_calls': 8}
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({k: v for k, v in report.items() if k != 'tasks'}, ensure_ascii=False))
    return 0 if report['engineering_status'] == 'pass' else 1


if __name__ == '__main__':
    sys.exit(main())
