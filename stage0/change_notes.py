"""自然语言用药变化登记：把用户说的一句话变成本次回访上**可核对**的待确认候选。

三层，各自能单独看懂：

1. **收录**（确定性）：空输入、长度、重复提交、权限、对象存在性、版本，以及"用户
   只点了一个明确的状态按钮"。这些都不需要模型，也**不判断**这段话有没有业务
   价值——没有数字、单位或时间词不等于没有新信息。
2. **理解**（一次有上限的调用）：用当前问题、相关药物、原文与必要上下文，让模型
   说出这段话在报告什么、指向哪个已有对象、涉及哪个字段、用户表达的值、原文依据、
   哪些部分不确定、要不要补问。一次 completion，一个函数 schema：不给工具、不让它
   检索、**不启动调查**。
3. **落地**（确定性校验）：原文片段必须是原话的精确子串；对象必须唯一解析到一条
   权威记录；字段必须是我们能核对的；时间只在语义足够明确时才标准化。任何一条
   不满足就降级成"要补问"，绝不猜。

三条写死的规矩：

* **模型的理解只是解释，不是事实。** 它落成的是"待确认的解释"，确认之前权威记录
  一个字节都不动。
* **计划不进可执行的确认列表。** "打算停药甲"和"上周已经停了药甲"在字段层面看着
  像，在业务上是两回事；一个可确认的候选离"写成已发生"只差一次点击。
* **漏服是可记录的用户报告**，不写药单，也不停止整段服用记录。
"""
from __future__ import annotations

import json
import os
import re
import uuid
from typing import Any, Callable

from . import review_visits as _visits
from .memory import utc_now
from .product import ProductError

#: 一次提交的长度上限。超了请分次——不是因为"太长就没价值"，是因为要留得下原文。
MAX_TEXT = 1000

# ---- 记录状态 ---------------------------------------------------------------
NOTE_RECEIVED = 'received'            # 原文已存下，还没理解
NOTE_INTERPRETING = 'interpreting'    # 正在理解（有一个人在跑）
NOTE_INTERPRETED = 'interpreted'      # 理解完成
NOTE_UNAVAILABLE = 'unavailable'      # 没有可用的模型配置 —— **不是**失败，也不是成功
NOTE_FAILED = 'failed'                # 调了模型但没成 —— 原文留着，可以重试
NOTE_STATUSES = (NOTE_RECEIVED, NOTE_INTERPRETING, NOTE_INTERPRETED,
                 NOTE_UNAVAILABLE, NOTE_FAILED)

# ---- 每一条操作各自的"这是在说什么" -----------------------------------------
WHEN_OCCURRED = 'occurred'            # 报告已经发生的变化
WHEN_PLANNED = 'planned'              # 表达以后要做的计划
WHEN_QUESTION = 'question'            # 在问该不该改
WHEN_CORRECTION = 'correction'        # 纠正之前的说法或登记
WHEN_MISSED_DOSE = 'missed_dose'      # 只是漏了一次，不是停药
WHEN_UNCLEAR = 'unclear'              # 判不出来
WHEN_VALUES = (WHEN_OCCURRED, WHEN_PLANNED, WHEN_QUESTION, WHEN_CORRECTION,
               WHEN_MISSED_DOSE, WHEN_UNCLEAR)

#: 只有这两种"在说什么"能产生可确认的候选。
WHEN_ACTIONABLE = (WHEN_OCCURRED, WHEN_CORRECTION)

READING_OPERATIONS = ('add', 'remove', 'dose_change', 'resume')
READING_FIELDS = ('dose', 'schedule', 'route', 'start_at')
READING_PRECISIONS = ('exact', 'day', 'week', 'month', 'vague', 'unknown')
UNCERTAINTIES = ('object_ambiguous', 'value_missing', 'time_vague', 'conflicting',
                 'multiple_records', 'unsupported')

#: 一次理解的**硬上限**。它自成一个预算单元，不占用回访调查的额度。
#: 一句话的理解不该需要第二次调用；重试只能由用户**显式**发起。
#:
#: ``accounting_version`` 必须跟着走：缺了它 `BudgetSession` 会把这次运行判成
#: "记账口径不明"并**拒绝发起调用**——那是 fail closed，是对的，但这里我们要的是
#: 一次正常记账的调用。
INTERPRET_LIMITS = {'max_cycles': 1, 'wall_clock_seconds': 25.0,
                    'token_budget': 8000, 'call_budget': 2,
                    'accounting_version': 2}

UNCERTAINTY_LABELS = {
    'object_ambiguous': '您指的是哪一种药还不确定',
    'value_missing': '还缺一个具体的值',
    'time_vague': '时间只说了个大概，没有具体日期',
    'conflicting': '这句话前后有对不上的地方',
    'multiple_records': '这味药停用过不止一次，不确定指哪一次',
    'unsupported': '这种说法系统还表达不了',
}

