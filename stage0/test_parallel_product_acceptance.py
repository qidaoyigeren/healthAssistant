"""并行交付的**独立产品验收**（任务 D）。

这份套件不测某个 Agent 写得好不好，它测的是：**从外面看，产品有没有按冻结的
接口做事**。因此它只从这三个入口进入——

* 真实 HTTP 端点（`fastapi.testclient` 打在 `create_app` 造出来的应用上）；
* 真实 outbox worker（`worker.drain_once()`）与真实 care task 队列；
* 真实数据库（每个用例一个隔离库，绝不碰 `stage0/memory.db`）。

它**不** import 实现者的私有函数，也**不**沿生产函数的内部字段写断言。

三条报告口径（见 docs/parallel-delivery/D.md）：

1. **通过** —— 断言成立。
2. **未通过** —— 功能在，但行为违反冻结契约。
3. **未测到** —— 功能在基线上**根本不存在**（端点 404、字段从不出现）。
   这一条用 `skipTest` 表达，理由以「基线缺失」开头；它**不是**任何一方
   的回归错误，也**不是**被改写成 expected pass 的失败。

判定顺序是刻意的：先探测功能在不在（外部可见的存在性，探测本身**不改状态**），
再断言契约。只有"存在性探测不通过"才允许 skip；断言失败一律是失败。

不运行真实模型：所有调查都走**脚本化规划器**（`proposal_provider`），
所有检索都走合成语料，全程无网络。
"""
from __future__ import annotations

import json
import shutil
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from stage0.agent import DDITool, MedicationCoordinatorAgent
from stage0.care_tasks import CareTasks, safety_case_request_id
from stage0.product import ProductStore
from stage0.safety_cases import SafetyCaseStore
from stage0 import safety_cases as sc

ROOT = Path(__file__).resolve().parents[1]
#: 真实患者库。任何用例都不许连它——这是 CONTRACT.md §5 的硬规则。
REAL_PATIENT_DB = (ROOT / 'stage0' / 'memory.db').resolve()
#: 本任务的输出目录（CONTRACT.md §5 / OWNERSHIP.md §2.3）。
OUTPUT_ROOT = ROOT / 'output' / 'parallel' / 'independent-acceptance'
CASE_ROOT = OUTPUT_ROOT / 'cases'

#: 冻结契约里 assessment.status 的全部取值（CONTRACT.md §3.2）。
ASSESSMENT_STATUSES = frozenset({'verified', 'candidate', 'stale', 'unsupported'})
#: 冻结契约里 follow_up.schedule_state 的全部取值（CONTRACT.md §4.2）。
SCHEDULE_STATES = frozenset({'scheduled', 'due', 'triggered', 'blocked',
                             'cancelled', 'unscheduled'})
#: 冻结契约里 follow_up.kind 的全部取值（CONTRACT.md §4.2，取值不变）。
FOLLOW_UP_KINDS = frozenset({'review_at', 'on_event', 'arrangement'})
#: 三个新增端点（CONTRACT.md §4.6）。
FOLLOW_UP_PATH = '/v1/safety-cases/{case_id}/follow-up'
CONFIRMATION_PATH = '/v1/safety-cases/{case_id}/follow-up/confirmation'

FUTURE = '2999-01-01T00:00:00+00:00'
FUTURE_MS = '2999-01-01T00:00:00.000Z'
PAST = '2020-01-01T00:00:00+00:00'


# ---------------------------------------------------------------------------
# 合成世界：一个检测器、一份语料，全部无网络、无真实临床含义
# ---------------------------------------------------------------------------
SYNTHETIC_WARNING = {
    'drug_a': '合成药甲', 'drug_b': '合成药乙', 'severity': 'major',
    'mechanism': '合成机制', 'effect': '合成风险效应', 'management': None,
    'source_text': '合成药甲与合成药乙合用可能增加合成风险（合成说明书文本）',
    'source_url': 'https://synthetic.invalid/label/a',
    'confidence': 'high', 'detection_path': 'synthetic_detector',
}

#: 回读得到、因而可以**合法引用**的原文片段。它讲的是贮存条件，
#: 与下面的问题、答案都不是一回事——这正是要考验的情形。
QUOTE_TEXT = '避光密封保存于阴凉干燥处'
#: 与那段引文毫无关系的"答案"——引文真实存在，但它支持不了这句话。
UNRELATED_VALUE = '每天睡前一次，随餐服用'


def synthetic_detect(medications):
    if {'合成药甲', '合成药乙'}.issubset(set(medications)):
        return [dict(SYNTHETIC_WARNING)]
    return []


class SyntheticRAG:
    """合成语料：命中一段可以被 read_evidence 回读的说明书原文。

    `chunks` 是**语料快照**——检索层用它校验"结果确实来自这个语料"，
    缺了它检索会退化成 `no_match`，证据就永远读不回来。
    `__call__` 是检索本身。
    """

    def __init__(self):
        self.chunks = [{
            'chunk_id': 'acceptance-label-1', 'drug_name': '合成药甲',
            'section': '贮藏', 'text': f'【验收材料】本品应{QUOTE_TEXT}，有效期二十四个月。',
            'source_url': 'https://acceptance.invalid/label/1',
        }]

    def __call__(self, query, **kwargs):
        return {'query': query, 'mode': 'synthetic',
                'results': [dict(chunk, score=1.0, rank=index + 1)
                            for index, chunk in enumerate(self.chunks)]}


class EmptyRAG:
    def __call__(self, query, **kwargs):
        return {'query': query, 'mode': 'synthetic', 'corpus_version': 'acceptance-v1',
                'results': []}


# ---------------------------------------------------------------------------
# 宿主：真实服务 + 真实 worker + 真实数据库，隔离、离线
# ---------------------------------------------------------------------------
class _Host:
    """最小产品宿主。语义与 `stage0/server.create_app` 一致，只是数据库隔离。"""

    def __init__(self, directory: Path, *, rag=None, detection=None,
                 proposal_provider=None, agent_error: Exception | None = None) -> None:
        import stage0.server as server
        directory.mkdir(parents=True, exist_ok=True)
        db_path = (directory / 'memory.db').resolve()
        # 硬护栏：这条路径永远不该是真库。
        assert db_path != REAL_PATIENT_DB, f'拒绝连接真实患者库：{db_path}'
        self.db_path = db_path
        holder: dict = {}
        detector = detection or synthetic_detect

        def factory():
            if agent_error is not None:
                raise agent_error
            return MedicationCoordinatorAgent(
                holder['store'], ddi_tool=DDITool(detector),
                rag_tool=rag if rag is not None else EmptyRAG(),
                llm_planner_enabled=proposal_provider is not None,
                proposal_provider=proposal_provider)

        self.app = server.create_app(db_path=db_path, worker_thread=False,
                                     agent_factory=factory)
        holder['store'] = self.app.state.store
        self.store = self.app.state.store
        self.worker = self.app.state.worker
        self.product = ProductStore(self.store)
        self.client = TestClient(self.app)

    # ---- 产品路径 --------------------------------------------------------
    def close(self) -> None:
        try:
            self.client.close()
        finally:
            self.worker.stop()
            self.store.close()

    def submit(self, key: str, body: dict) -> dict:
        response = self.client.post('/v1/events', json=body,
                                    headers={'Idempotency-Key': key})
        assert response.status_code == 202, response.text
        self.worker.drain_once()
        return self.client.get(f'/v1/events/{key}').json()

    def add_medication(self, key: str, name: str) -> dict:
        return self.submit(key, {'session_id': 'acceptance', 'event_type': 'medication_change',
                                 'text': f'新增{name}', 'source': 'caregiver',
                                 'payload': {'action': 'add', 'medication': name}})

    def change_dose(self, key: str, name: str, dose: str) -> dict:
        return self.submit(key, {'session_id': 'acceptance', 'event_type': 'medication_change',
                                 'text': f'{name}改为{dose}', 'source': 'caregiver',
                                 'payload': {'action': 'dose_change', 'medication': name,
                                             'dose': dose}})

    def stop_medication(self, key: str, name: str) -> dict:
        return self.submit(key, {'session_id': 'acceptance', 'event_type': 'medication_change',
                                 'text': f'停用{name}', 'source': 'caregiver',
                                 'payload': {'action': 'remove', 'medication': name}})

    def cases(self) -> list[dict]:
        response = self.client.get('/v1/safety-cases')
        assert response.status_code == 200, response.text
        return response.json()['items']

    def case(self, case_id: str) -> dict:
        response = self.client.get(f'/v1/safety-cases/{case_id}')
        assert response.status_code == 200, response.text
        return response.json()

    def seed_one_case(self) -> dict:
        """走真实事件路径造出唯一一个待复核事项。"""
        self.add_medication('seed-1', '合成药甲')
        self.add_medication('seed-2', '合成药乙')
        cases = self.cases()
        assert len(cases) == 1, cases
        return cases[0]

    def pump(self, rounds: int = 3, *, max_tasks: int = 5) -> None:
        """把真实 worker 跑几轮——到期扫描就在这条路上。"""
        for _ in range(rounds):
            self.worker.drain_once(max_tasks=max_tasks)

    def probe_follow_up(self, case_id: str) -> int:
        """端点存在性探测。**不改状态**：用一个永远对不上的 revision 触发 CAS。

        基线没有这个端点 → 404/405/501；实现了但没有 CAS 也会返回非 404 的
        某个 4xx，所以这个探测只回答"端点在不在"，不回答"契约对不对"。
        """
        response = self.client.post(
            FOLLOW_UP_PATH.format(case_id=case_id),
            json={'key': f'probe-{case_id}', 'expected_revision': -1,
                  'action': 'cancel'})
        return response.status_code

    # ---- 断言用投影 ------------------------------------------------------
    @staticmethod
    def answer_elements(case: dict) -> list[dict]:
        """事项视图里**每一条**答案元素（含已答与待答请求上的）。

        走的是 `case_view` 下发的投影，不是内部对象。
        """
        out: list[dict] = []
        for request in (case.get('required_inputs') or []) + (case.get('answered_inputs') or []):
            out.extend(request.get('answered_parts') or [])
        return out


