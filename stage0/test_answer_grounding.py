"""答案可信性（A 路）：被标为"已有依据"的答案，必须关联真实、适用、支持的来源。

这一份测的不是"函数返回了什么"，而是**那条硬约束**：

    一条答案只有在**真实存在的记录**上、用**这个来源的、真的读过的片段**、
    核对到它**确实陈述了这个答案**时，才允许把问题标成"已有依据"。

所以每条用例都走**生产答案工具路径**——真实 ``ToolExecutor`` 按
``ANSWER_QUESTION_SPEC`` 校验参数 → 真实观察 → ``InvestigationState.observe``
的唯一生效点。测试**不**直接改写 ``answered`` / ``information_state``：那些是
这条路径的结果，不是它的输入。把结果直接设成通过，等于什么都没验。

离线可跑，不调用任何外部模型。
"""
from pathlib import Path
import json
import tempfile
import unittest

from stage0 import answer_grounding as ag
from stage0 import investigation as inv_mod
from stage0.agent import Observation
from stage0.harness.default_tools import ANSWER_QUESTION_SPEC
from stage0.harness.evidence import EvidenceStore
from stage0.harness.runtime import Principal, RunContext
from stage0.harness.tools import ToolExecutor
from stage0.investigation import (
    INFO_AVAILABLE, INFO_RECEIVED_UNCONFIRMED,
    STRATEGY_GENERAL_REFERENCE, STRATEGY_PATIENT_MATERIAL, STRATEGY_PATIENT_RECORD,
    TARGET_GENERAL_REFERENCE, TARGET_MATERIAL_RECORD, TARGET_PATIENT_STATE,
    InvestigationState, is_question_answered, proposal_errors,
)
from stage0.memory import MemoryStore


def _echo_handler(request):
    """生产里 ``answer_question`` 的执行器处理器：只回显看到了什么。

    采纳**不**在这里发生——它发生在调查状态的观察阶段（唯一生效点）。这条分工
    是既有的，测试照抄它，不替它决定谁生效。
    """
    return {"submitted": request.arguments, "status": "pending_adoption",
            "note": "采纳结果由调查状态在这一步给出，见同一条观察的 accepted 字段。"}


