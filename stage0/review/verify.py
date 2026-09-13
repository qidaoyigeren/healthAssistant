"""material-review@2 —— 断言与证据的关系。

**读过引用不等于引用支持断言。** 这里回答的是：这条断言所引用的证据，是否真的
支持它。按事实类型分三档核查，各用各的方法：

* ``A. 结构化事实``  日期、字段值、剂量文字、材料差异——用可解释的确定性比较；
  注意单位、否定和记录缺失，**不是**检查数字子串；
* ``B. 原文摘录``    校验来源、位置、内容哈希与摘录一致性；
* ``C. 语义解释``    模型给出证据片段与结构化解释，代码只做**片段确实存在**这一
  步；状态是"需要人工确认"，不是"已支持"。

支持的判定**不复用**于修订后的断言：修订使旧判断失效（``verification_basis``
改变即重算）。原文本身可以跨问题复用。
"""
from __future__ import annotations

import re

from .contract import digest

# 断言谓词的三档归属。谓词由模型选择，但它决定用哪一档核查，因此是一个**枚举**
# 而不是自由文本——否则"用哪种核查方式"就变成了模型可以随口决定的事。
PREDICATES = {
    'record_consistency': 'A',
    'dose_consistency': 'A',
    'schedule_consistency': 'A',
    'date_consistency': 'A',
    'route_consistency': 'A',
    'label_statement': 'B',
    'source_excerpt': 'B',
    'interpretation': 'C',
    'significance': 'C',
}
STRUCTURED_FIELDS = {'record_consistency': None, 'dose_consistency': 'dose',
                     'schedule_consistency': 'schedule', 'date_consistency': 'date',
                     'route_consistency': 'route'}

STATUS_SUPPORTED = 'supported'
STATUS_CONTRADICTED = 'contradicted'
STATUS_CONFLICTING = 'conflicting'
STATUS_INSUFFICIENT = 'insufficient'
STATUS_HUMAN = 'requires_human_confirmation'
STATUS_UNVERIFIED = 'unverified'

_WS = re.compile(r'\s+')


def predicate_class(predicate: str) -> str:
    return PREDICATES.get(str(predicate or '').strip())


def _norm(value) -> str:
    """规范化：空白、大小写、全角空格。**单位不剥离**——5mg 与 5g 不是同一个值。"""
    return _WS.sub('', str(value if value is not None else '')).replace('　', '').lower()


def _unit_and_number(value) -> tuple:
    text = _norm(value)
    match = re.match(r'^(\d+(?:\.\d+)?)(.*)$', text)
    if match is None:
        return None, text
    return match.group(1), match.group(2)


def compare_structured(*, field, recorded, material):
    """确定性比较两个字段值。返回 ``same`` / ``different`` / ``missing``。

    单位与记录缺失都影响结论：剂量文字相同要求**数字与单位都对上**；
    一边缺失是 ``missing``（"没有记录"），不是 ``same``。
    """
    left, right = _norm(recorded), _norm(material)
    if not left or not right:
        return 'missing'
    if left == right:
        return 'same'
    left_number, left_unit = _unit_and_number(left)
    right_number, right_unit = _unit_and_number(right)
    if left_number is not None and left_number == right_number and left_unit != right_unit:
        # 数字相同、单位不同：这是一个**确实存在**的差异，不是"数字对得上"。
        return 'different'
    return 'different'


def verification_basis(state, assertion, *, material_index=None, evidence_store=None) -> str:
    """支持判断的有效性依据。

    它由断言修订、证据内容哈希与事实版本共同决定：这三者任意一个变了，判断就不
    再可复用——**原文可以跨问题复用，判断不可以**。
    """
    hashes = []
    for ref in assertion.get('evidence_refs') or []:
        meta = (evidence_store.get_meta(ref) if evidence_store is not None else None) or {}
        hashes.append([ref, meta.get('content_hash') or 'missing'])
    materials = []
    for ref in assertion.get('qualifiers', {}).get('material_refs') or []:
        materials.append([ref, state.material_fingerprints.get(ref)])
    return digest({'assertion': assertion['assertion_id'], 'revision': assertion['revision'],
                   'evidence': sorted(hashes), 'materials': sorted(materials),
                   'versions': state.spec.input_versions})


