"""完整用药变更流程：自然语言描述 → 待确认候选 → 补问 → 确认 → 正式记录 →
必要安全检查 → 相关事项更新 → 回访继续。

走的都是**真实入口**：真实的 HTTP 端点、真实的 worker、真实的候选与确认通道，
隔离库，不碰真实患者记录。**不直接写数据库最终状态制造通过。**

理解器是**脚本化**的（`_ScriptedInterpreter`）：这一套验的是机制——可核对性校验、
歧义降级、候选生成、原子写入、冲突、阶段与纠错历史、事项与回访的承接。模型自己
能不能把一句话读成这个样子，属于有限真实验收（`scripts/medication-change-live-
acceptance.py`），**不能**拿这里的结果记成真实模型自主成功。
"""
from __future__ import annotations

import pathlib
import shutil
import sqlite3
import tempfile
import unittest

from stage0 import change_notes as cn
from stage0 import review_visits as rv
from stage0 import safety_cases as sc
from stage0 import safety_checks
from stage0.memory import MemoryStore, MedicationWriteConflict
from stage0.product import ProductError
from stage0.test_review_visit_flow import OUTPUT_ROOT, _Host


class _ScriptedInterpreter:
    """脚本化的理解器。`plan` 是 ``(text, context) -> reading``。"""

    def __init__(self, plan=None):
        self.plan = plan
        self.reads: list[dict] = []
        self.mode = 'ok'
        self.error = '脚本化的模型故障'

    def available(self):
        if self.mode == 'unavailable':
            return False, '测试：没有可用的模型配置'
        return True, None

    def config(self):
        return {'provider': 'scripted', 'model': 'scripted-v1'}

    def read(self, *, text, context, hint=None):
        self.reads.append({'text': text, 'context': context, 'hint': hint})
        if self.mode == 'error':
            raise RuntimeError(self.error)
        reading = self.plan(text, context) if callable(self.plan) else self.plan
        return reading, {'calls': 1, 'tokens': 120, 'usage_unknown': False,
                         'quality': 'actual'}


def _item(when, *, operation='none', name='', quote='', field='none', value='',
          time_text=None, precision='unknown', normalised=None, uncertain=(),
          denies='', denies_quote='', group_role='none', overlap='unstated'):
    return {'when': when, 'operation': operation, 'drug_name': name, 'drug_quote': quote,
            'field': field, 'value': value, 'quote': quote or name,
            'time_text': time_text, 'time_normalised': normalised,
            'time_precision': precision, 'uncertain': list(uncertain),
            'denies_name': denies, 'denies_quote': denies_quote,
            'group_role': group_role, 'reported_overlap': overlap}


def _reading(*items, question='', unsupported=(), summary='脚本化理解'):
    return {'summary': summary, 'items': list(items), 'question': question,
            'unsupported': list(unsupported)}


class _ChangeFlow(unittest.TestCase):
    """一件真实事项 + 一次真实回访 + 一个脚本化理解器。"""

    def setUp(self):
        self.directory = OUTPUT_ROOT / 'medication-change' / self.id().rsplit('.', 1)[-1]
        if self.directory.exists():
            shutil.rmtree(self.directory, ignore_errors=True)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.addCleanup(shutil.rmtree, self.directory, True)
        self.interpreter = _ScriptedInterpreter(self.plan_reading)
        self.api = self._build()
        self.case_id = self.api.seed_one_case()['case_id']
        self.visit_id = self.start_visit()

    def _build(self) -> _Host:
        # 规划器也是脚本化的：这一套验的不是"模型会不会选中某一步"，让调查正常
        # 收尾即可。真实模型那一轮在 scripts/medication-change-live-acceptance.py。
        return _Host(self.directory / 'host',
                     proposal_provider=lambda payload: {'decision': 'respond'},
                     change_note_interpreter=self.interpreter)

    def tearDown(self):
        self.api.close()

    # ---- 装置 -------------------------------------------------------------
    def plan_reading(self, text, context):        # pragma: no cover - 子类覆写
        return _reading()

    def visits(self) -> rv.ReviewVisitStore:
        return rv.ReviewVisitStore(self.api.product)

    def store(self) -> sc.SafetyCaseStore:
        return sc.SafetyCaseStore(self.api.product)

    def start_visit(self, key: str = 'visit-change') -> str:
        view = self.api.start_visit(self.case_id, key=key)
        return view['visit']['visit_id']

    def reboot(self, plan=None):
        """换掉宿主、保留同一个数据库——这就是"重启"。"""
        self.api.close()
        self.interpreter = _ScriptedInterpreter(plan or self.plan_reading)
        self.api = self._build()
        return self.api

    def submit(self, text, *, key='note-1', hint=None):
        response = self.api.client.post(
            f'/v1/safety-cases/{self.case_id}/visits/{self.visit_id}/notes',
            json={'key': key, 'text': text, 'speech_act': hint})
        assert response.status_code == 200, response.text
        return response.json()

    def notes(self) -> list[dict]:
        response = self.api.client.get(
            f'/v1/safety-cases/{self.case_id}/visits/{self.visit_id}/notes')
        assert response.status_code == 200, response.text
        return response.json()['items']

    def case(self) -> dict:
        return self.api.case(self.case_id)

    def pending(self) -> list[dict]:
        return self.case()['visit']['pending_candidates']

    def medications(self) -> dict[str, dict]:
        rows = self.api.product.memory.connection.execute(
            "SELECT * FROM medications ORDER BY id").fetchall()
        return {row['display_name']: dict(row) for row in rows}

    def confirm(self, candidate_id, *, key='confirm-1'):
        return self.api.client.post(
            f'/v1/safety-cases/{self.case_id}/visits/{self.visit_id}'
            f'/candidates/{candidate_id}/confirm', json={'key': key})

    def retry(self, note_id, *, key='retry-1'):
        return self.api.client.post(
            f'/v1/safety-cases/{self.case_id}/visits/{self.visit_id}'
            f'/notes/{note_id}/retry', json={'key': key})