class _Rig:
    """一次安全事项调查的最小真实环境：真库、真证据库、真工具执行器。"""

    def __init__(self, *, policy='safety_case'):
        self.tmp = tempfile.TemporaryDirectory()
        self.memory = MemoryStore(Path(self.tmp.name) / 'memory.db', llm_enabled=False)
        self.evidence = EvidenceStore(self.memory.connection, scope_id='local-demo')
        self.executor = ToolExecutor(memory=self.memory)
        self.executor.register(ANSWER_QUESTION_SPEC, _echo_handler)
        self.ctx = RunContext(run_id='r1', turn_id='t1', principal=Principal())
        self.inv = InvestigationState('跟进一件药物相互作用风险安全事项。', 'local-demo')
        self.inv.policy = policy
        # `evidence_store` 是运行期绑定的实例属性（不是 dataclass 字段），
        # 回读回执的兼容路径要用它按作用域重读旧窗口。
        self.inv.evidence_store = self.evidence
        self.sync()

    def sync(self):
        """把权威记录的新状态读进调查。

        两次调用是有意的：第一次可能因**事实变了**而触发失效（那是真实行为，
        不该绕开），第二次表示"本 run 已经读到了变更后的权威记录"，于是状态
        收尾到"已核实"。
        """
        self.inv.sync_authority(self.memory)
        self.inv.authority_read = True
        self.inv.sync_authority(self.memory)

    def add_drug(self, name, *, key=None, dose='5mg'):
        medication = self.memory.apply_medication_change(
            action='add', name=name, ingredients=[], session_id='s',
            turn_id=key or name, source='caregiver', dose=dose)['medication']
        self.sync()
        return medication

    def declare(self, statement, *, target=TARGET_PATIENT_STATE, subjects=(),
                field=None, strategy=STRATEGY_PATIENT_RECORD):
        """声明一条问题——连同仍未解决的旧问题一起提交（声明是一次修订）。"""
        existing = [{'statement': q['statement'],
                     'information_target': q.get('information_target'),
                     'strategy': q.get('strategy'), 'subject_refs': q.get('subject_refs'),
                     'target_field': q.get('target_field')}
                    for q in self.inv.questions]
        errors = self.inv.accept_questions([*existing, {
            'statement': statement, 'information_target': target,
            'strategy': strategy, 'subject_refs': list(subjects),
            'target_field': field, 'why': '影响当前事项'}])
        assert errors == [], errors
        return self.inv.questions[-1]['question_id']

    # ---- 生产工具路径 ------------------------------------------------------

    def build_arguments(self, *, question_id, source, value, quote=None, source_ref=None,
                        field=None, object_ref=None):
        arguments = {'question_id': question_id, 'source': source, 'value': value}
        for key, item in (('quote', quote), ('source_ref', source_ref),
                          ('field', field), ('object_ref', object_ref)):
            if item is not None:
                arguments[key] = item
        return arguments

    def answer(self, **arguments):
        """一次 ``answer_question``：schema 校验 → 观察 → 唯一采纳生效点。

        工具 schema 是第一道闸门（模型看到的枚举里就没有被禁的来源种类）。
        被它挡下的调用不会被当成一次"采纳尝试"——它根本没进到执行器。
        """
        arguments = self.build_arguments(**arguments)
        result = self.executor.execute(self.ctx, 'answer_question', arguments, state=None)
        if not result.ok:
            error = dict(result.error or {})
            return {'accepted': False, 'stage': 'tool_schema',
                    'errors': [error.get('error_kind') or 'tool_rejected'],
                    'detail': error.get('message') or str(error)}
        observation = Observation('answer_question', '把已取得的来源落成答案', arguments,
                                  result.value, True,
                                  gap_id=arguments.get('question_id'))
        self.inv.observe(observation, self.evidence)
        return observation.result

    def adopt(self, **arguments):
        """直接调用采纳生效点。

        工具 schema 与采纳层是**两道**闸门：schema 管住模型看得到的入口，采纳层
        管住真正写状态的那一处。恢复出来的旧状态、或将来新增的第二个调用方，都
        不经过 schema——所以这里也要能挡住同一件事。
        """
        arguments = self.build_arguments(**arguments)
        question_id = arguments.pop('question_id')
        return self.inv.answer_question(question_id, **arguments)

    def guard(self, **arguments):
        """采纳**之前**的结构校验层（模型看到的第一次拒绝就在这里）。"""
        return proposal_errors(self.inv, {
            'decision': 'tool', 'tool': 'answer_question',
            'gap_id': arguments.get('question_id'), 'expected_observation': 'x',
            'arguments': arguments})

    # ---- 证据 --------------------------------------------------------------

    def capture(self, content, *, uri='label://a'):
        record = self.evidence.put(content=content, source_uri=uri, run_id='r1')
        if record.evidence_id not in self.inv.evidence_refs:
            self.inv.evidence_refs.append(record.evidence_id)
        return record.evidence_id

    def read(self, ref, *, offset=0, limit=2000):
        """走真实的 ``read_evidence`` 观察路径：回读回执**只**在这里产生。"""
        self.inv.observe(Observation(
            'read_evidence', '回读原文', {'evidence_id': ref, 'offset': offset,
                                          'limit': limit},
            self.evidence.read(ref, scope_id='local-demo', offset=offset, limit=limit)),
            self.evidence)

    # ---- 材料 --------------------------------------------------------------

    def list_material(self, ref, *, name):
        """列过索引：条目进入 `material_refs`（**不等于**读过原文）。"""
        case_id, item_id = ref.split('/', 1)
        if ref not in self.inv.material_refs:
            self.inv.material_refs.append(ref)
        self.inv.material_items[ref] = {'name': name, 'kind': None, 'current': []}

    def read_material(self, ref, *, fields, kind='same'):
        """读过原文：这是条目能作为依据的**唯一**入口。"""
        case_id, item_id = ref.split('/', 1)
        self.inv.observe(Observation(
            'read_material_item', '回读材料条目', {'case_id': case_id, 'item_id': item_id},
            {'case_id': case_id, 'item_id': item_id, 'fields': dict(fields),
             'original_fields': dict(fields), 'corrections': [], 'locations': [],
             'kind': kind, 'issues': []}),
            self.evidence)

    def close(self):
        try:
            self.memory.close()
        finally:
            self.tmp.cleanup()


def _assessment(rig, question_id):
    """这条问题上真正**存下来**的答案与它的 assessment。

    读的是持久状态里的 `question['answers']`，不是返回值：可信性存在答案的权威
    记录里，前端标签（或这一次的返回值）不能决定它。
    """
    answer = (rig.inv.question(question_id).get('answers') or [])
    return (answer[-1].get('assessment') if answer else None), answer


