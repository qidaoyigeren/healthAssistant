"""visitprep-eval@2 —— 三条独立轴，五项互斥计数。

为什么另开一个模块而不是原地改 ``run_visitprep.evaluate``：评分必须能**脱离
重跑**重评历史产物，也必须能被负向对照单测直接调用。

本模块不读任何具体 ``task_id``、族名或文件名——规则一律来自
``task['expected']``，与 ``run_visitprep`` 的既有约定一致。
"""
from __future__ import annotations

import re

PROTOCOL = 'visitprep-eval@2'

# 评分链路**实际读取**的 ``expected`` 键。数据集里出现这个集合之外的键，就
# 等于**声明了一条没有任何东西执行的规则**——``search_budget`` 曾经如此：
# 两个任务声明了它，代码却只读全局常量，于是那句声明毫无约束力。测试据此
# 断言数据集的键全在这个集合里，新增键而没有接线会立刻失败。
READ_EXPECTED_KEYS = frozenset({
    'expected_diff_kind',
    'must_report_diff_issue',
    'required_report_sections',
    'must_report_conflict',
    'must_ask_fields',
    'forbid_supported_when_absent',
    'forbid_supported_entities',
    'allowed_terminal_reasons',
    'search_budget',          # 由 run_visitprep 下发给 investigation.search_limit()
})

# 终态分类。``allowed_terminal_reasons`` 说明"允许停在哪"，
# 但"完整完成"只能由 completed 与 waiting 取得——预算耗尽与 provider
# 失败不得自动算完整完成。
COMPLETION_REASONS = {'checks_completed'}
WAITING_REASONS = {'waiting_review', 'waiting_input'}
STOPPED_REASONS = {'budget_insufficient', 'no_progress', 'cancelled', 'unrecoverable_failure'}

# ``report_text()`` 的空态句子。它们占着一节的位置却没有内容，
# 所以"标题存在"不等于"这一节写了东西"。
PLACEHOLDERS = (
    '本次未在已读取的材料与记录之间发现可记录的差异',
    '本契约内没有剩余缺口。',
    '本次没有得到可作为结论的事实',
    # 第 5 节的空态句：它是一句通用指引，不是"可以向医生确认什么"的答案。
    # 不把它算作占位，第 5 节就会永远"有内容"，这条检查也就永远为真。
    '可将本报告的差异与未决项逐条向医生或药师确认',
    '尚无完成项',
)

# 差异一节的标题。它是**报告格式**的一部分（与 PLACEHOLDERS 同类），不是
# 任务标识——不指向任何 task_id、族名或文件名。
DIFFERENCES_SECTION = '3. 不同材料之间的差异'

# 一条**否认**分歧的行：它写了实质内容，却断言两边相同。占位句由
# PLACEHOLDERS 过滤；这里处理的是"写了内容但内容与观察到的分歧相反"。
# ``(?<!不)`` 是必需的：``不一致``/``不相同``恰恰是在**报告**差异，把它们
# 当成否认会让这条检查从恒真翻成恒假。
_DENIES_DIFF = re.compile(r'(?<!不)一致|(?<!不)相同|没有差异|无差异|不存在差异|未发现差异|未见差异')

_HEADING = re.compile(r'^##\s+(?P<title>.+?)\s*$', re.MULTILINE)


def _matches(title: str, heading: str) -> bool:
    """任务按**声明名**指认一节，而渲染出的标题可以带限定后缀（第 4 节实际
    写作"仍缺少依据的问题（待核实）"）。所以判据是"以声明名开头"，不是子串
    ——子串会让一个嵌在别处的名字也算数，前缀不会。
    """
    return title.strip().startswith(heading.strip())


def section_body(report: str, heading: str) -> str | None:
    """``## <heading>`` 到下一个 ``## `` 之间的正文；找不到标题返回 ``None``。

    找不到与"找到但正文为空"必须可区分，否则"整节消失"会被读成"这一节没写
    内容"——两者要给的失败码不同。
    """
    matches = list(_HEADING.finditer(report or ''))
    for index, match in enumerate(matches):
        if _matches(match.group('title'), heading):
            start = match.end()
            end = matches[index + 1].start() if index + 1 < len(matches) else len(report)
            return report[start:end]
    return None