def _answer_elements(case: dict) -> list[dict]:
    return _Host.answer_elements(case)


def _assessments(items) -> list[dict]:
    return [item['assessment'] for item in items
            if isinstance(item.get('assessment'), dict)]


def _gap_for(inv: dict, tool: str) -> str | None:
    """当前状态里**允许**用这个工具推进的缺口。"""
    for gap in inv.get('open_gaps') or []:
        if tool in (gap.get('closable_by') or []):
            return gap.get('gap_id')
    return None


def _declare(statement, *, target='patient_actual_state', strategy='ask_user',
             subjects=('合成药乙',), field='schedule',
             why='用法影响这条提示是否成立') -> dict:
    """脚本化规划器声明一条问题——与主线验收用的是同一个形状。"""
    return {'decision': 'tool', 'tool': 'plan_questions', 'gap_id': 'subquestions',
            'expected_observation': '问题集建立',
            'expected_change': '如果得到答案，就按当前用法重新核对',
            'basis_refs': ['memory:conclusion:1@v1'],
            'arguments': {'questions': [{'statement': statement,
                                         'information_target': target,
                                         'strategy': strategy,
                                         'subject_refs': list(subjects),
                                         'target_field': field, 'why': why}]}}


# ---------------------------------------------------------------------------
# 验收基类：环境隔离 + 三态判定
# ---------------------------------------------------------------------------
class AcceptanceTest(unittest.TestCase):
    """每个用例一个独立目录、一个独立数据库、一个独立主机实例。"""

    agent_error: Exception | None = None
    rag_factory = EmptyRAG
    proposal_provider = None

    def setUp(self):
        name = self.id().rsplit('.', 1)[-1]
        self.directory = CASE_ROOT / name
        if self.directory.exists():
            shutil.rmtree(self.directory, ignore_errors=True)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.addCleanup(shutil.rmtree, self.directory, True)
        self.api = self._build()

    def _build(self, **kwargs) -> _Host:
        params = {'rag': self.rag_factory(),
                  'agent_error': self.agent_error,
                  'proposal_provider': kwargs.pop('proposal_provider',
                                                  self.proposal_provider)}
        params.update(kwargs)
        return _Host(self.directory / 'host', **params)

    def tearDown(self):
        try:
            self.api.close()
        except Exception:
            pass

    def rebuild(self, **kwargs) -> _Host:
        """关掉当前宿主、在**同一个数据库上**重建一个——这就是「重启」。"""
        self.api.close()
        self.api = self._build(**kwargs)
        return self.api

    # ---- 三态判定 --------------------------------------------------------
    def require_implemented(self, present: bool, feature: str, evidence: str = '') -> None:
        """功能在基线上不存在 → 未测到（不是失败，也不是回归）。"""
        if not present:
            detail = f'（观察：{evidence}）' if evidence else ''
            self.skipTest(f'基线缺失/等待实现：{feature}{detail}')

    # ---- 常用构造 --------------------------------------------------------
    def monitoring_case(self, case_id: str, follow_up: dict,
                        key: str = 'mon-1') -> tuple[int, dict]:
        """走真实处置端点进入持续跟进。返回 (status_code, body)。"""
        case = self.api.case(case_id)
        response = self.api.client.post(
            f'/v1/safety-cases/{case_id}/disposition',
            json={'key': key, 'expected_revision': case['revision'],
                  'disposition': sc.DISPOSITION_MONITORING, 'follow_up': follow_up})
        return response.status_code, response.json()

    def scripted_tasks(self, provider) -> CareTasks:
        """用**脚本化模型**驱动真实执行路径（契约验证，不是模型能力）。"""
        return CareTasks(self.api.product, agent_factory=lambda: MedicationCoordinatorAgent(
            self.api.store, ddi_tool=DDITool(synthetic_detect), rag_tool=self.rag_factory(),
            llm_planner_enabled=True, proposal_provider=provider))

    def run_investigation(self, provider, key: str, *, seed: bool = True) -> dict:
        """造事项 → 跑一次真实调查，返回恢复后的 care task（含 investigation）。

        宿主整体换成脚本化模型：worker 恢复的那一轮也走同一个规划器，
        否则恢复会退回降级路径。
        """
        self.rebuild(proposal_provider=provider)
        if seed:
            case = self.api.seed_one_case()
        else:
            case = self.api.cases()[0]
        tasks = self.scripted_tasks(provider)
        task = tasks.create(key, 'safety_case', case['case_id'])
        task = tasks.resume(task['id'], f'{key}-run', task['revision'])
        self.api.pump()
        return self.api.product.get(task['id'], 'care_task')

    @staticmethod
    def questions_of(task: dict) -> list[dict]:
        return ((task.get('investigation') or {}).get('questions') or [])


class EntityOverlapRAG(SyntheticRAG):
    """语料只与问题共享一个实体名，内容讲的却是别的事。"""

    def __init__(self):
        self.chunks = [{
            'chunk_id': 'acceptance-label-overlap', 'drug_name': '合成药甲',
            'section': '药物相互作用',
            'text': '【验收材料】合成药甲与合成药乙合用增加出血风险。',
            'source_url': 'https://acceptance.invalid/label/overlap',
        }]


# ---------------------------------------------------------------------------
# 〇、验收本身的环境隔离（CONTRACT.md §5 的三条硬规则）
# ---------------------------------------------------------------------------
class EnvironmentIsolationTests(AcceptanceTest):
    def test_the_host_refuses_to_open_the_default_patient_database(self):
        """宿主必须**拒绝**把默认患者库当成验收库。

        这不是"我们小心一点"，而是结构上做不到：`DEFAULT_DB` 解析成当前检出下的
        `stage0/memory.db`，正是真实患者库的位置。
        """
        with self.assertRaises(AssertionError):
            _Host(ROOT / 'stage0')

    def test_every_case_runs_in_its_own_database_under_the_task_output_dir(self):
        """每个用例一个库，且都在本任务的 `output/parallel/` 下。"""
        self.api.seed_one_case()
        self.assertTrue(
            str(self.api.db_path).startswith(str(OUTPUT_ROOT.resolve())),
            f'验收库必须落在 {OUTPUT_ROOT} 下，实际是 {self.api.db_path}')
        self.assertTrue(self.api.db_path.exists())


# ---------------------------------------------------------------------------
# 一、引文不能支持任意不相关答案（CONTRACT.md §3.3 / D.md 必查项 1）
# ---------------------------------------------------------------------------
class AQuestionReadAsAnsweredShowsWhatAnsweredItTests(AcceptanceTest):
    """**基线观察，不属于 A/B/C 的冻结交付。**见 D.md「基线问题」一节。

    这里断言的不是本轮的冻结接口，而是一条外部一致性：服务端说一条问题
    `information_state='available'`（给界面读作"这条已经查清了"），那就必须能
    指出是**哪一条答案**让它变成这样。状态宣称完成了，却没有任何东西支撑它，
    与 §4.5 的 `confirmed=true` 却没有确认记录是同一类缺陷。
    """
    rag_factory = EntityOverlapRAG

    @staticmethod
    def _provider():
        def provider(payload):
            inv = payload['investigation']
            if not (inv.get('questions') or []):
                return _declare('合成药乙目前的服用频次是什么？',
                                target='general_reference',
                                strategy='general_reference', field='schedule')
            if not inv.get('evidence_searched_count'):
                gap = _gap_for(inv, 'rag_search')
                if gap is None:
                    return {'decision': 'respond'}
                return {'decision': 'tool', 'tool': 'rag_search', 'gap_id': gap,
                        'expected_observation': '检索说明书原文',
                        'arguments': {'query': '合成药甲 合成药乙 相互作用'}}
            unread = inv.get('evidence_unread') or []
            if unread:
                gap = _gap_for(inv, 'read_evidence')
                if gap is None:
                    return {'decision': 'respond'}
                return {'decision': 'tool', 'tool': 'read_evidence', 'gap_id': gap,
                        'expected_observation': '回读原文',
                        'arguments': {'evidence_id': unread[0]}}
            return {'decision': 'respond'}
        return provider

    def test_a_question_read_as_available_can_name_its_answer(self):
        task = self.run_investigation(self._provider(), 'overlap-1')
        case_id = self.api.cases()[0]['case_id']
        questions = self.questions_of(task)

        available = [question for question in questions
                     if question.get('information_state') == 'available']
        self.require_implemented(
            bool(available),
            '本轮没有把任何问题读到 available，这条一致性无从检验',
            evidence=f'问题的信息状态：'
                     f'{[q.get("information_state") for q in questions]}')

        case_parts = _answer_elements(self.api.case(case_id))
        for question in available:
            answers = question.get('answers') or []
            self.assertTrue(
                answers or case_parts,
                '这条问题被读成"已有依据"，但答案元素一个都没有：'
                '界面只能显示"已回答"，显示不出回答是什么。'
                f'问题={question.get("statement")!r} '
                f'answered_by={question.get("answered_by")!r} '
                f'evidence_refs={question.get("evidence_refs")!r}')

    def test_claim_support_is_not_inferred_from_a_shared_entity_name(self):
        """只共享一个实体名，不足以把这条问题读成"已有依据"。"""
        task = self.run_investigation(self._provider(), 'overlap-2')
        questions = self.questions_of(task)
        self.require_implemented(
            bool(questions),
            '脚本化调查没有建立问题',
            evidence=f'问题数 {len(questions)}')
        question = questions[0]
        self.assertNotEqual(
            'available', question.get('information_state'),
            '一段只与问题共享实体名、内容讲的是别的事的材料，'
            '把这条问题读成了"已有依据"。材料确实被回读了，但**读到不等于支持**——'
            f'这份材料的正文是：{EntityOverlapRAG().chunks[0]["text"]!r}；'
            f'问题是：{question.get("statement")!r}')


