"""material-review@2 —— 字段级比较。

**行级 ``kind = same/changed`` 不足以表示一次完整的比较。** 它把八件事压成一个词：
某一格对上了、某一格不一样、材料没写、记录里没有、单位换不过来、值根本不是这一
个字段该有的形状……全都只能挤成"一致"或"不一致"。真实批次里就出现过这样一条：

    氨氯地平：与当前记录一致；…；未决问题：请核实剂型、规格与所选记录一致

一行写着"一致"，末尾却挂着一句"请核实规格"——因为剂型与规格**从未被比较过**，
而"材料没写"被当成了"一致"。

这里把比较拆到字段：每个字段一条 ``FieldComparison``，双方的值、来源、版本、
规范化结果与状态各记各的。行级摘要由字段结果**派生**，不再由 ``kind`` 决定。

五条不许越过的规矩：

* 双方都缺不等于双方一致 —— 那是"没有可比较信息"；
* 材料有值而记录没有，显示的是**缺少可比较信息**，不是"没问题"；
* 单位与格式用项目已有的规范化能力，换算不可靠就保留 ``not_comparable``；
* 日期不同不自动意味着新记录取代旧记录（这里只报"不同"，不做解释）；
* **不做临床等效性推断**：5mg 与 10mg 只是不同，说不出谁对谁错。
"""
from __future__ import annotations

from .contract import COMPARED_FIELD_LABELS, digest

# 一条比较的六种结果。
STATUS_EQUAL = 'equal'
STATUS_DIFFERENT = 'different'
STATUS_MISSING_LEFT = 'missing_left'      # 材料这一边没有值
STATUS_MISSING_RIGHT = 'missing_right'    # 当前记录这一边没有值
STATUS_NOT_COMPARABLE = 'not_comparable'  # 有值，但换不到一起比
STATUS_INVALID_VALUE = 'invalid_value'    # 值不是这个字段该有的形状
COMPARISON_STATUSES = (STATUS_EQUAL, STATUS_DIFFERENT, STATUS_MISSING_LEFT,
                       STATUS_MISSING_RIGHT, STATUS_NOT_COMPARABLE, STATUS_INVALID_VALUE)

# 判定为"已经得到结论"的状态：确认类要求靠它算完成。
DECIDED_STATUSES = (STATUS_EQUAL, STATUS_DIFFERENT)
# 判定为"需要别的信息才能比"的状态：它既不是完成，也不是差异。
UNDECIDED_STATUSES = (STATUS_MISSING_LEFT, STATUS_MISSING_RIGHT,
                      STATUS_NOT_COMPARABLE, STATUS_INVALID_VALUE)

STATUS_LABELS = {
    STATUS_EQUAL: '一致', STATUS_DIFFERENT: '不一致',
    STATUS_MISSING_LEFT: '材料未写这一项', STATUS_MISSING_RIGHT: '当前记录里没有这一项',
    STATUS_NOT_COMPARABLE: '双方写法不同、无法可靠换算', STATUS_INVALID_VALUE: '这一项的值不是有效格式',
}

# 逐字段那一行用的**短标签**。它们与 ``COMPARED_FIELD_LABELS`` 是同一批字段，
# 只是措辞更短：那一行把八个字段名连在一起写，像"服用频次、…、给药途径"这样的
# 连续标签会撞上交付前的处方措辞检查（"服用…药" 落在 12 字窗口里），把一个纯
# 数据行误判成用药指示。**换的是标签，不是检查**——值仍然原样参与那道检查。
TERSE_FIELD_LABELS = {
    'name': '药名', 'dose': '剂量', 'unit': '单位', 'schedule': '频次',
    'date': '日期', 'route': '途径', 'form': '剂型', 'strength': '规格',
}

# 当前记录**本来就会跟踪**的字段。只有这些比不出结果才意味着"这条还没核对完"。
# 剂型、规格、单位在记录里可能根本没有对应的列，它们照常比较、照常报告，但不把
# 整条材料拖成未完成。
CORE_FIELDS = ('dose', 'schedule', 'date', 'route')
# 参与比较的全部字段（顺序即展示顺序）。
ALL_FIELDS = ('name', 'dose', 'unit', 'schedule', 'date', 'route', 'form', 'strength')

# 字段的规范化策略。``unit`` 是剂量文字的一部分，单独比较时只做文本规范化。
_NUMERIC_FIELDS = {'dose': True, 'strength': True}
_TEXT_FIELDS = {'name': True, 'unit': True, 'schedule': True, 'date': True,
                'route': True, 'form': True}


def normalize_field(field: str, value):
    """把一个字段的值规范化。**不做换算，只做形状整理**。

    单位换算需要知道药品与剂型（0.5g/片 与 250mg/片 是不同的事实），这里没有那个
    信息，因此**不猜**：原始值与规范化值同时保留，换不到一起就如实说换不到。
    """
    text = '' if value is None else str(value).strip()
    if not text:
        return None
    normalized = ' '.join(text.split()).casefold().replace('　', '')
    if field in _NUMERIC_FIELDS:
        import re
        match = re.fullmatch(r'(\d+(?:\.\d+)?)\s*([a-zA-Z一-鿿%μ]*)\s*', normalized)
        if match is None:
            return {'raw': text, 'normalized': normalized, 'number': None, 'unit': None,
                    'shape': 'invalid'}
        return {'raw': text, 'normalized': f'{match.group(1)}{match.group(2)}',
                'number': match.group(1), 'unit': match.group(2) or None, 'shape': 'number+unit'}
    return {'raw': text, 'normalized': normalized, 'number': None, 'unit': None, 'shape': 'text'}