# ---------------------------------------------------------------------------
class NewMedicationTests(_ChangeFlow):
    """新增一种药：进入当前记录，并触发必要检查。"""

    def plan_reading(self, text, context):
        return _reading(_item('occurred', operation='add', name='合成药丙',
                              quote='开始吃合成药丙了', field='dose', value='2mg',
                              time_text='今天', precision='day'))

    def test_a_new_medication_enters_the_record_and_queues_a_check(self):
        before_checks = dict(safety_checks.pending(self.api.store))
        self.submit('今天开始吃合成药丙了，2mg')
        pending = self.pending()
        self.assertEqual(1, len(pending), pending)
        candidate = pending[0]
        self.assertEqual(rv.CANDIDATE_ADD, candidate['operation'])
        self.assertEqual('合成药丙', candidate['target']['name'])
        self.assertIsNone(candidate['target']['record_id'], '新增不该指向一条已有记录')
        self.assertEqual('model_proposed', candidate['source'])
        self.assertNotIn('合成药丙', self.medications(), '候选未经确认就改了权威记录')

        response = self.confirm(candidate['id'])
        self.assertEqual(200, response.status_code, response.text)
        rows = self.medications()
        self.assertIn('合成药丙', rows)
        self.assertEqual('active', rows['合成药丙']['status'])
        self.assertEqual('2mg', rows['合成药丙']['dose'])
        self.assertEqual('add', rows['合成药丙']['operation'])
        self.assertEqual(rows['合成药丙']['id'], rows['合成药丙']['episode_id'],
                         '新增应开启一个新阶段')
        self.assertGreater(safety_checks.pending(self.api.store)['unfinished'],
                           before_checks.get('unfinished', 0),
                           '确认用药变更之后，必要检查没有重新排队')


class StopMedicationTests(_ChangeFlow):
    """停用：记录与历史都正确，且**不写一个没有来源的停药时间**。"""

    def plan_reading(self, text, context):
        return _reading(_item('occurred', operation='remove', name='合成药甲',
                              quote=text, time_text='上周', precision='week'))

    def test_a_stop_keeps_the_record_and_the_history_honest(self):
        target = self.medications()['合成药甲']
        self.submit('上周已经停了合成药甲')
        candidate = self.pending()[0]
        self.assertEqual(rv.CANDIDATE_STOP, candidate['operation'])
        self.assertEqual('reported_vague', candidate['occurred']['basis'],
                         '"上周"不是一个时间戳，不该被标准化成某一天')
        self.assertIsNone(candidate['occurred']['value'])
        self.assertEqual('active', self.medications()['合成药甲']['status'],
                         '候选未经确认就停了药')

        response = self.confirm(candidate['id'])
        self.assertEqual(200, response.status_code, response.text)
        row = self.medications()['合成药甲']
        self.assertEqual('stopped', row['status'])
        self.assertEqual('add', row['operation'],
                         '停用是原地更新，不该覆盖这一行原本的"开始"事件')
        self.assertIsNotNone(row['start_at'], '"服药开始"必须仍然查得到')
        self.assertEqual('reported_vague', row['end_at_basis'])
        self.assertIsNone(row['end_at'],
                          '未知的实际停药时间不能写 now 再当作实际发生时间')
        self.assertEqual('上周', row['time_text'], '用户的原话要留档')
        self.assertEqual('active', self.medications()['合成药乙']['status'],
                         '其它药不该被这次停用波及')

    def test_a_stop_time_reported_exactly_is_recorded_with_its_source(self):
        self.interpreter.plan = lambda text, context: _reading(_item(
            'occurred', operation='remove', name='合成药甲', quote=text,
            time_text='2026-09-10', precision='day', normalised='2026-09-10'))
        self.submit('9月10号就停了合成药甲')
        self.confirm(self.pending()[0]['id'])
        row = self.medications()['合成药甲']
        self.assertEqual('reported', row['end_at_basis'])
        self.assertTrue(str(row['end_at']).startswith('2026-09-10'), row['end_at'])


class ResumeTests(_ChangeFlow):
    """恢复服用：形成**新的阶段**，并且链接到那条具体的停用记录。"""

    def plan_reading(self, text, context):
        return _reading(_item('occurred', operation='resume', name='合成药甲',
                              quote=text, field='dose', value='5mg'))

    def test_a_resume_opens_a_new_phase_linked_to_that_stop(self):
        first = self.medications()['合成药甲']
        self.interpreter.plan = lambda text, context: _reading(_item(
            'occurred', operation='remove', name='合成药甲', quote=text))
        self.submit('先停了合成药甲', key='note-stop')
        self.confirm(self.pending()[0]['id'], key='confirm-stop')
        stopped = self.medications()['合成药甲']
        self.assertEqual('stopped', stopped['status'])

        self.interpreter.plan = self.plan_reading
        self.submit('又接着吃合成药甲了，5mg', key='note-resume')
        resumed = [item for item in self.pending()
                   if item['operation'] == rv.CANDIDATE_RESUME]
        self.assertTrue(resumed, self.pending())
        self.assertEqual(stopped['id'], resumed[0]['target']['record_id'],
                         '恢复必须指向**具体**那条停用记录')
        self.assertEqual(stopped['version'], resumed[0]['target']['record_version'],
                         '而且要带上是**哪一版**——名称不足以当写入身份')

        response = self.confirm(resumed[0]['id'], key='confirm-resume')
        self.assertEqual(200, response.status_code, response.text)
        active = [item for item in self.api.product.memory.current_medications()
                  if item['display_name'] == '合成药甲']
        self.assertEqual(1, len(active), active)
        self.assertEqual(stopped['id'], active[0]['predecessor_id'],
                         '新阶段要指回那条停用记录')
        self.assertNotEqual(stopped['episode_id'], active[0]['episode_id'],
                            '真实停用之后恢复是**新阶段**')
        self.assertEqual('resume', active[0]['operation'])