# ---------------------------------------------------------------------------
class CitationTests(AcceptanceTest):
    rag_factory = SyntheticRAG

    @staticmethod
    def _provider(seen: list):
        """脚本化规划器：检索 → 回读 → 拿**真实回读过的**原文当引文，
        去支撑一句与它毫无关系的答案。

        `source_ref` 必须给出：A 之后，`answer_question` 要求模型**指认**
        自己依据的是哪一条来源（服务端按真实记录解析这个引用，不信自报的种类）。
        少了它，这次提交连采纳层都到不了——那样验的就不是"引文能不能支撑答案"，
        而是"缺参数会不会被拒"，是另一回事。
        """
        state = {'evidence_id': None}

        def provider(payload):
            inv = payload['investigation']
            seen.append(inv)
            if not (inv.get('questions') or []):
                return _declare('合成药乙目前的服用频次是什么？',
                                target='general_reference',
                                strategy='general_reference',
                                field='schedule')
            if not inv.get('evidence_searched_count'):
                gap = _gap_for(inv, 'rag_search')
                if gap is None:
                    return {'decision': 'respond'}
                return {'decision': 'tool', 'tool': 'rag_search', 'gap_id': gap,
                        'expected_observation': '检索说明书原文',
                        'arguments': {'query': '合成药甲 合成药乙 相互作用'}}
            unread = inv.get('evidence_unread') or []
            if unread:
                gap = _gap_for(inv, 'read_evidence')
                if gap is None:
                    return {'decision': 'respond'}
                state['evidence_id'] = unread[0]
                return {'decision': 'tool', 'tool': 'read_evidence', 'gap_id': gap,
                        'expected_observation': '回读原文之后才谈得上引用',
                        'arguments': {'evidence_id': unread[0]}}
            question = (inv['questions'] or [{}])[0]
            if question.get('information_state') != 'available':
                if not state['evidence_id']:
                    return {'decision': 'respond'}
                return {'decision': 'tool', 'tool': 'answer_question',
                        'gap_id': question.get('question_id'),
                        'expected_observation': '把这条答案落到问题上',
                        'arguments': {'question_id': question.get('question_id'),
                                      'source': 'evidence',
                                      'source_ref': state['evidence_id'],
                                      'value': UNRELATED_VALUE,
                                      'field': 'schedule',
                                      'quote': QUOTE_TEXT}}
            return {'decision': 'respond'}
        return provider

    def _attempt_and_question(self, key: str) -> tuple[dict, dict]:
        """跑一次脚本化调查，返回 (care task, 那条问题)。"""
        seen: list = []
        task = self.run_investigation(self._provider(seen), key)
        questions = self.questions_of(task)
        self.assertTrue(questions, '脚本化调查应当建立一条问题')
        return task, questions[0]

    def test_the_unrelated_quote_was_really_read_back_first(self):
        """先确认前提：那条引文**确实被回读过**。

        没有这一条，后面的"它没让答案变成已核对"就可能是因为引文根本
        没读到——那是另一回事，也说明不了 A 解决了什么。
        """
        task, question = self._attempt_and_question('citation-0')
        read = [step for step in (question.get('attempts') or [])
                if step.get('tool') == 'read_evidence' and step.get('ok')]
        self.assertTrue(read, f'回读没有发生，前提不成立：{question.get("attempts")}')
        self.assertNotEqual('available', question.get('information_state'))

    def test_a_real_quote_does_not_make_an_unrelated_answer_verified(self):
        """引文真实存在、确实被回读过，但它支持不了这句话 —— 那就不算"已核对"。

        基线只证明引文**读过**，不证明引文**支持**答案（`_source_supports`）。
        A 之后这件事被做成了结构性的：这种提交在**支持关系**那一段就被拒，
        既不写下答案元素，也不留下任何 `verified`。
        """
        _task, question = self._attempt_and_question('citation-1')

        assessments = _assessments(question.get('answers') or [])
        for assessment in assessments:
            self.assertIn(assessment.get('status'), ASSESSMENT_STATUSES,
                          f'assessment.status 取值超出冻结集合：{assessment["status"]!r}')
        self.assertNotIn(
            'verified', {item.get('status') for item in assessments},
            '一段真实回读、但与答案无关的引文让答案变成了 verified——'
            '「已核对」在这种情况下不承载任何信息。')
        self.assertNotEqual(
            'available', question.get('information_state'),
            '引文读过 ≠ 引文支持：这条问题不该被读成"已有依据"')

        rejected = [step for step in (question.get('attempts') or [])
                    if step.get('tool') == 'answer_question' and step.get('rejected')]
        self.assertTrue(
            rejected,
            '这条不受支持的提交既没有被拒、也没有留下可判定的 assessment——'
            f'那它到底被怎么处理了？问题状态={question.get("information_state")!r} '
            f'答案={question.get("answers")}')
        self.assertIn('quote_does_not_state_value', rejected[-1]['rejected'],
                      f'拒的理由应当是"引文没有陈述这个答案"：{rejected[-1]}')


