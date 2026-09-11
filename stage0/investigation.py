"""Code-owned, versioned bounded medication evidence review (A1).

This is a run artifact, not an A2 persistent open task. Raw documents are data;
only authorised tool observations and fresh memory can advance coverage.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import hashlib
import itertools
import json
import re

from .evidence_quality import assess_claim

VERSION = 'investigation@1'
CONTRACT = 'medication-evidence-review@1'
REQUIRED = ('authority', 'interaction_evidence', 'applicability')
MAX_CLAIMS = 12
MAX_SEARCHES = 3
MAX_EVIDENCE = 12
# Protocol v2: state-conditional tool exposure + structured rejection
# feedback.  The state schema itself is unchanged (restore() still validates
# VERSION/CONTRACT), so persisted investigations stay loadable.
PROTOCOL_VERSION = 'propose-next-action@2'


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
    allowed = ['memory_write', 'memory_read']
    if any(g['kind'] == 'patient_fact_missing' and g['status'] == 'open' and g.get('field') for g in inv.gaps):
        allowed.append('ask_clarification')
    if any(g['kind'] == 'evidence_missing' and g['status'] == 'open' for g in inv.gaps):
        allowed.extend(('rag_search', 'ddi_check'))
        if any(ref not in inv.read_refs for ref in inv.evidence_refs):
            allowed.append('read_evidence')
    return tuple(dict.fromkeys(allowed))


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
            names = [m['display_name'] for m in meds]
            pairs = list(itertools.islice(itertools.combinations(names, 2), MAX_CLAIMS + 1)) if len(names) > 1 else [(names[0],)]
            if len(pairs) > MAX_CLAIMS:
                self.gap('coverage_limit', 'evidence_missing', '药物组合超过本轮有界核查范围，剩余组合未检查。')
            for pair in pairs[:MAX_CLAIMS]:
                identifier = 'claim:' + digest(pair)[:12]
                self.claims.append({'claim_id': identifier, 'statement': '、'.join(pair) + '的标签证据',
                    'entities': list(pair), 'status': 'insufficient', 'supporting_evidence': [], 'opposing_evidence': [],
                    'source_status': 'unknown', 'condition_status': 'unknown'})
                self.gap(identifier, 'evidence_missing', '核查' + '、'.join(pair) + '的支持和反对证据', claim_id=identifier)
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

    def next_action(self):
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
                claim_id = next((g['gap_id'] for g in self.gaps if g['kind'] == 'evidence_missing' and g['status'] == 'open'), self.claims[0]['claim_id'])
                return action('read_evidence', claim_id, {'evidence_id': ref, 'limit': 2000}, '回读并校验原文、实体、否定和适用条件')
        if any(g['kind'] == 'evidence_conflict' and g['status'] == 'open' for g in self.gaps):
            self.termination_reason = 'waiting_review'
            return None
        if self.claims and all(v == 'checked' for v in self.checks.values()) and not any(g['status'] == 'open' for g in self.gaps):
            self.termination_reason = 'checks_completed'
            return None
        if len(self.queries) >= MAX_SEARCHES:
            self.termination_reason = 'budget_insufficient'
            return None
        gap = next((g for g in self.gaps if g['kind'] == 'evidence_missing' and g['status'] == 'open'), None)
        if gap is None:
            self.termination_reason = 'no_progress'
            return None
        claim = next((c for c in self.claims if c['claim_id'] == gap.get('claim_id')), self.claims[0] if self.claims else None)
        terms = ' '.join(claim['entities']) if claim else self.goal[:150]
        suffix = ('药物相互作用 风险', '适用条件 禁忌 否定 相互作用', '证据不足 相互作用 日期')[len(self.queries)]
        return action('rag_search', gap['gap_id'], {'query': terms + ' ' + suffix, 'top_k': 5}, '获得新增可核验证据或明确冲突')

    def finish(self, degraded_reason=None):
        if degraded_reason and degraded_reason.startswith('planner_circuit_break:') and self.termination_reason:
            # A code-verified result may finish after the planner was disabled.
            # The run still exposes its degraded planner status separately.
            return
        if degraded_reason:
            self.termination_reason = ('cancelled' if degraded_reason == 'cancelled' else
                'budget_insufficient' if 'budget' in degraded_reason or 'max_cycles' in degraded_reason else
                'no_progress' if 'no_progress' in degraded_reason else 'unrecoverable_failure')
        if not self.termination_reason:
            self.next_action()
        self.termination_reason = self.termination_reason or 'no_progress'

    def report_text(self):
        checked = [k for k, value in self.checks.items() if value == 'checked']
        labels = {'authority': '权威用药及关键事实', 'interaction_evidence': '标签证据', 'applicability': '材料适用条件'}
        lines = ['有界证据核查报告', '已核查范围：' + ('、'.join(labels[k] for k in checked) or '尚无完成项') + '。']
        for claim in self.claims:
            # Statements are code-produced record labels; do not quote untrusted instructions.
            lines.append(f"{claim['statement']}：{claim['status']}。支持引用：{', '.join(claim['supporting_evidence']) or '无'}；反对引用：{', '.join(claim['opposing_evidence']) or '无'}。")
        remaining = [g['description'] for g in self.gaps if g['status'] == 'open']
        lines.append('待补充/未解决：' + ('；'.join(remaining) or '本契约内没有剩余缺口') + '。')
        lines.append('终止原因：' + str(self.termination_reason) + '。')
        lines.append('这份报告仅说明有界标签核查结果；insufficient/unknown 不是无风险，未列药物不代表停药。建议咨询医生/药师。')
        return '\n'.join(lines)


def proposal_errors(inv, proposal):
    """Additive schema applies only to versioned investigation runs."""
    if proposal.get('decision') == 'respond':
        return [] if inv.termination_reason else ['investigation_not_terminal']
    tool = proposal.get('tool')
    args = proposal.get('arguments') or {}
    if tool == 'memory_write':
        return []  # Existing write policy and receipt guard still apply.
    gap = next((g for g in inv.gaps if g['gap_id'] == proposal.get('gap_id') and g['status'] == 'open'), None)
    if gap is None or not isinstance(proposal.get('expected_observation'), str) or not proposal['expected_observation'].strip():
        return ['invalid_gap_link']
    if tool not in {'memory_read', 'rag_search', 'read_evidence', 'ask_clarification', 'ddi_check'}:
        return ['investigation_tool_not_allowed']
    if tool == 'read_evidence' and args.get('evidence_id') not in inv.evidence_refs:
        return ['evidence_not_observed_in_scope']
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
