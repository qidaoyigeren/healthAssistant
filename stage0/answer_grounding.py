"""答案可信性：来源解析、引文—片段绑定、支持关系与依赖失效。

本模块把"这句话是从哪来的、核对到什么程度"从**模型自述**变成**可按真实记录
复算**的判定。它是 `investigation.answer_question` 的判定内核，也可以被任何
需要"这条答案现在还可信吗"的地方直接调用。

三条边界写在最前面，实现里的每一处取舍都回到它们：

1. **来源种类由真实记录解析，不信模型自报。** 模型说 ``user_answer`` 不等于
   真的有一条用户回答；``professional`` 需要真实医护服务，本项目没有连接，
   所以它永远解析不出真实来源。本地模拟记录也不能被包装成专业确认。
2. **引文绑定到指定来源的、实际回读过的片段。** 多份原文**不拼接**——拼接
   会让 A 的引文配上 B 的原文，也会在接缝处制造原文里并不存在的相邻关系。
   校验时也**不重新读取**模型没看过的内容：用的是回读回执，不是再次翻库。
3. **来源存在 / 引文匹配 / 答案支持是三件事。** 引文逐字存在，不等于它陈述了
   这个答案。精确结构化字段（对象、值、单位、状态、版本）可以机械核对；自由
   文本无法可靠验证支持关系时，如实记为 ``candidate``，绝不仅因引文存在就
   写成 ``verified``。

本模块**不认识**数据库、工具执行器和调查状态：真实记录一律由调用方以
``lookup_*`` 注入。因此它可以独立测试，也不与 ``investigation`` 成环。

`verified` 的含义边界（必须与 CONTRACT §3.3 一致）：它**仅**表示约定范围内的
答案依据已核对，**不**表示整体用药安全，也**不**表示已经完成专业医疗判断。
"""
from __future__ import annotations

from dataclasses import dataclass, field
import re
import unicodedata
from typing import Any, Callable, Iterable, Mapping, Sequence

__all__ = [
    'ASSESSMENT_STATUSES', 'STATUS_VERIFIED', 'STATUS_CANDIDATE', 'STATUS_STALE',
    'STATUS_UNSUPPORTED', 'SOURCE_KINDS', 'MODEL_SUBMITTABLE_SOURCES',
    'SOURCE_PATIENT_RECORD', 'SOURCE_EVIDENCE', 'SOURCE_MATERIAL',
    'SOURCE_USER_ANSWER', 'SOURCE_PROFESSIONAL', 'STRUCTURED_FIELDS',
    'ReadWindow', 'ReadLedger', 'RecordFacts', 'Resolution', 'Grounding',
    'GroundingContext', 'assess', 'resolve_source', 'assessment_of',
    'assessment_status', 'dependency_refs_for', 'parse_versioned_ref',
    'versions_from_snapshot', 'dependency_state', 'retire', 'withdraw',
    'revalidate', 'VERIFIED_MEANING',
]

# ---- status（冻结，见 CONTRACT §3.3） ---------------------------------------
STATUS_VERIFIED = 'verified'
STATUS_CANDIDATE = 'candidate'
STATUS_STALE = 'stale'
STATUS_UNSUPPORTED = 'unsupported'
ASSESSMENT_STATUSES = (STATUS_VERIFIED, STATUS_CANDIDATE, STATUS_STALE,
                       STATUS_UNSUPPORTED)

#: `verified` 的含义边界。写进实现、给 UI 抄，不要让任何一处重新解释这四个字。
VERIFIED_MEANING = ('约定范围内的答案依据已核对；不表示整体用药安全，'
                    '也不表示已完成专业医疗判断')

# ---- 来源种类（与 ``investigation.ANSWER_SOURCE_*`` 同值） ------------------
# 值在这里**再声明一次**而不是 import investigation：那是成环的。同名同值是
# 契约，test_answer_grounding 里有一条测试专门钉住两边一致。
SOURCE_PATIENT_RECORD = 'patient_record'
SOURCE_EVIDENCE = 'evidence'
SOURCE_MATERIAL = 'material'
SOURCE_USER_ANSWER = 'user_answer'
SOURCE_PROFESSIONAL = 'professional'
SOURCE_KINDS = (SOURCE_PATIENT_RECORD, SOURCE_EVIDENCE, SOURCE_MATERIAL,
                SOURCE_USER_ANSWER, SOURCE_PROFESSIONAL)

