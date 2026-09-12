"""就诊准备 A/B：固定流程 vs 模型调查路径，12 个成对合成任务。

评分规则一律从任务的 ``expected`` 字段读出——**没有任何按 task_id、族名或
文件名分支的逻辑**，所以换一批任务不需要改代码，也不会因为改代码而"变好"。

三个臂，用来把"看得到材料"与"谁来规划"分成两个可归因的因子：

    fixed  AGENT_INVESTIGATION_ENABLED=0 + 确定性规划器 + 不挂材料
           —— HEAD 的当前固定工作流
    det    AGENT_INVESTIGATION_ENABLED=1 + 确定性规划器 + 挂材料
           —— 同样看不到模型规划，只多出"材料可见"这一项能力
    model  AGENT_INVESTIGATION_ENABLED=1 + 真实模型规划 + 挂材料

离线替身（--arm fixed|det）零远程调用，只证明状态机与执行约束。
真实模型只在 --arm model 且显式 --live 时使用，且受 --call-cap 硬上限约束。

不计入成功指标的量：工具调用次数、与固定脚本的路径相似度。
合法的不同顺序不得判失败。
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
import json
import os
from pathlib import Path
import tempfile
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
DATA = Path(__file__).with_name('visitprep_dev.json')

ARMS = {
    'fixed': {'investigation': '0', 'llm': False, 'materials': False},
    'det': {'investigation': '1', 'llm': False, 'materials': True},
    # Control for `det`: same contract and planner, materials withheld.  The
    # only difference between the two is material visibility.
    'det-nomat': {'investigation': '1', 'llm': False, 'materials': False},
    'scripted': {'investigation': '1', 'llm': False, 'materials': True, 'script': True},
    # The 2x2 that isolates the two factors: whether a planner exists at all,
    # and whether materials are visible to it.  A single "model vs fixed"
    # comparison cannot say which half of the difference is the new
    # CAPABILITY and which is the model CHOOSING to use it.
    'scripted-nomat': {'investigation': '1', 'llm': False, 'materials': False, 'script': True},
    'model': {'investigation': '1', 'llm': True, 'materials': True},
    'model-nomat': {'investigation': '1', 'llm': True, 'materials': False},
}


def _pairs(names):
    if len(names) < 2:
        return [(names[0],)] if names else []
    import itertools
    return list(itertools.islice(itertools.combinations(names, 2), 12))


def _tool(name, gap_id, arguments):
    return {'decision': 'tool', 'tool': name, 'gap_id': gap_id,
            'expected_observation': f'{name} 的结果', 'arguments': arguments}


def model_double(payload):
    """Offline double for the MODEL path.

    It follows the same contract a real model is given — plan the sub-questions,
    look at the materials, read a discrepancy back before citing it, close the
    evidence gaps — so the state machine and the execution constraints can be
    exercised end to end without a provider.  It proves the contract is
    SATISFIABLE; it proves nothing about model planning, and is never reported
    as such.
    """
    investigation = payload.get('investigation') or {}
    allowed = set(investigation.get('allowed_tools') or [])
    open_gaps = investigation.get('open_gaps') or []
    facts = investigation.get('facts') or {}
    observations = payload.get('observations') or []

    if not investigation.get('authority_read', True) and 'memory_read' in allowed:
        return _tool('memory_read', 'authority', {'query': 'snapshot'})

    plan_gap = next((g for g in open_gaps if g['gap_id'] == 'subquestions'), None)
    if plan_gap is not None:
        names = [m['display_name'] for m in facts.get('medications') or []]
        return _tool('plan_questions', 'subquestions', {'questions': [
            {'statement': '、'.join(pair) + '的标签证据', 'entities': list(pair)}
            for pair in _pairs(names)]})

    evidence_gap = next((g for g in open_gaps if g['kind'] == 'evidence_missing'), None)
    listed = next((o for o in reversed(observations)
                   if o.get('tool') == 'list_materials' and o.get('ok')), None)
    if listed is None and 'list_materials' in allowed:
        return _tool('list_materials', evidence_gap['gap_id'], {})

    read_refs = {f"{o['arguments'].get('case_id')}/{o['arguments'].get('item_id')}"
                 for o in observations if o.get('tool') == 'read_material_item' and o.get('ok')}
    if listed is not None:
        for material in (listed.get('result') or {}).get('materials') or []:
            for item in material.get('items') or []:
                ref = f"{material['case_id']}/{item['item_id']}"
                # Only a discrepancy is worth reading back; a matching entry
                # needs no evidence and must not be turned into one.
                if item.get('kind') not in (None, 'same') and ref not in read_refs:
                    return _tool('read_material_item', evidence_gap['gap_id'],
                                 {'case_id': material['case_id'], 'item_id': item['item_id']})

    unread = investigation.get('evidence_unread') or []
    if unread:
        return _tool('read_evidence', evidence_gap['gap_id'],
                     {'evidence_id': unread[0], 'limit': 2000})
    if evidence_gap is not None:
        names = [m['display_name'] for m in facts.get('medications') or []]
        return _tool('rag_search', evidence_gap['gap_id'],
                     {'query': '、'.join(names) + ' 相互作用 说明书', 'top_k': 5})
    return {'decision': 'respond'}


def evaluate(task, observed):
    """The rubric. Delegates to the versioned scoring protocol."""
    from . import scoring
    outcome = scoring.score_outcome(task, observed)
    asked = set(observed.get('asked_fields') or [])
    required_questions = set((task.get('expected') or {}).get('must_ask_fields') or [])
    return {
        **outcome,
        'passed': bool(outcome.get('complete')),
        'failures': (outcome.get('report_quality') or {}).get('failures', []),
        'terminal_reason': observed.get('termination_reason'),
        'question_recall': {
            'numerator': len(required_questions & asked),
            'denominator': len(required_questions),
        },
        'unsupported_conclusions': len(observed.get('unsupported_statements') or []),
        'wall_ms': observed.get('wall_ms'),
        'planner_calls': observed.get('planner_calls'),
        'degraded': bool(observed.get('degraded_reason')),
        'degraded_reason': observed.get('degraded_reason'),
        'attribution': observed.get('attribution') or {},
    }


def _attribution(trace):
    """Model choice / code-forced action / model correction / policy fallback.

    Safety checks, permission checks and persistence are NOT fallbacks and are
    not counted here."""
    counts = {'model_selected': 0, 'system_forced': 0, 'model_corrected': 0,
              'policy_fallback': 0, 'deterministic': 0, 'rejected': 0}
    hydrated = 0
    for entry in trace or []:
        planner = entry.get('planner') or {}
        source = planner.get('source')
        if source == 'llm':
            counts['model_selected'] += 1
        elif source == 'llm_post_correction':
            counts['model_corrected'] += 1
        elif source == 'system_forced':
            counts['system_forced'] += 1
        elif source == 'fallback':
            counts['policy_fallback'] += 1
        elif source == 'deterministic':
            counts['deterministic'] += 1
        elif source == 'rejected':
            counts['rejected'] += 1
        if planner.get('hydrated_arguments'):
            hydrated += 1
    counts['hydrated_arguments'] = hydrated
    return counts


def run_task(task, arm, live=False):
    from stage0.agent import CareEvent, DDITool, MedicationCoordinatorAgent, RAGTool
    from stage0.memory import MemoryStore
    from stage0.product import MaterialIndex, ProductStore

    config = ARMS[arm]
    started = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix='visitprep-') as directory:
        root = Path(directory)
        chunks = task['materials']
        (root / 'chunks.jsonl').write_text(
            '\n'.join(json.dumps(chunk, ensure_ascii=False) for chunk in chunks), encoding='utf-8')

        class LocalOnlyRAG(RAGTool):
            """Production RAGTool over the synthetic corpus, embeddings off.

            Deliberately NOT a fixed-result double: retrieval must depend on
            the QUERY, or "首次检索无结果需要改写" could never be exercised and
            every query would look successful."""

            def _get_retriever(self):
                raise RuntimeError('offline exact retrieval; embeddings disabled')

        def offline_provider(payload):
            raise RuntimeError('synthetic provider 429 rate_limit')

        env = {'AGENT_INVESTIGATION_ENABLED': config['investigation'],
               'AGENT_TURN_BUDGET_SECONDS': '180', 'AGENT_TURN_TOKEN_BUDGET': '150000',
               'AGENT_TURN_CALL_BUDGET': '32', 'LLM_MAX_RETRIES': '0',
               'MEMORY_ENABLE_LLM': '0', 'AGENT_LLM_VERIFIER': '0',
               # The scripted double is a PLANNER, not a real model: it is
               # opted into the sub-question contract explicitly, and that
               # choice is recorded in the report.
               'AGENT_SUBQUESTION_PLANNER': 'model' if config.get('script') else ''}
        provider = model_double if config.get('script') else offline_provider
        client = config_ref = None
        if live:
            from stage0.extract_ddi import (assert_live_authorized, create_llm_client,
                                            resolve_llm_config)
            config_ref = resolve_llm_config()
            assert_live_authorized(config_ref)
            client = create_llm_client(config_ref)
        with patch.dict(os.environ, env), patch.object(__import__('stage0.rag', fromlist=['rag']),
                                                       'INDEX_DIR', root):
            store = MemoryStore(root / 'memory.db', llm_enabled=False)
            product = ProductStore(store)
            for index, medication in enumerate(task['initial_state']['medications']):
                store.apply_medication_change(action='add', name=medication['name'], ingredients=[],
                    session_id='visitprep', turn_id=f'seed-{index}', source='synthetic-fixture',
                    occurred_at=medication.get('date'), dose=medication.get('dose'),
                    schedule=medication.get('schedule'))
            for case_index, material in enumerate(task.get('material_cases') or []):
                product.import_csv(f'seed-case-{case_index}', material['csv'])
            agent = MedicationCoordinatorAgent(
                store, ddi_tool=DDITool(lambda meds: []), rag_tool=LocalOnlyRAG(),
                max_cycles=task['budget']['max_cycles'],
                # The scripted arm needs the hybrid planner so its provider is
                # consulted at all; the deterministic planner never proposes a
                # tool of its own choosing.
                llm_planner_enabled=config['llm'] or config.get('script', False),
                llm_planner_client=client,
                llm_planner_model=config_ref['model'] if config_ref else None,
                proposal_provider=None if live else provider)
            if config['materials']:
                agent.attach_material_index(MaterialIndex(product))
            outcome = {}
            try:
                response = agent.handle(CareEvent('user_message', task['goal']),
                                        session_id='visitprep', turn_id='visitprep')
                bundle = response.answer_bundle or {}
                investigation = bundle.get('investigation') or {}
                kinds, asked = set(), set()
                for entry in response.tool_trace:
                    if entry.get('phase') == 'observe' and entry.get('tool') == 'list_materials':
                        for material in (entry.get('observation') or {}).get('result', {}).get('materials', []):
                            for item in material.get('items', []):
                                kinds.add(item.get('kind'))
                for question in investigation.get('questions') or []:
                    asked.add(question.get('field'))
                # "无效调用" counts only calls the executor REFUSED as
                # malformed — a rejected proposal (safety or contract) is a
                # separate, already-attributed event, not a wasted call.
                invalid_calls = sum(
                    1 for entry in response.tool_trace
                    if entry.get('phase') == 'observe' and not entry.get('ok')
                    and (((entry.get('observation') or {}).get('error_kind')) == 'invalid_arguments'))
                planner_calls = sum(
                    len((entry.get('planner') or {}).get('provider_attempts') or [])
                    for entry in response.tool_trace if entry.get('phase') == 'plan')
                outcome = {
                    'text': response.text,
                    'report_markdown': response.text,
                    'termination_reason': investigation.get('termination_reason'),
                    # Carried onto `observed` because the autonomy axis reads it
                    # from there.  It lives on the investigation object, so
                    # without this line `score_autonomy` returns False for every
                    # real run and `complete` can never be True — a condition
                    # that can never be SATISFIED, the mirror image of the
                    # unreachable checks this round exists to remove.
                    'subquestion_source': investigation.get('subquestion_source'),
                    'goal_status': bundle.get('goal_status'),
                    'execution_status': bundle.get('execution_status'),
                    'diff_kinds_seen': sorted(kind for kind in kinds if kind),
                    'asked_fields': sorted(field for field in asked if field),
                    'supported_claims': [claim['claim_id'] for claim in investigation.get('claims') or []
                                         if claim.get('status') == 'supported'],
                    'supported_claim_entities': [claim.get('entities') or []
                                                 for claim in investigation.get('claims') or []
                                                 if claim.get('status') == 'supported'],
                    'unsupported_statements': investigation.get('pending_statements') or [],
                    'degraded_reason': (bundle.get('coverage') or {}).get('degraded_reason')
                        or response.audit_trail.get('response_fallback_reason'),
                    'attribution': _attribution(response.tool_trace),
                    'invalid_calls': invalid_calls,
                    'planner_calls': planner_calls,
                    # Every planning step, so a claim about model behaviour can
                    # be checked against what the model actually proposed.
                    'planner_steps': [
                        {'source': (entry.get('planner') or {}).get('source'),
                         'tool': ((entry.get('planner') or {}).get('proposal') or {}).get('tool'),
                         'decision': ((entry.get('planner') or {}).get('proposal') or {}).get('decision'),
                         'arguments': ((entry.get('planner') or {}).get('proposal') or {}).get('arguments'),
                         'gap_id': ((entry.get('planner') or {}).get('proposal') or {}).get('gap_id'),
                         'status': ((entry.get('planner') or {}).get('validation') or {}).get('status'),
                         'errors': [(item.get('code')) for item in
                                    ((entry.get('planner') or {}).get('validation') or {}).get('errors') or []],
                         'hydrated': (entry.get('planner') or {}).get('hydrated_arguments'),
                         'dropped_calls': (entry.get('planner') or {}).get('dropped_calls') or [],
                         # The full per-attempt ledger: outcome, exception
                         # type, latency and token split per call.  Without
                         # the exception TYPE a provider failure cannot be
                         # attributed — "provider_error" covers a 60s gateway
                         # timeout, a 429 and a 5xx alike, and those need
                         # opposite responses.
                         'provider_attempts': (entry.get('planner') or {}).get('provider_attempts') or [],
                         'latency_ms': (entry.get('planner') or {}).get('latency_ms')}
                        for entry in response.tool_trace if entry.get('phase') == 'plan'],
                }
            except Exception as exc:
                outcome = {'error': f'{type(exc).__name__}: {exc}',
                           'invalid_calls': 0, 'planner_calls': 0}
            store.close()
            if client:
                client.close()
    outcome['wall_ms'] = round((time.perf_counter() - started) * 1000, 2)
    return {'task_id': task['task_id'], 'family_id': task['family_id'],
            'pair_id': task['pair_id'], 'arm': arm, 'score': evaluate(task, outcome),
            'observed': outcome}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--arm', choices=sorted(ARMS), default='fixed')
    parser.add_argument('--out', required=True)
    parser.add_argument('--live', action='store_true',
                        help='显式开启真实模型调用（仅 --arm model 有效，且受 --call-cap 限制）')
    parser.add_argument('--call-cap', type=int, default=400)
    parser.add_argument('--only', default=None, help='逗号分隔的 task_id 子集')
    args = parser.parse_args()
    tasks = json.loads(DATA.read_text(encoding='utf-8'))
    if args.only:
        wanted = {item.strip() for item in args.only.split(',') if item.strip()}
        tasks = [task for task in tasks if task['task_id'] in wanted]
    if args.live and not ARMS[args.arm]['llm']:
        raise SystemExit('--live 只在启用真实模型规划的臂下有意义')
    endpoint = None
    if args.live:
        from stage0.extract_ddi import _load_dotenv, resolve_llm_config
        _load_dotenv()
        if Path(args.out).exists():
            raise RuntimeError('live 结果已存在：保留它，不要重复采样')
        # Recorded so a report can always be attributed to the model that
        # produced it — never the key, and never a restated constant.
        resolved = resolve_llm_config()
        endpoint = {'provider': resolved['provider'], 'model': resolved['model'],
                    'base_url': resolved.get('base_url')}

    results, spent, not_sampled = [], 0, []
    for task in tasks:
        if args.live and spent >= args.call_cap:
            not_sampled.append(task['task_id'])
            continue
        result = run_task(task, args.arm, live=args.live)
        spent += result['observed'].get('planner_calls') or 0
        results.append(result)

    report = {
        'protocol': 'visitprep-eval@1',
        'arm': args.arm,
        'dataset': 'author-synthetic-visitprep@1',
        'source_fingerprint': _fingerprint(),
        'dataset_sha256': _sha(DATA.read_bytes()),
        'planner_endpoint': endpoint,
        'call_cap': args.call_cap if args.live else None,
        'planner_calls_spent': spent,
        'not_sampled': not_sampled,
        'real_model_quality': ('sampled_within_cap' if args.live else 'unavailable'),
        'independent_held_out': 'unavailable',
        'summary': {
            'total': len(results),
            'passed': sum(1 for item in results if item['score']['passed']),
            'by_family': _by_family(results),
        },
        'provider_failures': _provider_failures(results),
        'tasks': results,
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report['summary'], ensure_ascii=False))
    return 0 if results and report['summary']['passed'] == len(results) else 1


def _by_family(results):
    families = {}
    for item in results:
        entry = families.setdefault(item['family_id'], {'passed': 0, 'total': 0})
        entry['total'] += 1
        entry['passed'] += 1 if item['score']['passed'] else 0
    return families


def _provider_failures(results):
    """What actually went wrong at the provider, by exception type and outcome.

    "provider_error" alone cannot be acted on: a gateway timeout, a 429 and a
    5xx call for opposite responses (the first must NOT be retried — the remote
    outcome is unknown and a retry may double-bill — while a definitive refusal
    can be).  This is the breakdown that decides whether the next optimisation
    belongs to the gateway or to the planner.
    """
    outcomes, error_types, latencies = {}, {}, []
    for item in results:
        for step in (item['observed'].get('planner_steps') or []):
            for attempt in step.get('provider_attempts') or []:
                outcome = attempt.get('outcome')
                outcomes[outcome] = outcomes.get(outcome, 0) + 1
                if outcome and outcome != 'response':
                    error_types[attempt.get('error_type')] = error_types.get(attempt.get('error_type'), 0) + 1
                if isinstance(attempt.get('latency_ms'), (int, float)):
                    latencies.append(attempt['latency_ms'])
    calls = outcomes.get('response', 0)
    return {
        'attempts_by_outcome': outcomes,
        'failures_by_error_type': error_types,
        'successful_calls': calls,
        'failed_calls': sum(error_types.values()),
        # Per-CALL latency, never a turn aggregate: a turn total mixes model
        # calls with local tool work and cannot be compared across arms.
        'call_latency_ms': {
            'min': round(min(latencies), 1) if latencies else None,
            'max': round(max(latencies), 1) if latencies else None,
            'median': round(sorted(latencies)[len(latencies) // 2], 1) if latencies else None,
            'n': len(latencies),
        },
        'note': ('按调用记账；失败按异常类型分开。超时类失败在本项目语义下不重试'
                 '（远端结果未知，重试可能重复计费），429 类才走有界重试。'),
    }


def _sha(payload):
    import hashlib
    return hashlib.sha256(payload).hexdigest()


def _fingerprint():
    from stage0.source_fingerprint import SCOPE_REPO, source_fingerprint
    return source_fingerprint(SCOPE_REPO)


if __name__ == '__main__':
    raise SystemExit(main())