class SourceResolutionTests(unittest.TestCase):
    """来源种类由**真实记录**解析——模型自报的身份不算数。"""

    def setUp(self):
        self.rig = _Rig()
        self.addCleanup(self.rig.close)

    def test_the_source_kind_constants_are_the_same_on_both_sides(self):
        """两边各声明一次（避免成环），所以这条测试钉住它们不会漂。"""
        for ours, theirs in [
                (ag.SOURCE_PATIENT_RECORD, inv_mod.ANSWER_SOURCE_PATIENT_RECORD),
                (ag.SOURCE_EVIDENCE, inv_mod.ANSWER_SOURCE_EVIDENCE),
                (ag.SOURCE_MATERIAL, inv_mod.ANSWER_SOURCE_MATERIAL),
                (ag.SOURCE_USER_ANSWER, inv_mod.ANSWER_SOURCE_USER),
                (ag.SOURCE_PROFESSIONAL, inv_mod.ANSWER_SOURCE_PROFESSIONAL)]:
            self.assertEqual(ours, theirs)
        for source in inv_mod.ANSWER_SOURCES:
            self.assertIn(source, ag.SOURCE_KINDS)

    def test_a_model_cannot_declare_a_user_answer(self):
        """用户回答由提交路径写入。模型写一句"用户说过"造不出这条记录。

        （真实用户回答那条路在 test_safety_mainline_e2e 的路径二里。）
        """
        med = self.rig.add_drug('合成药甲')
        qid = self.rig.declare('合成药甲的服用频次是什么？', subjects=(med['ref'],),
                               field='schedule')
        self.assertIn('answer_source_not_model_declarable',
                      self.rig.guard(question_id=qid, source='user_answer',
                                     value='每日一次', field='schedule',
                                     source_ref=med['ref']))
        # 工具 schema 里根本没有这个取值：模型连提交都提交不出去。
        refused = self.rig.answer(question_id=qid, source='user_answer',
                                  value='每日一次', field='schedule',
                                  source_ref=med['ref'])
        self.assertFalse(refused['accepted'], refused)
        self.assertEqual('tool_schema', refused['stage'])
        # 采纳生效点自己也挡——schema 之外还有恢复出来的旧状态与别的调用方。
        direct = self.rig.adopt(question_id=qid, source='user_answer', value='每日一次',
                                field='schedule', source_ref=med['ref'])
        self.assertFalse(direct['accepted'], direct)
        self.assertEqual('source', direct['stage'])
        self.assertIn('user_answer_not_model_declarable', direct['errors'])
        question = self.rig.inv.question(qid)
        self.assertEqual([], question.get('answers') or [],
                         '拒绝的提交不留下任何答案元素')
        self.assertFalse(is_question_answered(question))

    def test_a_model_cannot_declare_a_professional_opinion(self):
        """没有真实医护服务，就没有专业确认——模拟记录也不行。"""
        med = self.rig.add_drug('合成药甲')
        qid = self.rig.declare('这个剂量是否需要调整？', subjects=(med['ref'],),
                               field='dose')
        refused = self.rig.answer(question_id=qid, source='professional', value='5mg',
                                  field='dose', source_ref='review-decision:7')
        self.assertFalse(refused['accepted'], refused)
        self.assertEqual('tool_schema', refused['stage'])
        # 一个**看起来很像真的**复核决定引用，同样解析不出真实来源——包括
        # 本地模拟工作台给出的那种：项目里没有连接真实医护服务。
        for ref in ('review-decision:applied:7', 'local-demo-simulated-reviewer', None):
            outcome = self.rig.adopt(question_id=qid, source='professional', value='5mg',
                                     field='dose', source_ref=ref)
            self.assertFalse(outcome['accepted'], outcome)
            self.assertIn('no_professional_service', outcome['errors'])
        question = self.rig.inv.question(qid)
        self.assertFalse(is_question_answered(question))
        self.assertEqual([], question.get('answers') or [])

    def test_a_source_ref_is_required_to_cite_anything(self):
        """没有引用就没有可核对的对象。省略 source_ref 不再等于免检。"""
        med = self.rig.add_drug('合成药甲')
        qid = self.rig.declare('合成药甲现在的剂量是多少？', subjects=(med['ref'],),
                               field='dose')
        # 工具 schema 把 source_ref 标成必填：模型第一次就提不出这种调用。
        refused = self.rig.answer(question_id=qid, source='patient_record', value='5mg',
                                  field='dose')
        self.assertFalse(refused['accepted'], refused)
        self.assertEqual('tool_schema', refused['stage'])
        # 采纳生效点同样要求引用——schema 之外还有恢复出来的旧状态。
        direct = self.rig.adopt(question_id=qid, source='patient_record', value='5mg',
                                field='dose')
        self.assertFalse(direct['accepted'], direct)
        self.assertIn('no_source_ref', direct['errors'])

    def test_an_unknown_source_kind_is_still_refused(self):
        med = self.rig.add_drug('合成药甲')
        qid = self.rig.declare('合成药甲现在的剂量是多少？', subjects=(med['ref'],),
                               field='dose')
        self.assertIn('unknown_answer_source',
                      self.rig.guard(question_id=qid, source='vibes', value='5mg'))


