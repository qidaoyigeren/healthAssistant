"""A3 request routing: three explicit execution paths with a verifiable basis.

The route is recorded in the answer bundle so every response says which path
produced it.  Natural-language compound or ambiguous requests are never
dropped by keyword matching: only an exact single-purpose medication ask
routes to the fixed deterministic query path; everything else keeps the full
gap-driven planner (or the legacy planner when the capability is off).
"""
from __future__ import annotations

ROUTE_VERSION = 'request-router@1'

# Explicit API event types are deterministic routing signals (constraint 3.1):
# the query type goes to the exact authoritative query, domain-write types go
# through their fixed contract flow.
EXACT_QUERY_EVENT_TYPES = {'query_current_medications'}
CONTRACT_EVENT_TYPES = {'medication_change', 'measurement', 'symptom',
                        'procedure_exposure', 'material_reconciliation'}

# Sentence-composition markers: a "药单" keyword inside such a request must
# not discard the rest of the goals.
_COMPOUND_MARKERS = ('；', '另外', '还有', '以及', '然后', '再帮我', '同时',
                     '顺便', '并且', '还是帮我')


def route_request(event, *, investigation_enabled: bool) -> dict:
    """Return {'route', 'basis', 'version'} for one incoming event.

    route ∈ {exact_query, contract_flow, open_planning, legacy}.  The basis is
    a short checkable reason string, never a model statement.
    """
    if event.event_type in EXACT_QUERY_EVENT_TYPES:
        return {'route': 'exact_query', 'basis': f'explicit_api_event_type:{event.event_type}',
                'version': ROUTE_VERSION}
    if event.event_type in CONTRACT_EVENT_TYPES:
        return {'route': 'contract_flow', 'basis': f'explicit_api_event_type:{event.event_type}',
                'version': ROUTE_VERSION}
    if event.event_type != 'user_message':
        return {'route': 'contract_flow', 'basis': f'explicit_api_event_type:{event.event_type}',
                'version': ROUTE_VERSION}
    from .agent import AgentPlanner, SafetyBoundary
    if SafetyBoundary.refuses_medical_authority(event.text):
        return {'route': 'legacy', 'basis': 'safety_boundary_precheck', 'version': ROUTE_VERSION}
    if not investigation_enabled:
        return {'route': 'legacy', 'basis': 'investigation_disabled', 'version': ROUTE_VERSION}
    simple_med_ask = AgentPlanner._asks_current_medications(event.text)
    compound = (any(marker in event.text for marker in _COMPOUND_MARKERS)
                or len(event.text.strip()) > 40)
    if simple_med_ask and not compound:
        return {'route': 'exact_query', 'basis': 'exact_single_purpose_medication_ask',
                'version': ROUTE_VERSION}
    # The med-ask may appear as one clause of a longer request; the keyword
    # must not route the WHOLE request to the exact query path.
    import re as _re
    clauses = [c for c in _re.split(r'[；;。！!？?\n]|另外|还有|以及|然后|同时|顺便|并且|再帮我', event.text) if c.strip()]
    if any(AgentPlanner._asks_current_medications(c) for c in clauses):
        return {'route': 'open_planning',
                'basis': 'compound_request_keeps_all_goals', 'version': ROUTE_VERSION}
    return {'route': 'open_planning', 'basis': 'open_nl_request', 'version': ROUTE_VERSION}