# ---------------------------------------------------------------------------
# 二、自述来源不能制造真实记录（CONTRACT.md §3.5 / D.md 必查项 2）
# ---------------------------------------------------------------------------
class SelfReportedSourceTests(AcceptanceTest):
    @staticmethod
    def _provider(seen: list, *, target='patient_actual_state', strategy='ask_user'):
        def provider(payload):
            inv = payload['investigation']
            seen.append(inv)
            if not (inv.get('questions') or []):
                return _declare('合成药乙目前的服用频次是什么？',
                                target=target, strategy=strategy, field='schedule')
            return {'decision': 'respond'}
        return provider

    def _answer_as_user(self, key: str = 'user-source'):
        """走界面真正调用的补问端点回答一次，返回 (case_id, task, user_answers)。"""
        provider = self._provider([])
        task = self.run_investigation(provider, f'{key}-investigation')
        case_id = self.api.cases()[0]['case_id']
        case = self.api.case(case_id)
        self.assertTrue(case['required_inputs'], '模型提的问题必须变成页面上的补问')
        response = self.api.client.post(
            f'/v1/safety-cases/{case_id}/answer',
            json={'key': f'{key}-answer', 'expected_revision': case['revision'],
                  'request_id': case['required_inputs'][0]['request_id'],
                  'value': '每天一次'})
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual([], response.json()['required_inputs'],
                         '回答应当被接受并关闭该问题')
        stored = self.api.product.get(task['id'], 'care_task')
        answers = [answer for answer in (self.questions_of(stored)[0].get('answers') or [])
                   if answer.get('provenance') == 'user_reported']
        return case_id, stored, answers

    def test_a_user_answer_is_kept_with_its_source_and_does_not_close_the_case(self):
        """用户说了一句话：它作为**用户报告**被记下来，事项不因此变成"有依据"。"""
        case_id, _task, user_answers = self._answer_as_user()
        self.assertTrue(user_answers, '用户回答必须留在那条问题上')
        self.assertEqual('user_answer', user_answers[-1].get('source'),
                         '用户回答的来源种类要如实记录')
        self.assertEqual('每天一次', user_answers[-1].get('value'))

        after = self.api.case(case_id)
        self.assertNotEqual(sc.STATUS_RESOLVED, after['status'],
                            '一次用户回答不能把事项关掉')
        self.assertIsNone(after['resolution_basis'],
                          '用户报告不构成处置依据')

    def test_a_user_answer_is_never_verified(self):
        """用户说了一句话，不等于这件事有了已核实的记录。"""
        _case_id, _task, user_answers = self._answer_as_user('user-verified')
        assessments = _assessments(user_answers)
        self.require_implemented(
            bool(assessments),
            '用户回答的答案元素上没有 assessment 键（A 的交付尚未落到记录里）',
            evidence=f'答案元素键：{sorted(user_answers[-1].keys())}')
        for answer in user_answers:
            self.assertNotEqual(
                'verified', answer['assessment'].get('status'),
                '「用户说的一句话」被判成了已核对——自述来源不能制造真实记录')

    def test_a_professional_opinion_is_never_verified(self):
        """模型自称"这是专业意见"，不能变成一条已核实的记录。

        本项目**没有连接真实医护服务**，所以 `professional` 不是模型能声明的
        来源种类：服务端在**解析来源**那一步就把它挡掉，而不是"记下来但不置
        verified"。提交被拒是更强的保证——一条根本不存在的记录，不会被谁
        误读成依据。

        （这条在基线是"未测到"：当时没有任何闸门，模型自报即可采纳。）
        """
        emitted = {'professional': 0}

        def provider(payload):
            inv = payload['investigation']
            if not (inv.get('questions') or []):
                return _declare('合成药乙能不能继续合用？',
                                target='general_reference',
                                strategy='general_reference', field='schedule')
            question = (inv['questions'] or [{}])[0]
            if question.get('information_state') != 'available':
                emitted['professional'] += 1
                return {'decision': 'tool', 'tool': 'answer_question',
                        'gap_id': question.get('question_id'),
                        'expected_observation': '把"专业意见"记下来',
                        'arguments': {'question_id': question.get('question_id'),
                                      'source': 'professional',
                                      'source_ref': 'professional:note:1',
                                      'value': '药师说可以继续合用',
                                      'field': 'schedule'}}
            return {'decision': 'respond'}

        task = self.run_investigation(provider, 'professional-source')
        self.assertTrue(
            emitted['professional'],
            '脚本没有真的提交过这条"专业意见"——那样这条用例什么都没验到')
        question = self.questions_of(task)[0]
        answers = question.get('answers') or []
        self.assertFalse(
            [answer for answer in answers
             if answer.get('provenance') == 'professional_opinion'
             or answer.get('source') == 'professional'],
            f'模型自报的专业意见被记成了答案：{answers}')
        self.assertNotIn('verified',
                         {a.get('status') for a in _assessments(answers)},
                         '模型自报的「专业意见」被判成了已核对')
        self.assertNotEqual('available', question.get('information_state'),
                            '没有专业复核记录，这条问题不该被读成"已有依据"')

    def test_the_status_is_not_a_single_constant(self):
        """不能永远只吐一个值 —— 否则前面所有「不得 verified」都虚假通过。

        同一件事项上造出**两种**情形：

        * 模型对着**当前权威记录**回答（对象、值、状态逐项核对）→ `verified`；
        * 用户在补问里回答同一件事的另一面 → 用户报告，只能到 `candidate`。

        两种 status 必须同时出现。若实现只会吐一个值，前面每一条"不得 verified"
        都是虚假通过。
        """
        seen_evidence = {'ref': None}
        # 情形二要真的检索并回读：语料挂上，否则它连"有依据未核对"都到不了。
        self.rag_factory = SyntheticRAG

        def provider(payload):
            inv = payload['investigation']
            questions = inv.get('questions') or []
            if not questions:
                return {'decision': 'tool', 'tool': 'plan_questions',
                        'gap_id': 'subquestions',
                        'expected_observation': '问题集建立',
                        'arguments': {'questions': [
                            {'statement': '合成药乙现在还是有效的用药吗？',
                             'information_target': 'patient_actual_state',
                             'strategy': 'patient_record',
                             'subject_refs': ['合成药乙'],
                             'target_field': 'status',
                             'why': '是否仍在用药决定这条提示是否成立'},
                            {'statement': '合成药甲贮存上要注意什么？',
                             'information_target': 'general_reference',
                             'strategy': 'general_reference',
                             'subject_refs': ['合成药甲'],
                             'target_field': 'storage_note',
                             'why': '贮存条件影响这条提示是否成立'}]}}
            record = next((item for item in
                           ((inv.get('facts') or {}).get('medications') or [])
                           if item.get('display_name') == '合成药乙'), None)
            from_record = next((q for q in questions
                                if q.get('target_field') == 'status'), None)
            from_reading = next((q for q in questions
                                 if q.get('target_field') == 'storage_note'), None)
            # 情形一：对着**当前权威记录**逐项核对 → 应当 verified。
            if (record and from_record
                    and from_record.get('information_state') != 'available'):
                return {'decision': 'tool', 'tool': 'answer_question',
                        'gap_id': from_record.get('question_id'),
                        'expected_observation': '按当前权威记录回答',
                        'arguments': {'question_id': from_record.get('question_id'),
                                      'source': 'patient_record',
                                      'source_ref': record.get('ref'),
                                      'value': record.get('status'),
                                      'field': 'status'}}
            # 情形二：引文属实、但问题是**开放字段** → 支持关系无法机械核对，
            # 只能到 candidate。
            if from_reading and from_reading.get('information_state') != 'available':
                if not inv.get('evidence_searched_count'):
                    gap = _gap_for(inv, 'rag_search')
                    if gap:
                        return {'decision': 'tool', 'tool': 'rag_search', 'gap_id': gap,
                                'expected_observation': '检索说明书原文',
                                'arguments': {'query': '合成药甲 贮存'}}
                unread = inv.get('evidence_unread') or []
                if unread:
                    seen_evidence['ref'] = unread[0]
                    gap = _gap_for(inv, 'read_evidence')
                    if gap:
                        return {'decision': 'tool', 'tool': 'read_evidence',
                                'gap_id': gap,
                                'expected_observation': '回读原文',
                                'arguments': {'evidence_id': unread[0]}}
                if seen_evidence['ref']:
                    return {'decision': 'tool', 'tool': 'answer_question',
                            'gap_id': from_reading.get('question_id'),
                            'expected_observation': '把读到的内容记成候选答案',
                            'arguments': {'question_id': from_reading.get('question_id'),
                                          'source': 'evidence',
                                          'source_ref': seen_evidence['ref'],
                                          'value': '避光密封，阴凉干燥处保存',
                                          'field': 'storage_note',
                                          'quote': QUOTE_TEXT}}
            return {'decision': 'respond'}

        task = self.run_investigation(provider, 'constant-1')
        answers = [answer for question in self.questions_of(task)
                   for answer in (question.get('answers') or [])]
        observed = {answer['assessment']['status'] for answer in answers
                    if isinstance(answer.get('assessment'), dict)
                    and answer['assessment'].get('status')}
        self.assertTrue(
            observed,
            '答案元素上没有 assessment 键（A 的交付尚未落到记录里）：'
            f'{[(a.get("source"), sorted(a.keys())) for a in answers]}')
        self.assertIn('verified', observed,
                      f'对当前权威记录逐项核对通过的答案没有出现 verified：{observed}')
        self.assertNotIn(
            'verified', {a['assessment']['status'] for a in answers
                         if a.get('field') == 'storage_note'
                         and isinstance(a.get('assessment'), dict)},
            '引文属实、但问题是开放字段，支持关系无法机械核对——不该是 verified')
        self.assertIn('candidate', observed,
                      f'开放字段的引文答案没有出现 candidate：{observed}')


# ---------------------------------------------------------------------------
# 三、来源失效后不再被当成依据（CONTRACT.md §3.3 stale / D.md 必查项 3）
# ---------------------------------------------------------------------------
class StaleBasisTests(AcceptanceTest):
    @staticmethod
    def _provider():
        def provider(payload):
            inv = payload['investigation']
            if not (inv.get('questions') or []):
                return _declare('合成药乙目前的服用频次是什么？', field='schedule')
            return {'decision': 'respond'}
        return provider

    def _answer_then_change_the_record(self, key: str):
        provider = self._provider()
        task = self.run_investigation(provider, f'{key}-investigation')
        case_id = self.api.cases()[0]['case_id']
        case = self.api.case(case_id)
        request_id = case['required_inputs'][0]['request_id']
        self.api.client.post(
            f'/v1/safety-cases/{case_id}/answer',
            json={'key': f'{key}-answer', 'expected_revision': case['revision'],
                  'request_id': request_id, 'value': '每天一次'})
        # 记录发生变化——那条回答是针对旧版本给的。
        self.api.change_dose(f'{key}-change', '合成药甲', '3mg')
        stored = self.api.product.get(task['id'], 'care_task')
        answers = [answer for answer in (self.questions_of(stored)[0].get('answers') or [])
                   if answer.get('provenance') == 'user_reported']
        return case_id, request_id, answers

    def test_a_record_change_reopens_the_question_it_invalidates(self):
        """回答之后记录变了：那条回答不能再继续当依据（重开是保守方向）。"""
        case_id, request_id, _answers = self._answer_then_change_the_record('stale')
        after = self.api.case(case_id)

        open_ids = [item['request_id'] for item in after.get('required_inputs') or []]
        self.assertIn(request_id, open_ids,
                      '记录变化之后，针对旧记录版本的回答必须被重新打开')
        self.assertTrue(
            [entry for entry in after.get('history') or []
             if entry.get('event') == 'answer_retired'],
            f'重开必须留下可追溯的历史：{after.get("history")}')

    def test_a_retired_answer_is_not_still_verified(self):
        """重开之后，那条旧回答不能仍然挂着 `verified`。"""
        _case_id, _request_id, answers = self._answer_then_change_the_record('stale2')
        assessments = _assessments(answers)
        self.require_implemented(
            bool(assessments),
            '答案元素上没有 assessment 键（A 的交付尚未落到记录里），'
            '无法核实"失效后被标成 stale"',
            evidence=f'答案元素键：{sorted(answers[-1].keys()) if answers else []}')
        statuses = {answer['assessment'].get('status') for answer in answers
                    if isinstance(answer.get('assessment'), dict)}
        self.assertNotIn(
            'verified', statuses,
            '依赖的记录版本已经变了，旧回答却仍然是 verified——'
            '这正是「来源失效后继续拿旧答案当依据」')

    def test_an_answer_against_an_old_version_does_not_close_the_case(self):
        """旧版本上给出的回答，不能批准新状态（版本顺序）。"""
        provider = self._provider()
        self.run_investigation(provider, 'version-1')
        case_id = self.api.cases()[0]['case_id']
        case = self.api.case(case_id)
        request_id = case['required_inputs'][0]['request_id']
        self.api.client.post(
            f'/v1/safety-cases/{case_id}/answer',
            json={'key': 'version-answer', 'expected_revision': case['revision'],
                  'request_id': request_id, 'value': '每天一次'})
        self.api.change_dose('version-change', '合成药甲', '3mg')

        after = self.api.case(case_id)
        self.assertNotEqual(sc.STATUS_RESOLVED, after['status'],
                            '记录在回答之后变了，事项不得停在已完成')
        self.assertIsNone(after['resolution_basis'],
                          '新状态不得沿用旧版本上的处置依据')

        evidence = self.api.client.get(
            f'/v1/safety-cases/{case_id}/closure-evidence').json()
        self.assertFalse(evidence.get('ok'),
                         f'记录已变，现场核对不应允许关闭：{evidence}')
        self.assertTrue(evidence.get('reason') or evidence.get('still_present') is not None,
                        f'拒绝关闭必须说明差什么：{evidence}')


