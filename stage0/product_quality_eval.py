"""Synthetic engineering ablation. No model-quality or clinical claims."""
from __future__ import annotations
import argparse
import json
import statistics
import time
from pathlib import Path
from .evidence_quality import assess_claim, ExactCoverageReranker

CASES = [
    ('positive', '药甲与药乙合用增加出血风险', True, True),
    ('negative', '药甲与药乙未发现相互作用', False, True),
    ('unrelated', '药甲和药乙属于常见药物', False, True),
    ('wrong_entity', '药甲与药丙合用增加出血风险', False, True),
    ('unknown_conditions', '药甲与药乙合用增加出血风险', False, False),
    ('avoid', '药甲与药乙避免合用', True, True),
    ('monitor', '药甲与药乙合用需监测', True, True),
    ('no_increase', '药甲与药乙合用不增加出血风险', False, True),
    ('question', '药甲和药乙是否存在相互作用尚需研究', False, True),
    ('insufficient', '药甲与药乙研究资料不足', False, True),
]


def evaluate():
    results = []
    for mode in ('baseline', 'rerank_only', 'support_only', 'combined'):
        samples = []
        for name, text, expected, known in CASES:
            start = time.perf_counter()
            candidate = ({'chunk_id': name, 'text': text, 'section': '药物相互作用'}, {}, {})
            rows = [candidate]
            if mode in ('rerank_only', 'combined'):
                rows = ExactCoverageReranker().rank(rows, ['药甲', '药乙'])
            quote = rows[0][0]['text']
            # The former fallback checked exact quote plus partner presence.
            predicted = '药乙' in quote
            status = 'baseline_citation_gate_only'
            if mode in ('support_only', 'combined'):
                result = assess_claim(quote=quote, text=quote, entities=['药甲', '药乙'], evidence_id=name, conditions_known=known)
                status, predicted = result['status'], result['status'] == 'supported'
            samples.append({'case_id': name, 'expected_support': expected, 'predicted_support': predicted,
                'status': status, 'correct': predicted == expected, 'elapsed_ms': (time.perf_counter() - start) * 1000})
        latencies = sorted(s['elapsed_ms'] for s in samples)
        results.append({'mode': mode, 'n': len(samples), 'correct': sum(s['correct'] for s in samples),
            'false_support': sum(s['predicted_support'] and not s['expected_support'] for s in samples),
            'p50_ms': statistics.median(latencies), 'p95_ms': latencies[-1], 'model_calls': 0, 'tokens': 0, 'samples': samples})
    return {'dataset_version': 'synthetic-claim-screen-v1', 'track': 'local_engineering_dev',
        'generalization_claim': False, 'results': results, 'reranker_adoption': 'default_off_no_ranking_gain_demonstrated',
        'limitations': ['single-candidate replay does not establish retrieval ranking gains', 'lexical screening is not semantic or clinical validation', 'no independent held-out material'],
        'status': 'pass' if all(s['correct'] for s in results[-1]['samples']) else 'fail'}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    result = evaluate()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2), 'utf-8')
    print(result['status'])
    return 0 if result['status'] == 'pass' else 1


if __name__ == '__main__':
    raise SystemExit(main())