#: 模型**可以**在一次工具调用里提交的来源种类。
#:
#: ``user_answer`` 与 ``professional`` **不在**其中，这是刻意的：
#:
#: * 用户回答由提交路径（``/v1/safety-cases/{id}/answer``）写进调查，不由模型
#:   在工具调用里声明——否则模型只要写 ``source=user_answer`` 就能凭空造出
#:   一条"用户说过的话"。
#: * 专业意见需要**真实医护服务**。本项目没有连接，所以它解析不出真实来源；
#:   把本地模拟工作台的决定包装成"专业医疗确认"是这条路径上最危险的谎。
MODEL_SUBMITTABLE_SOURCES = (SOURCE_PATIENT_RECORD, SOURCE_EVIDENCE, SOURCE_MATERIAL)

#: 可以机械核对"答案是否被来源陈述"的字段。不在其中的（自由文本、开放问题）
#: 一律记 ``candidate``：不是"核对通过"，也不是"没有依据"。
STRUCTURED_FIELDS = frozenset({
    'dose', 'schedule', 'start_date', 'start_at', 'end_date', 'end_at',
    'route', 'frequency', 'duration', 'name', 'display_name', 'status',
})

#: 记录必须处于这个状态才算**当前**权威值。``medications.status`` 的取值域是
#: active/stopped/superseded/disputed，只有 active 是"现在的用药"。
CURRENT_RECORD_STATUS = 'active'


def _normalise(value: Any) -> str:
    """归一化：兼容字符（全角/半角）、空白、大小写。

    与 ``investigation._normalise_answer`` 同口径并多做一步 NFKC，所以
    "５ｍｇ"、"5 mg"、"5MG" 在这里是同一个值。
    """
    text = unicodedata.normalize('NFKC', str(value if value is not None else ''))
    return re.sub(r'\s+', '', text).casefold()


_CJK_DIGITS = {'〇': '0', '零': '0', '一': '1', '二': '2', '两': '2', '三': '3',
               '四': '4', '五': '5', '六': '6', '七': '7', '八': '8', '九': '9'}


def _attestation_form(value: Any) -> str:
    """用于"引文有没有陈述这个值"的比对形式。

    在 ``_normalise`` 之上把中文数字折成阿拉伯数字：中文材料里"每日一次"与
    记录里的"每日1次"指的是同一件事，不该因为写法不同就判成"引文不支持"。
    这**不是**宽松匹配——比对仍然是逐字的，只是先把两种等价的计数写法统一。
    """
    text = _normalise(value)
    return ''.join(_CJK_DIGITS.get(ch, ch) for ch in text)


# ---- 回读回执：引文绑定到指定来源的、真的读过的片段 -------------------------

@dataclass(frozen=True)
class ReadWindow:
    """一次 ``read_*`` 真的返回给模型的片段。"""
    ref: str
    offset: int
    content: str

    @property
    def end(self) -> int:
        return self.offset + len(self.content)


