"""visitprep-eval@2 —— 三条独立轴，五项互斥计数。

为什么另开一个模块而不是原地改 ``run_visitprep.evaluate``：评分必须能**脱离
重跑**重评历史产物，也必须能被负向对照单测直接调用。

本模块不读任何具体 ``task_id``、族名或文件名——规则一律来自
``task['expected']``，与 ``run_visitprep`` 的既有约定一致。
"""
from __future__ import annotations

import re

PROTOCOL = 'visitprep-eval@2'

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
    '尚无完成项',
)

_HEADING = re.compile(r'^##\s+(?P<title>.+?)\s*$', re.MULTILINE)


def section_body(report: str, heading: str) -> str:
    """``## <heading>`` 到下一个 ``## `` 之间的正文；找不到标题返回空串。"""
    matches = list(_HEADING.finditer(report or ''))
    for index, match in enumerate(matches):
        if match.group('title').strip() == heading.strip():
            start = match.end()
            end = matches[index + 1].start() if index + 1 < len(matches) else len(report)
            return report[start:end]
    return ''


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

    for section in expected.get('required_report_sections') or []:
        if not has_content(section_body(report, section)):
            failures.append('empty_or_missing_section:' + section)

    # "该问的没问" 与 "不该问的问了" 分开计：前者是能力缺口，后者是干扰项
    # 被当成了主线，两者的修法不同。
    asked = set(observed.get('asked_fields') or [])
    required_questions = set(expected.get('must_ask_fields') or [])
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
    return {'ok': not failures, 'failures': failures}


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
