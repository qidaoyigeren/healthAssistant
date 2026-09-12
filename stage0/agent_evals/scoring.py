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
    """内容规则。Task 2 与 Task 3 会往这里加可达性检查。"""
    expected = task.get('expected') or {}
    report = observed.get('report_markdown') or ''
    failures = []
    if observed.get('error'):
        failures.append('execution_error')

    for section in expected.get('required_report_sections') or []:
        if not has_content(section_body(report, section)):
            failures.append('empty_or_missing_section:' + section)

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