class RecordGroundingTests(unittest.TestCase):
    """精确结构化字段：对象、值、单位、状态、版本逐项核对。"""

    def setUp(self):
        self.rig = _Rig()
        self.addCleanup(self.rig.close)

    def test_a_current_authoritative_field_is_reused_without_re_approval(self):
        """合法当前字段正常复用：程序直接复用已确认的记录。"""
        med = self.rig.add_drug('合成药甲', dose='5 mg')
        qid = self.rig.declare('合成药甲现在的剂量是多少？', subjects=(med['ref'],),
                               field='dose')
        outcome = self.rig.answer(question_id=qid, source='patient_record', value='5mg',
                                  field='dose', source_ref=med['ref'])
        self.assertTrue(outcome['accepted'], outcome)
        self.assertEqual(ag.STATUS_VERIFIED, outcome['assessment_status'])
        self.assertEqual('authoritative_record', outcome['provenance'])
        assessment, answers = _assessment(self.rig, qid)
        self.assertEqual(ag.STATUS_VERIFIED, assessment['status'])
        self.assertEqual(med['ref'], assessment['source_ref'])
        self.assertIn('版本', assessment['reason'], '核对到了什么必须能回看')
        self.assertIn(med['ref'], assessment['dependency_refs'])
        self.assertTrue(is_question_answered(self.rig.inv.question(qid)))
        self.assertEqual(INFO_AVAILABLE, self.rig.inv.question(qid)['information_state'])

    def test_a_value_that_does_not_match_the_record_is_refused(self):
        med = self.rig.add_drug('合成药甲', dose='5mg')
        qid = self.rig.declare('合成药甲现在的剂量是多少？', subjects=(med['ref'],),
                               field='dose')
        outcome = self.rig.answer(question_id=qid, source='patient_record', value='20mg',
                                  field='dose', source_ref=med['ref'])
        self.assertFalse(outcome['accepted'], outcome)
        self.assertIn('record_differs', outcome['errors'])
        self.assertFalse(is_question_answered(self.rig.inv.question(qid)))

    def test_a_superseded_record_is_not_a_current_record(self):
        """旧版本 / 已停用的记录不是"当前权威值"——状态与版本一起核。"""
        med = self.rig.add_drug('合成药甲', dose='5mg')
        qid = self.rig.declare('合成药甲现在的剂量是多少？', subjects=(med['ref'],),
                               field='dose')
        # 药停了：那一行还在（按 id 照样取得出来），但已不在当前用药集合里。
        self.rig.memory.apply_medication_change(
            action='remove', name='合成药甲', ingredients=[], session_id='s',
            turn_id='stop', source='caregiver')
        self.rig.sync()
        self.assertIsNotNone(
            self.rig.memory.connection.execute(
                'SELECT id FROM medications WHERE id=?', (med['id'],)).fetchone(),
            '旧记录并没有被删掉——这正是"取得到 != 还是现在的值"')
        outcome = self.rig.answer(question_id=qid, source='patient_record', value='5mg',
                                  field='dose', source_ref=med['ref'])
        self.assertFalse(outcome['accepted'], outcome)
        self.assertIn('record_not_current', outcome['errors'])
        self.assertFalse(is_question_answered(self.rig.inv.question(qid)))

    def test_a_record_of_another_object_cannot_answer_this_question(self):
        """另一种药的值不是这条问题的答案——对象与答案的对应关系要留住。"""
        first = self.rig.add_drug('合成药甲', dose='5mg')
        other = self.rig.add_drug('合成药乙', dose='10mg')
        qid = self.rig.declare('合成药甲现在的剂量是多少？', subjects=(first['ref'],),
                               field='dose')
        outcome = self.rig.answer(question_id=qid, source='patient_record', value='10mg',
                                  field='dose', source_ref=other['ref'])
        self.assertFalse(outcome['accepted'], outcome)
        self.assertIn('object_not_in_question', outcome['errors'])
        self.assertEqual([], (self.rig.inv.question(qid).get('answers') or []))

    def test_a_field_the_record_does_not_carry_is_not_verified(self):
        """字段名对上不等于答案完成：记录里没有这个字段就无从核对。"""
        med = self.rig.add_drug('合成药甲', dose='5mg')
        qid = self.rig.declare('合成药甲现在的剂量是多少？', subjects=(med['ref'],),
                               field='dose')
        outcome = self.rig.answer(question_id=qid, source='patient_record', value='5mg',
                                  field='interaction_severity', source_ref=med['ref'])
        self.assertFalse(outcome['accepted'], outcome)
        self.assertIn('record_missing', outcome['errors'])


class MultiObjectTests(unittest.TestCase):
    """多对象问题保留对象与答案的对应关系。"""

    def setUp(self):
        self.rig = _Rig()
        self.addCleanup(self.rig.close)
        self.first = self.rig.add_drug('合成药甲', dose='5mg')
        self.second = self.rig.add_drug('合成药乙', dose='10mg')
        self.qid = self.rig.declare('两种药现在的剂量分别是多少？',
                                    subjects=(self.first['ref'], self.second['ref']),
                                    field='dose')

    def test_answering_one_object_does_not_complete_the_other(self):
        outcome = self.rig.answer(question_id=self.qid, source='patient_record',
                                  value='5mg', field='dose', source_ref=self.first['ref'])
        self.assertTrue(outcome['accepted'], outcome)
        self.assertTrue(outcome['partial'])
        self.assertEqual([f'dose@{self.second["ref"]}'], outcome['still_open'])
        question = self.rig.inv.question(self.qid)
        self.assertFalse(is_question_answered(question))
        self.assertEqual(INFO_RECEIVED_UNCONFIRMED, question['information_state'])
        self.assertEqual(1, len(question['answers']))
        self.assertEqual(self.first['ref'], question['answers'][0]['object_ref'])
        self.assertTrue(any(g.get('missing_field') == f'dose@{self.second["ref"]}'
                            for g in self.rig.inv.gaps), '还缺哪一条要说清楚')

    def test_both_objects_answered_completes_the_question(self):
        self.rig.answer(question_id=self.qid, source='patient_record', value='5mg',
                        field='dose', source_ref=self.first['ref'])
        outcome = self.rig.answer(question_id=self.qid, source='patient_record',
                                  value='10mg', field='dose', source_ref=self.second['ref'])
        self.assertTrue(outcome['accepted'], outcome)
        self.assertFalse(outcome['partial'])
        self.assertEqual([], outcome['still_open'])
        question = self.rig.inv.question(self.qid)
        self.assertTrue(is_question_answered(question))
        self.assertEqual({self.first['ref'], self.second['ref']},
                         {a['object_ref'] for a in question['answers']})

    def test_naming_one_object_while_citing_another_record_is_refused(self):
        """声明答的是甲、引的却是乙的记录：对象与答案的对应关系对不上。"""
        outcome = self.rig.answer(question_id=self.qid, source='patient_record',
                                  value='10mg', field='dose',
                                  source_ref=self.second['ref'],
                                  object_ref=self.first['ref'])
        self.assertFalse(outcome['accepted'], outcome)
        self.assertIn('object_not_in_question', outcome['errors'])
        self.assertEqual([], (self.rig.inv.question(self.qid).get('answers') or []))

    def test_a_multi_object_question_requires_naming_the_object(self):
        outcome = self.rig.answer(question_id=self.qid, source='patient_record',
                                  value='5mg', field='dose', source_ref=self.first['ref'])
        self.assertTrue(outcome['accepted'], outcome)
        # 不指明对象、也不带能对上对象的 source_ref 的第二次提交没有出处。
        guess = self.rig.answer(question_id=self.qid, source='patient_record',
                                value='10mg', field='dose',
                                source_ref='memory:medication:999@v1')
        self.assertFalse(guess['accepted'], guess)
        self.assertIn(guess['errors'][0], {'source_not_in_scope', 'object_required'})


