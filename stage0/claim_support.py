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

#: 属性词表：一条问题问的是哪个字段，片段里就必须真的谈到那个属性。
#:
#: 词表是**必要**条件，不是充分条件——它只回答"这段文字有没有在讲这件事"，
#: 不回答"讲得对不对"（那是既有断言判定与 evidence_quality 的事）。缺了它，
#: 实体名相同就足以让「服药频次是什么」被一段讲出血风险的文字判成已有依据。
ATTRIBUTE_TERMS = {
    'schedule': ('频次', '频率', '次数', '每日', '每天', '一日', '隔日', '服药时间',
                 '用药时间', 'bid', 'tid', 'qd', 'qod'),
    'dose': ('剂量', '用量', '每次', '片', 'mg', '毫克', '克', '单位'),
    'route': ('途径', '给药', '口服', '静脉', '皮下', '肌注', '外用', '吸入'),
    'start_at': ('开始', '起始', '启用', '起用', '首次'),
}

#: 兜底用的停用词与疑问成分：它们在任何一句中文问题里都出现，不携带属性信息。
#: 按**字符**去除，再取两字组合——比整词切分稳，不引第三方分词器。
_STOPWORDS = frozenset('的了呢吗吧啊是什么目前现在请问是否有没有以及和与或这那该其'
                       '我要想知道多少怎样如何怎么为什么哪些哪个')


def _content_bigrams(text: str) -> set[str]:
    """去停用词与数字后剩下的连续两字组合。

    兜底判断"片段有没有在讲同一件事"——没有字段可查属性词表时用它。
    取两字而不是单字：单字（如"药"）在任何一句医药文本里都出现，等于没判。
    """
    chars = [c for c in (text or '') if c not in _STOPWORDS and not c.isdigit()]
    return {''.join(chars[i:i + 2]) for i in range(len(chars) - 1)}


def _facts(text: str) -> set[str]:
    return {re.sub(r'\s+', '', item).lower() for item in _FACT.findall(text or '')}


def _flatten(text: str) -> str:
    return re.sub(r'\s+', '', text or '').lower()


def assess_support(*, statement: str, quote: str, entities, material_item=None,
                   target_field: str | None = None) -> dict:
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

    # 谓语检查：片段有没有在讲**这件事**。只查实体名与数字时，一段讲出血风险的
    # 文字会因为主词是同一味药，而去"支持"一条问服药频次的断言。
    #
    # 材料路径不走这一层：那里判的是"材料与记录是什么关系"，而一句关于差异的
    # 元陈述本来就不会逐字出现在片段里——对它做词汇检查只会惩罚正确结论。
    if material_item is None:
        flat = _flatten(quote)
        terms = ATTRIBUTE_TERMS.get(str(target_field or ''))
        if terms:
            if not any(term.lower() in flat for term in terms):
                reasons.append('attribute_absent_from_span:' + str(target_field))
        else:
            stripped = statement or ''
            for entity in (entities or ()):
                stripped = stripped.replace(str(entity), '')
            if not (_content_bigrams(stripped) & _content_bigrams(quote)):
                reasons.append('content_absent_from_span')

    if reasons:
        return {'scope': SCOPE, 'status': 'no_span', 'reasons': reasons}
    return {'scope': SCOPE, 'status': 'supported_by_span', 'reasons': []}