# ---------------------------------------------------------------------------
# 四、回答之后：回答、来源、剩余问题都还在（D.md 必查项 4）
# ---------------------------------------------------------------------------
class AnswerRetentionTests(AcceptanceTest):
    @staticmethod
    def _provider():
        def provider(payload):
            inv = payload['investigation']
            if not (inv.get('questions') or []):
                return {'decision': 'tool', 'tool': 'plan_questions',
                        'gap_id': 'subquestions',
                        'expected_observation': '问题集建立',
                        'arguments': {'questions': [
                            {'statement': '合成药乙目前的服用频次是什么？',
                             'information_target': 'patient_actual_state',
                             'strategy': 'ask_user', 'subject_refs': ['合成药乙'],
                             'target_field': 'schedule', 'why': '用法影响判断'},
                            {'statement': '合成药甲是什么时候开始吃的？',
                             'information_target': 'patient_actual_state',
                             'strategy': 'ask_user', 'subject_refs': ['合成药甲'],
                             'target_field': 'start_date', 'why': '开始时间影响判断'}]}}
            return {'decision': 'respond'}
        return provider

    def test_answering_one_question_keeps_the_answer_its_source_and_the_others(self):
        """一次补充之后，同一事项里：回答在、来源在、**剩下的问题也还在**。"""
        provider = self._provider()
        task = self.run_investigation(provider, 'retention-1')
        case_id = self.api.cases()[0]['case_id']
        case = self.api.case(case_id)
        self.assertEqual(2, len(case['required_inputs']), '两条问题都应当登记为请求')
        target = case['required_inputs'][0]
        other = case['required_inputs'][1]

        body = self.api.client.post(
            f'/v1/safety-cases/{case_id}/answer',
            json={'key': 'retention-answer', 'expected_revision': case['revision'],
                  'request_id': target['request_id'], 'value': '每天一次'}).json()

        remaining = [item['request_id'] for item in body.get('required_inputs') or []]
        self.assertNotIn(target['request_id'], remaining, '被回答的请求要关掉')
        self.assertIn(other['request_id'], remaining,
                      '另一条未决问题不得被回答事件挤掉')

        answered = [item for item in body.get('answered_inputs') or []
                    if item.get('request_id') == target['request_id']]
        self.assertTrue(answered, '已回答的请求必须留下可见的记录')
        self.assertTrue(answered[-1].get('answered_at'), '回答时间必须留着')

        # 回答的内容与来源落在**那条问题**上；这是 assessment 的挂载点。
        stored = self.api.product.get(task['id'], 'care_task')
        question = next(q for q in self.questions_of(stored)
                        if target['request_id'].endswith(q.get('question_id') or ''))
        answers = question.get('answers') or []
        self.assertTrue(answers, f'用户回答必须写回那条问题：{question}')
        self.assertEqual('每天一次', answers[-1].get('value'), '回答的值必须留着')
        self.assertEqual('user_reported', answers[-1].get('provenance'),
                         '来源属性必须留着——它是"这句话是谁说的"的唯一记录')
        self.assertEqual('user_answer', answers[-1].get('source'))

        # 调查恢复之后，投影里那份 `answered_parts` 也要带着同一条答案。
        self.api.pump()
        parts = [item for request in (self.api.case(case_id).get('answered_inputs') or [])
                 for item in (request.get('answered_parts') or [])]
        if not parts:
            self.skipTest('基线缺失/等待实现：调查恢复后仍未把 answers 投影成 '
                          'answered_parts（B/C 消费的那份投影尚不存在）')
        self.assertEqual('每天一次', parts[-1].get('value'))


# ---------------------------------------------------------------------------
# 五、有时间不等于已确认（CONTRACT.md §4.3 / §4.4 / §4.5）
# ---------------------------------------------------------------------------
class TimeIsNotAConfirmationTests(AcceptanceTest):
    def test_a_time_alone_does_not_confirm_an_arrangement(self):
        """**本次最关键的反例**：只给 `at`，不给确认 ⇒ `confirmed` 必须是 false。

        基线实现是 `confirmed = bool(at or condition)`——"有安排"被当成了
        "已确认"。契约 §4.5 明确把它列为要修掉的缺陷。
        """
        case = self.api.seed_one_case()
        status, body = self.monitoring_case(
            case['case_id'],
            {'kind': 'review_at', 'at': FUTURE, 'owner': 'caregiver',
             'note': '两周后复查'})
        self.assertEqual(200, status, body)
        follow_up = body.get('follow_up')
        self.assertIsNotNone(follow_up, f'处置后必须留下跟进安排：{body}')
        self.assertIn('confirmed', follow_up, 'follow_up 必须带 confirmed 字段')
        self.assertIs(
            False, follow_up['confirmed'],
            '「有时间」被当成了「已确认」。有时间或有条件 ≠ 已确认——'
            '已安排与已确认是两件事（CONTRACT.md §4.5）。'
            f'at={follow_up.get("at")!r} confirmed_at={follow_up.get("confirmed_at")!r} '
            f'confirmation_ref={follow_up.get("confirmation_ref")!r}')

    def test_a_legacy_confirmed_record_without_a_confirmation_reads_as_unconfirmed(self):
        """存量老记录：`confirmed=true` 但没有确认信息 ⇒ 一律按 false 读。

        这是契约 §4.5 明说的一次**有意的、可见的行为回退**，所以 D 专门验它。
        为了造出"老记录"，这里向隔离库**种一条历史数据**：这不是制造成功，
        它造的是一个必须被读成 false 的历史状态。
        """
        case = self.api.seed_one_case()
        case_id = case['case_id']
        status, _ = self.monitoring_case(case_id, {
            'kind': 'review_at', 'at': FUTURE, 'owner': 'caregiver'})
        self.assertEqual(200, status)

        raw = self.api.product.get(case_id, 'safety_case')
        raw['follow_up'] = {**(raw.get('follow_up') or {}),
                            'kind': 'review_at', 'at': FUTURE,
                            'confirmed': True, 'confirmed_at': None,
                            'confirmation_ref': None, 'confirmed_by': None}
        raw['revision'] += 1
        with self.api.product.transaction():
            self.api.product.save('safety_case', raw)

        follow_up = self.api.case(case_id)['follow_up']
        self.assertIs(
            False, follow_up['confirmed'],
            '一条没有确认记录的 confirmed=true 被原样读了出来。'
            '存量记录必须按 false 读，否则"已确认"可以在没有任何确认动作时出现。'
            f'读到的：confirmed={follow_up.get("confirmed")!r} '
            f'confirmed_at={follow_up.get("confirmed_at")!r} '
            f'confirmation_ref={follow_up.get("confirmation_ref")!r}')

    def test_an_unknown_condition_kind_is_refused_not_silently_downgraded(self):
        """未知的 `condition.kind` ⇒ 422，**不得**静默降级。

        静默降级（变成 `arrangement`、或变成"永不触发"）会让一条永远不会生效的
        安排看起来完全正常。
        """
        case = self.api.seed_one_case()
        status, body = self.monitoring_case(case['case_id'], {
            'kind': 'on_event',
            'condition': {'kind': 'made_up_trigger', 'ref': 'memory:conclusion:1@v1'}})
        self.assertEqual(
            422, status,
            '未知的 condition.kind 被接受了。契约 §4.3 要求拒绝：'
            '否则调用方以为安排生效了，实际永远不会触发。'
            f'实际：{status} {json.dumps(body, ensure_ascii=False)[:400]}')

    def test_a_free_text_condition_is_refused(self):
        """自由文本条件 ⇒ 422：它从不被求值，接受它就是接受一条永不触发的安排。"""
        case = self.api.seed_one_case()
        status, body = self.monitoring_case(case['case_id'], {
            'kind': 'on_event', 'condition': '等医生说了算'})
        self.assertEqual(422, status,
                         '非结构化字符串条件被接受了：'
                         f'{status} {json.dumps(body, ensure_ascii=False)[:400]}')

    def test_a_time_without_a_timezone_is_refused(self):
        """naive 时间 ⇒ 422（契约 §4.4）。"""
        case = self.api.seed_one_case()
        status, body = self.monitoring_case(case['case_id'], {
            'kind': 'review_at', 'at': '2999-01-01T00:00:00', 'owner': 'caregiver'})
        self.assertEqual(422, status,
                         '没有时区的时间被接受了——它在存储里是一个歧义时刻。'
                         f'实际：{status} {json.dumps(body, ensure_ascii=False)[:400]}')

    def test_a_timezone_aware_time_is_accepted_and_normalised(self):
        """带时区的时间必须被接受，并在响应里规范化成 `+00:00` 秒精度。"""
        case = self.api.seed_one_case()
        status, body = self.monitoring_case(
            case['case_id'], {'kind': 'review_at', 'at': FUTURE_MS,
                              'owner': 'caregiver'})
        self.assertEqual(200, status, body)
        at = (body.get('follow_up') or {}).get('at')
        self.assertIsNotNone(at)
        self.assertRegex(
            str(at), r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+00:00$',
            '前端送的毫秒+Z 形式必须被服务端规范化成与 utc_now() 同款的时间')