class EvidenceGroundingTests(unittest.TestCase):
    """证据：来源存在、引文匹配、答案支持是三件事。"""

    def setUp(self):
        self.rig = _Rig()
        self.addCleanup(self.rig.close)
        # 药名要在权威药单里，子问题的对象才算"真实存在"（既有的边界）。
        self.rig.add_drug('合成药甲')
        self.ev_a = self.rig.capture('合成药甲的常用起始剂量为 5mg，每日一次。', uri='label://a')
        self.ev_b = self.rig.capture('合成药乙的最大剂量为 10mg，每日两次。', uri='label://b')
        self.qid = self.rig.declare('合成药甲的常用起始剂量是多少？',
                                    target=TARGET_GENERAL_REFERENCE, subjects=('合成药甲',),
                                    field='dose', strategy=STRATEGY_GENERAL_REFERENCE)

    def test_a_quote_that_was_never_read_back_is_refused(self):
        """搜到 != 读过。没回读过就没有回执，引文无从核对。"""
        outcome = self.rig.answer(question_id=self.qid, source='evidence', value='5mg',
                                  field='dose', source_ref=self.ev_a,
                                  quote='常用起始剂量为 5mg')
        self.assertFalse(outcome['accepted'], outcome)
        self.assertIn('source_not_read_back', outcome['errors'])
        self.assertEqual([], (self.rig.inv.question(self.qid).get('answers') or []))

    def test_a_quote_from_a_part_the_model_never_read_is_refused(self):
        """读过前 20 字，就不能引用第 40 字——校验时不替模型补读。"""
        self.rig.read(self.ev_a, offset=0, limit=20)
        outcome = self.rig.answer(question_id=self.qid, source='evidence', value='5mg',
                                  field='dose', source_ref=self.ev_a,
                                  quote='每日一次')
        self.assertFalse(outcome['accepted'], outcome)
        self.assertIn('quote_not_read_back', outcome['errors'])

    def test_a_quote_from_another_source_cannot_be_used_for_this_one(self):
        """引用 A 却拿 B 的引文：来源与片段错配，必须挡下来。"""
        self.rig.read(self.ev_a)
        self.rig.read(self.ev_b)
        outcome = self.rig.answer(question_id=self.qid, source='evidence', value='10mg',
                                  field='dose', source_ref=self.ev_a,
                                  quote='最大剂量为 10mg')
        self.assertFalse(outcome['accepted'], outcome)
        self.assertIn('quote_not_read_back', outcome['errors'])
        # 同一段引文配上它真正出自的那个来源就成立。
        ok = self.rig.answer(question_id=self.qid, source='evidence', value='10mg',
                             field='dose', source_ref=self.ev_b,
                             quote='最大剂量为 10mg')
        self.assertTrue(ok['accepted'], ok)

    def test_a_real_quote_that_does_not_state_the_answer_is_not_verified(self):
        """引文真实但答案不受支持：引文存在 != 来源支持这个答案。"""
        self.rig.read(self.ev_a)
        outcome = self.rig.answer(question_id=self.qid, source='evidence', value='20mg',
                                  field='dose', source_ref=self.ev_a,
                                  quote='合成药甲的常用起始剂量为 5mg')
        self.assertFalse(outcome['accepted'], outcome)
        self.assertEqual('support', outcome['stage'])
        self.assertIn('quote_does_not_state_value', outcome['errors'])
        self.assertFalse(is_question_answered(self.rig.inv.question(self.qid)))

    def test_a_quote_that_states_the_answer_is_verified_with_a_locator(self):
        self.rig.read(self.ev_a)
        outcome = self.rig.answer(question_id=self.qid, source='evidence', value='5mg',
                                  field='dose', source_ref=self.ev_a,
                                  quote='常用起始剂量为 5mg')
        self.assertTrue(outcome['accepted'], outcome)
        self.assertEqual(ag.STATUS_VERIFIED, outcome['assessment_status'])
        assessment, _ = _assessment(self.rig, self.qid)
        self.assertEqual(self.ev_a, assessment['source_ref'])
        self.assertTrue(str(assessment['locator']).startswith(self.ev_a))
        self.assertIn('5mg', assessment['reason'])

    def test_free_text_support_is_saved_as_a_candidate_not_verified(self):
        """自由文本无法可靠验证支持关系：记成解释候选，绝不写成 verified。"""
        self.rig.read(self.ev_a)
        qid = self.rig.declare('合成药甲怎么吃？', target=TARGET_GENERAL_REFERENCE,
                               subjects=('合成药甲',), strategy=STRATEGY_GENERAL_REFERENCE)
        outcome = self.rig.answer(question_id=qid, source='evidence',
                                  value='随餐服用并观察有无出血',
                                  source_ref=self.ev_a, quote='常用起始剂量为 5mg')
        self.assertTrue(outcome['accepted'], outcome)
        self.assertEqual(ag.STATUS_CANDIDATE, outcome['assessment_status'])
        question = self.rig.inv.question(qid)
        self.assertFalse(is_question_answered(question),
                         '候选依据不等于已有依据')
        self.assertEqual(INFO_RECEIVED_UNCONFIRMED, question['information_state'])
        assessment = question['answers'][-1]['assessment']
        self.assertEqual(ag.STATUS_CANDIDATE, assessment['status'])
        self.assertEqual(self.ev_a, assessment['source_ref'],
                         '依据指向哪一条来源，答案上必须写清楚')
        # 这条问题的对象是**药名**，没有版本化引用可依赖；dependency_refs 只装
        # §1.4 那种 `<layer>:<kind>:<id>@<version>` 的引用，不拿证据 id 凑数。
        self.assertEqual([], assessment['dependency_refs'])
        self.assertTrue(all(ag.parse_versioned_ref(ref)
                            for ref in assessment['dependency_refs']))