class CorrectionTests(_ChangeFlow):
    """记录纠错：不冒充一次新的实际用药事件，并回到原来那个阶段。"""

    def plan_reading(self, text, context):
        return _reading(_item('correction', operation='none', name='合成药甲',
                              quote=text))

    def test_a_wrongly_recorded_stop_is_corrected_back_into_the_original_phase(self):
        original = self.medications()['合成药甲']
        self.interpreter.plan = lambda text, context: _reading(_item(
            'occurred', operation='remove', name='合成药甲', quote=text))
        self.submit('停了合成药甲', key='note-stop')
        self.confirm(self.pending()[0]['id'], key='confirm-stop')
        self.assertEqual('stopped', self.medications()['合成药甲']['status'])

        self.interpreter.plan = self.plan_reading
        self.submit('之前登记停用是填错了', key='note-fix')
        fixing = [item for item in self.pending()
                  if item['operation'] == rv.CANDIDATE_CORRECTION]
        self.assertTrue(fixing, self.pending())
        self.assertEqual(original['id'], fixing[0]['target']['record_id'])
        response = self.confirm(fixing[0]['id'], key='confirm-fix')
        self.assertEqual(200, response.status_code, response.text)

        row = self.medications()['合成药甲']
        self.assertEqual('active', row['status'])
        self.assertEqual('correction', row['operation'])
        self.assertEqual(original['episode_id'], row['episode_id'],
                         '纠错要回到**被纠正的那一行所属的阶段**，不是新阶段')
        self.assertEqual(original['id'], row['corrects_id'])
        self.assertEqual(original['start_at'], row['start_at'],
                         '纠错不等于今天重新开始服用')
        # 原错误记录仍在，而且看得出被纠正了。
        history = self.api.product.memory.connection.execute(
            "SELECT status, end_at, operation FROM medications WHERE id=?",
            (original['id'],)).fetchone()
        self.assertEqual('stopped', history['status'], '原始历史不被覆盖来隐藏纠正')

    def test_correcting_a_stop_that_has_a_real_resume_is_refused(self):
        """已有后继真实记录时纠正旧记录：显式冲突，**零写入**。"""
        original = self.medications()['合成药甲']
        self.interpreter.plan = lambda text, context: _reading(_item(
            'occurred', operation='remove', name='合成药甲', quote=text))
        self.submit('停了合成药甲', key='note-stop')
        self.confirm(self.pending()[0]['id'], key='confirm-stop')

        self.interpreter.plan = lambda text, context: _reading(_item(
            'occurred', operation='resume', name='合成药甲', quote=text))
        self.submit('又接着吃了', key='note-resume')
        resumed = [item for item in self.pending()
                   if item['operation'] == rv.CANDIDATE_RESUME][0]
        response = self.confirm(resumed['id'], key='confirm-resume')
        self.assertEqual(200, response.status_code, response.text)
        active = [item for item in self.api.product.memory.current_medications()
                  if item['display_name'] == '合成药甲']
        self.assertEqual(1, len(active), active)
        real_phase = active[0]['episode_id']

        self.interpreter.plan = self.plan_reading
        self.submit('之前登记停用是填错了', key='note-fix')
        fixing = [item for item in self.pending()
                  if item['operation'] == rv.CANDIDATE_CORRECTION][0]
        response = self.confirm(fixing['id'], key='confirm-fix')
        self.assertEqual(409, response.status_code, response.text)
        active_after = [item for item in self.api.product.memory.current_medications()
                        if item['display_name'] == '合成药甲']
        self.assertEqual(1, len(active_after),
                         '纠错不该造出第二条在用的记录')
        self.assertEqual(real_phase, active_after[0]['episode_id'],
                         '那段真实恢复记录不能被一次历史纠错悄悄合并掉')