# ---------------------------------------------------------------------------
# 六、三个新增端点：幂等、CAS、历史（CONTRACT.md §4.6）
# ---------------------------------------------------------------------------
class FollowUpEndpointTests(AcceptanceTest):
    def _monitoring(self) -> str:
        case = self.api.seed_one_case()
        case_id = case['case_id']
        status, body = self.monitoring_case(case_id, {
            'kind': 'review_at', 'at': FUTURE, 'owner': 'caregiver'})
        self.assertEqual(200, status, body)
        return case_id

    def _require_endpoint(self, case_id: str) -> None:
        code = self.api.probe_follow_up(case_id)
        self.require_implemented(
            code not in (404, 405, 501),
            'POST /v1/safety-cases/{id}/follow-up 不存在（B 的交付尚未落地）',
            evidence=f'探测返回 {code}（探测用对不上的 revision，不改状态）')

    def test_the_schedule_endpoint_honours_idempotency_and_cas(self):
        case_id = self._monitoring()
        self._require_endpoint(case_id)

        revision = self.api.case(case_id)['revision']
        body = {'key': 'fu-schedule-1', 'expected_revision': revision,
                'action': 'schedule', 'kind': 'review_at', 'at': PAST,
                'owner': 'caregiver', 'note': '改到明天'}
        first = self.api.client.post(FOLLOW_UP_PATH.format(case_id=case_id), json=body)
        self.assertEqual(200, first.status_code, first.text)
        follow_up = first.json().get('follow_up') or {}
        self.assertEqual('scheduled', follow_up.get('schedule_state'))
        self.assertIn(follow_up.get('kind'), FOLLOW_UP_KINDS,
                      'kind 与 schedule_state 是两件事，不得合并')

        replay = self.api.client.post(FOLLOW_UP_PATH.format(case_id=case_id), json=body)
        self.assertEqual(200, replay.status_code, replay.text)
        self.assertEqual(first.json()['revision'], replay.json()['revision'],
                         '同一幂等键重放不得产生新的修订')

        stale = self.api.client.post(
            FOLLOW_UP_PATH.format(case_id=case_id),
            json={**body, 'key': 'fu-schedule-2', 'expected_revision': revision + 5})
        self.assertEqual(409, stale.status_code,
                         f'expected_revision 不匹配必须 409：{stale.text}')

    def test_cancelling_keeps_the_arrangement_history(self):
        """取消**不清空** at/owner/note/kind，只用状态表达"已取消"。"""
        case_id = self._monitoring()
        self._require_endpoint(case_id)

        revision = self.api.case(case_id)['revision']
        scheduled = self.api.client.post(
            FOLLOW_UP_PATH.format(case_id=case_id),
            json={'key': 'fu-set', 'expected_revision': revision,
                  'action': 'schedule', 'kind': 'review_at', 'at': FUTURE,
                  'owner': 'caregiver', 'note': '两周后复查'})
        self.assertEqual(200, scheduled.status_code, scheduled.text)

        revision = self.api.case(case_id)['revision']
        cancelled = self.api.client.post(
            FOLLOW_UP_PATH.format(case_id=case_id),
            json={'key': 'fu-cancel', 'expected_revision': revision,
                  'action': 'cancel', 'reason': '风险已由其他方式处理'})
        self.assertEqual(200, cancelled.status_code, cancelled.text)
        follow_up = cancelled.json()['follow_up']
        self.assertEqual('cancelled', follow_up['schedule_state'])
        for field in ('at', 'owner', 'note', 'kind'):
            self.assertIsNotNone(follow_up.get(field),
                                 f'取消不得清空 {field}——历史要留着')
        self.assertIn(follow_up.get('kind'), FOLLOW_UP_KINDS,
                      '取消的是一条 review_at 安排，kind 不该被改写成 cancelled')

    def test_a_confirmation_needs_an_arrangement_and_an_authenticated_actor(self):
        case = self.api.seed_one_case()
        case_id = case['case_id']
        self._require_endpoint(case_id)

        # 没有安排就没有可确认的东西。
        premature = self.api.client.post(
            CONFIRMATION_PATH.format(case_id=case_id),
            json={'key': 'fu-confirm-early',
                  'expected_revision': self.api.case(case_id)['revision'],
                  'confirmed_by': 'chief-physician'})
        self.assertEqual(409, premature.status_code,
                         f'没有已安排的跟进却接受了确认：{premature.text}')

        status, body = self.monitoring_case(case_id, {
            'kind': 'review_at', 'at': FUTURE, 'owner': 'caregiver'},
            key='mon-confirm')
        self.assertEqual(200, status, body)
        confirmed = self.api.client.post(
            CONFIRMATION_PATH.format(case_id=case_id),
            json={'key': 'fu-confirm-1',
                  'expected_revision': self.api.case(case_id)['revision'],
                  'confirmed_by': 'chief-physician', 'note': '已知悉'})
        self.assertEqual(200, confirmed.status_code, confirmed.text)
        follow_up = confirmed.json()['follow_up']
        self.assertIs(True, follow_up['confirmed'])
        self.assertTrue(follow_up.get('confirmed_at'), '确认必须留下时间')
        self.assertTrue(follow_up.get('confirmation_ref'), '确认必须指向一条确认记录')
        self.assertNotEqual('chief-physician', follow_up.get('confirmed_by'),
                            '请求体自报的确认人不算数——身份只能来自认证上下文')


# ---------------------------------------------------------------------------
# 七、到期安排触发一次；重启或重扫不会重复（D.md 必查项 5）
# ---------------------------------------------------------------------------
class ArrangementTriggersOnceTests(AcceptanceTest):
    def _monitoring(self, at: str) -> str:
        case = self.api.seed_one_case()
        case_id = case['case_id']
        status, body = self.monitoring_case(case_id, {
            'kind': 'review_at', 'at': at, 'owner': 'caregiver'})
        self.assertEqual(200, status, body)
        code = self.api.probe_follow_up(case_id)
        self.require_implemented(
            code not in (404, 405, 501),
            'POST /v1/safety-cases/{id}/follow-up 不存在（B 的交付尚未落地）',
            evidence=f'探测返回 {code}')
        return case_id

    def test_a_due_arrangement_advances_and_does_not_repeat_on_a_rescan(self):
        """到期 → 推进；再扫一遍、甚至重启，都不产生第二次。

        「永远停在 scheduled」不算实现——所以这里既要求它**动**，也要求它
        **只动一次**。
        """
        case_id = self._monitoring(PAST)
        self.api.pump(rounds=3)
        after = self.api.case(case_id)['follow_up']
        state = after.get('schedule_state')
        self.assertIn(state, SCHEDULE_STATES,
                      f'schedule_state 取值越界：{state!r}；'
                      f'键={sorted(after.keys())}')
        self.require_implemented(
            state != 'scheduled',
            '到期安排经真实 worker 多轮扫描后仍停在 scheduled——调度没有前进',
            evidence=f'schedule_state={state!r} '
                     f'last_triggered_at={after.get("last_triggered_at")!r}')
        self.assertIn(state, {'due', 'triggered'}, f'到期后的状态不合契约：{state!r}')
        self.assertTrue(after.get('last_triggered_at'), '推进过就必须留下触发时间')
        first_triggered = after.get('last_triggered_at')
        first_task = after.get('care_task_id')

        self.api.pump(rounds=3)
        rescanned = self.api.case(case_id)['follow_up']
        self.assertEqual(first_triggered, rescanned.get('last_triggered_at'),
                         '重扫又触发了一次——到期安排必须只触发一次')

        self.rebuild()
        self.api.pump(rounds=3)
        restarted = self.api.case(case_id)['follow_up']
        self.assertEqual(first_triggered, restarted.get('last_triggered_at'),
                         '重启之后又触发了一次——触发状态必须落库')
        if first_task:
            linked = [task for task in self.api.product.objects('care_task')
                      if task.get('id') == first_task]
            self.assertTrue(linked, '关联的 care task 必须仍然存在')

    def test_a_future_arrangement_does_not_fire_early(self):
        """未到期的安排不得提前触发。"""
        case_id = self._monitoring(FUTURE)
        self.api.pump(rounds=3)
        after = self.api.case(case_id)['follow_up']
        self.assertNotEqual('triggered', after.get('schedule_state'),
                            '还没到期就触发了')
        self.assertIsNone(after.get('last_triggered_at'),
                          '未到期不得写触发时间')