def verify_assertion(state, assertion, *, material_index=None, evidence_store=None) -> dict:
    """返回 ``{'status', 'reasons', 'basis', 'auto_assessment'}``。

    调用方负责把结果写回断言；本函数不改状态（除了读取材料项）。
    """
    predicate = assertion.get('predicate')
    kind = predicate_class(predicate)
    basis = verification_basis(state, assertion, evidence_store=evidence_store)
    if kind is None:
        return {'status': STATUS_INSUFFICIENT, 'basis': basis,
                'reasons': ['unknown_predicate:' + str(predicate)], 'auto_assessment': None}
    qualifiers = assertion.get('qualifiers') or {}
    if kind == 'A':
        return _verify_structured(state, assertion, qualifiers, basis)
    if kind == 'B':
        return _verify_verbatim(state, assertion, qualifiers, basis, evidence_store)
    return _verify_semantic(state, assertion, qualifiers, basis, evidence_store)


def _material_items(state, qualifiers):
    refs = list(qualifiers.get('material_refs') or [])
    if not refs:
        for ref in state.material_items:
            if ref in (qualifiers.get('subject_refs') or []):
                refs.append(ref)
    return refs


def _verify_structured(state, assertion, qualifiers, basis):
    """结构化断言：``value`` 说明**期待什么**，``qualifiers`` 说明**对谁**期待。"""
    field = STRUCTURED_FIELDS.get(assertion['predicate'])
    value = assertion.get('value')
    expect = value.get('expect') if isinstance(value, dict) else qualifiers.get('expect')
    field = qualifiers.get('field', field)
    if expect not in ('same', 'different', 'missing'):
        return {'status': STATUS_INSUFFICIENT, 'basis': basis, 'auto_assessment': None,
                'reasons': ['expect must be one of same/different/missing']}
    refs = _material_items(state, qualifiers)
    if not refs:
        return {'status': STATUS_INSUFFICIENT, 'basis': basis, 'auto_assessment': None,
                'reasons': ['no material reference to compare']}
    for ref in refs:
        item = state.material_items.get(ref)
        if not item:
            return {'status': STATUS_INSUFFICIENT, 'basis': basis, 'auto_assessment': None,
                    'reasons': ['material_not_in_scope:' + ref]}
        if field is None:
            observed = 'same' if item.get('kind') == 'same' else 'different'
        else:
            observed = compare_structured(field=field, recorded=_recorded_value(state, item, field),
                                          material=(item.get('fields') or {}).get(field))
        if observed != expect:
            return {'status': STATUS_CONTRADICTED, 'basis': basis, 'auto_assessment': observed,
                    'reasons': [f'{field or "record"}_is_{observed}_not_{expect}']}
    return {'status': STATUS_SUPPORTED, 'basis': basis, 'auto_assessment': expect, 'reasons': []}


def _recorded_value(state, item, field):
    """从当前权威记录里取被比较字段的值——取不到就是取不到，不用材料的值顶替。"""
    known = state.medications_by_ref()
    for ref in (item.get('current') or []):
        medication = known.get(str(ref))
        if medication is not None:
            return medication.get(field)
    return None