class ReadLedger:
    """本 run（或跨会话复用的）**实际回读**过的片段，按来源分开保存。

    与"检索到了"无关：只有真的走了 ``read_evidence`` / ``read_material_item``
    的片段才在这里。检索结果的摘要视图是**派生视图**，不携带引用权——这是
    ``harness.evidence`` 的既有口径，这里沿用而不另立一套。

    分开保存是关键：``_read_contents`` 旧写法把多份原文 ``'\\n'.join`` 成一个
    大字符串，于是 A 的引文可以拿去配 B 的来源，接缝处还能拼出一句原文里根本
    不存在的话。
    """

    def __init__(self, windows: Iterable[ReadWindow] = ()):
        self._windows: dict[str, list[ReadWindow]] = {}
        for window in windows:
            self.record(window.ref, window.offset, window.content)

    def record(self, ref: str, offset: int, content: str) -> ReadWindow:
        window = ReadWindow(str(ref), int(offset), str(content))
        self._windows.setdefault(window.ref, []).append(window)
        return window

    def refs(self) -> tuple[str, ...]:
        return tuple(self._windows)

    def has_ref(self, ref: str) -> bool:
        return bool(self._windows.get(str(ref)))

    def windows(self, ref: str) -> tuple[ReadWindow, ...]:
        return tuple(self._windows.get(str(ref), ()))

    def segments(self, ref: str) -> tuple[str, ...]:
        """这个来源**实际读过**的连续片段。

        只有**相接**的回读窗口才会合并：``[0,2000)`` 与 ``[2000,4000)`` 是
        原文里真正相邻的两段，可以连起来判引文；``[0,2000)`` 与 ``[5000,7000)``
        中间隔着没读过的部分，合并就会让引文"跨过"一段没人看过的原文——所以
        它们保持两段，引文只能落在其中一段之内。
        """
        windows = sorted(self.windows(ref), key=lambda w: (w.offset, w.end))
        segments: list[str] = []
        start = end = None
        for window in windows:
            if not window.content:
                continue
            if start is None:
                start, end, segments = window.offset, window.end, [window.content]
                continue
            if window.offset > end:
                start, end = window.offset, window.end
                segments.append(window.content)
                continue
            # 相接或重叠：同一来源的原文是不变内容，接上没读过的尾巴即可。
            if window.end > end:
                segments[-1] += window.content[max(0, end - window.offset):]
                end = window.end
        return tuple(segments)

    def quote_in(self, ref: str, quote: Any) -> bool:
        """引文是不是**这个来源**已读片段里逐字存在的一段。

        逐字（不做归一化），并且只在**同一来源**的**同一连续片段**内匹配。
        """
        text = str(quote or '')
        if not text.strip():
            return False
        return any(text in segment for segment in self.segments(ref))

    def merge(self, other: 'ReadLedger') -> None:
        """并入另一本回执（跨会话恢复时用）。已记过的窗口不重复记。"""
        for ref in other.refs():
            known = {(w.offset, w.content) for w in self.windows(ref)}
            for window in other.windows(ref):
                if (window.offset, window.content) not in known:
                    self.record(ref, window.offset, window.content)

    def to_list(self) -> list[dict[str, Any]]:
        return [{'ref': w.ref, 'offset': w.offset, 'content': w.content}
                for ref in self.refs() for w in self.windows(ref)]

    @classmethod
    def from_list(cls, items: Iterable[Mapping[str, Any]]) -> 'ReadLedger':
        ledger = cls()
        for item in items or ():
            if isinstance(item, Mapping):
                ledger.record(item.get('ref', ''), item.get('offset', 0),
                              item.get('content', ''))
        return ledger

    @classmethod
    def legacy(cls, refs: Iterable[str], content_of: Callable[[str], str | None],
               *, limit: int | None = None) -> 'ReadLedger':
        """给**旧状态**补一本回执：老记录只存了 ``read_refs`` 名单，没存窗口。

        这是一条**兼容**路径，不是校验路径：只有在滚动升级前的持久化状态上才会
        走到（``read_refs`` 有名字、窗口为空）。新读的片段一律经 ``record``
        写入真实窗口，不会从这里经过。``content_of`` 读不到就跳过——宁可少一条
        候选回执，也不凭空造一条。
        """
        ledger = cls()
        for ref in refs or ():
            content = content_of(ref)
            if content:
                ledger.record(ref, 0, content[:limit] if limit else content)
        return ledger


# ---- 真实记录 ---------------------------------------------------------------

@dataclass
class RecordFacts:
    """一条**真实记录**上被核对到的字段。

    ``value`` 是记录里的值；``unit`` / ``status`` / ``version`` 是记录**声明了
    就填**的部分。没声明的字段（例如只回值的最小替身）不会被当成"核对过"，
    判定的 ``reason`` 里会写明这一点——**可审计**比"看起来很确定"重要。
    """
    value: Any = None
    unit: str | None = None
    status: str | None = None
    version: int | None = None
    locator: str | None = None
    #: 这条记录是不是**当前**的。``False`` = 它已不在当前权威集合里（停用、
    #: 被替换、或属于别的对象）。历史版本照样能按 ref 取出来，所以"取得到"
    #: 绝不等于"还是现在的值"。
    current: bool = True

    def states(self) -> tuple[str, ...]:
        return tuple(name for name in ('value', 'unit', 'status', 'version')
                     if getattr(self, name) is not None)


@dataclass
class Resolution:
    """模型声明的来源 → **真实记录**解析出的来源。"""
    kind: str | None = None
    source_ref: str | None = None
    object_ref: str | None = None
    record: RecordFacts | None = None
    reason: str = ''
    detail: str = ''