class MaterialGroundingTests(unittest.TestCase):
    """材料：列过索引 != 读过原文；读到了也只按**记录真的记了什么**核对。"""

    def setUp(self):
        self.rig = _Rig()
        self.addCleanup(self.rig.close)
        self.rig.add_drug('合成药甲')
        self.ref = 'case-1/m1'
        self.rig.list_material(self.ref, name='合成药甲')
        self.qid = self.rig.declare('材料里记的剂量是多少？',
                                    target=TARGET_MATERIAL_RECORD, subjects=('合成药甲',),
                                    field='dose', strategy=STRATEGY_PATIENT_MATERIAL)

    def test_a_material_that_was_only_listed_is_not_a_source(self):
        """列过索引的条目不是依据——它在两个清单里的位置决定这一点。"""
        outcome = self.rig.answer(question_id=self.qid, source='material', value='5mg',
                                  field='dose', source_ref=self.ref)
        self.assertFalse(outcome['accepted'], outcome)
        self.assertIn('material_not_read', outcome['errors'])
        self.assertEqual([], (self.rig.inv.question(self.qid).get('answers') or []))

    def test_a_read_material_entry_with_a_matching_field_is_verified(self):
        self.rig.read_material(self.ref, fields={'name': '合成药甲', 'dose': '5mg'})
        outcome = self.rig.answer(question_id=self.qid, source='material', value='5mg',
                                  field='dose', source_ref=self.ref)
        self.assertTrue(outcome['accepted'], outcome)
        self.assertEqual(ag.STATUS_VERIFIED, outcome['assessment_status'])
        assessment, _ = _assessment(self.rig, self.qid)
        self.assertEqual(self.ref, assessment['source_ref'])
        self.assertIn('材料条目', assessment['reason'])

    def test_a_read_material_entry_that_records_another_value_is_refused(self):
        self.rig.read_material(self.ref, fields={'name': '合成药甲', 'dose': '10mg'})
        outcome = self.rig.answer(question_id=self.qid, source='material', value='5mg',
                                  field='dose', source_ref=self.ref)
        self.assertFalse(outcome['accepted'], outcome)
        self.assertIn('record_differs', outcome['errors'])

    def test_a_read_material_entry_is_a_candidate_for_free_text(self):
        """材料是 caregiver 尚未确认的候选，不是患者事实。"""
        self.rig.read_material(self.ref, fields={'name': '合成药甲', 'note': '随餐'})
        qid = self.rig.declare('材料上还写了什么？', target=TARGET_MATERIAL_RECORD,
                               subjects=('合成药甲',), strategy=STRATEGY_PATIENT_MATERIAL)
        outcome = self.rig.answer(question_id=qid, source='material', value='随餐服用',
                                  source_ref=self.ref)
        self.assertTrue(outcome['accepted'], outcome)
        self.assertEqual(ag.STATUS_CANDIDATE, outcome['assessment_status'])
        self.assertFalse(is_question_answered(self.rig.inv.question(qid)))