def has_section(report: str, heading: str) -> bool:
    """标题本身在不在。与"这一节写了东西"是两件事，必须分开判。"""
    return any(_matches(match.group('title'), heading)
               for match in _HEADING.finditer(report or ''))


def has_content(body: str) -> bool:
    """正文里有非占位条目才算有内容。"""
    for line in (body or '').splitlines():
        line = line.strip()
        if not line or not line.startswith('-'):
            continue
        if any(placeholder in line for placeholder in PLACEHOLDERS):
            continue
        return True
    return False


def classify_terminal(task: dict, observed: dict) -> str:
    """允许集合 + 完成/等待/停止三分类。"""
    reason = observed.get('termination_reason')
    if not reason:
        return 'absent'
    allowed = set((task.get('expected') or {}).get('allowed_terminal_reasons') or [])
    if allowed and reason not in allowed:
        return 'stopped'
    if reason in COMPLETION_REASONS:
        return 'completed'
    if reason in WAITING_REASONS:
        return 'waiting'
    return 'stopped'


def score_report_quality(task: dict, observed: dict) -> dict:
    """内容规则。每一条都从 ``expected`` 读出，并随 ``observed`` 可被证伪。

    Task 2 与 Task 3 会在这里把 ``must_report_conflict`` 等检查换成可达版本；
    本函数承载 visitprep-eval@1 ``evaluate`` 的全部既有判据，只把
    "标题存在" 升级为 "这一节真的写了东西"。
    """
    expected = task.get('expected') or {}
    report = observed.get('report_markdown') or ''
    failures = []
    if observed.get('error'):
        failures.append('execution_error')

    # "材料索引从未被读取" 与 "读了但没看到预期的那种差异" 是两种不同的失败：
    # 前者说明这项能力根本没被用上，后者说明用上了而结论不对——归因方向相反，
    # 所以不能合并成一个错误码。
    diff_kinds = observed.get('diff_kinds_seen')
    if expected.get('expected_diff_kind'):
        if not diff_kinds:
            failures.append('material_index_never_read')
        elif expected['expected_diff_kind'] not in diff_kinds:
            failures.append('expected_diff_not_found')

    issue = expected.get('must_report_diff_issue')
    if issue and issue not in report:
        failures.append('material_issue_not_reported')

    # 节的判据必须**双向**可证伪，否则只是把恒真换成恒假：
    #   * 标题不在            → section_missing（结构缺失）
    #   * 只有占位句、而状态非空 → empty_or_missing_section（声称有内容却没写）
    #   * 只有占位句、而状态确为空 → 合法。占位句就是那一节的正确内容；
    #     要求它写出东西，等于要求"材料一致"的任务必须编一个差异出来。
    # ``empty_state_sections`` 由产品侧按渲染时的真实集合给出，评分器不猜。
    # ``empty_state_sections`` 装的是**渲染标题**（第 4 节带"（待核实）"后缀），
    # 而要求列表里是**声明名**。两边用同一套前缀判据比对，否则这里会按
    # "声明名不在空态表里"判失败——一个由命名差异造成的假失败。
    empty_ok = list(observed.get('empty_state_sections') or [])
    for section in expected.get('required_report_sections') or []:
        if not has_section(report, section):
            failures.append('section_missing:' + section)
        elif not has_content(section_body(report, section)) \
                and not any(_matches(title, section) for title in empty_ok):
            failures.append('empty_or_missing_section:' + section)

    # 冲突检查：不再做标题子串匹配（那样恒真）。必须有**真实分歧**可报告，
    # 报告里必须有**非否认**的实质条目，且该条目要**具名双方**——材料条目
    # 与它所对比的当前记录。只写"材料 X 有差异"是把一个反对来源写成半句
    # 话，读者无法去核对另一边。
    conflicts = observed.get('material_conflicts') or []
    if expected.get('must_report_conflict') and conflicts:
        body = section_body(report, DIFFERENCES_SECTION)
        named = [line for line in (body or '').splitlines()
                 if line.strip().startswith('-')
                 and not any(placeholder in line for placeholder in PLACEHOLDERS)]
        asserted = [line for line in named if not _DENIES_DIFF.search(line)]
        if not asserted:
            failures.append('conflict_not_reported')
        else:
            reported = False
            for conflict in conflicts:
                for line in asserted:
                    if conflict.get('ref') and conflict['ref'] in line and all(
                            counterpart in line
                            for counterpart in conflict.get('counterparts') or []):
                        reported = True
            if not reported:
                any_ref = any(conflict.get('ref') and conflict['ref'] in line
                              for conflict in conflicts for line in asserted)
                failures.append('conflict_side_missing' if any_ref else 'conflict_not_reported')

    # "该问的没问" 与 "不该问的问了" 分开计：前者是能力缺口，后者是干扰项
    # 被当成了主线，两者的修法不同。
    asked = set(observed.get('asked_fields') or [])
    required_questions = set(expected.get('must_ask_fields') or [])
    # 任务没有声明必问字段，就是**没有**对"哪些问题重要"作出任何断言：问了不算
    # 失败，没问也不算。仍然照判就会把这条检查判反——required 为空时
    # ``asked - required`` 等于 ``asked``，于是**任何**提问都成了
    # unnecessary_question，而被判失败的那个追问恰恰是本任务允许的正常结果。
    if required_questions:
        if not required_questions.issubset(asked):
            failures.append('necessary_question_missing')
        if asked - required_questions:
            failures.append('unnecessary_question')

    if expected.get('forbid_supported_when_absent') and observed.get('supported_claims'):
        failures.append('unsupported_claim')

    # 非干扰规则，精确表述：任务标记为越界的说法不得变成受支持的发现，而同一
    # 任务范围内的说法仍然可以受支持。判据因此是"与禁集有交集"，不是"出现过
    # 任何受支持的说法"——否则一条合法结论会把整个任务判失败。
    forbidden = set(expected.get('forbid_supported_entities') or [])
    if forbidden:
        claims = observed.get('supported_claim_entities') or []
        if any(forbidden & set(entities) for entities in claims):
            failures.append('distractor_became_a_finding')

    if observed.get('invalid_calls'):
        failures.append('invalid_tool_call')
    # 提问这一轴即使不判也要可见：不记下来，"模型到底问没问"就只能靠重跑才知道。
    return {'ok': not failures, 'failures': failures,
            'questions_asked': sorted(asked)}