@dataclass
class Grounding:
    """一次答案判定的完整结果。

    ``assessment`` 就是要写进答案元素的 §3.2 形状；``accepted`` 表示"这条答案
    依据成立、可以记录"；``stage`` 指出判定停在哪一步（来源存在 / 引文匹配 /
    答案支持），供测试与排错定位，**不**写进 assessment。
    """
    assessment: dict[str, Any]
    accepted: bool = False
    stage: str = 'source'
    kind: str | None = None
    errors: list[str] = field(default_factory=list)
    detail: str = ''

    @property
    def status(self) -> str:
        return self.assessment['status']


@dataclass
class GroundingContext:
    """判定需要的全部**真实记录**读取口。由调用方注入，模块自己不碰数据库。

    * ``lookup_record(ref, field)`` → 版本化的权威记录里该对象该字段的事实；
      对象不存在 / 字段没有值 → ``None``。
    * ``visible_ref(ref)`` → 这个引用**真的存在且属于当前作用域**吗。
    * ``lookup_material(ref, field)`` → 本 run 回读过的材料条目的字段事实。
    * ``lookup_professional(ref)`` → 真实的专业复核记录。本项目**没有**连接真实
      医护服务，所以调用方传进来的实现永远回 ``None``；保留这个口是为了将来
      接上真实服务时不用改判定逻辑。
    """
    ledger: ReadLedger = field(default_factory=ReadLedger)
    lookup_record: Callable[[str, str | None], RecordFacts | None] | None = None
    visible_ref: Callable[[str], bool] | None = None
    lookup_material: Callable[[str, str | None], RecordFacts | None] | None = None
    lookup_professional: Callable[[str], Any | None] | None = None


# ---- 依赖引用（CONTRACT §1.4：``<layer>:<kind>:<id>@<version>``） ------------

_VERSIONED_REF = re.compile(r'^(?P<prefix>[a-z_]+:[a-z_]+:\d+)@v(?P<version>\d+)$')


def parse_versioned_ref(ref: Any) -> tuple[str, int] | None:
    """``memory:medication:45@v2`` → ``('memory:medication:45', 2)``。"""
    match = _VERSIONED_REF.match(str(ref or ''))
    return (match.group('prefix'), int(match.group('version'))) if match else None


def dependency_refs_for(*refs: Any) -> list[str]:
    """去重、保序地留下**版本化**引用。非版本化的字符串不进来。"""
    out: list[str] = []
    for ref in refs:
        text = str(ref or '')
        if parse_versioned_ref(text) and text not in out:
            out.append(text)
    return out


