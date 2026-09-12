"""claim-support@1 —— 被引用的证据是否**支持**该断言，而非仅仅"被读取过"。

独立命名 scope，**叠加**在 ``evidence_quality.conservative-lexical-v1`` 之上，
不覆盖它：后者决定一条 claim 有没有支持/反对（决定 ``status``），本 scope 决定
该断言是否落在它所引用的**具体片段**里。分开的理由是历史 assessments 不能因为
口径升级而集体失效——缺本 scope 的旧记录按 ``not_applicable`` 恢复。

同药名、或"引用被回读过"，都不足以支持整个断言。
"""
from __future__ import annotations

import re

SCOPE = 'claim-support@1'

# 剂量/日期类数字事实。单位可省，但数字本身必须对得上。
_FACT = re.compile(r'\d+(?:\.\d+)?\s*(?:mg|μg|ug|g|ml|毫克|克|毫升|片|粒|单位)?', re.IGNORECASE)

# 断言对"两边是否相同"的表态。两个方向都要认：材料是 same 而断言说不同，
# 与材料是 changed 而断言说相同，都是断言与材料字段的冲突——不是"引用里没有
# 这个词"。方向只有一个时会漏掉一半，而被漏掉的那一半恰好是更危险的一半
# （把已知存在的差异说成一致）。
_DIFFERENCE_CLAIM = re.compile(r'差异|不同|不一致')
_SAMENESS_CLAIM = re.compile(r'(?<!不)相同|(?<!不)一致|没有差异|无差异|不存在差异')


def _facts(text: str) -> set[str]:
    return {re.sub(r'\s+', '', item).lower() for item in _FACT.findall(text or '')}


def _flatten(text: str) -> str:
    return re.sub(r'\s+', '', text or '').lower()


def assess_support(*, statement: str, quote: str, entities, material_item=None) -> dict:
    """Return ``{'scope', 'status', 'reasons'}``.

    ``supported_by_span`` 是本 scope 唯一的肯定判定；任何其他值都意味着该断言
    **不得**作为结论渲染。
    """
    quote = quote or ''
    if not quote.strip():
        return {'scope': SCOPE, 'status': 'not_applicable', 'reasons': ['no_evidence_body']}

    if material_item is not None:
        kind = str(material_item.get('kind') or '')
        if kind == 'same' and _DIFFERENCE_CLAIM.search(statement or ''):
            # 材料与当前记录被判定为 same，而断言在说它们不同。
            return {'scope': SCOPE, 'status': 'field_mismatch',
                    'reasons': ['material_kind_is_same']}
        if kind and kind != 'same' and _SAMENESS_CLAIM.search(statement or ''):
            # 反向：材料带着一处**未解决**的差异，而断言说两边一致。
            return {'scope': SCOPE, 'status': 'field_mismatch',
                    'reasons': ['material_kind_is_' + kind]}

    reasons = []
    missing = [entity for entity in (entities or []) if entity not in quote]
    if missing:
        reasons.append('entity_absent_from_span:' + ','.join(map(str, missing)))

    absent_facts = sorted(fact for fact in _facts(statement) if fact not in _flatten(quote))
    if absent_facts:
        reasons.append('fact_absent_from_span:' + ','.join(absent_facts))

    if reasons:
        return {'scope': SCOPE, 'status': 'no_span', 'reasons': reasons}
    return {'scope': SCOPE, 'status': 'supported_by_span', 'reasons': []}