class SwitchTests(_ChangeFlow):
    """换药：一组有关联的变更，各自说明自己发生了什么。"""

    def plan_reading(self, text, context):
        return _reading(
            _item('occurred', operation='remove', name='合成药甲', quote='旧药已经停了',
                  group_role='replace_from', overlap='unstated'),
            _item('planned', operation='add', name='合成药丙', quote='新药打算明天开始',
                  group_role='replace_to', overlap='unstated'))

    def test_a_half_done_switch_is_not_shown_as_a_finished_one(self):
        self.submit('旧药已经停了，新药打算明天开始')
        pending = self.pending()
        self.assertEqual(1, len(pending),
                         '计划不该进入可执行的确认列表')
        self.assertEqual(rv.CANDIDATE_STOP, pending[0]['operation'])
        note = self.notes()[-1]
        self.assertEqual('interpreted', note['status'])
        self.assertEqual(1, len(note['plans']), note['plans'])
        self.assertEqual('add', note['plans'][0]['operation'])

        self.confirm(pending[0]['id'], key='confirm-half')
        group_id = (pending[0].get('group') or {}).get('id')
        self.assertTrue(group_id)
        statements = [entry['text'] for entry in self.case()['visit']['result']['groups'][0]
                      ['statements']]
        self.assertTrue(any('停用' in line and '已经登记进记录' in line
                            for line in statements), statements)
        self.assertTrue(any('仍在计划中' in line and '尚未确认发生' in line
                            for line in statements), statements)
        self.assertFalse(any('一半' in line for line in statements),
                         '旧药已停、新药仍在计划时，不能写成"只登记了一半"')

    def test_a_whole_switch_group_is_written_in_one_transaction(self):
        text = '合成药甲换成合成药丙了，3mg'
        self.interpreter.plan = lambda raw, context: _reading(
            _item('occurred', operation='remove', name='合成药甲', quote=text,
                  group_role='replace_from'),
            _item('occurred', operation='add', name='合成药丙', quote=text,
                  field='dose', value='3mg', group_role='replace_to'))
        self.submit(text)
        members = self.pending()
        self.assertEqual(2, len(members), members)
        response = self.api.client.post(
            f'/v1/safety-cases/{self.case_id}/visits/{self.visit_id}/candidates/'
            f'{members[0]["id"]}/confirm-group', json={'key': 'group-1'})
        self.assertEqual(200, response.status_code, response.text)
        rows = self.medications()
        self.assertEqual('stopped', rows['合成药甲']['status'])
        self.assertEqual('active', rows['合成药丙']['status'])
        self.assertTrue(all(item['status'] == rv.CANDIDATE_CONFIRMED
                            for item in self.case()['visit']['change_candidates']
                            if (item.get('group') or {}).get('id')))

    def test_a_failed_group_leaves_no_half_written_medication(self):
        """整组原子：第二条写不进去时，第一条也不留下。

        用**故障注入**制造"第二条失败"：真实的第二条失败只可能来自并发或其他写
        插进来，那没法稳定复现。这里要验的是事务边界本身——第一条已经在这个事务里
        写进去了，第二条一炸，它必须跟着回滚。
        """
        text = '合成药甲换成合成药丙了，3mg'
        self.interpreter.plan = lambda raw, context: _reading(
            _item('occurred', operation='remove', name='合成药甲', quote=text,
                  group_role='replace_from'),
            _item('occurred', operation='add', name='合成药丙', quote=text,
                  field='dose', value='3mg', group_role='replace_to'))
        self.submit(text)
        members = [item for item in self.pending() if item.get('group')]
        self.assertEqual(2, len(members), self.pending())

        memory = self.api.product.memory
        original = memory._apply_medication_change_tx
        calls = {'n': 0}

        def fail_second(*args, **kwargs):
            calls['n'] += 1
            if calls['n'] == 2:
                raise MedicationWriteConflict('测试注入：第二条写入失败')
            return original(*args, **kwargs)

        memory._apply_medication_change_tx = fail_second
        try:
            response = self.api.client.post(
                f'/v1/safety-cases/{self.case_id}/visits/{self.visit_id}/candidates/'
                f'{members[0]["id"]}/confirm-group', json={'key': 'group-bad'})
        finally:
            memory._apply_medication_change_tx = original
        self.assertEqual(2, calls['n'])
        self.assertEqual(409, response.status_code, response.text)
        self.assertEqual('active', self.medications()['合成药甲']['status'],
                         '整组失败时不能留下没有说明的半组')
        self.assertNotIn('合成药丙', self.medications())
        self.assertEqual(2, len(self.pending()), '整组都没写进去，候选一条也不该被吃掉')


class NonActionableTests(_ChangeFlow):
    """漏服、计划、询问：三条对照，都不改正式药单。"""

    def plan_reading(self, text, context):
        if '漏' in text or '忘了' in text:
            return _reading(_item('missed_dose', name='合成药甲', quote=text))
        if '打算' in text:
            return _reading(_item('planned', operation='remove', name='合成药甲',
                                  quote=text, time_text='下周', precision='week'))
        return _reading(_item('question', name='合成药甲', quote=text))

    def test_none_of_them_touch_the_authoritative_record(self):
        before = self.medications()
        for index, text in enumerate(('昨天漏了一次合成药甲', '打算停掉合成药甲',
                                      '合成药甲要不要停')):
            self.submit(text, key=f'note-{index}')
        self.assertEqual([], self.pending(), '这三句话都不该产生可确认候选')
        self.assertEqual(before, self.medications(), '正式药单被改动了')

    def test_a_plan_does_not_become_a_completed_stop(self):
        self.submit('打算停掉合成药甲')
        note = self.notes()[-1]
        self.assertEqual(1, len(note['plans']))
        self.assertEqual('planned', note['plans'][0]['status'])
        self.assertEqual('active', self.medications()['合成药甲']['status'])


class AmbiguityTests(_ChangeFlow):
    """指代不明确时补问，不猜。"""

    def plan_reading(self, text, context):
        return _reading(_item('occurred', operation='dose_change', name='',
                              quote=text, field='schedule', value='每天两次',
                              uncertain=['object_ambiguous']),
                        question='您说的是哪一种药？')

    def test_an_ambiguous_object_becomes_a_question_not_a_guess(self):
        before = self.medications()
        self.submit('这个药改成每天两次了')
        self.assertEqual([], self.pending(), '对象不明确时不能生成候选')
        note = self.notes()[-1]
        self.assertEqual(cn.NOTE_INTERPRETED, note['status'])
        self.assertTrue(note['questions'], note['questions'])
        self.assertTrue(any('哪一种药' in entry['text'] for entry in note['questions']),
                        note['questions'])
        self.assertEqual(before, self.medications())

    def test_a_quote_that_is_not_in_the_users_words_is_dropped(self):
        """没有原文依据的判定不采信——这是"可核对"的硬要求。"""
        self.interpreter.plan = lambda text, context: _reading(
            _item('occurred', operation='dose_change', name='合成药甲',
                  quote='这句话用户根本没有说过', field='dose', value='9mg'))
        self.submit('这个药改了')
        self.assertEqual([], self.pending())
        note = self.notes()[-1]
        self.assertTrue(note['questions'], '依据对不上时应当补问，而不是静默丢弃')


