"""Code-owned, versioned bounded medication evidence review (A1).

This is a run artifact, not an A2 persistent open task. Raw documents are data;
only authorised tool observations and fresh memory can advance coverage.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import hashlib
import itertools
import json
import os
import re

from .evidence_quality import assess_claim
from .response_safety import composed_text_prescribes

VERSION = 'investigation@1'
CONTRACT = 'medication-evidence-review@1'
REQUIRED = ('authority', 'interaction_evidence', 'applicability')
MAX_CLAIMS = 12
MAX_SEARCHES = 3
MAX_EVIDENCE = 12
# 连续被拒的子问题声明次数上限。A planner is allowed to revise; it is not
# allowed to spin. 界是**连续**的：一次成功即归零，所以反复摸索不会累积成
# 一次停摆，而原地打转很快就会撞上。
MAX_PLAN_ATTEMPTS = 3
# Protocol v2: state-conditional tool exposure + structured rejection
# feedback.  The state schema itself is unchanged (restore() still validates
# VERSION/CONTRACT), so persisted investigations stay loadable.
PROTOCOL_VERSION = 'investigation-protocol@2'

# Protocol v2: the ONE gap through which this investigation's sub-questions are
# declared.  It is the only legal ``gap_id`` for ``plan_questions``, so that
# tool can never be used to sidestep another open gap.
GAP_PLAN = 'subquestions'

# 只作为**结论**存在的缺口：它们必须出现在报告里，但不是待办，因此不阻塞
# 完成。产生 material_conflict 的地方写得很清楚——"Recorded as a finding,
# not a stop: a discrepancy is something to report"。可它**没有任何工具能
# 关闭**，而完成条件要求"无任何开放缺口"，于是任何读到过材料差异的回合都
# 只能以 no_progress 收尾：一个被声明为"结论"的东西实际上是一道永久闸门。
# 清单是**白名单**：未知缺口种类一律照旧阻塞（fail-closed）。
FINDING_GAPS = frozenset({'material_conflict'})


def allowed_tools(inv):
    """Tools the current state may legally use (presentation only).

    Mirrors ``proposal_errors``: narrowing the advertised catalog shrinks the
    parameter space the model searches, while the validator remains the sole
    authority.  ``memory_write`` stays available for consolidation before
    respond; ``respond`` appears only once the code has set a termination
    reason."""
    if inv.termination_reason:
        return ('respond',)
    if not inv.authority_read:
        return ('memory_write', 'memory_read')
    # Read-only, always legal inside a review.  When no MaterialIndex is
    # attached these are not registered, so tool_definitions skips them (it
    # drops names with no schema) and a proposal naming one is unknown_tool.
    allowed = ['memory_write', 'memory_read', 'list_materials', 'read_material_item']
    if any(g['gap_id'] == GAP_PLAN and g['status'] == 'open' for g in inv.gaps):
        # Sub-questions are not yet declared: planning is the only way forward.
        allowed.append('plan_questions')
    if any(g['kind'] == 'patient_fact_missing' and g['status'] == 'open' and g.get('field') for g in inv.gaps):
        allowed.append('ask_clarification')
    if any(g['kind'] == 'evidence_missing' and g['status'] == 'open' for g in inv.gaps):
        allowed.extend(('rag_search', 'ddi_check'))
    # 回读**本身就是完成条件的一部分**：``forced_stop`` 要求
    # ``not unread_evidence()``。所以只要还有已检索未回读的原文，这个工具就
    # 必须可用，与"当前有没有开放的证据缺口"无关——缺口已经关闭而原文尚未
    # 回读是最常见的情形，此时若把工具收走，"还差一次回读"与"没有工具能回读"
    # 就同时成立，回合只能靠预算耗尽或熔断收尾。
    if any(ref not in inv.read_refs for ref in inv.evidence_refs):
        allowed.append('read_evidence')
    return tuple(dict.fromkeys(allowed))


# Vocabulary the delivered-text safety check treats as a hazard assertion.
# Mirrors response_safety's concrete-hazard set; a line naming one of these
# needs a grounded citation, so the report never REPEATS such a sentence from
# model-authored text — it reports the subjects instead.
CONCRETE_HAZARD = re.compile(r'出血|低血压|致命|肾损伤|肝损伤|bleeding|fatal', re.IGNORECASE)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


@dataclass
class InvestigationState:
    goal: str
    scope_id: str
    version: str = VERSION
    contract_version: str = CONTRACT
    patient_version: dict = field(default_factory=dict)
    claims: list = field(default_factory=list)
    gaps: list = field(default_factory=list)
    checks: dict = field(default_factory=lambda: {k: 'uncovered' for k in REQUIRED})
    facts: dict = field(default_factory=dict)
    conflicts: list = field(default_factory=list)
    questions: list = field(default_factory=list)
    candidates: list = field(default_factory=list)
    evidence_refs: list = field(default_factory=list)
    read_refs: list = field(default_factory=list)
    content_hashes: list = field(default_factory=list)
    queries: list = field(default_factory=list)
    assessments: dict = field(default_factory=dict)
    no_progress_count: int = 0
    termination_reason: str | None = None
    authority_read: bool = False
    invalidations: list = field(default_factory=list)
    context_integrity: str = 'full_authority_checked_evidence_body_on_demand'
    mode: str = 'deterministic'
    # Protocol v2.  Additive only — an older persisted state restores fine and
    # simply reports 'unset'.  'model' means the model declared the
    # sub-questions; 'code_default' means the degraded policy had to.
    subquestion_source: str = 'unset'
    # 连续被拒的子问题声明次数。改数这个计数器而不是数 ``plan:*`` 缺口：缺口
    # 按 id 去重，而 id 是错误签名的拼接——同样的错误重复多少次都只有一个缺口
    # （上限永远够不到），三种不同的单次错误却会凑够三个（误触发）。
    plan_attempts: int = 0
    # Materials the model has SEEN (index) versus READ BACK (item).  Only the
    # latter can support a citation, mirroring the label-evidence rule that
    # "found" is not "read and verified".
    material_refs: list = field(default_factory=list)
    material_read_refs: list = field(default_factory=list)
    # 已"看到"的材料条目，唯一规范形状（ref -> {'name','kind','current'}）。
    # 写入端与读取端共用这一种形状，所以"材料里的药名能不能被子问题引用"与
    # "差异该点名哪条当前记录"读的是同一份数据。
    material_items: dict = field(default_factory=dict)
    pending_statements: list = field(default_factory=list)
    # 只渲染了空态句的节标题。**派生字段**：每次 ``report_text()`` 重新计算，
    # 只为让序列化视图能把它带给评分器。默认空列表 = "没有一节是空态"，
    # 于是渲染失败时评分器按"该写没写"判失败——失败方向是保守的。
    empty_sections: list = field(default_factory=list)

    @classmethod
    def restore(cls, raw, scope):
        if raw.get('version') != VERSION or raw.get('contract_version') != CONTRACT:
            raise ValueError('investigation migration required: unsupported version')
        if raw.get('scope_id') != scope:
            raise ValueError('investigation scope mismatch')
        state = cls(**raw)
        if set(state.checks) != set(REQUIRED):
            raise ValueError('investigation contract coverage mismatch')
        return state

    def to_dict(self):
        return asdict(self)

    def planner_view(self):
        from .harness.context import bounded_patient_snapshot, omissions
        view = self.to_dict()
        # Bound with explicit omission markers; do not silently prefilter
        # chronic conditions or other reported patient facts.
        view['facts'] = bounded_patient_snapshot(self.facts)
        view['context_omissions'] = omissions(view['facts'])
        view['authority_validation'] = 'full_snapshot_outside_model_context'
        # Protocol v2: make "searched" vs "read and verified" explicit, expose
        # the open-gap queue and the tools the current state may legally use.
        # Presentation only — the validator stays the authority.
        view['protocol_version'] = PROTOCOL_VERSION
        view['evidence_unread'] = [ref for ref in self.evidence_refs if ref not in self.read_refs]
        view['evidence_searched_count'] = len(self.evidence_refs)
        view['evidence_read_count'] = len(self.read_refs)
        view['open_gaps'] = [{'gap_id': g['gap_id'], 'kind': g['kind'], 'description': g['description']}
                             for g in self.gaps if g['status'] == 'open']
        view['allowed_tools'] = list(allowed_tools(self))
        return view

    def validate_sources(self, evidence_store):
        """Revalidate on restore/publication; immutable old content can be superseded."""
        invalid = []
        for ref in self.read_refs:
            try:
                evidence_store.read(ref, scope_id=self.scope_id, limit=1)
                with evidence_store._lock:
                    has_product = evidence_store.connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='product_objects'").fetchone()
                    replacements = evidence_store.connection.execute(
                        "SELECT body_json FROM product_objects WHERE scope_id=? AND kind='source_replacement'", (self.scope_id,)).fetchall() if has_product else []
                if any(json.loads(r[0]).get('old_evidence_id') == ref for r in replacements):
                    invalid.append(ref)
            except Exception:
                invalid.append(ref)
        for ref in invalid:
            self.gap('source:' + ref, 'source_invalid', '来源已失效或被明确替代；历史原文不作为当前支持。', evidence_ref=ref)
            for assessments in self.assessments.values():
                if ref in assessments:
                    assessments[ref].update(status='insufficient', source_status='invalid', condition_status='unknown')
            self.termination_reason = 'unrecoverable_failure'
        self._assess()

    # ---- Protocol v2: sub-questions -------------------------------------

    @property
    def subquestions_delegated(self) -> bool:
        """Whether the PLANNER owns the sub-question set.

        Only a real model does by default.  A ``scripted`` planner is the
        offline double: it proves the state machine and the execution
        constraints, and it must not be credited with a planning capability it
        was never given.  An evaluation may opt a script into the contract
        explicitly with ``AGENT_SUBQUESTION_PLANNER=model`` — that is a
        declared choice about who owns the decomposition, not a test branch.
        """
        if self.mode == 'llm':
            return True
        if self.mode == 'scripted':
            return os.getenv('AGENT_SUBQUESTION_PLANNER', '').strip().lower() == 'model'
        return False

    def allowed_entities(self) -> set[str]:
        """Names a sub-question may reference: the authoritative medication
        names, plus the names carried by materials staged for this scope.  A
        planner may not invent a drug.

        材料那一半读的是 ``material_items``——与写入端**同一个形状**。旧写法
        在这里按字典取 ``entry['candidate']['fields']['name']``，而写入端放的
        是字符串 ref，两处都不符，这段于是成了永不生效的死代码：材料独有的
        药名（"维生素D"）从来没能进入规划范围。
        """
        names = {str(m['display_name']) for m in self.facts.get('medications', []) if m.get('display_name')}
        for detail in self.material_items.values():
            name = (detail or {}).get('name')
            if name:
                names.add(str(name))
        return names

    def _new_claim(self, statement, entities, source):
        # Id derived from the ENTITIES alone, which is the formula this contract
        # has always used: persisted claim ids stay stable across the upgrade,
        # and two sub-questions over the same drug set are the same evidence
        # question rather than two colliding records.
        identifier = 'claim:' + digest(list(entities))[:12]
        if any(claim['claim_id'] == identifier for claim in self.claims):
            return identifier
        self.claims.append({'claim_id': identifier, 'statement': statement, 'entities': list(entities),
            'status': 'insufficient', 'supporting_evidence': [], 'opposing_evidence': [],
            'source_status': 'unknown', 'condition_status': 'unknown', 'source': source})
        self.gap(identifier, 'evidence_missing',
                 '核查' + '、'.join(entities) + '的支持和反对证据', claim_id=identifier)
        return identifier

    def apply_default_subquestions(self):
        """The DEGRADED sub-question policy: every drug pair, bounded.

        Used only where no planner can declare sub-questions (deterministic
        mode) or where the planner failed and the loop fell back.  The source
        is recorded so a report can never present this as model reasoning.
        """
        meds = self.facts.get('medications', [])
        if self.claims or not meds:
            return
        names = [m['display_name'] for m in meds]
        pairs = (list(itertools.islice(itertools.combinations(names, 2), MAX_CLAIMS + 1))
                 if len(names) > 1 else [(names[0],)])
        if len(pairs) > MAX_CLAIMS:
            self.gap('coverage_limit', 'evidence_missing',
                     '药物组合超过本轮有界核查范围，剩余组合未检查。')
        for pair in pairs[:MAX_CLAIMS]:
            self._new_claim('、'.join(pair) + '的标签证据', list(pair), 'code_default')
        self.subquestion_source = 'code_default'
        for g in self.gaps:
            if g['gap_id'] == GAP_PLAN:
                g['status'] = 'resolved'

    def accept_questions(self, questions) -> list[str]:
        """Validate and adopt planner-declared sub-questions.

        Returns error codes; empty means accepted.  A rejection NEVER falls
        back to a code-authored substitute — the model either satisfies the
        contract or the degraded policy is used, labelled.  Validation here is
        grounding, not scriptedness: any set of sub-questions is legal as long
        as its entities exist in this scope and the authoritative list is
        covered, so different-but-valid decompositions all pass.
        """
        if not isinstance(questions, list) or not 1 <= len(questions) <= MAX_CLAIMS:
            return ['invalid_subquestion_count']
        allowed = self.allowed_entities()
        normalised = []
        for item in questions:
            if not isinstance(item, dict) or not isinstance(item.get('statement'), str) \
                    or not item['statement'].strip() or len(item['statement']) > 200:
                return ['invalid_subquestion_statement']
            if composed_text_prescribes(item['statement']):
                # A sub-question is a LABEL for evidence to gather, and the
                # report renders it.  Text that diagnoses, prescribes or
                # changes a dose must never enter the review in that role —
                # rejecting it here is the only place that keeps it out of
                # every downstream rendering.
                return ['subquestion_prescribes']
            entities = item.get('entities')
            if not isinstance(entities, list) or not entities \
                    or any(not isinstance(entity, str) or not entity for entity in entities):
                return ['invalid_subquestion_entities']
            if any(entity not in allowed for entity in entities):
                return ['subquestion_entity_not_in_scope']
            normalised.append({'statement': item['statement'].strip(), 'entities': list(entities)})
        covered = {entity for item in normalised for entity in item['entities']}
        required = {str(m['display_name']) for m in self.facts.get('medications', []) if m.get('display_name')}
        if not required.issubset(covered):
            return ['subquestion_coverage_incomplete']
        self.claims = []
        for item in normalised:
            self._new_claim(item['statement'], item['entities'], 'model')
        self.subquestion_source = 'model'
        self.plan_attempts = 0
        for g in self.gaps:
            # 一次成功的声明同时取代 GAP_PLAN 与此前**所有**的 plan:* 错误
            # 缺口。不关掉它们，"已经被修正的错误"会永久挡住 checks_completed
            # ——完成条件要求无任何开放缺口，而这些缺口没有任何工具能关。
            if g['gap_id'] == GAP_PLAN or g['gap_id'].startswith('plan:'):
                g['status'] = 'resolved'
        return []

    def gap(self, identifier, kind, description, **details):
        item = next((g for g in self.gaps if g['gap_id'] == identifier), None)
        if item is None:
            item = {'gap_id': identifier, 'kind': kind, 'description': description, 'status': 'open', **details}
            self.gaps.append(item)
        return item

    def sync_authority(self, memory):
        snapshot = memory.snapshot()  # Full authoritative set: never relevance-truncated.
        versions = {k: memory.scope_revision(k) for k in ('medications', 'semantic')}
        if self.patient_version and versions != self.patient_version:
            medications_changed = versions.get('medications') != self.patient_version.get('medications')
            if medications_changed:
                # The claim set is derived from the medication list; a changed
                # list invalidates every claim and all collected evidence use.
                self.invalidations.append({'reason': 'medications_changed', 'before': self.patient_version, 'after': versions})
                self.assessments.clear()
                self.claims.clear()
                self.gaps.clear()
                self.read_refs.clear()
                self.evidence_refs.clear()
                self.queries.clear()
            else:
                # Semantic-only change: applicability must be re-verified against
                # the corrected fact, but unchanged material evidence and reads
                # are reused (A2 selective invalidation; conservative on purpose —
                # applicability depends on semantic facts, so they re-check).
                self.invalidations.append({'reason': 'semantic_changed_applicability_recheck', 'before': self.patient_version, 'after': versions})
                self.assessments.clear()
                # Collected evidence and past searches are reused; the bodies
                # must be re-read so applicability is checked against the
                # corrected facts (read_refs cleared, evidence_refs kept).
                self.read_refs.clear()
            self.authority_read = False
            self.no_progress_count = 0
            self.termination_reason = None
        self.patient_version = versions
        self.facts = snapshot
        self.conflicts = snapshot.get('open_conflicts', [])
        self.checks['authority'] = 'checked' if self.authority_read else 'uncovered'
        if not self.authority_read:
            self.gap('authority', 'patient_fact_missing', '读取完整权威药单与关键事实')
            return
        for g in self.gaps:
            if g['gap_id'] == 'authority':
                g['status'] = 'resolved'
        meds = snapshot.get('medications', [])
        if not meds:
            self.gap('fact:medication_name', 'patient_fact_missing', '当前权威记忆没有药名，请补充需要核查的药名。', field='medication_name')
        for med in meds:
            name = med['display_name']
            if re.search(r'剂量|单位|用量', self.goal) and not re.search(r'mg|μg|ug|g|ml|毫克|克|片|粒|单位|毫升', str(med.get('dose') or ''), re.I):
                self.gap('fact:dose_unit:' + name, 'patient_fact_missing', f'请补充{name}记录剂量的单位。', field='dose_unit:' + name)
            if re.search(r'日期|何时|什么时候|开始时间', self.goal) and (not med.get('start_at') or med.get('start_at_basis') != 'reported'):
                self.gap('fact:start_date:' + name, 'patient_fact_missing', f'请补充{name}的开始日期。', field='start_date:' + name)
        # A supplemented fact closes its recorded gap; a stale open gap would
        # otherwise re-ask an already-answered question.
        namespaces = {f['namespace'] for f in self.facts.get('semantic', [])}
        doses = {med['display_name']: str(med.get('dose') or '') for med in meds}
        for g in self.gaps:
            if g['kind'] != 'patient_fact_missing' or g['status'] != 'open' or not g.get('field'):
                continue
            field = g['field']
            if field == 'medication_name':
                resolved = bool(meds)
            elif field.startswith('dose_unit:'):
                resolved = bool(re.search(r'mg|μg|ug|g|ml|毫克|克|片|粒|单位|毫升', doses.get(field.split(':', 1)[1], ''), re.I))
            elif field.startswith('start_date:'):
                resolved = any(m['display_name'] == field.split(':', 1)[1] and m.get('start_at') and m.get('start_at_basis') == 'reported' for m in meds)
            else:
                resolved = field in namespaces
            if resolved:
                g['status'] = 'resolved'
        if not self.claims and meds:
            if self.subquestions_delegated:
                # Protocol v2: splitting the question into sub-questions is
                # investigation strategy, so it belongs to the planner.  The
                # code only opens the gap and states the contract.
                self.gap(GAP_PLAN, 'plan_missing',
                         '声明本轮要核查的子问题（拆分问题的唯一入口）。')
            else:
                # No planner here can propose sub-questions, so the degraded
                # policy owns them — and says so.
                self.apply_default_subquestions()
        if self.conflicts:
            self.gap('authority_conflict', 'evidence_conflict', '权威记录存在未决冲突；保留两侧，需通过现有审核流程核实。')

    def observe(self, observation, evidence_store):
        if not observation.ok:
            self.gap('failure:' + observation.tool, 'tool_failure', '工具执行失败，已有结果保留。', error_kind=observation.error_kind)
            self.termination_reason = 'unrecoverable_failure'
            return
        value = observation.result if isinstance(observation.result, dict) else {}
        if observation.tool == 'memory_read' and observation.arguments.get('query') == 'snapshot':
            self.authority_read = True
        if observation.tool == 'ask_clarification':
            self.termination_reason = 'waiting_input'
        if observation.tool == 'list_materials':
            # Enumerate what this run may subsequently read.  Listing is not
            # reading: these refs only become citations once read back.
            for material in (observation.result or {}).get('materials', []) or []:
                for item in material.get('items', []) or []:
                    ref = f"{material.get('case_id')}/{item.get('item_id')}"
                    if ref not in self.material_refs:
                        self.material_refs.append(ref)
                    # 唯一规范形状，写入端与读取端共用：药名供
                    # ``allowed_entities``，``current`` 供差异的双方具名。
                    self.material_items[ref] = {
                        'name': (item.get('fields') or {}).get('name'),
                        'kind': item.get('kind'),
                        'current': list(item.get('current') or []),
                    }
            return
        if observation.tool == 'read_material_item':
            # A material item read back in this run; the ONLY way a material
            # entry can support a report citation (the index alone cannot).
            ref = f"{observation.arguments.get('case_id')}/{observation.arguments.get('item_id')}"
            if ref not in self.material_read_refs:
                self.material_read_refs.append(ref)
            detail = observation.result if isinstance(observation.result, dict) else {}
            kind = detail.get('kind')
            if kind and kind != 'same':
                # A difference between a material and the authoritative record
                # that the planner actually READ.  Recorded as a finding, not a
                # stop: a discrepancy is something to report, not something
                # that ends the review (that is what evidence_conflict is for).
                issues = [str(item) for item in (detail.get('issues') or [])]
                # 差异必须**具名双方**：只写"材料 X 有差异"，读者无法去核对
                # 另一边，质检也就只能退回标题匹配。对方 ref 取自
                # list_materials 时记下的同一条目（material_items）。
                counterparts = list((self.material_items.get(ref) or {}).get('current') or [])
                self.gap('material:' + ref, 'material_conflict',
                         f"材料 {ref} 与当前记录"
                         + ('（' + '、'.join(counterparts) + '）' if counterparts else '')
                         + f"的差异：{kind}"
                         + ('；未决问题：' + '、'.join(issues) if issues else ''),
                         material_ref=ref, kind_detail=kind, counterparts=counterparts)
            return
        if observation.tool == 'plan_questions':
            # The sub-question set is adopted by the STATE, from the observed
            # arguments — the executor only echoes what it saw.  A rejected set
            # is recorded as a gap the planner can see and revise against; it
            # is never silently replaced by a code-authored substitute.  Only a
            # planner that keeps failing is stopped, so one badly worded
            # statement does not end an otherwise valid review.
            errors = self.accept_questions(observation.arguments.get('questions'))
            if errors:
                self.plan_attempts += 1
                self.gap('plan:' + ','.join(errors), 'plan_missing',
                         '子问题声明未通过校验（' + ','.join(errors) + '），请修订后重新提交。')
                if self.plan_attempts >= MAX_PLAN_ATTEMPTS:
                    self.termination_reason = 'no_progress'
            return
        if observation.tool in {'rag_search', 'ddi_check'}:
            query = str(observation.arguments.get('query', '')).strip().casefold()
            repeated = query in self.queries
            if not repeated and observation.tool == 'rag_search':
                self.queries.append(query)
            added = 0
            for ref in observation.evidence_refs:
                # First read enforces scope and hash before metadata influences progress.
                try:
                    page = evidence_store.read(ref, scope_id=self.scope_id, limit=1)
                    content_hash = page['content_hash']
                except Exception:
                    self.gap('source:' + ref, 'source_invalid', '证据来源不可回读或完整性失效。', evidence_ref=ref)
                    continue
                if ref not in self.evidence_refs and len(self.evidence_refs) < MAX_EVIDENCE:
                    self.evidence_refs.append(ref)
                if content_hash not in self.content_hashes:
                    self.content_hashes.append(content_hash)
                    added += 1
            self.no_progress_count = self.no_progress_count + 1 if repeated or not added else 0
            if self.no_progress_count >= 1 and len(self.queries) + int(repeated) >= 2:
                self.termination_reason = 'no_progress'
        if observation.tool == 'read_evidence':
            ref = observation.arguments.get('evidence_id')
            if ref not in self.evidence_refs:
                return  # Arbitrary tool-visible references cannot join this investigation.
            # Re-read using the same authorised store; observations never author authority.
            page = evidence_store.read(ref, scope_id=self.scope_id, offset=observation.arguments.get('offset', 0), limit=2000)
            if ref not in self.read_refs:
                self.read_refs.append(ref)
            meta = evidence_store.get_meta(ref) or {}
            namespaces = {f['namespace'] for f in self.facts.get('semantic', [])}
            for claim in self.claims:
                text = page['content']
                # Applicability v1 (lexical, conservative): when the chunk
                # states a population/condition, the claim stays insufficient
                # until a matching patient fact is RECORDED in authoritative
                # memory; the check then runs against that recorded context.
                # A model can never assert this — only the versioned fact store.
                required_subject = None
                conditional_namespaces = set()
                for word, namespace in [('肾功能', 'renal_function'), ('肝功能', 'hepatic_function'),
                                        ('儿童', 'age'), ('孕妇', 'pregnancy'), ('妊娠', 'pregnancy')]:
                    if word in text:
                        conditional_namespaces.add(namespace)
                # A recorded number/status alone does not prove the label's
                # population condition applies. Keep lexical screening conservative;
                # no clinical thresholds or inferred patient eligibility here.
                assessment = assess_claim(quote=text, text=text, entities=claim['entities'], evidence_id=ref,
                    subject=required_subject, required_subject=required_subject,
                    source_status='current' if meta.get('corpus_version') else 'unknown',
                    conditions_known=True, evidence_date=meta.get('retrieved_at'),
                    content_complete=not page.get('truncated'))
                self.assessments.setdefault(claim['claim_id'], {})[ref] = assessment
                if conditional_namespaces and conditional_namespaces.issubset(namespaces):
                    assessment['unresolved'].append('recorded_context_does_not_verify_applicability')
                if 'unstated_population_or_condition' in assessment['unresolved']:
                    for word, namespace in [('肾功能', 'renal_function'), ('肝功能', 'hepatic_function'),
                                            ('儿童', 'age'), ('孕妇', 'pregnancy'), ('妊娠', 'pregnancy')]:
                        if word in text and namespace not in namespaces:
                            self.gap('fact:' + namespace, 'patient_fact_missing', '请补充与材料适用条件相关的' + word + '记录；这不是临床审批。', field=namespace)
                    self.gap('condition:' + claim['claim_id'], 'evidence_missing', '材料的适用条件尚未核实，保留 insufficient。')
            self._assess()

    def _assess(self):
        for claim in self.claims:
            assessments = self.assessments.get(claim['claim_id'], {})
            support = [ref for ref, a in assessments.items() if a['status'] == 'supported']
            opposing = [ref for ref, a in assessments.items() if a['status'] == 'contradicted']
            claim.update(supporting_evidence=support, opposing_evidence=opposing,
                source_status='current' if assessments and all(a['source_status'] == 'current' for a in assessments.values()) else 'unknown',
                condition_status='verified' if (support or opposing) else 'unknown')
            claim['status'] = 'insufficient' if support and opposing else 'supported' if support else 'contradicted' if opposing else 'insufficient'
            if support and opposing:
                self.gap('conflict:' + claim['claim_id'], 'evidence_conflict', '支持与反对证据并存，不能以投票或用户选边消除。', evidence_refs=support + opposing)
            for g in self.gaps:
                if g['gap_id'] == claim['claim_id']:
                    g['status'] = 'resolved' if claim['status'] != 'insufficient' else 'open'
            cond_gap = next((g for g in self.gaps if g['gap_id'] == 'condition:' + claim['claim_id']), None)
            if cond_gap and claim['condition_status'] == 'verified':
                cond_gap['status'] = 'resolved'
        self.checks['interaction_evidence'] = 'checked' if self.claims and all(c['status'] != 'insufficient' for c in self.claims) else 'uncovered'
        self.checks['applicability'] = 'checked' if self.claims and all(c['condition_status'] == 'verified' for c in self.claims) else 'uncovered'

    # ---- 职责四分（协议 v2）------------------------------------------------
    # forced_stop()              纯状态检查 + 强制停止条件   —— 执行约束，不可协商
    # model_policy()             由 LLMPlanner 执行          —— 本类不实现
    # degraded_next_action()     确定性降级策略（含固定搜索词）
    # observe()/sync_authority() 事实读取与状态同步          —— 基础设施

    def forced_stop(self) -> str | None:
        """Non-negotiable stop conditions only.

        Returns and sets ``termination_reason``, and produces NO action — so it
        can never pre-plan.  These are the conditions a model must not be able
        to argue past: an already-set termination, an unresolved evidence
        conflict (code must not vote, and must not let the user pick a side), a
        conflict-free completion, and an exhausted search budget.  A repeated
        read with no new information is terminated inside ``observe``.
        """
        if self.termination_reason:
            return self.termination_reason
        if any(g['kind'] == 'evidence_conflict' and g['status'] == 'open' for g in self.gaps):
            self.termination_reason = 'waiting_review'
        elif (self.claims and all(v == 'checked' for v in self.checks.values())
              and not any(g['status'] == 'open' and g['kind'] not in FINDING_GAPS
                          for g in self.gaps)
              and not self.unread_evidence()):
            # A captured body that was never read back can carry the OPPOSING
            # source.  Declaring completion with one outstanding would make
            # coverage cheaper by skipping the read-back the evidence contract
            # rests on, and would hide a disagreement behind "completed".
            # Retrieval is not verification; this is the same rule the label
            # path already states as "'搜到' 不等于 '已读取并验证'".
            self.termination_reason = 'checks_completed'
        elif len(self.queries) >= MAX_SEARCHES:
            self.termination_reason = 'budget_insufficient'
        return self.termination_reason

    def unread_evidence(self) -> list:
        """Captured evidence whose body was never read back in this run."""
        return [ref for ref in self.evidence_refs if ref not in self.read_refs]

    def degraded_next_action(self):
        """The scripted, deterministic policy — the DEGRADED path only.

        Its fixed ordering and fixed search wording live here deliberately:
        they are a fallback for when the model path is unavailable, not the
        normal planning policy.  The model path must never see this action, so
        ``candidates`` is populated here and nowhere else.
        """
        from .agent import ToolAction
        self.candidates = []
        def action(tool, gap_id, arguments, expected):
            item = ToolAction(tool, 'investigation:' + gap_id, arguments, '解决已记录缺口', gap_id, expected)
            self.candidates = [asdict(item)]
            return item
        if self.termination_reason:
            return None
        if not self.authority_read:
            return action('memory_read', 'authority', {'query': 'snapshot'}, '获得完整当前事实及版本')
        missing = [g for g in self.gaps if g['kind'] == 'patient_fact_missing' and g['status'] == 'open' and g.get('field')]
        if missing:
            self.questions = [{'gap_id': g['gap_id'], 'field': g['field'], 'question': g['description']} for g in missing]
            return action('ask_clarification', missing[0]['gap_id'], {'question': '\n'.join(g['description'] for g in missing)}, '等待补充指定字段；未写入临床审批')
        for ref in self.evidence_refs:
            if ref not in self.read_refs:
                # 链接到一个**真实存在**的缺口即可：完成条件要求回读所有已
                # 检索的原文，而那时证据缺口往往已经关闭。
                claim_id = next((g['gap_id'] for g in self.gaps if g['kind'] == 'evidence_missing' and g['status'] == 'open'),
                                next((g['gap_id'] for g in self.gaps if g['status'] == 'open'),
                                     self.claims[0]['claim_id'] if self.claims else 'authority'))
                return action('read_evidence', claim_id, {'evidence_id': ref, 'limit': 2000}, '回读并校验原文、实体、否定和适用条件')
        if self.forced_stop():
            return None
        gap = next((g for g in self.gaps if g['kind'] == 'evidence_missing' and g['status'] == 'open'), None)
        if gap is None:
            self.termination_reason = 'no_progress'
            return None
        claim = next((c for c in self.claims if c['claim_id'] == gap.get('claim_id')), self.claims[0] if self.claims else None)
        terms = ' '.join(claim['entities']) if claim else self.goal[:150]
        suffix = ('药物相互作用 风险', '适用条件 禁忌 否定 相互作用', '证据不足 相互作用 日期')[len(self.queries) % 3]
        return action('rag_search', gap['gap_id'], {'query': terms + ' ' + suffix, 'top_k': 5}, '获得新增可核验证据或明确冲突')

    def next_action(self):
        """Compatibility shim.  The normal (model) path calls ``forced_stop()``
        instead; only the degraded path plans an action."""
        return self.degraded_next_action()

    def supply_default_subquestions(self):
        """If the planner never declared sub-questions, the degraded policy
        supplies them at wrap-up — labelled ``code_default``, so a report can
        never present them as the model's own decomposition.  Never raises:
        a bounded report with fewer questions is a valid outcome."""
        if any(g['gap_id'] == GAP_PLAN and g['status'] == 'open' for g in self.gaps):
            self.apply_default_subquestions()

    def finish(self, degraded_reason=None):
        if degraded_reason and degraded_reason.startswith('planner_circuit_break:') and self.termination_reason:
            # A code-verified result may finish after the planner was disabled.
            # The run still exposes its degraded planner status separately.
            self.supply_default_subquestions()
            return
        if degraded_reason:
            self.termination_reason = ('cancelled' if degraded_reason == 'cancelled' else
                'budget_insufficient' if 'budget' in degraded_reason or 'max_cycles' in degraded_reason else
                'no_progress' if 'no_progress' in degraded_reason else 'unrecoverable_failure')
        if not self.termination_reason:
            self.next_action()
        self.termination_reason = self.termination_reason or 'no_progress'
        self.supply_default_subquestions()

    def verify_statements(self) -> list[dict]:
        """Evidence-support check for MODEL-authored explanations.

        A statement written by the model is only a conclusion when the evidence
        it cites was actually READ BACK in this run — the same rule the label
        path already enforces ('搜到' 不等于 '已读取并验证').  Anything else is
        demoted to a question to raise at the visit: never silently deleted, and
        never rendered as a finding.
        """
        read = set(self.read_refs) | set(self.material_read_refs)
        pending = []
        for claim in self.claims:
            if claim.get('source') != 'model':
                continue
            refs = set(claim.get('supporting_evidence') or []) | set(claim.get('opposing_evidence') or [])
            if not refs:
                # Nothing read back supports it at all.
                pending.append({'claim_id': claim['claim_id'], 'statement': claim['statement'],
                                'reason': 'no_read_evidence'})
            elif not refs.issubset(read):
                pending.append({'claim_id': claim['claim_id'], 'statement': claim['statement'],
                                'reason': 'citation_not_read_back'})
        self.pending_statements = pending
        return pending

    def _render_statement(self, statement, entities):
        """Render one model-authored statement into the DELIVERED report.

        The delivered text passes a code-owned safety check that flags any line
        naming a hazard concept without a grounded citation or an explicit
        disclaimer.  Code-authored labels always satisfied that; model wording
        is arbitrary, so a sentence asserting a CONCRETE harm is reported by
        its subjects rather than repeated.  Nothing is hidden: the original
        statement stays in the structured artifact and in the claim record.
        """
        subjects = '、'.join(entities) or '相关药物'
        if CONCRETE_HAZARD.search(statement or ''):
            return f'- 涉及 {subjects} 的一项说法包含未经逐字核实的危害描述，本报告不复述；请与医生或药师核对。'
        if composed_text_prescribes(statement or ''):
            # Defence in depth: accept_questions already refuses these, so this
            # only fires for a statement that reached the claim set another way.
            return f'- 涉及 {subjects} 的一项说法带有诊断或用药调整措辞，本报告不复述；请与医生或药师核对。'
        return f'- {statement}（仅基于已回读原文并列呈现，不构成诊断）'

    # 空态句：某一节没有实质内容时写的句子。**渲染与评分共用**同一份定义
    # （评分器读的是同名副本），所以"这一节写了东西没有"只有一个答案。
    EMPTY_SECTION_SENTENCE = {
        '2. 有来源支持的事实': '- 本次没有得到可作为结论的事实；证据不足不等于证明绝对安全。',
        '3. 不同材料之间的差异': '- 本次未在已读取的材料与记录之间发现可记录的差异；未读取的材料不在此列。',
        '4. 仍缺少依据的问题（待核实）': '- 本契约内没有剩余缺口。',
        '5. 就诊时可以向医生或药师确认什么': '- 可将本报告的差异与未决项逐条向医生或药师确认。',
    }

    def section_content(self):
        """每一节的**实质**条目（不含空态句）。

        渲染与空态判定读同一个集合，于是不会出现"渲染说有内容、评分说没有"
        的漂移——那种漂移会让评分器判的其实是另一份报告。
        """
        self.verify_statements()
        pending_ids = {item['claim_id'] for item in self.pending_statements}
        concluded = []
        for claim in self.claims:
            if claim['status'] == 'insufficient' or claim['claim_id'] in pending_ids:
                continue
            concluded.append(self._render_statement(claim['statement'], claim.get('entities') or []))
            concluded.append(f"  状态：{claim['status']}。"
                             f"支持引用：{', '.join(claim['supporting_evidence']) or '无'}；"
                             f"反对引用：{', '.join(claim['opposing_evidence']) or '无'}。")
        return {
            '2. 有来源支持的事实': concluded,
            '3. 不同材料之间的差异': [
                f"- {g['description']}" for g in self.gaps
                if g.get('kind') in {'evidence_conflict', 'material_conflict'}],
            '4. 仍缺少依据的问题（待核实）': (
                [f"- {g['description']}" for g in self.gaps if g['status'] == 'open']
                + [self._render_statement(item['statement'], [])
                   + f"（未核实的解释，原因：{item['reason']}，列为待确认问题）"
                   for item in self.pending_statements]),
            '5. 就诊时可以向医生或药师确认什么': [
                f"- {question['question']}" for question in self.questions],
        }

    def citable_memory_refs(self) -> list:
        """本报告有权引用的记忆 ref。

        报告的差异一节会**点名双方**——材料条目与它所对比的当前记录，后者
        是 ``memory:<kind>:<n>`` 形状。响应的最后一道安全校验把"报告里出现
        而它没被告知"的 ref 判为 ``fabricated_memory_ref``，所以这些 ref 必须
        一并交出去；否则双方具名会被自己的安全边界拦下，报告根本发不出去。
        """
        refs = []
        for medication in self.facts.get('medications') or []:
            if medication.get('ref'):
                refs.append(str(medication['ref']))
        for gap in self.gaps:
            refs.extend(str(ref) for ref in (gap.get('counterparts') or []) if ref)
        for conflict in self.conflicts or []:
            for key in ('left_ref', 'right_ref', 'ref'):
                if conflict.get(key):
                    refs.append(str(conflict[key]))
        return list(dict.fromkeys(refs))

    def empty_report_sections(self) -> list:
        """只渲染了空态句的节标题。

        评分器据此区分两种"这一节没写东西"：状态**确实为空**时占位句是正确
        内容（诚实空态），状态非空时才是缺陷。没有这份声明，要求"必须有实质
        条目"会把"材料本就一致"的任务判成**恒假**——与恒真一样不可证伪。
        """
        return [title for title, items in self.section_content().items() if not items]

    def report_text(self):
        """The visit-preparation report: five answers, each grounded.

        The order is fixed because a caregiver reads it that way; the CONTENT
        is entirely derived from what was actually read back.
        """
        self.verify_statements()
        checked = [key for key, value in self.checks.items() if value == 'checked']
        labels = {'authority': '权威用药及关键事实', 'interaction_evidence': '标签证据',
                  'applicability': '材料适用条件'}
        sections = self.section_content()
        # 刷新派生字段，使序列化视图与**这份**报告一致。
        self.empty_sections = [title for title, items in sections.items() if not items]
        # The goal is deliberately NOT echoed here: it is the caregiver's own
        # free text, and the delivered response passes a keyword safety check
        # that a quoted "…风险…" would trip even though nothing is asserted.
        # `goal` stays on the investigation and in the saved artifact.
        lines = ['# 有界证据核查报告 · 就诊准备', '']
        lines += ['## 1. 本次调查解决了什么', '',
                  '已核查范围：' + ('、'.join(labels[key] for key in checked) or '尚无完成项') + '。',
                  '终止原因：' + str(self.termination_reason) + '。', '']
        for title in ('2. 有来源支持的事实', '3. 不同材料之间的差异',
                      '4. 仍缺少依据的问题（待核实）',
                      '5. 就诊时可以向医生或药师确认什么'):
            lines += ['## ' + title, '']
            lines += sections[title] or [self.EMPTY_SECTION_SENTENCE[title]]
            lines += ['']
        lines += ['这份报告仅说明有界核查结果；insufficient/unknown 不是无风险，'
                  '未列药物不代表停药，也不代表已停用。本系统不做诊断、处方或用药调整建议。'
                  '请携带本报告与医生或药师当面确认；建议咨询医生/药师后再做任何用药决定。']
        return '\n'.join(lines)


def proposal_errors(inv, proposal):
    """Additive schema applies only to versioned investigation runs."""
    if proposal.get('decision') == 'respond':
        return [] if inv.termination_reason else ['investigation_not_terminal']
    tool = proposal.get('tool')
    args = proposal.get('arguments') or {}
    if tool == 'memory_write':
        return []  # Existing write policy and receipt guard still apply.
    if tool == 'read_evidence':
        # 回读的必要性**先于**它所服务的缺口：完成条件要求把所有已检索的原文
        # 回读一遍，而"证据缺口已关闭"恰恰是那时最常见的状态。因此这一条只
        # 要求指向一个真实存在的缺口（开放或已关闭），不像其他工具那样要求
        # 它仍然开放——否则完成条件与可用工具互相矛盾，形成死锁。
        if args.get('evidence_id') not in inv.evidence_refs:
            return ['evidence_not_observed_in_scope']
        if not any(g['gap_id'] == proposal.get('gap_id') for g in inv.gaps):
            return ['invalid_gap_link']
        return []
    gap = next((g for g in inv.gaps if g['gap_id'] == proposal.get('gap_id') and g['status'] == 'open'), None)
    if gap is None or not isinstance(proposal.get('expected_observation'), str) or not proposal['expected_observation'].strip():
        return ['invalid_gap_link']
    if tool == 'plan_questions':
        # Only the planning gap accepts it: this tool must never become a way
        # to sidestep a different open gap.
        return [] if gap['gap_id'] == GAP_PLAN else ['plan_questions_only_for_subquestions_gap']
    if tool == 'read_material_item':
        # Same rule as read_evidence: only a material this run actually
        # enumerated may be read, so an id cannot be probed into existence.
        ref = f"{args.get('case_id')}/{args.get('item_id')}"
        if ref not in inv.material_refs:
            return ['material_not_observed_in_scope']
        return []
    if tool not in {'memory_read', 'rag_search', 'read_evidence', 'ask_clarification',
                    'ddi_check', 'list_materials'}:
        return ['investigation_tool_not_allowed']
    if tool == 'ask_clarification' and (not inv.authority_read or gap['kind'] != 'patient_fact_missing' or not gap.get('field')):
        return ['clarification_without_missing_fact']
    # The authority gap accepts only the full snapshot read.  A memory_read
    # with the query OMITTED is a correctable omission (the guard hydrates
    # query='snapshot' and records the correction) — only a WRONG query value
    # violates the contract here; a missing required argument is still caught
    # by the shared schema check when hydration is disabled.
    if gap['gap_id'] == 'authority' and (tool != 'memory_read'
                                         or (tool == 'memory_read' and 'query' in args and args.get('query') != 'snapshot')):
        return ['authority_requires_full_memory_read']
    if tool == 'ask_clarification' and args.get('question') not in {g['description'] for g in inv.gaps if g['kind'] == 'patient_fact_missing' and g.get('field')}:
        return ['question_does_not_match_missing_fact']
    return []
