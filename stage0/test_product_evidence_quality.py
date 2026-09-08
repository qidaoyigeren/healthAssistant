import unittest
from stage0.evidence_quality import FallbackPolicy, assess_claim, bounded_research


class EvidenceQualityTests(unittest.TestCase):
    def test_cache_retry_ttl_versions_and_legacy(self):
        clock = [100.0]
        policy = FallbackPolicy({'corpus': 'v1'}, lambda: clock[0], {'matched': 100, 'not_found': 5, 'provider_error': 2, 'parse_error': 3})
        for status in policy.ttls:
            entry = policy.entry(status, evidence=[])
            self.assertEqual('hit', policy.read(entry)[1])
            clock[0] += policy.ttls[status]
            self.assertEqual('expired', policy.read(entry)[1])
        self.assertIsNone(policy.read({'status': 'matched', 'evidence': ['old']})[0])
        old = policy.entry('matched')
        policy.versions = {'corpus': 'v2'}
        self.assertIsNone(policy.read(old)[0])

    def test_real_citation_is_not_support(self):
        def check(text, **kwargs):
            return assess_claim(quote=text, text=text, entities=['药甲', '药乙'], evidence_id='e1', **kwargs)['status']
        self.assertEqual('insufficient', check('药甲和药乙都是药物'))
        self.assertEqual('contradicted', check('药甲与药乙未发现相互作用'))
        self.assertEqual('supported', check('药甲与药乙合用增加出血风险'))
        self.assertEqual('insufficient', check('药甲与药丙合用增加出血风险'))
        self.assertEqual('insufficient', check('药甲与药乙合用增加出血风险', conditions_known=False))
        self.assertEqual('insufficient', check('药甲与药乙合用增加出血风险', subject='儿童', required_subject='成人'))

    def test_no_progress_and_budget(self):
        search = lambda q: [{'chunk_id': 'same', 'text': 'text'}]
        self.assertEqual('no_progress', bounded_research(search, ['a', 'b', 'c'])['termination_reason'])
        self.assertEqual('budget_exhausted', bounded_research(search, ['a', 'b'], max_queries=1)['termination_reason'])