class AmbiguityFlagTests(_ChangeFlow):
    """歧义由**能不能唯一解析到权威记录**判定，不照抄模型的自评。

    这一组是真实模型那一轮暴露出来的：它一边点名"合成药甲"、一边又把
    `object_ambiguous` 标上，于是候选被挡下——用户明明点了名，却被告知"您说的是
    哪一种药"。同一轮也确认了另一面：用"这个药"指代时，那个标记得留着。
    """

    def plan_reading(self, text, context):
        return _reading()

    def test_a_name_that_is_quoted_verbatim_is_not_ambiguous(self):
        self.interpreter.plan = lambda text, context: _reading(_item(
            'occurred', operation='remove', name='合成药甲', quote=text,
            denies_quote='', uncertain=['object_ambiguous']))
        # drug_quote 就是用户点名的那个词 —— 名称出现在依据里，不是指代。
        self.interpreter.plan = lambda text, context: _reading(
            {**_item('occurred', operation='remove', name='合成药甲', quote=text,
                     uncertain=['object_ambiguous']),
             'drug_quote': '合成药甲'})
        self.submit('是合成药甲，上周就停了')
        pending = self.pending()
        self.assertEqual(1, len(pending), pending)
        self.assertEqual('合成药甲', pending[0]['target']['name'])
        self.assertEqual([], self.notes()[-1]['questions'],
                         '点名说了是哪一个药，就不该再问"您说的是哪一种药"')

    def test_a_pronoun_with_a_guessed_name_stays_ambiguous(self):
        self.interpreter.plan = lambda text, context: _reading(
            {**_item('occurred', operation='remove', name='合成药甲', quote=text,
                     uncertain=['object_ambiguous']),
             'drug_quote': '这个药'})
        self.submit('这个药不吃了')
        self.assertEqual([], self.pending(), '指代不明时不能拿模型猜的名字去写')
        note = self.notes()[-1]
        self.assertTrue(any('哪一种药' in entry['text'] for entry in note['questions']),
                        note['questions'])

    def test_a_vague_time_does_not_block_the_candidate(self):
        """"上周"是可表达的（reported_vague），不是"说不清所以不能登记"。"""
        self.interpreter.plan = lambda text, context: _reading(_item(
            'occurred', operation='remove', name='合成药甲', quote=text,
            time_text='上周', precision='week', uncertain=['time_vague']))
        self.submit('合成药甲上周就停了')
        pending = self.pending()
        self.assertEqual(1, len(pending), pending)
        self.assertEqual('reported_vague', pending[0]['occurred']['basis'])
        self.assertIsNone(pending[0]['occurred']['value'])
        self.assertEqual('上周', pending[0]['occurred']['text'])
        self.assertEqual([], self.notes()[-1]['questions'])


class ChineseNumberTests(_ChangeFlow):
    """中文数字与改写：没有阿拉伯数字不等于没有新信息。"""

    def plan_reading(self, text, context):
        return _reading(_item('occurred', operation='dose_change', name='合成药甲',
                              quote=text, field='schedule', value='每天两次'))

    def test_a_chinese_numeral_schedule_becomes_a_candidate(self):
        """没有阿拉伯数字不等于没有新信息——判"有没有价值"的不是一串正则。"""
        self.submit('合成药甲改成每天两次了')
        pending = self.pending()
        self.assertEqual(1, len(pending), pending)
        self.assertEqual({'schedule': '每天两次'}, pending[0]['changes'])
        # 原来没记录过频次时如实给 None，不假装原来是什么。
        self.assertEqual({'schedule': None}, pending[0]['before'])
        self.confirm(pending[0]['id'])
        self.assertEqual('每天两次', self.medications()['合成药甲']['schedule'])


class ConflictTests(_ChangeFlow):
    """版本与状态冲突：不覆盖较新的记录。"""

    def plan_reading(self, text, context):
        return _reading(_item('occurred', operation='dose_change', name='合成药甲',
                              quote=text, field='dose', value='10mg'))

    def test_a_stale_candidate_is_refused_and_the_conflict_is_kept(self):
        self.submit('合成药甲改成10mg')
        candidate = self.pending()[0]
        self.assertEqual('add', self.medications()['合成药甲']['operation'])

        # 别处先把这味药改了：候选依据的那一版已经不在。
        memory = self.api.product.memory
        row = next(item for item in memory.current_medications()
                   if item['display_name'] == '合成药甲')
        memory.apply_medication_change(
            action='dose_change', name='合成药甲', ingredients=[], session_id='s',
            turn_id='elsewhere', source='test', dose='8mg',
            expect={'record_id': row['id'], 'record_version': row['version'],
                    'status': 'active'})
        self.api.pump()

        response = self.confirm(candidate['id'])
        self.assertEqual(409, response.status_code, response.text)
        self.assertNotEqual('10mg', self.medications()['合成药甲']['dose'],
                            '冲突时不能覆盖较新的记录')
        kept = self.visits().candidate(self.visit_id, candidate['id'])
        self.assertIsNotNone(kept.get('conflict'),
                             '409 之后冲突说明必须还在——它是在**另一个事务**里落盘的')
        self.assertEqual('pending', kept['status'])

    def test_the_same_value_after_a_round_trip_is_still_stale(self):
        """A→B→A：值回到原样，但记录已经换过两版，旧候选仍然过期。"""
        def write(value):
            memory = self.api.product.memory
            row = next(item for item in memory.current_medications()
                       if item['display_name'] == '合成药甲')
            memory.apply_medication_change(
                action='dose_change', name='合成药甲', ingredients=[], session_id='s',
                turn_id=f'direct-{value}', source='test', dose=value,
                expect={'record_id': row['id'], 'record_version': row['version'],
                        'status': 'active'})

        write('5mg')
        original = self.medications()['合成药甲']
        self.submit('合成药甲改成10mg')
        candidate = self.pending()[0]
        self.assertEqual({'dose': '5mg'}, candidate['before'])

        write('9mg')
        write('5mg')
        now = self.medications()['合成药甲']
        self.assertEqual(original['dose'], now['dose'], '值确实回到了原样')
        self.assertGreater(now['version'], original['version'], '但版本已经变了')

        response = self.confirm(candidate['id'])
        self.assertEqual(409, response.status_code,
                         '只比字段值会把这条过期候选放过去')