def _verify_verbatim(state, assertion, qualifiers, basis, evidence_store):
    excerpt = str(qualifiers.get('excerpt') or '')
    refs = list(assertion.get('evidence_refs') or [])
    if not excerpt.strip():
        return {'status': STATUS_INSUFFICIENT, 'basis': basis, 'auto_assessment': None,
                'reasons': ['excerpt_required']}
    if not refs:
        return {'status': STATUS_INSUFFICIENT, 'basis': basis, 'auto_assessment': None,
                'reasons': ['no_evidence_ref']}
    from ..claim_support import assess_support
    for ref in refs:
        body = _read_body(state, ref, evidence_store)
        if body is None:
            return {'status': STATUS_INSUFFICIENT, 'basis': basis, 'auto_assessment': None,
                    'reasons': ['evidence_unavailable:' + ref]}
        if _norm(excerpt) not in _norm(body):
            # 摘录根本不在原文里——引用与原文不一致，不能被当作支持。
            return {'status': STATUS_CONTRADICTED, 'basis': basis, 'auto_assessment': None,
                    'reasons': ['excerpt_not_in_evidence:' + ref]}
        if ref not in state.read_evidence_refs:
            return {'status': STATUS_INSUFFICIENT, 'basis': basis, 'auto_assessment': None,
                    'reasons': ['evidence_not_read_back:' + ref]}
        support = assess_support(statement=assertion.get('predicate_value') or excerpt,
                                 quote=body, entities=assertion.get('subject_refs') or [])
        if support['status'] != 'supported_by_span':
            return {'status': STATUS_INSUFFICIENT, 'basis': basis,
                    'auto_assessment': support['status'],
                    'reasons': ['excerpt_does_not_support_statement'] + list(support['reasons'])}
    return {'status': STATUS_SUPPORTED, 'basis': basis, 'auto_assessment': 'excerpt_present', 'reasons': []}


def _verify_semantic(state, assertion, qualifiers, basis, evidence_store):
    """语义解释**不判 supported**。

    自动评估不等于人工确认，也不保证医学正确性。代码能做的只有一件事：确认
    模型给出的片段确实存在。剩下的明确标成需要人工确认，而不是靠关键词命中
    宣称"语义支持已验证"。
    """
    excerpt = str(qualifiers.get('excerpt') or '')
    explanation = str(qualifiers.get('explanation') or '')
    refs = list(assertion.get('evidence_refs') or [])
    if not excerpt.strip() or not explanation.strip():
        return {'status': STATUS_INSUFFICIENT, 'basis': basis, 'auto_assessment': None,
                'reasons': ['excerpt_and_explanation_required']}
    for ref in refs:
        body = _read_body(state, ref, evidence_store)
        if body is None:
            return {'status': STATUS_INSUFFICIENT, 'basis': basis, 'auto_assessment': None,
                    'reasons': ['evidence_unavailable:' + ref]}
        if _norm(excerpt) not in _norm(body):
            return {'status': STATUS_CONTRADICTED, 'basis': basis, 'auto_assessment': None,
                    'reasons': ['excerpt_not_in_evidence:' + ref]}
    if not refs:
        return {'status': STATUS_INSUFFICIENT, 'basis': basis, 'auto_assessment': None,
                'reasons': ['no_evidence_ref']}
    return {'status': STATUS_HUMAN, 'basis': basis,
            'auto_assessment': 'excerpt_present_explanation_not_clinically_verified',
            'reasons': ['semantic_interpretation_requires_human_confirmation']}


def _read_body(state, ref, evidence_store):
    if evidence_store is None or ref not in state.read_evidence_refs:
        return None
    try:
        page = evidence_store.read(ref, scope_id=state.spec.scope_id, offset=0, limit=2000)
    except Exception:
        return None
    return page.get('content') or ''


def verify_all(state, *, evidence_store=None) -> dict:
    """重算所有断言的支持判断，并刷新证据轴。

    只对**依据变了**的断言重算（修订、证据哈希变化、事实版本变化）；其余沿用
    已有判断——这样"跨问题复用原文"是允许的，而"复用旧判断"不会悄悄发生。
    """
    recomputed, reused, invalidated = 0, 0, []
    for assertion in state.assertions:
        basis = verification_basis(state, assertion, evidence_store=evidence_store)
        if assertion.get('verification_basis') == basis and assertion['verification_status'] != STATUS_UNVERIFIED:
            reused += 1
            continue
        result = verify_assertion(state, assertion, evidence_store=evidence_store)
        if assertion.get('verification_basis') and assertion['verification_basis'] != basis:
            invalidated.append(assertion['assertion_id'])
        assertion['verification_status'] = result['status']
        assertion['verification_reasons'] = result['reasons']
        assertion['auto_assessment'] = result.get('auto_assessment')
        assertion['verification_basis'] = result['basis']
        recomputed += 1
    state.refresh_axes()
    return {'recomputed': recomputed, 'reused': reused, 'invalidated': invalidated}