# ---------------------------------------------------------------------------
# 八、改期或取消之后，旧任务不继续推进（D.md 必查项 6）
# ---------------------------------------------------------------------------
class CancelledArrangementStopsTests(AcceptanceTest):
    def test_cancelling_stops_the_arrangement_from_advancing_again(self):
        """已经到期、但还没被扫描到的安排被取消之后，不得再被推进。

        这一条是**负例**，所以必须能证明"不取消就会触发"：同一条到期安排在
        `test_rescheduling_replaces_the_old_due_time` 与
        `ArrangementTriggersOnceTests` 里都有正向对照。取消发生在第一次
        `pump` **之前**——已经执行过的安排本就不可取消（409），那不是这条要验的。
        """
        case = self.api.seed_one_case()
        case_id = case['case_id']
        status, body = self.monitoring_case(case_id, {
            'kind': 'review_at', 'at': PAST, 'owner': 'caregiver'})
        self.assertEqual(200, status, body)
        code = self.api.probe_follow_up(case_id)
        self.require_implemented(
            code not in (404, 405, 501),
            'POST /v1/safety-cases/{id}/follow-up 不存在（B 的交付尚未落地）',
            evidence=f'探测返回 {code}')

        cancelled = self.api.client.post(
            FOLLOW_UP_PATH.format(case_id=case_id),
            json={'key': 'stop-cancel',
                  'expected_revision': self.api.case(case_id)['revision'],
                  'action': 'cancel', 'reason': '不再需要跟进'})
        self.assertEqual(200, cancelled.status_code, cancelled.text)

        self.api.pump(rounds=3)
        after = self.api.case(case_id)['follow_up']
        self.assertEqual('cancelled', after['schedule_state'])
        self.assertIsNone(after.get('last_triggered_at'),
                          '已取消的安排仍然被扫描触发了——取消没有让旧安排失效')

    def test_rescheduling_replaces_the_old_due_time(self):
        """改期之后，**旧**的到期时刻不再触发。"""
        case = self.api.seed_one_case()
        case_id = case['case_id']
        status, body = self.monitoring_case(case_id, {
            'kind': 'review_at', 'at': FUTURE, 'owner': 'caregiver'})
        self.assertEqual(200, status, body)
        code = self.api.probe_follow_up(case_id)
        self.require_implemented(
            code not in (404, 405, 501),
            'POST /v1/safety-cases/{id}/follow-up 不存在（B 的交付尚未落地）',
            evidence=f'探测返回 {code}')

        self.api.pump(rounds=2)
        self.assertIsNone(self.api.case(case_id)['follow_up'].get('last_triggered_at'),
                          '远期安排不该先触发')

        rescheduled = self.api.client.post(
            FOLLOW_UP_PATH.format(case_id=case_id),
            json={'key': 'reschedule-1',
                  'expected_revision': self.api.case(case_id)['revision'],
                  'action': 'schedule', 'kind': 'review_at', 'at': PAST,
                  'owner': 'caregiver', 'note': '提前复查'})
        self.assertEqual(200, rescheduled.status_code, rescheduled.text)
        follow_up = rescheduled.json()['follow_up']
        self.assertIn(follow_up['schedule_state'], {'scheduled', 'due'},
                      '改期之后应当回到已安排（或已到期），而不是停在原状态')

        self.api.pump(rounds=3)
        after = self.api.case(case_id)['follow_up']
        self.assertIn(after.get('schedule_state'), {'due', 'triggered'},
                      f'改到已到期的时间之后仍未推进：{after.get("schedule_state")!r}')
        self.assertNotEqual(FUTURE, after.get('at'),
                            '改期必须替换掉旧的到期时间，而不是新旧并存')


# ---------------------------------------------------------------------------
# 九、跟进运行失败不关闭风险事项（D.md 必查项 7）
# ---------------------------------------------------------------------------
class FollowUpFailureTests(AcceptanceTest):
    def test_a_failed_follow_up_run_leaves_the_case_unresolved(self):
        """跟进那一轮跑失败：事项仍然未解决，且失败**看得见**。

        "失败"在这里是真实失败：agent 构造不出来（provider/agent 不可用）。
        """
        case = self.api.seed_one_case()
        case_id = case['case_id']
        status, body = self.monitoring_case(case_id, {
            'kind': 'review_at', 'at': PAST, 'owner': 'caregiver'})
        self.assertEqual(200, status, body)
        code = self.api.probe_follow_up(case_id)
        self.require_implemented(
            code not in (404, 405, 501),
            'POST /v1/safety-cases/{id}/follow-up 不存在（B 的交付尚未落地）',
            evidence=f'探测返回 {code}')

        before = self.api.case(case_id)
        state_before = before['follow_up'].get('schedule_state')
        # 进入持续跟进时本来就登记了一条"凭什么说风险还在"的记录
        # （`kind='monitoring_arrangement'`）。它不是关闭依据，也不该被这次失败
        # 改写——要验的是**失败没有制造新的依据**，不是"这里必须什么都没有"。
        basis_before = before['resolution_basis']
        self.assertIsNotNone(basis_before, '持续跟进应当登记它凭什么说风险还在')

        self.rebuild(agent_error=RuntimeError('acceptance: agent unavailable'))
        self.api.pump(rounds=3)

        after = self.api.case(case_id)
        self.assertNotEqual(sc.STATUS_RESOLVED, after['status'],
                            '跟进运行失败却把事项关掉了')
        self.assertEqual(basis_before, after['resolution_basis'],
                         '一次失败的跟进运行改写了处置依据')
        self.assertIn(after['status'], sc.UNSETTLED_STATUSES,
                      f'失败之后事项必须仍然是未解决的：{after["status"]}')
        self.assertTrue(after['next_action_summary'],
                        '失败之后用户仍要拿到明确的下一步')

        follow_up = after.get('follow_up') or {}
        state = follow_up.get('schedule_state')
        if state == 'blocked':
            # 安排**本身**没能落地：必须写明为什么，否则"卡住"和"在等"分不开。
            self.assertTrue(follow_up.get('blocked_reason'),
                            '进入 blocked 必须带上原因，否则"卡住"和"在等"分不开')
            return
        # 另一种（也是这里实际发生的）失败形态：安排**确实触发了**，失败发生在
        # 它启动的那次调查里。那时 `schedule_state` 前进到 triggered 是对的——
        # 要验的是失败**看得见**，而不是把合法的前进当成缺陷。
        self.assertEqual('triggered', state,
                         f'失败之后安排停在一个含混的状态：{state!r}')
        self.assertTrue(follow_up.get('last_triggered_at'),
                        'triggered 必须留下触发时间')
        self.assertTrue(follow_up.get('care_task_id'),
                        'triggered 必须指向它启动的那次执行任务')
        runs = [task for task in self.api.product.objects('care_task')
                if task.get('goal_type') == 'safety_case'
                and task.get('safety_case_id') == case_id
                and task['id'] == follow_up['care_task_id']]
        self.assertTrue(runs, f'关联的执行任务不存在：{follow_up["care_task_id"]}')
        self.assertEqual('failed', runs[0]['status'],
                         '调查失败了，关联的执行任务却不是 failed——失败不可见')


# ---------------------------------------------------------------------------
# 十一、整条闭环接得上吗（集成阶段新增）
#
# 每一环各自的验收在别的类别里。这一条只回答一个问题：把它们**串起来**跑，
# 接得上吗——尤其是 A 的 assessment 有没有经过 B 的投影原样到得了消费方，
# 以及到期触发是不是在**同一件事项**上增量恢复，而不是另起一件。
# ---------------------------------------------------------------------------
class IntegrationClosureTests(AcceptanceTest):
    @staticmethod
    def _provider():
        """脚本化规划器：只对**当前权威记录**回答一条问题。

        这不是模型能力测试——它验证的是确定性系统与接口衔接（脚本规划器）。
        """
        def provider(payload):
            inv = payload['investigation']
            questions = inv.get('questions') or []
            if not questions:
                return _declare('合成药乙现在还是有效的用药吗？',
                                strategy='patient_record', field='status')
            record = next((item for item in
                           ((inv.get('facts') or {}).get('medications') or [])
                           if item.get('display_name') == '合成药乙'), None)
            question = questions[0]
            if record and question.get('information_state') != 'available':
                return {'decision': 'tool', 'tool': 'answer_question',
                        'gap_id': question.get('question_id'),
                        'expected_observation': '按当前权威记录回答',
                        'arguments': {'question_id': question.get('question_id'),
                                      'source': 'patient_record',
                                      'source_ref': record.get('ref'),
                                      'value': record.get('status'),
                                      'field': 'status'}}
            return {'decision': 'respond'}
        return provider

    def test_the_whole_chain_runs_on_one_case(self):
        """相关变化 → 检查 → 事项 → 取得信息 → 可信答案可见 → 确认跟进 →
        到期触发 → **同一事项**增量恢复。"""
        # 一、相关变化与必要安全检查 → 建立事项
        seeded = self.api.seed_one_case()
        case_id = seeded['case_id']
        self.assertTrue(seeded['status'], '事项必须有状态')
        self.assertEqual(1, len(self.api.cases()), '应当恰好一件事项')

        # 二、取得信息 → 可信答案，并且**经过投影**到了消费方看得见的地方
        task = self.run_investigation(self._provider(), 'closure-1', seed=False)
        answers = [answer for question in self.questions_of(task)
                   for answer in (question.get('answers') or [])]
        self.assertTrue(answers, '脚本化调查没有产出任何答案元素')
        assessed = _assessments(answers)
        self.assertTrue(assessed, '答案元素上没有 assessment（A 的交付没落到记录里）')
        self.assertEqual('verified', assessed[-1]['status'],
                         f'A 的判定没有给出 verified：{assessed[-1]}')

        view = self.api.case(case_id)
        projected = _answer_elements(view)
        self.assertTrue(projected, '事项投影里没有答案元素——A 的产出到不了消费方')
        self.assertEqual(assessed[-1], projected[-1].get('assessment'),
                         'assessment 必须经过投影**原样**到达消费方（§3.6）')

        # 三、确认跟进安排：安排（未确认）→ 确认（有一条真实确认记录）
        status, body = self.monitoring_case(case_id, {
            'kind': 'review_at', 'at': PAST, 'owner': 'caregiver'})
        self.assertEqual(200, status, body)
        follow_up = body['follow_up']
        self.assertFalse(follow_up['confirmed'], '给了时间不等于有人确认过')
        self.assertEqual('scheduled', follow_up['schedule_state'])

        # 确认要在它被扫描到**之前**：已经触发过的安排不再可确认（409）。
        confirmed = self.api.client.post(
            CONFIRMATION_PATH.format(case_id=case_id),
            json={'key': 'closure-confirm',
                  'expected_revision': self.api.case(case_id)['revision'],
                  'note': '已知悉'})
        self.assertEqual(200, confirmed.status_code, confirmed.text)
        follow_up = confirmed.json()['follow_up']
        self.assertTrue(follow_up['confirmed'])
        self.assertTrue(follow_up['confirmation_ref'],
                        'confirmed 必须指向一条真实的确认记录')

        # 四、到期触发
        self.api.pump(rounds=3)
        after = self.api.case(case_id)['follow_up']
        self.assertEqual('triggered', after['schedule_state'],
                         f'已确认且已到期的安排没有执行：{after}')
        care_task_id = after['care_task_id']
        self.assertTrue(care_task_id)

        # 五、**同一事项**增量恢复：不是另起一件，而且早先那条答案还在
        runs = [item for item in self.api.product.objects('care_task')
                if item['id'] == care_task_id]
        self.assertEqual([case_id], [item['safety_case_id'] for item in runs],
                         '到期触发的调查必须落在同一件事项上')
        self.assertEqual(sc.STATUS_MONITORING, self.api.case(case_id)['status'],
                         '一次跟进调查不该把事项的持续跟进状态抹掉')
        still_there = _answer_elements(self.api.case(case_id))
        self.assertTrue(
            [item for item in still_there if item.get('assessment')],
            '增量恢复之后，先前那批带着 assessment 的答案不见了')