class IdempotencyTests(_ChangeFlow):
    """重复确认、重放、刷新、重启：不重复写入，也不重复花模型额度。"""

    def plan_reading(self, text, context):
        return _reading(_item('occurred', operation='remove', name='合成药甲', quote=text))

    def test_a_duplicate_submission_does_not_call_the_model_twice(self):
        self.submit('停了合成药甲', key='note-a')
        self.assertEqual(1, len(self.interpreter.reads))
        response = self.api.client.post(
            f'/v1/safety-cases/{self.case_id}/visits/{self.visit_id}/notes',
            json={'key': 'note-a', 'text': '停了合成药甲'})
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual(1, len(self.interpreter.reads),
                         '同一个 key 的重放不该再花一次模型额度')
        self.assertEqual(1, len(self.notes()), '重复提交不该新建一条')

    def test_replaying_a_successful_confirmation_returns_the_same_receipt(self):
        self.submit('停了合成药甲')
        candidate = self.pending()[0]
        first = self.confirm(candidate['id'], key='confirm-x')
        self.assertEqual(200, first.status_code, first.text)
        again = self.confirm(candidate['id'], key='confirm-x')
        self.assertEqual(200, again.status_code,
                         '同一个 key 的重放是正常业务路径（超时重试），不是冲突')
        self.assertEqual(first.json(), again.json())
        self.assertEqual('stopped', self.medications()['合成药甲']['status'])

    def test_a_different_request_against_a_decided_candidate_is_a_conflict(self):
        self.submit('停了合成药甲')
        candidate = self.pending()[0]
        self.confirm(candidate['id'], key='confirm-y')
        second = self.confirm(candidate['id'], key='confirm-z')
        self.assertEqual(409, second.status_code,
                         '另一个请求用过期的候选应当明确冲突')

    def test_a_dismissed_candidate_does_not_come_back_after_a_restart(self):
        self.submit('合成药甲改成每天两次')
        candidate = self.pending()[0]
        dismissed = self.api.client.post(
            f'/v1/safety-cases/{self.case_id}/visits/{self.visit_id}'
            f'/candidates/{candidate["id"]}/dismiss', json={'key': 'dismiss-1'})
        self.assertEqual(200, dismissed.status_code, dismissed.text)
        self.assertEqual([], self.pending())

        self.reboot()
        self.assertEqual([], self.pending(),
                         '放弃过的候选不该在重启之后自己回到待确认')
        statuses = [item['status'] for item in
                    self.case()['visit']['change_candidates']]
        self.assertEqual([rv.CANDIDATE_DISMISSED], statuses)


class NegationTests(_ChangeFlow):
    """否定与纠正：不能说"不是药甲"却把药甲改了。"""

    def plan_reading(self, text, context):
        # "不是药甲，是药乙的剂量改了" —— 药甲被**否定**，药乙才是目标。
        return _reading(
            _item('correction', operation='dose_change', name='合成药乙', quote=text,
                  field='dose', value='7mg', denies='合成药甲', denies_quote='不是药甲'))

    def test_the_denied_drug_is_never_touched_and_its_candidate_is_withdrawn(self):
        # 先有一条关于药甲的待确认候选。
        self.interpreter.plan = lambda text, context: _reading(_item(
            'occurred', operation='dose_change', name='合成药甲', quote=text,
            field='dose', value='9mg'))
        self.submit('合成药甲改成9mg', key='note-1')
        about_jia = self.pending()[0]
        self.assertEqual('合成药甲', about_jia['target']['name'])

        before = self.medications()
        self.interpreter.plan = self.plan_reading
        self.submit('不是药甲，是合成药乙的剂量改了', key='note-2')

        pending = self.pending()
        self.assertEqual(['合成药乙'], [item['target']['name'] for item in pending],
                         '药甲那条必须被撤回，不能留下两条互相矛盾的当前候选')
        withdrawn = self.visits().candidate(self.visit_id, about_jia['id'])
        self.assertEqual(rv.CANDIDATE_SUPERSEDED, withdrawn['status'])
        self.assertIsNotNone(withdrawn['superseded_by'], '要留下"被谁取代"的指向')
        self.assertEqual(before, self.medications(), '否定不等于修改')

        self.confirm(pending[0]['id'], key='confirm-yi')
        rows = self.medications()
        self.assertEqual('7mg', rows['合成药乙']['dose'])
        self.assertEqual(before['合成药甲']['dose'], rows['合成药甲']['dose'],
                         '被否定的药甲一个字节都不该动')


class FailureTests(_ChangeFlow):
    """模型的两种不成：没有配置，和调了但失败。都不伪装成成功。"""

    def plan_reading(self, text, context):
        return _reading(_item('occurred', operation='remove', name='合成药甲', quote=text))

    def test_no_provider_keeps_the_text_and_says_so(self):
        self.interpreter.mode = 'unavailable'
        self.submit('停了合成药甲')
        note = self.notes()[-1]
        self.assertEqual(cn.NOTE_UNAVAILABLE, note['status'])
        self.assertEqual('停了合成药甲', note['text'], '原文必须留着')
        self.assertIn('模型配置', note['error'])
        self.assertEqual([], self.pending())
        self.assertEqual('active', self.medications()['合成药甲']['status'])

    def test_a_model_failure_keeps_the_text_and_offers_a_retry(self):
        self.interpreter.mode = 'error'
        self.submit('停了合成药甲')
        note = self.notes()[-1]
        self.assertEqual(cn.NOTE_FAILED, note['status'])
        self.assertEqual('停了合成药甲', note['text'])
        self.assertTrue(note['error'])

        self.interpreter.mode = 'ok'
        response = self.retry(note['id'])
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual(cn.NOTE_INTERPRETED, response.json()['status'])
        self.assertEqual(1, len(self.pending()))

    def test_an_empty_submission_is_refused_without_touching_anything(self):
        response = self.api.client.post(
            f'/v1/safety-cases/{self.case_id}/visits/{self.visit_id}/notes',
            json={'key': 'note-empty', 'text': '   '})
        self.assertEqual(422, response.status_code, response.text)
        self.assertEqual([], self.notes())
        self.assertEqual([], self.interpreter.reads)