READING_TOOL = {
    'type': 'function',
    'function': {
        'name': 'record_medication_change_reading',
        'description': ('记录对用户这句话的理解。只抽取用户说过的内容；'
                        '没有提到的信息一律留空，不要补。'),
        'parameters': {
            'type': 'object',
            'properties': {
                'summary': {'type': 'string',
                            'description': '一句话概括用户说了什么（用用户的原话词汇）'},
                'items': {
                    'type': 'array',
                    'description': '这段话里的每一件事各占一条；一句话说了两件事就两条',
                    'items': {
                        'type': 'object',
                        'properties': {
                            'when': {'type': 'string', 'enum': list(WHEN_VALUES)},
                            'operation': {'type': 'string',
                                          'enum': list(READING_OPERATIONS) + ['none']},
                            'drug_name': {'type': 'string',
                                          'description': '用户说的药名；没提就填空字符串'},
                            'drug_quote': {'type': 'string'},
                            'denies_name': {'type': 'string',
                                            'description': '纠正时被否定的那个药；没有就填空'},
                            'denies_quote': {'type': 'string'},
                            'field': {'type': 'string',
                                      'enum': list(READING_FIELDS) + ['none']},
                            'value': {'type': 'string'},
                            'time_text': {'type': 'string',
                                          'description': '用户原话里的时间表达，原样抄'},
                            'time_normalised': {
                                'type': 'string',
                                'description': ('只有用户的话能**唯一确定**一个日期时才填 '
                                                'YYYY-MM-DD；"上周""这两天"一律留空')},
                            'time_precision': {'type': 'string',
                                               'enum': list(READING_PRECISIONS)},
                            'uncertain': {'type': 'array',
                                          'items': {'type': 'string',
                                                    'enum': list(UNCERTAINTIES)}},
                            'group_role': {'type': 'string',
                                           'enum': ['replace_from', 'replace_to', 'none']},
                            'reported_overlap': {
                                'type': 'string', 'enum': ['yes', 'no', 'unstated']},
                            'quote': {'type': 'string',
                                      'description': '支撑这条判定的**原文连续片段**'},
                        },
                        'required': ['when', 'quote'],
                    },
                },
                'unsupported': {
                    'type': 'array',
                    'description': '用户说了、但系统表达不了的用药操作',
                    'items': {'type': 'object',
                              'properties': {'what': {'type': 'string'},
                                             'quote': {'type': 'string'}},
                              'required': ['what', 'quote']},
                },
                'question': {'type': 'string',
                             'description': '对象不明确、前后矛盾或缺必要信息时，'
                                            '要问用户的**一个具体问题**；否则填空'},
            },
            'required': ['summary', 'items'],
        },
    },
}

SYSTEM_PROMPT = """你是用药变化的**理解器**，不是医生，也不给用药建议。
用户会说一段关于自己用药的话。你要做的只是说清"这段话在说什么事"。

对每一件事给出一条 item。判断规则：

1. when 必须逐条判断，不能用一句话的总体语气代替：
   - occurred：报告**已经发生**的变化（"上周已经停了药甲"）
   - planned：表达**以后要做**的计划（"打算停药甲""准备下周开始吃"）
   - question：在**问**要不要改（"药甲要不要停"）
   - correction：在**纠正**之前的说法或之前的登记（"不是药甲，是药乙改了"
     "之前登记停用是填错了"）
   - missed_dose：只是**漏了一次**，不是停药（"昨天漏了一次""忘了吃"）
   - unclear：确实判不出来
2. 计划不能写成已经发生。问题不能写成已经执行。漏服不是停药。
3. operation 只在 when 是 occurred 或 correction 时才填：
   add 新增 / remove 停用 / dose_change 调整用法 / resume 恢复服用。
   纠正的是"之前登记错了"时，operation 也填 none——那是记录纠错，不是一次新的用药变化。
4. drug_name 必须是用户话里出现过的药名，原样抄。用户用"这个药""它"指代而没有
   说清是哪一个时，drug_name 留空，并在 uncertain 里加 object_ambiguous。
5. quote 必须是从用户原话里**原样截取的连续片段**，是这条判定的依据。
6. time_text 原样抄用户的时间说法。time_normalised **只**在用户的话能唯一确定一个
   日期时才填；"上周""这两天""前阵子"一律留空，precision 填 week/month/vague。
7. 用户**没有**提供的剂量、药名、日期、频次，一律留空。不要推断，不要补充常识。
8. 一句话里有两件事（"旧药已经停了，新药打算明天开始"）就输出两条 item，
   各自带自己的 when 和 operation。**不要**用一个总体判断把其中一半丢掉。
9. 换药时两条各给一个 group_role：换出来的那条 replace_from，换进去的那条
   replace_to。用户明确报告两种药有一段时间同时服用时 reported_overlap 填 yes；
   明确说没有重叠填 no；没说填 unstated。
10. 需要用户补充才能确定时，在 question 里写**一个**具体的问题（例如
    "您说的是哪一种药？"），不要问一串。

只输出函数调用，不要在正文里解释。"""


def _key(name: Any) -> str:
    return re.sub(r'\s+', '', str(name or '')).lower()


# ---- 1. 收录（确定性） ------------------------------------------------------
def screen(text: Any, *, hint: str | None = None) -> str:
    """确定性前置检查：**只**判空、长度与格式。

    刻意不判断"这段话有没有数字/单位/时间词"。那不是确定性代码能回答的问题——
    它属于第 2 层的语义理解。用户只点了一个明确的状态按钮（没有文字）时，
    hint 本身就是全部信息，也不需要模型。
    """
    cleaned = str(text or '').strip()
    if not cleaned:
        if hint:
            return ''
        raise ProductError('请先写下发生了什么变化')
    if len(cleaned) > MAX_TEXT:
        raise ProductError(f'这段说明超过 {MAX_TEXT} 字，请分成几次提交')
    return cleaned


def _note_record(visit_id: str, case_id: str, text: str, *, hint: str | None,
                 tz: str | None) -> dict[str, Any]:
    return {
        'id': f'change-note:{uuid.uuid4().hex}',
        'visit_id': visit_id, 'case_id': case_id,
        'text': text,                 # 原文照留 —— 模型失败也不丢
        'speech_act_hint': hint,
        'received_at': utc_now(),
        'received_tz': tz or os.getenv('STAGE0_TZ') or 'local',
        'status': NOTE_RECEIVED,
        'attempt': 0,
        'reading': None,
        'candidate_ids': [], 'superseded_ids': [],
        'plans': [], 'questions': [], 'unsupported': [],
        'usage': None, 'model': None, 'error': None,
    }