def versions_from_snapshot(medications: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    """``snapshot()['medications']`` → ``{'memory:medication:45': 2}``。

    给 B 用的：依赖核对需要的是"每个对象**现在**是第几版"，这正好是权威快照
    里每条记录自己的版本。不用另建一张版本表。
    """
    versions: dict[str, int] = {}
    for item in medications or ():
        parsed = parse_versioned_ref((item or {}).get('ref'))
        if parsed:
            versions[parsed[0]] = parsed[1]
    return versions


def dependency_state(dependency_refs: Iterable[str], *,
                     current_version_of: Mapping[str, int] | Callable[[str], int | None],
                     ) -> dict[str, Any]:
    """这些版本化依赖**现在**还成立吗。

    ``current_version_of`` 可以是映射（``versions_from_snapshot(...)``）或函数。
    返回 ``{'state', 'changed', 'checked', 'unknown', 'detail'}``：

    * ``current``——所有依赖都还是引用时的那一版；
    * ``stale``——至少一条依赖的版本变了（列出是哪几条）；
    * ``unknown``——有依赖现在查不到版本（对象没了或没提供版本表），
      **不当成 current**：查不到不等于没变。
    """
    checked: list[str] = []
    changed: list[str] = []
    unknown: list[str] = []
    lookup = (current_version_of.get if isinstance(current_version_of, Mapping)
              else current_version_of)
    for ref in dependency_refs or ():
        parsed = parse_versioned_ref(ref)
        if not parsed:
            continue
        prefix, pinned = parsed
        checked.append(prefix)
        try:
            now = lookup(prefix)
        except Exception:
            now = None
        if now is None:
            unknown.append(prefix)
        elif int(now) != pinned:
            changed.append(prefix)
    if changed:
        state, detail = 'stale', f'依赖版本已变化：{"、".join(changed)}'
    elif unknown:
        state, detail = 'unknown', f'依赖当前版本查不到：{"、".join(unknown)}'
    else:
        state, detail = 'current', '依赖版本与引用时一致'
    return {'state': state, 'changed': changed, 'checked': checked,
            'unknown': unknown, 'detail': detail}


def retire(assessment: Mapping[str, Any] | None, *, changed_refs: Sequence[str] = (),
           reason: str | None = None) -> dict[str, Any]:
    """把一条已核对的答案**降级为 stale**（依赖版本变了，需要重新核对）。

    **幂等**：函数是确定性的——同样的输入永远返回相等的字典，所以重复调用
    （包括对已经 stale 的结果再调一次）不会改写任何东西。
    **单向**：只会降级，任何情况下都不会把答案升回 ``verified``——升级只能由
    一次真实的核对动作产生。``verified`` 之外的状态（``candidate`` 等）也照降，
    因为它们依赖的版本同样变了。
    """
    current = dict(assessment or {})
    current['status'] = STATUS_STALE
    current['reason'] = reason or (f'依赖版本已变化：{"、".join(changed_refs)}'
                                   if changed_refs else '依赖版本已变化，需要重新核对')
    # 保留 source_ref：stale 是"依据还在、需要重核"，不是"没有依据"。丢引用会
    # 让重新核对无从下手。
    current['source_ref'] = current.get('source_ref') or None
    current.setdefault('locator', None)
    current.setdefault('dependency_refs', [])
    return current


def withdraw(assessment: Mapping[str, Any] | None, *, reason: str) -> dict[str, Any]:
    """来源**失效**后撤回可信性：状态落到 ``unsupported``。

    与 ``retire`` 分开是有意的：版本变化是"依据还在、但需要重新核对"（stale），
    来源本身没了／读不到是"现在没有可支撑这条答案的依据"（unsupported）。
    两者都不再是 ``verified``。
    """
    current = dict(assessment or {})
    current['status'] = STATUS_UNSUPPORTED
    current['reason'] = reason
    current['source_ref'] = None
    current.setdefault('locator', None)
    current.setdefault('dependency_refs', [])
    return current


def revalidate(assessment: Mapping[str, Any] | None, *,
               source_available: bool = True,
               current_version_of: Mapping[str, int] | Callable[[str], int | None] | None = None,
               detail: str | None = None) -> dict[str, Any]:
    """一条**旧答案**现在的可信性。B 的重开/失效协调点调这个。

    * 来源已不可用 → ``withdraw``；
    * 依赖版本变了 → ``retire``；
    * 都还好 → **原样返回**（不补默认值、不升级；没有 assessment 的老答案
      返回 ``{}``，消费方按"未核实"处理，见 CONTRACT §3.4）。

    **幂等**：同样的输入重复调用返回相等的字典。
    """
    if not assessment:
        return {}                      # 老答案：没有 assessment 就是没有。不补。
    if not source_available:
        return withdraw(assessment, reason=detail or '来源已不可用，可信性撤回')
    if current_version_of is not None:
        state = dependency_state(assessment.get('dependency_refs') or (),
                                 current_version_of=current_version_of)
        if state['state'] == 'stale':
            return retire(assessment, changed_refs=state['changed'], reason=detail)
    return dict(assessment)


def assessment_of(answer: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """答案元素上的 assessment；没有就是 ``None``。

    **不返回默认值**：CONTRACT §3.4 要求"缺失即未核实"，任何补默认值的写法都会
    让老答案看起来比实际更可信。
    """
    if not isinstance(answer, Mapping):
        return None
    assessment = answer.get('assessment')
    return dict(assessment) if isinstance(assessment, Mapping) else None


def assessment_status(answer: Mapping[str, Any] | None) -> str | None:
    """答案的可信状态；**没有 assessment 的老答案返回 ``None``**（=未核实）。

    消费方必须把 ``None`` 与四种 status 分开显示——5 个状态，不是 4 个。
    """
    assessment = assessment_of(answer)
    return assessment.get('status') if assessment else None


# ---- 判定 -------------------------------------------------------------------

def _refuse(reason: str, detail: str, *, stage: str, source_ref: str | None = None,
            dependency_refs: Sequence[str] = (), locator: str | None = None,
            kind: str | None = None) -> Grounding:
    return Grounding(
        assessment={'status': STATUS_UNSUPPORTED, 'reason': detail,
                    'source_ref': source_ref, 'locator': locator,
                    'dependency_refs': list(dependency_refs)},
        accepted=False, stage=stage, kind=kind, errors=[reason], detail=detail)


def resolve_source(*, declared: str, source_ref: str | None, target_field: str | None,
                   ctx: GroundingContext) -> Resolution:
    """模型声明的来源 → 真实记录能支持的那个来源。

    **模型自报在这里被丢掉。** 解析不出来就没有真实来源，答案不会被标成
    "已有依据"——不管模型把 ``source`` 写成什么。
    """
    if declared == SOURCE_USER_ANSWER:
        return Resolution(
            reason='user_answer_not_model_declarable',
            detail=('用户回答由提交路径写入调查，不能由模型在工具调用里声明；'
                    '没有对应的回答记录就不能记为一次采纳'))
    if declared == SOURCE_PROFESSIONAL:
        record = (ctx.lookup_professional(source_ref) if ctx.lookup_professional
                  and source_ref else None)
        if record is None:
            return Resolution(
                reason='no_professional_service',
                detail=('本项目未连接真实医护服务，也没有针对这条问题的专业复核记录；'
                        '模型不能自行制造专业意见，本地模拟记录也不能当作专业确认'))
        return Resolution(kind=SOURCE_PROFESSIONAL, source_ref=source_ref,
                          reason='professional_record', detail='来自真实专业复核记录')
    if declared == SOURCE_PATIENT_RECORD:
        record = (ctx.lookup_record(source_ref, target_field)
                  if ctx.lookup_record and source_ref else None)
        if record is None:
            return Resolution(reason='record_missing',
                              detail=f'当前权威记录里没有 {source_ref or "该对象"} 的 '
                                     f'{target_field or "该字段"}')
        return Resolution(kind=SOURCE_PATIENT_RECORD, source_ref=source_ref,
                          object_ref=source_ref, record=record,
                          reason='record_found', detail='解析到当前权威记录')
    if declared == SOURCE_EVIDENCE:
        return Resolution(kind=SOURCE_EVIDENCE, source_ref=source_ref,
                          reason='evidence_ref', detail='引用了本 run 回读过的证据')
    if declared == SOURCE_MATERIAL:
        record = (ctx.lookup_material(source_ref, target_field)
                  if ctx.lookup_material and source_ref else None)
        if record is None:
            return Resolution(reason='material_not_read',
                              detail=f'材料 {source_ref or "（未指明）"} 不是本 run 回读过的条目')
        return Resolution(kind=SOURCE_MATERIAL, source_ref=source_ref,
                          object_ref=source_ref, record=record,
                          reason='material_read', detail='解析到本 run 回读过的材料条目')
    return Resolution(reason='unknown_source', detail=f'未识别的来源种类：{declared}')


def _check_record(record: RecordFacts, value: Any, *, target_field: str | None,
                  source_ref: str, kind: str, dependencies: Sequence[str] = (),
                  label: str = '当前权威记录') -> Grounding | None:
    """精确结构化字段核对：对象、值、单位、状态、版本。对不上就返回拒绝。"""
    if not record.current:
        return _refuse('record_not_current',
                       f'{source_ref} 已经不在{label}里（'
                       f'{"状态 " + str(record.status) if record.status else "已被替换或移除"}），'
                       f'历史版本取得到不等于它还是现在的值',
                       stage='support', source_ref=source_ref, kind=kind,
                       dependency_refs=dependencies)
    if _normalise(record.value) != _normalise(value):
        return _refuse('record_differs',
                       f'{label}里 {target_field or "该字段"} 记的是 {record.value}，'
                       f'与候选答案 {value} 不一致',
                       stage='support', source_ref=source_ref, kind=kind,
                       dependency_refs=dependencies)
    if record.status is not None and str(record.status) != CURRENT_RECORD_STATUS:
        return _refuse('record_not_current',
                       f'{source_ref} 的状态是 {record.status}，不是当前有效的记录'
                       f'（{"、".join(record.states())}）',
                       stage='support', source_ref=source_ref, kind=kind,
                       dependency_refs=dependencies)
    checked = ('对象、值' + ('、单位' if record.unit is not None else '')
               + ('、状态' if record.status is not None else '')
               + ('、版本' if record.version is not None else ''))
    # "记录没声明这一项"与"声明了并且对上了"必须分得开：前者写进 reason，让读的人
    # 知道哪一部分**没有**核对，而不是让整条判定看起来哪一部分都核过了。
    unstated = [name for name in ('unit', 'status', 'version')
                if getattr(record, name) is None]
    return Grounding(
        assessment={
            'status': STATUS_VERIFIED,
            'reason': f'与{label}逐字段核对一致（已核 {checked}'
                      + (f'；该记录未声明 {"、".join(unstated)}，未做该部分核对'
                         if unstated else '') + '）',
            'source_ref': source_ref,
            'locator': record.locator or f'{source_ref}#{target_field or "value"}',
            'dependency_refs': list(dependencies),
        },
        accepted=True, stage='support', kind=kind,
        detail=f'{label} {target_field or "该字段"}={record.value}')


def assess(*, declared_source: str, value: Any, quote: Any = None,
           source_ref: str | None = None, target_field: str | None = None,
           object_ref: str | None = None, question_objects: Sequence[str] = (),
           ctx: GroundingContext) -> Grounding:
    """对一条候选答案做三阶段判定：来源存在 → 引文匹配 → 答案支持。

    三个阶段**分开**，是因为它们失败的含义完全不同：来源不存在是"没有依据"，
    引文对不上是"引的不是这段"，答案不被陈述是"这段原文并没有说这个答案"。
    把三者压成一个布尔量，模型就永远不知道自己差在哪里，质检也只能退回标题匹配。
    """
    dependencies = dependency_refs_for(*(list(question_objects) + [source_ref]))

    if declared_source not in SOURCE_KINDS:
        return _refuse('unknown_answer_source',
                       f'来源种类只能是 {list(SOURCE_KINDS)}，收到 {declared_source!r}',
                       stage='source')
    if declared_source not in MODEL_SUBMITTABLE_SOURCES:
        # 用户回答 / 专业意见**不是模型能声明的**：它们只能来自真实记录。
        resolution = resolve_source(declared=declared_source, source_ref=source_ref,
                                    target_field=target_field, ctx=ctx)
        return _refuse(resolution.reason, resolution.detail, stage='source',
                       dependency_refs=dependencies)
    if not source_ref:
        return _refuse('no_source_ref',
                       '必须指明来源引用：没有引用的答案无法与任何真实记录对上'
                       '（引用存在 != 来源支持答案，但没有引用连核对都无从谈起）',
                       stage='source', dependency_refs=dependencies)
    source_ref = str(source_ref)
    if ctx.visible_ref is not None and not ctx.visible_ref(source_ref):
        return _refuse('source_not_in_scope',
                       f'来源引用不存在或不在当前作用域：{source_ref}',
                       stage='source', dependency_refs=dependencies)
    objects = [str(item) for item in question_objects]
    if objects:
        if object_ref is not None and str(object_ref) not in objects:
            return _refuse('object_not_in_question',
                           f'{object_ref} 不是这条问题涉及的对象（{"、".join(objects)}）；'
                           f'答的是另一个对象，不能算这条问题的答案',
                           stage='source', source_ref=source_ref,
                           dependency_refs=dependencies)
        if len(objects) > 1 and object_ref is None and source_ref not in objects:
            return _refuse('object_required',
                           f'这条问题涉及 {len(objects)} 个对象（{"、".join(objects)}），'
                           f'必须指明这次回答的是哪一个，否则无法保留对象与答案的对应关系',
                           stage='source', source_ref=source_ref,
                           dependency_refs=dependencies)

    resolution = resolve_source(declared=declared_source, source_ref=source_ref,
                                target_field=target_field, ctx=ctx)
    if resolution.kind is None:
        return _refuse(resolution.reason, resolution.detail, stage='source',
                       source_ref=source_ref, dependency_refs=dependencies)

    # 问题用**版本化记录引用**指名对象时（多对象问题就是这样），引用哪条记录
    # 就是答哪个对象：声明的对象与引用的记录必须指向同一条，且都得是问题问过的
    # 那几条。用**药名**指名对象的问题不在此列：那时 source_ref 指向哪条记录由
    # 记录解析决定，不是答错了对象。
    record_objects = [obj for obj in objects if parse_versioned_ref(obj)]
    if resolution.kind == SOURCE_PATIENT_RECORD and record_objects:
        mismatch = (source_ref not in record_objects
                    or (object_ref is not None and str(object_ref) != source_ref))
        if mismatch:
            return _refuse('object_not_in_question',
                           f'这条问题问的是 {"、".join(record_objects)}，'
                           f'而这次声明的是 {object_ref or source_ref}、引用的记录是 '
                           f'{source_ref}；对不上就不能算这条问题的答案',
                           stage='source', source_ref=source_ref,
                           dependency_refs=dependencies)

    if resolution.kind == SOURCE_PATIENT_RECORD:
        return _check_record(resolution.record, value, target_field=target_field,
                             source_ref=source_ref, kind=SOURCE_PATIENT_RECORD,
                             dependencies=dependencies)

    if resolution.kind == SOURCE_MATERIAL:
        record = resolution.record
        if target_field in STRUCTURED_FIELDS and record.value is not None:
            checked = _check_record(record, value, target_field=target_field,
                                    source_ref=source_ref, kind=SOURCE_MATERIAL,
                                    dependencies=dependencies,
                                    label='本 run 回读过的材料条目')
            if checked is not None:
                return checked
        return Grounding(
            assessment={
                'status': STATUS_CANDIDATE,
                'reason': ('材料条目 {ref} 已在本 run 回读，但它记的是候选字段，'
                           '支持关系未经核实（材料记录不等于患者事实）').format(ref=source_ref),
                'source_ref': source_ref,
                'locator': record.locator or source_ref,
                'dependency_refs': dependencies,
            },
            accepted=True, stage='support', kind=SOURCE_MATERIAL,
            detail='材料记录；保留原文定位，未经核实')

    # ---- 证据：先看引文是不是**这个来源**已读片段里逐字存在的一段 ----------
    if not str(quote or '').strip():
        return _refuse('no_quote',
                       '引用证据必须给出原文片段，否则无法建立支持关系',
                       stage='quote', source_ref=source_ref,
                       dependency_refs=dependencies)
    if not ctx.ledger.has_ref(source_ref):
        return _refuse('source_not_read_back',
                       f'{source_ref} 在本 run 没有被回读过；检索到不等于读到，'
                       f'引用只能指向真的读回来的原文',
                       stage='quote', source_ref=source_ref,
                       dependency_refs=dependencies)
    if not ctx.ledger.quote_in(source_ref, quote):
        return _refuse('quote_not_read_back',
                       f'引文不是 {source_ref} 已回读片段里逐字存在的一段；'
                       f'引用 A 的话不能用 B 的原文，也不能跨过没读过的部分拼接',
                       stage='quote', source_ref=source_ref,
                       dependency_refs=dependencies)
    locator = _locate(ctx.ledger, source_ref, quote)
    if target_field not in STRUCTURED_FIELDS:
        return Grounding(
            assessment={
                'status': STATUS_CANDIDATE,
                'reason': ('引文在 {ref} 已读片段中逐字存在，但这条答案没有可机械核对'
                           '的结构化字段（{field}），支持关系无法可靠验证，'
                           '按解释候选保存').format(ref=source_ref,
                                                     field=target_field or '开放问题'),
                'source_ref': source_ref,
                'locator': locator,
                'dependency_refs': dependencies,
            },
            accepted=True, stage='support', kind=SOURCE_EVIDENCE,
            detail='引文属实但支持关系未经核实')
    if _attestation_form(value) not in _attestation_form(quote):
        return _refuse('quote_does_not_state_value',
                       f'引文在 {source_ref} 里属实，但它没有陈述 {target_field}={value}；'
                       f'引文真实 != 来源支持这个答案',
                       stage='support', source_ref=source_ref, locator=locator,
                       dependency_refs=dependencies, kind=SOURCE_EVIDENCE)
    return Grounding(
        assessment={
            'status': STATUS_VERIFIED,
            'reason': f'引文与 {source_ref} 已读片段逐字一致，且陈述了 {target_field}={value}',
            'source_ref': source_ref,
            'locator': locator,
            'dependency_refs': dependencies,
        },
        accepted=True, stage='support', kind=SOURCE_EVIDENCE,
        detail=f'引文可定位且陈述了 {target_field}')


def _locate(ledger: ReadLedger, ref: str, quote: Any) -> str | None:
    """引文落在哪个回读片段的哪个偏移。定位不到就 ``None``——**不编造**。"""
    text = str(quote or '')
    for window in sorted(ledger.windows(ref), key=lambda w: w.offset):
        index = window.content.find(text)
        if index >= 0:
            return f'{ref}@{window.offset + index}'
    return None