def score_autonomy(observed: dict) -> bool:
    """无策略降级、子问题归模型、零 fallback。"""
    if observed.get('degraded_reason'):
        return False
    if observed.get('subquestion_source') != 'model':
        return False
    return not (observed.get('attribution') or {}).get('policy_fallback')


def score_outcome(task: dict, observed: dict) -> dict:
    """三条轴 + 五项互斥计数之一。``undetermined`` 是并列标记，不占计数。"""
    if observed.get('undetermined'):
        return {'protocol': PROTOCOL, 'undetermined': True,
                'undetermined_reasons': list(observed.get('undetermined') or [])}

    quality = score_report_quality(task, observed)
    terminal = classify_terminal(task, observed)
    autonomous = score_autonomy(observed)

    if observed.get('error'):
        bucket = 'execution_failed_or_not_sampled'
    elif observed.get('degraded_reason') or observed.get('subquestion_source') != 'model':
        bucket = 'degraded_outcome'
    elif quality['ok'] and terminal in {'completed', 'waiting'} and autonomous:
        bucket = 'autonomous_without_degradation'
    elif quality['ok'] and terminal in {'completed', 'waiting'}:
        bucket = 'report_quality_pass'
    else:
        bucket = 'terminal_expected' if terminal != 'stopped' else 'report_quality_pass'

    return {
        'protocol': PROTOCOL,
        'report_quality': quality,
        'terminal_state': terminal,
        'autonomy': autonomous,
        'complete': bool(quality['ok'] and terminal == 'completed' and autonomous),
        'bucket': bucket,
    }