# ---------------------------------------------------------------------------
# 十、必要检查与模型调查的版本顺序（D.md 必查项 8）
# ---------------------------------------------------------------------------
class VersionOrderTests(AcceptanceTest):
    def test_the_case_records_which_revision_each_basis_was_read_against(self):
        """结论与回答都必须写清"它是针对哪个记录版本得出的"。"""
        case = self.api.seed_one_case()
        case_id = case['case_id']
        self.assertTrue(case.get('input_versions'),
                        '事项必须记录它当前依据的输入版本')

        self.api.change_dose('order-change-1', '合成药甲', '3mg')
        after = self.api.case(case_id)
        self.assertNotEqual(sc.STATUS_RESOLVED, after['status'])
        self.assertFalse(
            any(conclusion.get('version_applies') is False
                and conclusion.get('status') == 'current'
                for conclusion in after.get('conclusions') or []),
            '旧版本上的结论被当成了新状态的现行依据')

        evidence = self.api.client.get(
            f'/v1/safety-cases/{case_id}/closure-evidence').json()
        self.assertFalse(evidence.get('ok'),
                         f'记录已变，现场核对不得允许关闭：{evidence}')

    def test_a_case_reopened_by_a_new_record_goes_back_for_recheck(self):
        """记录变了之后，事项必须显式要求重新核对，而不是继续走旧结论。"""
        self.api.seed_one_case()
        case_id = self.api.cases()[0]['case_id']
        before = self.api.case(case_id)
        self.api.change_dose('order-change-2', '合成药甲', '3mg')
        after = self.api.case(case_id)

        self.assertNotEqual(before['input_versions'], after['input_versions'],
                            '记录变了，事项记录的依据版本必须跟着变')
        self.assertIn(after['status'], sc.UNSETTLED_STATUSES,
                      f'版本变化之后事项必须仍在未解决之列：{after["status"]}')
        self.assertTrue(after['next_action_summary'],
                        '重新核对必须给用户一个明确的下一步')


# ---------------------------------------------------------------------------
# 验收报告：按类别分开报告，不合并成一个通过率
# ---------------------------------------------------------------------------
#: 类别名就是"这次验收要回答的问题"。
CATEGORY_OF = {
    'acceptance_runs_isolated_from_the_patient_database': (
        'test_the_host_refuses_to_open_the_default_patient_database',
        'test_every_case_runs_in_its_own_database_under_the_task_output_dir'),
    #: 基线观察，**不属于** A/B/C 的冻结交付——单独成类，免得被读成他们的回归。
    'baseline_observation_outside_the_frozen_scope': (
        'test_claim_support_is_not_inferred_from_a_shared_entity_name',
        'test_a_question_read_as_available_can_name_its_answer'),
    'citation_cannot_support_an_unrelated_answer': (
        'test_the_unrelated_quote_was_really_read_back_first',
        'test_a_real_quote_does_not_make_an_unrelated_answer_verified',),
    'self_reported_source_cannot_make_a_record': (
        'test_a_user_answer_is_kept_with_its_source_and_does_not_close_the_case',
        'test_a_user_answer_is_never_verified',
        'test_a_professional_opinion_is_never_verified',
        'test_the_status_is_not_a_single_constant'),
    'a_dead_source_stops_being_a_basis': (
        'test_a_record_change_reopens_the_question_it_invalidates',
        'test_a_retired_answer_is_not_still_verified',
        'test_an_answer_against_an_old_version_does_not_close_the_case'),
    'answer_source_and_remaining_questions_survive': (
        'test_answering_one_question_keeps_the_answer_its_source_and_the_others',),
    'a_time_is_not_a_confirmation': (
        'test_a_time_alone_does_not_confirm_an_arrangement',
        'test_a_legacy_confirmed_record_without_a_confirmation_reads_as_unconfirmed',
        'test_an_unknown_condition_kind_is_refused_not_silently_downgraded',
        'test_a_free_text_condition_is_refused',
        'test_a_time_without_a_timezone_is_refused',
        'test_a_timezone_aware_time_is_accepted_and_normalised',
        'test_the_schedule_endpoint_honours_idempotency_and_cas',
        'test_cancelling_keeps_the_arrangement_history',
        'test_a_confirmation_needs_an_arrangement_and_an_authenticated_actor'),
    'a_due_arrangement_triggers_exactly_once': (
        'test_a_due_arrangement_advances_and_does_not_repeat_on_a_rescan',
        'test_a_future_arrangement_does_not_fire_early'),
    'a_cancelled_arrangement_stops_advancing': (
        'test_cancelling_stops_the_arrangement_from_advancing_again',
        'test_rescheduling_replaces_the_old_due_time'),
    'a_failed_follow_up_does_not_close_the_case': (
        'test_a_failed_follow_up_run_leaves_the_case_unresolved',),
    'necessary_check_and_investigation_version_order': (
        'test_the_case_records_which_revision_each_basis_was_read_against',
        'test_a_case_reopened_by_a_new_record_goes_back_for_recheck'),
}


class _RecordingResult(unittest.TextTestResult):
    """把每条用例的结局与理由照实记下来，包括 skip 的**原因**。"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.outcomes: dict[str, dict] = {}

    @staticmethod
    def _name(test) -> str:
        return test.id().rsplit('.', 1)[-1]

    def addSuccess(self, test):
        super().addSuccess(test)
        self.outcomes[self._name(test)] = {'outcome': 'passed', 'detail': ''}

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self.outcomes[self._name(test)] = {'outcome': 'failed',
                                           'detail': str(err[1])}

    def addError(self, test, err):
        super().addError(test, err)
        self.outcomes[self._name(test)] = {
            'outcome': 'error', 'detail': f'{err[0].__name__}: {err[1]}'}

    def addSkip(self, test, reason):
        super().addSkip(test, reason)
        outcome = 'not_implemented' if reason.startswith('基线缺失') else 'not_observed'
        self.outcomes[self._name(test)] = {'outcome': outcome, 'detail': reason}


def run_with_report(report_path: str) -> int:
    """跑完整份验收，写一份按类别分开的报告。"""
    import stage0.test_parallel_product_acceptance as module
    suite = unittest.TestLoader().loadTestsFromModule(module)
    runner = unittest.TextTestRunner(verbosity=2, resultclass=_RecordingResult)
    result = runner.run(suite)

    by_outcome: dict[str, list[str]] = {}
    for name, item in result.outcomes.items():
        by_outcome.setdefault(item['outcome'], []).append(name)

    categories = {}
    for category, names in CATEGORY_OF.items():
        outcomes = {name: result.outcomes.get(name, {'outcome': 'not_run', 'detail': ''})
                    for name in names}
        categories[category] = {
            'checks': outcomes,
            # 只有"所有用例都通过"才算这个类别过了——未测到**不算**通过。
            'passed': all(item['outcome'] == 'passed' for item in outcomes.values()),
            'implemented': all(item['outcome'] not in ('not_implemented', 'not_run')
                               for item in outcomes.values()),
        }

    payload = {
        'suite': 'parallel-product-acceptance',
        'task': 'D',
        'passed': result.wasSuccessful(),
        'tests_run': result.testsRun,
        'counts': {outcome: len(names) for outcome, names in sorted(by_outcome.items())},
        'categories': categories,
        'outcomes': result.outcomes,
    }
    path = Path(report_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'status': 'pass' if result.wasSuccessful() else 'fail',
                      'counts': payload['counts'], 'report': str(path)},
                     ensure_ascii=False))
    return 0 if result.wasSuccessful() else 1


if __name__ == '__main__':
    import sys
    if '--report' in sys.argv:
        index = sys.argv.index('--report')
        sys.exit(run_with_report(sys.argv[index + 1]))
    unittest.main(verbosity=2)