class AssessmentContractTests(unittest.TestCase):
    """§3.2 / §3.4：形状、必填、以及"缺失即未核实"。"""

    def setUp(self):
        self.rig = _Rig()
        self.addCleanup(self.rig.close)

    def test_the_assessment_has_exactly_the_frozen_shape(self):
        med = self.rig.add_drug('合成药甲', dose='5mg')
        qid = self.rig.declare('合成药甲现在的剂量是多少？', subjects=(med['ref'],),
                               field='dose')
        self.rig.answer(question_id=qid, source='patient_record', value='5mg',
                        field='dose', source_ref=med['ref'])
        assessment = self.rig.inv.question(qid)['answers'][-1]['assessment']
        self.assertEqual({'status', 'reason', 'source_ref', 'locator', 'dependency_refs'},
                         set(assessment))
        self.assertIn(assessment['status'], ag.ASSESSMENT_STATUSES)
        self.assertIsInstance(assessment['reason'], str)
        self.assertTrue(assessment['reason'].strip())
        self.assertNotIn(assessment['reason'].strip().lower(), {'ok', '通过', 'pass'})
        self.assertIsInstance(assessment['dependency_refs'], list)
        self.assertTrue(all(isinstance(ref, str) for ref in assessment['dependency_refs']))

    def test_a_historical_answer_without_an_assessment_is_not_given_a_default(self):
        """缺失即未核实：老答案读出来还是"没有 assessment"，不补、不升级。"""
        legacy = {'value': '5mg', 'field': 'dose', 'source': 'patient_record',
                  'provenance': 'authoritative_record'}
        self.assertIsNone(ag.assessment_of(legacy))
        self.assertIsNone(ag.assessment_status(legacy))
        self.assertEqual({}, ag.revalidate(legacy.get('assessment'), source_available=False))

    def test_the_verified_meaning_boundary_travels_with_the_status(self):
        """`verified` 的含义边界写死在实现里，供 UI 抄，不让各处重新解释。"""
        self.assertIn('不表示整体用药安全', ag.VERIFIED_MEANING)
        self.assertIn('专业医疗判断', ag.VERIFIED_MEANING)


class DependencyRetractionTests(unittest.TestCase):
    """§3.6 / 需求 6：给 B 的依赖校验与失效接口。全部纯函数、幂等、只降不升。"""

    def setUp(self):
        self.rig = _Rig()
        self.addCleanup(self.rig.close)
        self.med = self.rig.add_drug('合成药甲', dose='5mg')
        qid = self.rig.declare('合成药甲现在的剂量是多少？', subjects=(self.med['ref'],),
                               field='dose')
        self.rig.answer(question_id=qid, source='patient_record', value='5mg',
                        field='dose', source_ref=self.med['ref'])
        self.verified = dict(self.rig.inv.question(qid)['answers'][-1]['assessment'])

    def test_versions_from_a_snapshot_are_the_current_versions(self):
        versions = ag.versions_from_snapshot(self.rig.inv.facts['medications'])
        self.assertEqual({f'memory:medication:{self.med["id"]}': self.med['version']},
                         versions)

    def test_an_unchanged_dependency_stays_current(self):
        versions = ag.versions_from_snapshot(self.rig.inv.facts['medications'])
        state = ag.dependency_state(self.verified['dependency_refs'],
                                    current_version_of=versions)
        self.assertEqual('current', state['state'])
        self.assertEqual([], state['changed'])
        self.assertEqual(self.verified,
                         ag.revalidate(self.verified, current_version_of=versions))

    def test_a_changed_dependency_retires_the_assessment_to_stale(self):
        versions = ag.versions_from_snapshot(self.rig.inv.facts['medications'])
        prefix = next(iter(versions))
        retired = ag.revalidate(self.verified, current_version_of={prefix: 99})
        self.assertEqual(ag.STATUS_STALE, retired['status'])
        self.assertIn(prefix, retired['reason'])
        self.assertNotEqual(ag.STATUS_VERIFIED, retired['status'])
        # 幂等：同样的输入再来一次，结果一样。
        self.assertEqual(retired, ag.revalidate(retired, current_version_of={prefix: 99}))

    def test_a_vanished_source_withdraws_the_assessment(self):
        withdrawn = ag.revalidate(self.verified, source_available=False)
        self.assertEqual(ag.STATUS_UNSUPPORTED, withdrawn['status'])
        self.assertIsNone(withdrawn['source_ref'])
        self.assertEqual(withdrawn, ag.revalidate(self.verified, source_available=False))

    def test_an_unknown_current_version_is_not_treated_as_current(self):
        state = ag.dependency_state(self.verified['dependency_refs'],
                                    current_version_of={})
        self.assertEqual('unknown', state['state'])
        self.assertNotEqual('current', state['state'])

    def test_nothing_here_ever_upgrades_a_status(self):
        """失效接口只会降级：升级只能由一次真实核对动作产生。"""
        for start in (ag.STATUS_CANDIDATE, ag.STATUS_UNSUPPORTED, ag.STATUS_STALE):
            lowered = ag.retire({'status': start, 'reason': 'x', 'source_ref': 'r',
                                 'locator': None, 'dependency_refs': []},
                                changed_refs=['memory:medication:1'])
            self.assertEqual(ag.STATUS_STALE, lowered['status'])
            self.assertNotEqual(ag.STATUS_VERIFIED, lowered['status'])