class WriteAuthorityTests(_ChangeFlow):
    """受控写入：不能靠请求体的一个字段绕过候选确认。"""

    def plan_reading(self, text, context):
        return _reading(_item('occurred', operation='remove', name='合成药甲', quote=text))

    def test_a_plain_answer_cannot_stop_a_medication(self):
        from stage0.care_tasks import CareTasks
        task = sc._visit_task(self.api.product, self.case_id,
                              self.visits().get(self.visit_id))
        with self.assertRaises(ProductError):
            CareTasks(self.api.product).record_input(
                task['id'], 'plain-remove', task['revision'],
                medications=[{'name': '合成药甲', 'action': 'remove'}])
        self.assertEqual('active', self.medications()['合成药甲']['status'],
                         '普通回答不该能停掉一个药')

    def test_an_unknown_medication_action_is_still_refused(self):
        from stage0.care_tasks import CareTasks
        task = sc._visit_task(self.api.product, self.case_id,
                              self.visits().get(self.visit_id))
        with self.assertRaises(ProductError):
            CareTasks(self.api.product).record_input(
                task['id'], 'plain-weird', task['revision'],
                medications=[{'name': '合成药甲', 'action': 'teleport'}])

    def test_a_pending_candidate_cannot_be_written_directly(self):
        """未经确认的候选走写入原语：服务端核对的是**候选自己的**版本与状态。"""
        self.submit('停了合成药甲')
        candidate = self.pending()[0]
        with self.assertRaises(MedicationWriteConflict):
            self.api.product.memory._apply_medication_change_tx(
                action='remove', name='合成药甲', ingredients=[], session_id='direct',
                turn_id='direct', source='test',
                expect={'record_id': 999999, 'record_version': 1, 'status': 'active'})
        self.assertEqual('active', self.medications()['合成药甲']['status'])
        self.assertEqual('pending',
                         self.visits().candidate(self.visit_id, candidate['id'])['status'])


class CaseContinuationTests(_ChangeFlow):
    """变更之后：事项与回访继续，未决风险不被自动清除。"""

    def plan_reading(self, text, context):
        return _reading(_item('occurred', operation='remove', name='合成药甲', quote=text))

    def test_a_stop_never_reads_as_the_whole_risk_being_resolved(self):
        """"组合不再出现"与"整体风险已解除"必须是两句话。

        停药之后触发条件**客观**上没有了，按既有规则这是可以据以处置的依据——
        这一轮不改关闭条件。改的是**表达**：关闭依据的性质要写清楚它说的是
        "本事项的触发条件在当前记录下不再出现"，而系统从不主张"整体风险已解除"。
        """
        self.submit('上周已经停了合成药甲')
        self.confirm(self.pending()[0]['id'])
        self.api.pump()

        after = self.case()
        self.assertNotEqual('resolved', after['status'],
                            '没有任何人处置过，事项不该自己变成已解决')
        evidence = self.store().closure_evidence(self.store().get(self.case_id))
        nature = evidence['nature']
        self.assertTrue(nature['evaluated'])
        self.assertIn('medication_no_longer_in_current_record', nature['basis'])
        self.assertFalse(nature['overall_risk_resolved'],
                         '这一位是常量 False：系统从不做这个判断')
        self.assertIn('不等于整体用药风险已经解除', nature['note'])

    def test_an_open_question_still_blocks_a_closure(self):
        """未决问题仍然阻止不成立的关闭——不确定性不会因为停药而消失。"""
        self.store().require_input(self.case_id, request_id='req-open',
                                   question='最近一次复查结果如何？')
        self.submit('上周已经停了合成药甲')
        self.confirm(self.pending()[0]['id'])
        self.api.pump()
        evidence = self.store().closure_evidence(self.store().get(self.case_id))
        self.assertFalse(evidence['ok'])
        self.assertEqual(['req-open'], evidence['blocking_inputs'])

    def test_a_conclusion_from_the_previous_version_cannot_approve_the_new_one(self):
        """旧结论不能批准新记录：检查之后记录又变了，关闭必须被挡下。"""
        self.submit('上周已经停了合成药甲')
        self.confirm(self.pending()[0]['id'])
        self.api.pump()

        memory = self.api.product.memory
        row = next(item for item in memory.current_medications()
                   if item['display_name'] == '合成药乙')
        memory.apply_medication_change(
            action='dose_change', name='合成药乙', ingredients=[], session_id='s',
            turn_id='after-check', source='test', dose='3mg',
            expect={'record_id': row['id'], 'record_version': row['version'],
                    'status': 'active'})
        evidence = self.store().closure_evidence(self.store().get(self.case_id))
        self.assertFalse(evidence['ok'])
        # 两道闸哪一道先响都行，但必须是"这条依据已经配不上当前记录"这一类理由，
        # 不能是"没有结论表明触发条件已消除"这种含糊的挡回。
        self.assertIn(evidence['reason'],
                      ('依据结论已失效，请重新核对后再处置',)
                      + tuple(f'{key} 记录在检查后发生变化，旧结论不能批准当前状态'
                              for key in ('medications', 'semantic')))

    def test_the_visit_keeps_going_on_the_same_case(self):
        visit_before = self.case()['visit']['visit_id']
        self.submit('上周已经停了合成药甲')
        self.confirm(self.pending()[0]['id'])
        self.api.pump()
        after = self.case()
        self.assertEqual(self.case_id, after['case_id'], '变更不该产生第二件事项')
        self.assertEqual(visit_before, after['visit']['visit_id'],
                         '确认之后继续的是**同一次**回访')
        self.assertEqual(1, after['visit']['sequence'])
        actions = after['visit']['result']['actions']
        self.assertTrue(any('停用' in item['text'] for item in actions), actions)
        self.assertTrue(any(item['basis']['kind'] == 'record' for item in actions),
                        f'写入的依据类型应当是权威记录：{actions}')

    def test_the_result_and_the_notes_survive_a_restart(self):
        self.submit('上周已经停了合成药甲')
        self.confirm(self.pending()[0]['id'])
        self.reboot()
        visit = self.case()['visit']
        self.assertEqual(1, len(visit['change_notes']))
        self.assertEqual('上周已经停了合成药甲', visit['change_notes'][0]['text'])
        self.assertTrue(visit['result']['actions'])