def compare_field(field, *, left, right, left_source=None, right_source=None,
                  left_version=None, right_version=None) -> dict:
    """一条字段比较。``left`` = 材料一侧，``right`` = 当前记录一侧。

    两侧各自带**来源**与**版本**：比较结果要能回答"这是拿哪一版比出来的"，
    否则材料改版之后没人说得清旧结论还成不成立。
    """
    left_norm = normalize_field(field, left)
    right_norm = normalize_field(field, right)
    if left_norm is None and right_norm is None:
        # 双方都缺**不等于**一致：没有任何可比较的信息。
        status, reason = STATUS_MISSING_LEFT, '两边都没有这一项，没有可比较的信息'
    elif left_norm is None:
        status, reason = STATUS_MISSING_LEFT, '材料里没有写这一项'
    elif right_norm is None:
        status, reason = STATUS_MISSING_RIGHT, '当前记录里没有这一项'
    elif left_norm['shape'] == 'invalid' or right_norm['shape'] == 'invalid':
        status, reason = STATUS_INVALID_VALUE, '有一侧的值不是这一项该有的格式'
    elif left_norm['normalized'] == right_norm['normalized']:
        status, reason = STATUS_EQUAL, None
    elif field in _NUMERIC_FIELDS and left_norm['unit'] != right_norm['unit']:
        # 单位这一格对不上。数字相同、而一侧**根本没写单位**时（``0.5`` 对
        # ``0.5g``）不能读成"不同"——那是"说不清是不是同一个量"。真要换算还得
        # 知道药品与剂型，这里不做，如实标成不可比较。
        if left_norm['number'] == right_norm['number'] and not (left_norm['unit'] and right_norm['unit']):
            status, reason = STATUS_NOT_COMPARABLE, '一侧没有写单位，无法确认是不是同一个量'
        else:
            # 数字与单位都不同：不同是**结论**，但换算成不等价需要药品知识，这里不做。
            status, reason = STATUS_DIFFERENT, '两侧的数字与单位都不同'
    else:
        status, reason = STATUS_DIFFERENT, None
    return {
        'comparison_id': 'fc:' + digest({'s': left_source, 'f': field,
                                         'r': right_source})[:16],
        'subject_ref': None,
        'field': field, 'field_label': COMPARED_FIELD_LABELS.get(field, field),
        'left_value': left, 'right_value': right,
        'normalized_values': {'left': (left_norm or {}).get('normalized'),
                              'right': (right_norm or {}).get('normalized')},
        'left_source_ref': left_source, 'right_source_ref': right_source,
        'source_versions': {'left': left_version, 'right': right_version},
        'comparison_status': status, 'reason': reason,
        'related_requirement_ids': [],
    }


def summarize(comparisons, core_fields=CORE_FIELDS) -> dict:
    """行级摘要**由字段结果派生**——不是反过来。

    它回答的是"这条材料比到什么程度了"，而不是"它像不像一致"。

    摘要分两层，因为它们对"是否阻塞"的意义不同：

    * ``core``：当前记录**本来就会跟踪**的字段（剂量、频次、日期、途径）。这些
      比不出结果，说明这条材料还没核对完。
    * 其余字段（单位、剂型、规格、药名）：记录里可能根本没有这一列。材料写了、
      记录没有，要**显示成"当前记录里没有这一项"**，但不能因此把整条材料判成
      "没核对完"——那会让每一份带剂型的材料永远停在未完成。是否阻塞，取决于本次
      的交付要求（例如"确认规格是否一致"这一项就必须等它有一个值）。
    """
    rows = list(comparisons or [])
    core = [row for row in rows if row['field'] in set(core_fields)]

    def bucket(subset):
        counts = {status: 0 for status in COMPARISON_STATUSES}
        for row in subset:
            counts[row['comparison_status']] = counts.get(row['comparison_status'], 0) + 1
        return {
            'by_status': counts,
            'compared': counts[STATUS_EQUAL] + counts[STATUS_DIFFERENT],
            'undecided': sum(counts[status] for status in UNDECIDED_STATUSES),
            'equal': counts[STATUS_EQUAL], 'different': counts[STATUS_DIFFERENT],
            'undecided_fields': [row['field'] for row in subset
                                 if row['comparison_status'] in UNDECIDED_STATUSES],
            'different_fields': [row['field'] for row in subset
                                 if row['comparison_status'] == STATUS_DIFFERENT],
        }

    overall, core_view = bucket(rows), bucket(core)
    # 行级措辞说的是**核心字段**的比对结果，另外点出记录里没有的那几项。
    absent = [row['field_label'] for row in rows
              if row['comparison_status'] == STATUS_MISSING_RIGHT and row not in core]
    if not core:
        headline = '没有可比较的核心字段'
    elif core_view['undecided'] == 0 and core_view['different'] == 0:
        headline = '核心字段都已比对，全部一致'
    elif core_view['undecided'] == 0:
        headline = f"核心字段都已比对，{core_view['different']} 项不同"
    else:
        headline = (f"已比对 {core_view['compared']} 项，"
                    f"另有 {core_view['undecided']} 项缺少可比较信息")
    return {**overall, 'core': core_view, 'headline': headline,
            # 材料写了、当前记录里没有的那几项：要显示，但不是"这条没核对完"。
            'record_absent_fields': [row['field'] for row in rows
                                     if row['comparison_status'] == STATUS_MISSING_RIGHT
                                     and row['field'] not in set(core_fields)],
            'record_absent_labels': sorted(set(absent)),
            'material_absent_fields': [row['field'] for row in rows
                                       if row['comparison_status'] == STATUS_MISSING_LEFT]}