def record_note(product, visit_id: str, text: str, *, hint: str | None = None,
                tz: str | None = None, key: str | None = None) -> dict[str, Any]:
    """把一段原文存到这次回访上。**重复提交不重复处理**：同样的文字再来一次，
    返回原来那条，不新建、也不会再花一次模型额度。"""
    visits = _visits.ReviewVisitStore(product)

    def execute():
        visit = visits.get(visit_id)
        notes = list(visit.get('change_notes') or [])
        for existing in notes:
            if existing['text'] == text and (hint or None) == (existing.get('speech_act_hint') or None):
                return visit
        notes.append(_note_record(visit_id, visit['case_id'], text, hint=hint, tz=tz))
        visit['change_notes'] = notes
        visit['updated_at'] = utc_now()
        visit['revision'] += 1
        visits.p.save(_visits.KIND, visit)
        return visit

    visit = product.command(f'{visit_id}:note:{key or uuid.uuid4().hex}',
                            {'type': 'review_visit_note', 'visit_id': visit_id,
                             'text': text, 'hint': hint}, execute)
    return _visits.ReviewVisitStore(product).get(visit_id)['change_notes'][-1]


def find_note(product, note_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """返回 ``(note, visit)``。"""
    for visit in _visits.ReviewVisitStore(product).objects():
        for note in visit.get('change_notes') or []:
            if note['id'] == note_id:
                return note, visit
    raise ProductError('这条补充记录不存在', 404)


def _update_note(product, note_id: str, mutate: Callable[[dict], None], *,
                 command_key: str) -> dict[str, Any]:
    def execute():
        visit = _visits.ReviewVisitStore(product).get(
            find_note(product, note_id)[1]['id'])
        for note in visit.get('change_notes') or []:
            if note['id'] == note_id:
                mutate(note)
                break
        else:
            raise ProductError('这条补充记录不存在', 404)
        visit['updated_at'] = utc_now()
        visit['revision'] += 1
        product.save(_visits.KIND, visit)
        return visit
    product.command(command_key, {'type': 'review_visit_note_update',
                                  'note_id': note_id}, execute)
    return find_note(product, note_id)[0]


def begin_attempt(product, note_id: str, *, attempt: int) -> dict[str, Any]:
    """开始第 ``attempt`` 次理解。

    命令键带 attempt：**同一件请求重放会命中同一条回执**，于是刷新、双击、重启都
    不会产生第二次模型调用；而重试是另一次请求（attempt+1），必须由用户显式发起。
    """
    def mutate(note):
        note['status'] = NOTE_INTERPRETING
        note['attempt'] = int(attempt)
        note['error'] = None
    return _update_note(product, note_id, mutate,
                        command_key=f'{note_id}:interpret:{int(attempt)}')


def finish_attempt(product, note_id: str, *, attempt: int, status: str,
                   reading: Any = None, applied: dict[str, Any] | None = None,
                   usage: dict[str, Any] | None = None,
                   model: dict[str, Any] | None = None,
                   error: str | None = None) -> dict[str, Any]:
    applied = applied or {}

    def mutate(note):
        note['status'] = status
        note['reading'] = reading
        note['candidate_ids'] = list(applied.get('candidate_ids') or [])
        note['superseded_ids'] = list(applied.get('superseded_ids') or [])
        note['plans'] = list(applied.get('plans') or [])
        note['questions'] = list(applied.get('questions') or [])
        note['unsupported'] = list(applied.get('unsupported') or [])
        note['usage'] = usage
        note['model'] = model
        note['error'] = error
    return _update_note(product, note_id, mutate,
                        command_key=f'{note_id}:interpreted:{int(attempt)}')


# ---- 2. 理解（一次有上限的调用） --------------------------------------------
class ChangeNoteInterpreter:
    """一次有预算上限的语义理解。

    **没有可用的模型配置不是失败**：那种情况下原文照留、状态记 `unavailable`，
    界面明说"尚未完成自动理解"，并给结构化登记入口。把它记成失败或记成成功，
    都是在骗用户。
    """

    def __init__(self, *, memory, client_factory: Callable[[], Any] | None = None,
                 model: str | None = None, provider: str | None = None,
                 enabled: bool | None = None):
        self.memory = memory
        self.client_factory = client_factory
        self.model = model
        self.provider = provider
        if enabled is None:
            enabled = os.getenv('CHANGE_NOTE_LLM', '1').strip().lower() not in {
                '0', 'false', 'no', 'off'}
        self.enabled = enabled

    # -- 可用性 -------------------------------------------------------------
    def config(self) -> dict[str, str] | None:
        if self.client_factory is not None:
            return {'provider': self.provider or 'scripted', 'model': self.model or 'scripted',
                    'base_url': ''}
        if not self.enabled:
            return None
        try:
            from . import extract_ddi
            return extract_ddi.resolve_llm_config(model=self.model)
        except Exception:
            return None

    def available(self) -> tuple[bool, str | None]:
        if self.client_factory is not None:
            return True, None
        if not self.enabled:
            return False, '自动理解已被关闭（CHANGE_NOTE_LLM=0）'
        try:
            from . import extract_ddi
            config = extract_ddi.resolve_llm_config(model=self.model)
        except Exception as exc:
            return False, f'没有可用的模型配置：{exc}'
        if not config.get('api_key'):
            return False, '没有配置模型凭据，无法自动理解这段话'
        return True, None

    def _client(self) -> Any:
        if self.client_factory is not None:
            return self.client_factory()
        from . import extract_ddi
        return extract_ddi.create_llm_client()

    # -- 调用 ---------------------------------------------------------------
    def read(self, *, text: str, context: dict[str, Any],
             hint: str | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
        """跑一次理解，返回 ``(reading, usage)``。

        它有自己的**运行记录**：预算、尝试台账、用量都进既有那本账，与回访调查
        同源。没有这条记录 `budget_scope` 会直接拒绝——这是好事，它逼着这次调用
        留下可核对的痕迹，而不是一次没人记账的网络请求。
        """
        from .turn_budget import budget_scope, completion_call
        client = self._client()
        run_id = f'change-note:{uuid.uuid4().hex}'
        payload = _reading_prompt(text, context, hint)
        self.memory.workflow_run_start(
            run_id=run_id, graph_version='change-note@1', state_schema_version='2',
            model_id=(self.config() or {}).get('model'),
            budget=dict(INTERPRET_LIMITS))
        try:
            return self._read_in_run(run_id, client, payload)
        finally:
            self.memory.workflow_run_update(run_id, status='succeeded')

    def _read_in_run(self, run_id: str, client: Any,
                     payload: str) -> tuple[dict[str, Any], dict[str, Any]]:
        from .turn_budget import budget_scope, completion_call
        with budget_scope(self.memory, run_id, 1, dict(INTERPRET_LIMITS)) as session:
            response = completion_call(
                'change_note_reader', client,
                model=(self.config() or {}).get('model'),
                messages=[{'role': 'system', 'content': SYSTEM_PROMPT},
                          {'role': 'user', 'content': payload}],
                tools=[READING_TOOL],
                tool_choice={'type': 'function',
                             'function': {'name': 'record_medication_change_reading'}},
                temperature=0,
                **self._options())
            data = dict(session.data)
        usage = {
            'calls': int(data.get('calls_attempted') or 0),
            # 缺失记 None（unknown），**不记 0**——0 是一个测量结果，"没测到"不是。
            'tokens': int(data['tokens_actual']) if data.get('tokens_actual') else None,
            'usage_unknown': bool(data.get('usage_unknown')),
            'quality': data.get('usage_quality'),
        }
        return parse_response(response), usage

    @staticmethod
    def _options() -> dict[str, Any]:
        try:
            from . import extract_ddi
            return dict(extract_ddi.llm_completion_options())
        except Exception:
            return {}


def _reading_prompt(text: str, context: dict[str, Any], hint: str | None) -> str:
    meds = context.get('medications') or []
    lines = [f"今天：{context.get('today')}（时区：{context.get('tz')}）", '当前用药记录：']
    if meds:
        for item in meds:
            detail = '，'.join(filter(None, [
                f"剂量 {item.get('dose')}" if item.get('dose') else None,
                f"频次 {item.get('schedule')}" if item.get('schedule') else None,
                f"途径 {item.get('route')}" if item.get('route') else None,
                f"开始于 {str(item.get('start_at'))[:10]}" if item.get('start_at') else None,
            ]))
            lines.append(f"- {item.get('display_name')}（{detail or '其它字段未记录'}）")
    else:
        lines.append('- （当前没有在用的药）')
    stopped = context.get('stopped_medications') or []
    if stopped:
        lines.append('曾经停用的记录：')
        for item in stopped:
            lines.append(f"- {item.get('display_name')}（停用于 {(item.get('end_at') or '时间未记录')}）")
    questions = context.get('open_questions') or []
    if questions:
        lines.append('这次回访正在等您回答的问题：' + '；'.join(questions))
    if hint:
        lines.append(f'用户点选的状态按钮：{hint}')
    lines.append('')
    lines.append('用户的原话（下面的 quote 必须从这里原样截取）：')
    lines.append(text or '（用户只点了状态按钮，没有文字）')
    return '\n'.join(lines)


def parse_response(response: Any) -> dict[str, Any]:
    """从模型响应里取出那次函数调用的参数。取不到就抛——由调用方如实记成失败。"""
    try:
        message = response.choices[0].message
    except Exception as exc:
        raise ValueError('模型没有返回可读的结果') from exc
    calls = getattr(message, 'tool_calls', None) or []
    if calls:
        raw = getattr(calls[0], 'function', None)
        arguments = getattr(raw, 'arguments', None)
        if arguments:
            return json.loads(arguments)
    content = getattr(message, 'content', None)
    if content:
        return json.loads(content)
    raise ValueError('模型没有返回结构化的理解结果')


# ---- 3. 落地（确定性校验） --------------------------------------------------
def resolve_medication(product, name: str, *, status: str = 'active') -> tuple[dict | None, str]:
    """把药名唯一解析到一条权威记录。

    解析不到、或者同名对上了不止一条时返回 ``(None, 'unmatched'|'ambiguous')``——
    名称只用于解析与展示，解析不唯一就**补问**，绝不猜一个。
    """
    wanted = _key(name)
    if not wanted:
        return None, 'unmatched'
    rows = product.memory.connection.execute(
        "SELECT * FROM medications WHERE status=? ORDER BY version DESC", (status,)).fetchall()
    exact = [row for row in rows if _key(row['display_name']) == wanted]
    if len(exact) == 1:
        return _row_view(exact[0]), 'exact'
    if len(exact) > 1:
        return None, 'ambiguous'
    partial = [row for row in rows if wanted and wanted in _key(row['display_name'])]
    if len(partial) == 1:
        return _row_view(partial[0]), 'partial'
    if len(partial) > 1:
        return None, 'ambiguous'
    return None, 'unmatched'


def _row_view(row) -> dict[str, Any]:
    from .product import SCOPE
    return {'name': row['display_name'], 'record_id': int(row['id']),
            'record_version': int(row['version']),
            'record_ref': f"memory:medication:{row['id']}@v{row['version']}",
            'scope_id': SCOPE, 'episode_id': row['episode_id'],
            'status': row['status'], 'dose': row['dose'], 'schedule': row['schedule'],
            'route': row['route'], 'start_at': row['start_at'], 'end_at': row['end_at'],
            'end_at_basis': row['end_at_basis'] if 'end_at_basis' in row.keys() else None,
            'version': int(row['version'])}


def _stopped_targets(product, name: str) -> list[dict[str, Any]]:
    """同名药**全部**停用记录，按版本倒序。多于一条时上层要补问是哪一次。"""
    wanted = _key(name)
    rows = product.memory.connection.execute(
        "SELECT * FROM medications WHERE status='stopped' ORDER BY version DESC").fetchall()
    return [_row_view(row) for row in rows if _key(row['display_name']) == wanted]


def validate_reading(reading: Any, *, text: str, product,
                     ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """把模型的理解过一遍**可核对性**。

    返回 ``(可落地的操作, 要补问的问题)``。任何一条判定的原文依据对不上，就直接
    丢掉那一条——没有依据的解释不是解释，是编的。
    """
    items: list[dict[str, Any]] = []
    questions: list[dict[str, Any]] = []
    for raw in (reading.get('items') or []) if isinstance(reading, dict) else []:
        if not isinstance(raw, dict):
            continue
        quote = str(raw.get('quote') or '')
        if not quote or (text and quote not in text):
            # 依据必须能在**用户的原话**里逐字找到。对不上就不采信这条。
            continue
        when = str(raw.get('when') or WHEN_UNCLEAR)
        if when not in WHEN_VALUES:
            when = WHEN_UNCLEAR
        uncertain = [str(value) for value in (raw.get('uncertain') or [])
                     if str(value) in UNCERTAINTIES]
        name = str(raw.get('drug_name') or '').strip()
        denies = str(raw.get('denies_name') or '').strip()
        item = {'when': when, 'quote': quote, 'name': name, 'denies': denies,
                # 名称的**依据片段**：用来判断用户是点名说的还是用指代说的。
                'drug_quote': str(raw.get('drug_quote') or ''),
                'denies_quote': str(raw.get('denies_quote') or ''),
                'operation': str(raw.get('operation') or 'none'),
                'field': str(raw.get('field') or 'none'),
                'value': str(raw.get('value') or '').strip(),
                'time_text': str(raw.get('time_text') or '').strip(),
                'time_normalised': str(raw.get('time_normalised') or '').strip(),
                'time_precision': str(raw.get('time_precision') or 'unknown'),
                'group_role': str(raw.get('group_role') or 'none'),
                'reported_overlap': str(raw.get('reported_overlap') or 'unstated'),
                'uncertain': uncertain}
        if when in (WHEN_PLANNED, WHEN_QUESTION, WHEN_MISSED_DOSE, WHEN_UNCLEAR):
            items.append(item)
            continue
        # 有动作的两种：对象必须唯一解析到一条权威记录。
        effective = _operation_of(item)
        if effective == _visits.CANDIDATE_ADD:
            # 新增**不需要**已有记录：药单里没有它，正是新增的前提，不是歧义。
            # 但如果这味药现在已经在用，用户说的"新增"就和记录对不上了——要问清楚，
            # 不能悄悄套成一次调整。
            existing, _matched = resolve_medication(product, name, status='active')
            if existing is not None:
                item['uncertain'] = sorted(set(uncertain) | {'conflicting'})
                item['existing'] = existing
            item['target'] = None
            items.append(item)
            continue
        status = 'stopped' if effective in (_visits.CANDIDATE_CORRECTION,
                                            _visits.CANDIDATE_RESUME) else 'active'
        target, matched = resolve_medication(product, name, status=status)
        if target is None:
            item['uncertain'] = sorted(set(uncertain) | {'object_ambiguous'})
            item['target'] = None
            items.append(item)
            continue
        item['target'] = target
        item['matched_by'] = matched
        # 歧义由**能不能唯一解析到一条权威记录**判定，不照抄模型的自评。
        #
        # 模型可能一边点名"合成药甲"、一边又把 object_ambiguous 标上（真实模型
        # 跑出来就是这样）。它自己在同一句话里前后矛盾，这时用一个**可核对**的
        # 规则解掉：`drug_quote` 是它给出的依据片段——名称**就出现在那段依据里**，
        # 说明用户是点名说的，不是指代。指代（"这个药"）则不在其中，歧义保留。
        if 'object_ambiguous' in item['uncertain'] and _names_explicitly(item, target):
            item['uncertain'] = [flag for flag in item['uncertain']
                                 if flag != 'object_ambiguous']
        items.append(item)
    raw_items = [entry for entry in (reading.get('items') or [])
                 if isinstance(entry, dict)] if isinstance(reading, dict) else []
    if raw_items and not items:
        # 模型说它读出了东西，但没有一条能对回原文——**不能静默丢掉**。
        questions.append({'about': 'no_evidence', 'quote': None,
                          'text': '我没能从这句话里确认出具体的变化，'
                                  '能不能再说得具体一点（哪种药、改成了什么）？'})
    # 需要补问的：判不出动作、对象不唯一、值缺失、前后矛盾。
    # 时间含糊**不在其列**——它由候选的 `reported_vague` 如实表达。
    for item in items:
        if item['when'] in (WHEN_QUESTION, WHEN_MISSED_DOSE):
            continue
        if _blocking_uncertainties(item):
            questions.append({'about': 'clarify', 'quote': item['quote'],
                              'text': _clarifying_question(item)})
    return items, questions


def _names_explicitly(item: dict[str, Any], target: dict[str, Any]) -> bool:
    """用户是不是**点名**说的这个药（而不是用"这个药""它"指代）。

    依据是模型自己给出的那段原文片段：名称出现在里面，就是点名。
    """
    quote = _key(item.get('drug_quote'))
    name = _key(target.get('name'))
    return bool(quote and name and name in quote)


def _blocking_uncertainties(item: dict[str, Any]) -> list[str]:
    """哪些不确定**真的**挡住候选。

    ``time_vague`` 不挡：时间含糊是**可表达**的——候选带 `reported_vague`、
    原文照留、不解析成具体日期。把"上周"当成"说不清所以不能登记"，等于因为
    时间说得不精确就拒绝记下这次变化。
    """
    return [flag for flag in item.get('uncertain') or [] if flag != 'time_vague']


def _clarifying_question(item: dict[str, Any]) -> str:
    name = item.get('name') or '这味药'
    if item.get('existing') is not None and item.get('operation') == 'add':
        return (f'{name}已经在当前用药记录里了。您是要调整它现在的用法，'
                f'还是新增另一种药？（原话：「{item["quote"]}」）')
    if 'object_ambiguous' in item['uncertain']:
        return f'您说的是哪一种药？（原话：「{item["quote"]}」）'
    if 'multiple_records' in item['uncertain']:
        return f'{name}停用过不止一次，您指的是哪一次？'
    if 'value_missing' in item['uncertain']:
        return f'{name}要改成什么？'
    if 'conflicting' in item['uncertain']:
        return f'「{item["quote"]}」前后对不上，能不能再说一遍具体是怎么用的？'
    return f'「{item["quote"]}」这句我还不能确定，能不能再说明一下？'


def plan_record(item: dict[str, Any], *, group_id: str | None = None) -> dict[str, Any]:
    """一条**计划**。它不进可确认列表——计划不是已经发生的事。

    用户日后报告"已经开始了"时，这条计划会被关联上，并生成一条**新的**实际操作
    候选，重新核对记录版本与时间。

    计划**也要带换药组标识**：不然"旧药已停、新药仍在计划"里那半句计划就丢了，
    页面只能读出"旧药停用已记录"，读不出"新药开始尚未确认发生"。
    """
    return {'operation': item.get('operation') if item.get('operation') != 'none' else None,
            'target': {'name': item.get('name') or ''},
            'changes': _changes_of(item),
            'time': _time_of(item),
            'quote': item['quote'],
            'group': _group_of(item, group_id=group_id),
            'status': 'planned'}


def _changes_of(item: dict[str, Any]) -> dict[str, Any]:
    field, value = item.get('field'), item.get('value')
    if field in _visits.CANDIDATE_FIELDS and value:
        return {field: value}
    return {}


def _time_of(item: dict[str, Any]) -> dict[str, Any]:
    """时间表达。**语义足够明确时才标准化**：'上周'保留原话，不给一个日期。"""
    normalised = item.get('time_normalised') or ''
    precision = item.get('time_precision') or 'unknown'
    if normalised and precision in ('exact', 'day') and _iso_date(normalised):
        return {'text': item.get('time_text') or normalised, 'value': normalised,
                'precision': precision, 'basis': 'reported'}
    if item.get('time_text'):
        # 说了时间但只说了个大概：原话留着，**不解析成具体日期**。
        return {'text': item['time_text'], 'value': None,
                'precision': precision if precision in READING_PRECISIONS else 'vague',
                'basis': 'reported_vague'}
    return {'text': None, 'value': None, 'precision': 'unknown', 'basis': 'unknown'}


def _group_of(item: dict[str, Any], *, group_id: str | None = None) -> dict | None:
    role = item.get('group_role')
    if role in _visits.GROUP_ROLES and group_id:
        return {'id': group_id, 'role': role}
    return None


def _iso_date(value: str) -> bool:
    import datetime
    try:
        datetime.date.fromisoformat(str(value)[:10])
        return True
    except ValueError:
        return False


def retry_note(product, *, note_id: str, interpreter: Any = None,
               tz: str | None = None, key: str | None = None) -> dict[str, Any]:
    """**显式**重试一次理解。

    同一个 key 的重放**不再花一次模型额度**：重试是一次有人按下的动作，而按下之后
    的重复请求（超时重发、双开）不该变成第二次调用。记录在命令回执里，与其它幂等
    一样持久。
    """
    if key and product.receipt(f'{note_id}:retry:{key}') is not None:
        return find_note(product, note_id)[0]
    note = interpret_note(product, note_id=note_id, interpreter=interpreter, tz=tz)
    if key:
        product.command(f'{note_id}:retry:{key}',
                        {'type': 'change_note_retry', 'note_id': note_id},
                        lambda: {'retried': note_id})
    return note


def apply_reading(product, *, visit_id: str, note: dict[str, Any],
                  reading: dict[str, Any]) -> dict[str, Any]:
    """把一次通过校验的理解落到候选/计划/问题上。

    顺序是刻意的：**先撤回被纠正的候选**，再生成新的。这样任何时候都不会同时存在
    两条互相矛盾、都可确认的当前候选。
    """
    visits = _visits.ReviewVisitStore(product)
    visit = visits.get(visit_id)
    items, questions = validate_reading(reading, text=note.get('text') or '', product=product)
    unsupported = [{'what': str(entry.get('what') or ''),
                    'quote': str(entry.get('quote') or ''),
                    'guidance': '这种变更请在「用药记录」页面用结构化方式登记。'}
                   for entry in (reading.get('unsupported') or [])
                   if isinstance(entry, dict)]
    asked = str(reading.get('question') or '').strip()
    if asked:
        questions.insert(0, {'about': 'asked', 'text': asked, 'quote': None})

    group_id = None
    if sum(1 for item in items if item.get('group_role') in _visits.GROUP_ROLES) >= 2:
        group_id = f"switch:{note['id']}"

    superseded: list[str] = []
    plans: list[dict[str, Any]] = []
    candidate_ids: list[str] = []
    origin = {'kind': 'note', 'note_id': note['id'],
              'interpretation_revision': int(note.get('attempt') or 1)}

    for item in items:
        when = item['when']
        if when in (WHEN_QUESTION, WHEN_MISSED_DOSE, WHEN_UNCLEAR):
            # 询问、漏服、判不出来：**都不写**。漏服作为一条用户报告留在这里。
            continue
        if when == WHEN_PLANNED:
            plans.append(plan_record(item, group_id=group_id))
            continue
        if _blocking_uncertainties(item):
            continue                     # 判不实的不生成候选，转成问题
        denied = item.get('denies')
        if denied:
            for candidate in list(visits.pending_candidates(visit_id)):
                if _key((candidate.get('target') or {}).get('name')) != _key(denied):
                    continue
                # 纠正先前候选时**保留历史**：撤回它，不删除、不覆盖。
                visits.supersede_candidate(
                    visit_id, candidate['id'],
                    superseded_by=f"{note['id']}:{item['quote']}",
                    reason=f"用户后来的说法不再支持这一条：{item['quote']}",
                    command_key=f"{note['id']}:{candidate['id']}:supersede")
                superseded.append(candidate['id'])
        operation = _operation_of(item)
        if operation is None:
            continue
        target = item.get('target')
        if target is None and operation not in (_visits.CANDIDATE_ADD,):
            continue
        if operation in (_visits.CANDIDATE_RESUME, _visits.CANDIDATE_CORRECTION):
            options = _stopped_targets(product, item.get('name'))
            if len(options) > 1:
                questions.append({'about': 'which_record', 'quote': item['quote'],
                                  'text': _clarifying_question(
                                      {**item, 'uncertain': ['multiple_records']})})
                continue
            if not options:
                questions.append({'about': 'no_record', 'quote': item['quote'],
                                  'text': f"{item.get('name')}没有可据以恢复的停用记录。"})
                continue
            target = options[0]
        spec = {
            'operation': operation,
            'target': {'name': target['name'] if target else item.get('name'),
                       'matched_by': item.get('matched_by') or 'unmatched',
                       'record_id': target['record_id'] if target else None,
                       'record_version': target['record_version'] if target else None,
                       'record_ref': target['record_ref'] if target else None,
                       'scope_id': target['scope_id'] if target else None,
                       'episode_id': target['episode_id'] if target else None},
            'changes': _changes_of(item),
            'before': (_before_of(target, item) if target else {}),
            'before_ref': target['record_ref'] if target else None,
            'occurred': _time_of(item),
            'group': _group_of(item, group_id=group_id),
            # 换药时"两种药有没有同时吃过一段时间"是用户才能回答的，模型只转述。
            'reported_overlap': ({'yes': True, 'no': False}.get(item.get('reported_overlap'))
                                  if item.get('reported_overlap') in ('yes', 'no') else None),
            'origin': origin,
        }
        try:
            updated = visits.add_change_candidate(
                visit_id, spec=spec, source=_visits.SOURCE_MODEL_PROPOSED,
                basis={'kind': 'model_explanation', 'refs': [note['id']],
                       'quote': item['quote'], 'note': note.get('text')},
                command_key=(f"{note['id']}:{visits.get(visit_id)['revision']}:candidate:"
                             f"{_visits.candidate_command_key(spec)}"))
        except ProductError as exc:
            questions.append({'about': 'not_applicable', 'quote': item['quote'],
                              'text': f"「{item['quote']}」：{exc}"})
            continue
        fresh = _visits.ReviewVisitStore(product).get(visit_id)
        newest = [item_ for item_ in fresh.get('change_candidates') or []
                  if item_['status'] == _visits.CANDIDATE_PENDING
                  and _visits.candidate_identity(item_) == _visits.candidate_identity(spec)]
        if newest:
            candidate_ids.append(newest[-1]['id'])
    return {'candidate_ids': candidate_ids, 'superseded_ids': superseded,
            'plans': plans, 'questions': questions, 'unsupported': unsupported}


def _operation_of(item: dict[str, Any]) -> str | None:
    operation = item.get('operation')
    if item['when'] == WHEN_CORRECTION and operation == 'none':
        return _visits.CANDIDATE_CORRECTION
    if operation in _visits.CANDIDATE_OPERATIONS:
        return operation
    return None


def _before_of(target: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
    """`before` 一律取自**权威记录**，不采信任何自报的"原来是 X"。"""
    field = item.get('field')
    if field in _visits.CANDIDATE_FIELDS and field in target:
        return {field: target.get(field)}
    return {}


# ---- 编排 -------------------------------------------------------------------
def interpret_note(product, *, note_id: str, interpreter: Any = None,
                   tz: str | None = None) -> dict[str, Any]:
    """收录之后的一次理解。**不启动调查**：一句话的理解不该跑一整轮 Agent。"""
    note, visit = find_note(product, note_id)
    if note['status'] in (NOTE_INTERPRETED, NOTE_UNAVAILABLE):
        return note
    attempt = int(note.get('attempt') or 0) + 1
    begun = begin_attempt(product, note_id, attempt=attempt)
    if begun['status'] != NOTE_INTERPRETING or int(begun.get('attempt') or 0) != attempt:
        # 命令回执说明这次 attempt 已经跑过（重放/双击）——不再花一次模型额度。
        return begun

    if not note['text'] and note.get('speech_act_hint'):
        # 用户只点了一个明确的状态按钮：这就是全部信息，**不需要模型**。
        # 点一下按钮却去调一次模型，既慢又是在给一个确定性的事加不确定性。
        return finish_attempt(
            product, note_id, attempt=attempt, status=NOTE_INTERPRETED,
            reading={'summary': f"用户选择了状态：{note['speech_act_hint']}",
                     'items': [], 'question': '', 'unsupported': [],
                     'deterministic': True, 'speech_act': note['speech_act_hint']},
            usage={'calls': 0, 'tokens': 0, 'usage_unknown': False,
                   'quality': 'not_applicable'},
            model={'provider': None, 'model': None})

    interpreter = interpreter or ChangeNoteInterpreter(memory=product.memory)
    ok, reason = interpreter.available()
    if not ok:
        # 没有模型：原文留着，如实说明**没有做**自动理解。
        return finish_attempt(product, note_id, attempt=attempt,
                              status=NOTE_UNAVAILABLE, error=reason,
                              usage={'calls': 0, 'tokens': None, 'usage_unknown': False})
    context = reading_context(product, visit, tz=tz)
    try:
        reading, usage = interpreter.read(text=note['text'], context=context,
                                          hint=note.get('speech_act_hint'))
    except Exception as exc:
        return finish_attempt(product, note_id, attempt=attempt, status=NOTE_FAILED,
                              error=f'{type(exc).__name__}: {exc}',
                              usage={'calls': None, 'tokens': None, 'usage_unknown': True})
    applied = apply_reading(product, visit_id=visit['id'], note=note, reading=reading)
    return finish_attempt(
        product, note_id, attempt=attempt, status=NOTE_INTERPRETED,
        reading=reading, applied=applied, usage=usage,
        model=_model_ref(interpreter))


def _model_ref(interpreter: Any) -> dict[str, Any]:
    config = interpreter.config() if hasattr(interpreter, 'config') else None
    return {'provider': (config or {}).get('provider'), 'model': (config or {}).get('model')}


def reading_context(product, visit: dict[str, Any], *, tz: str | None = None) -> dict[str, Any]:
    """理解一句话需要的**最少**上下文：当前药单、停用记录、这次回访在等什么。

    刻意不给它工具、不让它检索——理解一句话不需要搜索资料。
    """
    import datetime
    stopped = [_row_view(row) for row in product.memory.connection.execute(
        "SELECT * FROM medications WHERE status='stopped' ORDER BY version DESC LIMIT 20")]
    case_id = visit.get('case_id')
    questions: list[str] = []
    try:
        from .safety_cases import SafetyCaseStore
        case = SafetyCaseStore(product).get(case_id)
        questions = [str(item.get('question')) for item in case.get('required_inputs') or []
                     if item.get('status') == 'open']
    except Exception:
        questions = []
    return {
        'today': datetime.datetime.now(datetime.timezone.utc).date().isoformat(),
        'tz': tz or os.getenv('STAGE0_TZ') or 'local',
        'medications': [{'display_name': item['display_name'], 'dose': item.get('dose'),
                         'schedule': item.get('schedule'), 'route': item.get('route'),
                         'start_at': item.get('start_at')}
                        for item in product.memory.current_medications()],
        'stopped_medications': [{'display_name': item['name'], 'end_at': item.get('end_at')}
                                for item in stopped],
        'open_questions': questions,
    }


def submit_note(product, *, visit_id: str, text: str, hint: str | None = None,
                tz: str | None = None, key: str | None = None,
                interpreter: Any = None, expected_revision: int | None = None) -> dict[str, Any]:
    """一次「补充情况」提交：收录 → （若可用）理解一次 → 落到候选/计划/问题。"""
    visits = _visits.ReviewVisitStore(product)
    visit = visits.get(visit_id)
    if expected_revision is not None and visit['revision'] != expected_revision:
        raise ProductError('这次回访已被其他操作更新，请刷新', 409)
    cleaned = screen(text, hint=hint)
    note = record_note(product, visit_id, cleaned, hint=hint, tz=tz, key=key)
    if note['status'] in (NOTE_INTERPRETED, NOTE_UNAVAILABLE):
        return note
    return interpret_note(product, note_id=note['id'], interpreter=interpreter, tz=tz)


def register_change_note_routes(app, product, access, invoke, principal=None):
    """「补充情况」的入口：提交一段自由文本、读回这条回访上的补充、显式重试。

    重试是**独立端点**且必须由用户发起：不自动换模型、不自动追加批次、不扩大预算。
    """
    from fastapi import Request
    # 本模块开了 `from __future__ import annotations`，注解是字符串。FastAPI 靠
    # 函数所在模块的全局去解析 `Request`，只写局部导入会被当成一个查询参数。
    globals()['Request'] = Request
    from .safety_cases import SafetyCaseStore, _refresh_visit_result

    def _interpreter():
        return getattr(app.state, 'change_note_interpreter', None)

    def _visit_or_404(visit_id: str, case_id: str) -> dict[str, Any]:
        visit = _visits.ReviewVisitStore(product).get(visit_id)
        if visit['case_id'] != case_id:
            raise ProductError('这次补充不属于该事项', 404)
        return visit

    @app.post('/v1/safety-cases/{case_id}/visits/{visit_id}/notes')
    def submit(case_id: str, visit_id: str, request: Request, body: dict):
        access(request, True)
        _visit_or_404(visit_id, case_id)

        def run():
            note = submit_note(product, visit_id=visit_id, text=body.get('text'),
                               hint=body.get('speech_act'), tz=body.get('tz'),
                               key=body.get('key'), interpreter=_interpreter(),
                               expected_revision=body.get('expected_revision'))
            _refresh_visit_result(product, SafetyCaseStore(product), case_id, visit_id)
            return note
        return invoke(run)

    @app.get('/v1/safety-cases/{case_id}/visits/{visit_id}/notes')
    def list_notes(case_id: str, visit_id: str, request: Request):
        access(request)
        visit = _visit_or_404(visit_id, case_id)
        return {'items': [dict(item) for item in visit.get('change_notes') or []]}

    @app.post('/v1/safety-cases/{case_id}/visits/{visit_id}/notes/{note_id}/retry')
    def retry(case_id: str, visit_id: str, note_id: str, request: Request, body: dict):
        """显式重试一次理解。**不自动重试、不换模型、不扩大预算。**"""
        access(request, True)
        _visit_or_404(visit_id, case_id)

        def run():
            note = retry_note(product, note_id=note_id, interpreter=_interpreter(),
                              tz=body.get('tz'), key=body.get('key'))
            _refresh_visit_result(product, SafetyCaseStore(product), case_id, visit_id)
            return note
        return invoke(run)


__all__ = [
    'register_change_note_routes', 'retry_note',
    'MAX_TEXT', 'NOTE_RECEIVED', 'NOTE_INTERPRETING', 'NOTE_INTERPRETED',
    'NOTE_UNAVAILABLE', 'NOTE_FAILED', 'NOTE_STATUSES',
    'WHEN_OCCURRED', 'WHEN_PLANNED', 'WHEN_QUESTION', 'WHEN_CORRECTION',
    'WHEN_MISSED_DOSE', 'WHEN_UNCLEAR', 'WHEN_VALUES', 'WHEN_ACTIONABLE',
    'UNCERTAINTY_LABELS', 'INTERPRET_LIMITS', 'ChangeNoteInterpreter',
    'screen', 'record_note', 'find_note', 'begin_attempt', 'finish_attempt',
    'validate_reading', 'apply_reading', 'interpret_note', 'submit_note',
    'reading_context', 'resolve_medication', 'plan_record',
]