class DeterministicEntryTests(_ChangeFlow):
    """用户只点了一个明确的状态按钮：确定性路径，**不调用模型**。"""

    def plan_reading(self, text, context):
        return _reading()

    def test_a_status_button_needs_no_model_and_writes_nothing(self):
        before = self.medications()
        note = self.submit('', key='note-button', hint='还没做')
        self.assertEqual([], self.interpreter.reads,
                         '只点按钮不该调模型——那是确定性代码能处理的事')
        self.assertEqual(cn.NOTE_INTERPRETED, note['status'])
        self.assertTrue(note['reading']['deterministic'])
        self.assertEqual(0, note['usage']['calls'])
        self.assertEqual('还没做', note['speech_act_hint'])
        self.assertEqual([], self.pending())
        self.assertEqual(before, self.medications())

    def test_an_empty_submission_without_a_button_is_refused(self):
        response = self.api.client.post(
            f'/v1/safety-cases/{self.case_id}/visits/{self.visit_id}/notes',
            json={'key': 'note-blank', 'text': ''})
        self.assertEqual(422, response.status_code, response.text)


class EpisodeMigrationTests(unittest.TestCase):
    """迁移不变式：回填 `episode_id` 之后，**存量事项的身份逐字节不变**。

    这是"不要为了少加一列让含义依赖可变 status"的落点。`episode_anchor` 的输出
    已经被持久化进事项的 `dedup_key`，所以回填值必须与迁移前那个算法的输出完全
    一致；对不上就会把同一件事重新认成一件新事项。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='episode-migration-')
        self.db = pathlib.Path(self.tmp.name) / 'memory.db'
        self.addCleanup(self.tmp.cleanup)

    def _history(self):
        """造出"停药 → 恢复 → 阶段内调整 → 又一次停药"的历史形状。"""
        memory = MemoryStore(self.db, llm_enabled=False)
        first = memory.apply_medication_change(
            action='add', name='合成药甲', ingredients=[], session_id='s', turn_id='t1',
            source='test', dose='5mg', occurred_at='2026-09-01T00:00:00+00:00')['medication']
        memory.apply_medication_change(
            action='dose_change', name='合成药甲', ingredients=[], session_id='s',
            turn_id='t2', source='test', dose='7mg',
            expect={'record_id': first['id'], 'record_version': first['version'],
                    'status': 'active'})
        stopped = memory.connection.execute(
            "SELECT * FROM medications WHERE status='active'").fetchone()
        memory.apply_medication_change(
            action='remove', name='合成药甲', ingredients=[], session_id='s', turn_id='t3',
            source='test', expect={'record_id': stopped['id'],
                                   'record_version': stopped['version'],
                                   'status': 'active'})
        memory.apply_medication_change(
            action='resume', name='合成药甲', ingredients=[], session_id='s', turn_id='t4',
            source='test', expect={'record_id': stopped['id'],
                                   'record_version': stopped['version'],
                                   'status': 'stopped'})
        return memory

    def test_a_backfilled_phase_is_byte_identical_to_the_historical_walk(self):
        from stage0 import safety_cases as sc
        from stage0.product import ProductStore
        memory = self._history()
        product = ProductStore(memory)
        rows = memory.connection.execute("SELECT id FROM medications ORDER BY id").fetchall()
        refs = [f"memory:medication:{row['id']}@v1" for row in rows]
        anchors = {row['id']: sc.episode_anchor(product, [f"memory:medication:{row['id']}@v1"])
                   for row in rows}
        memory.close()

        # 抹掉回填，回到"这一列还不存在"的世界：锚点只能按历史算法算。
        connection = sqlite3.connect(self.db)
        connection.execute('UPDATE medications SET episode_id=NULL')
        connection.commit()
        connection.close()
        memory = MemoryStore(self.db, llm_enabled=False)
        product = ProductStore(memory)
        legacy = {row['id']: sc.episode_anchor(product, [f"memory:medication:{row['id']}@v1"])
                  for row in rows}
        self.assertEqual(anchors, legacy,
                         '回填之后算出的阶段锚点必须与迁移前逐字节相同')
        # 迁移确实回填了，不是"两边都发愁"。
        episodes = {row['id']: row['episode_id'] for row in memory.connection.execute(
            "SELECT id, episode_id FROM medications")}
        self.assertTrue(all(value is not None for value in episodes.values()))
        self.assertEqual(len(set(episodes.values())), 2,
                         '停药前后应当是**两个**阶段（剂量调整留在同一阶段里）')
        memory.close()


if __name__ == '__main__':
    unittest.main()