class ReadReceiptTests(unittest.TestCase):
    """回读回执本身：不跨来源拼接、不跨未读区间拼接。"""

    def test_two_sources_are_never_joined_into_one_text(self):
        ledger = ag.ReadLedger()
        ledger.record('ev-a', 0, '前半句来自 A')
        ledger.record('ev-b', 0, '后半句来自 B')
        self.assertTrue(ledger.quote_in('ev-a', '前半句来自 A'))
        self.assertFalse(ledger.quote_in('ev-a', '后半句来自 B'))
        self.assertFalse(ledger.quote_in('ev-a', '前半句来自 A后半句来自 B'),
                         '拼接会让 A 的引文配上 B 的原文')

    def test_gaps_between_reads_are_not_bridged(self):
        """读过 [0,5) 与 [10,15)，中间没读过——引文不能跨过那段。"""
        ledger = ag.ReadLedger()
        ledger.record('ev-a', 0, '01234')
        ledger.record('ev-a', 10, '56789')
        self.assertEqual(('01234', '56789'), ledger.segments('ev-a'))
        self.assertFalse(ledger.quote_in('ev-a', '450'))

    def test_contiguous_reads_are_merged(self):
        ledger = ag.ReadLedger()
        ledger.record('ev-a', 0, '01234')
        ledger.record('ev-a', 5, '56789')
        self.assertEqual(('0123456789',), ledger.segments('ev-a'))
        self.assertTrue(ledger.quote_in('ev-a', '3456'))

    def test_an_empty_quote_matches_nothing(self):
        ledger = ag.ReadLedger([ag.ReadWindow('ev-a', 0, '随便什么原文')])
        self.assertFalse(ledger.quote_in('ev-a', ''))
        self.assertFalse(ledger.quote_in('ev-a', '   '))
        self.assertFalse(ledger.quote_in('ev-a', None))

    def test_receipts_survive_a_round_trip_through_persistence(self):
        """跨会话复用：窗口随状态持久化，恢复后仍是同一份回执。"""
        ledger = ag.ReadLedger([ag.ReadWindow('ev-a', 3, '读过的那一段')])
        restored = ag.ReadLedger.from_list(ledger.to_list())
        self.assertEqual(ledger.segments('ev-a'), restored.segments('ev-a'))
        self.assertEqual(ledger.to_list(), json.loads(json.dumps(restored.to_list())))

    def test_a_restored_investigation_keeps_its_read_windows(self):
        rig = _Rig()
        self.addCleanup(rig.close)
        ev = rig.capture('合成药甲的常用起始剂量为 5mg。')
        rig.read(ev)
        restored = InvestigationState.restore(rig.inv.to_dict(), 'local-demo')
        self.assertEqual(rig.inv.read_windows, restored.read_windows)
        self.assertTrue(restored._read_ledger().quote_in(ev, '常用起始剂量为 5mg'))

    def test_an_old_state_with_only_a_read_ref_list_is_still_usable(self):
        """滚动升级前的状态只存了 read_refs 名单：按旧口径恢复成整篇窗口。"""
        rig = _Rig()
        self.addCleanup(rig.close)
        ev = rig.capture('合成药甲的常用起始剂量为 5mg。')
        rig.inv.read_refs = [ev]
        rig.inv.read_windows = []
        self.assertTrue(rig.inv._read_ledger().quote_in(ev, '常用起始剂量为 5mg'))


class AdoptionBoundaryTests(unittest.TestCase):
    """需求 7：答案采纳不关闭 SafetyCase，也不改动药物事实。"""

    def setUp(self):
        self.rig = _Rig()
        self.addCleanup(self.rig.close)

    def test_taking_an_answer_does_not_touch_medication_facts(self):
        med = self.rig.add_drug('合成药甲', dose='5mg')
        before = [dict(row) for row in self.rig.memory.connection.execute(
            'SELECT * FROM medications ORDER BY id')]
        qid = self.rig.declare('合成药甲现在的剂量是多少？', subjects=(med['ref'],),
                               field='dose')
        self.rig.answer(question_id=qid, source='patient_record', value='5mg',
                        field='dose', source_ref=med['ref'])
        after = [dict(row) for row in self.rig.memory.connection.execute(
            'SELECT * FROM medications ORDER BY id')]
        self.assertEqual(before, after, '采纳答案不是一条改药的通道')

    def test_no_answer_outcome_claims_to_have_closed_anything(self):
        """返回值里没有"关闭事项"这类键：可信性只落在答案的 assessment 上。"""
        med = self.rig.add_drug('合成药甲', dose='5mg')
        qid = self.rig.declare('合成药甲现在的剂量是多少？', subjects=(med['ref'],),
                               field='dose')
        outcome = self.rig.answer(question_id=qid, source='patient_record', value='5mg',
                                  field='dose', source_ref=med['ref'])
        self.assertEqual(
            {'accepted', 'partial', 'answered', 'still_open', 'assessment',
             'assessment_status', 'provenance', 'detail', 'question', 'allowed_tools'},
            set(outcome))


if __name__ == '__main__':
    unittest.main()
