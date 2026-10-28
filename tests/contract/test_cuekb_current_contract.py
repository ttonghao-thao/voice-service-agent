"""Protect current provider fields without weakening closed evidence types."""
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from app.agent_runtime.context import RunContext
from app.agent_runtime.direct import EvidenceReady
from app.agent_runtime.native_cuekb import NativeCueKBExecutor, voice_result
from app.config import Settings
from app.contracts import Citation, Principal
from app.tools.adapters import CueKBAdapter
from app.tools.schemas import CueKBSearchInput, CueKBSearchRequest, CueKBSearchResponse
from pydantic import ValidationError


def test_current_provider_fields_preserve_scope_and_reject_unknown_fields():
    payload = json.loads(Path('tests/fixtures/cuekb-v2-response.json').read_text())
    hit = payload['hits'][0]
    hit.update(fact_kind='specification', dependency_issues=['unmatched_reference'])
    payload['diagnostics']['selection'] = {
        'method': 'adaptive', 'calibration_status': 'not_configured',
        'stop_reason': 'max_hits', 'considered': 5,
    }
    for part in hit.get('context_parts', []):
        part['applicability'] = {'status': 'unknown'}
        part['link_provenance'] = None
    result = CueKBSearchResponse.model_validate(payload)
    assert result.hits[0].dependency_issues == ['unmatched_reference']
    assert result.diagnostics.selection.coverage_assessment == 'unassessed'
    hit['invented_truth_probability'] = 1.0
    with pytest.raises(ValidationError):
        CueKBSearchResponse.model_validate(payload)


def latest_response():
    """Synthetic 8a5aacc wire fields, independently checked with upstream DTOs.

    No production documents, model inference or provider dependency in CI.
    """
    payload = json.loads(Path('tests/fixtures/cuekb-v2-response.json').read_text())
    hit = payload['hits'][0]
    hit['fact_kind'] = 'comparison'
    primary = hit['context_parts'][0]
    hit['context_parts'].append({
        **primary, 'chunk_id': '55555555-5555-4555-8555-555555555556',
        'source_text': 'Only indoors; do not apply this rule to all products.',
        'source_spans': [], 'role': 'exception', 'required': True,
        'applicability': {'status': 'unknown', 'reason_codes': ['missing_binding']},
        'link_provenance': {'kind': 'curated', 'source_unit_id': 'main',
                            'target_unit_id': 'condition', 'provenance_id': 'review-1'},
    })
    hit['context'] = '\n\n'.join(part['source_text'] for part in hit['context_parts'])
    return payload


async def consume(payload):
    ctx = RunContext(Principal(user_id='test', scopes=frozenset({'knowledge:read'}),
        knowledge_base_ids=tuple(payload['content_revisions'])), 'cid', 'tid', 0,
        native_voice_search=True, tool_arguments={'query': 'Compare Product A usage conditions'})
    settings = Settings(_env_file=None, cuekb_mode='real', cuekb_base_url='https://cuekb.fixture',
                        cuekb_api_key='synthetic-key')
    calls = []

    def handle(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json=payload)

    async def record(*args, **kwargs):
        pass

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        adapter = CueKBAdapter(settings, client)

        async def invoke(name, arguments, context):
            return await adapter.invoke(CueKBSearchInput(**arguments), context)

        executor = NativeCueKBExecutor(settings, SimpleNamespace(invoke=invoke, store=SimpleNamespace(record=record)))
        ready = await executor.run('Compare Product A usage conditions', ctx, [])
    return ready, ctx, calls


async def test_new_fields_survive_adapter_citation_roundtrip_and_actual_model_projection():
    ready, ctx, calls = await consume(latest_response())
    assert isinstance(ready, EvidenceReady) and len(calls) == 1
    assert calls[0]['selection_mode'] == 'ranked'  # no implicit adaptive rollout
    citation = Citation.model_validate_json(ctx.evidence['C1'].model_dump_json())
    assert citation.fact_kind == 'comparison' and not citation.dependency_issues
    part = citation.context_parts[1]
    assert part['link_provenance']['provenance_id'] == 'review-1'
    assert part['applicability']['reason_codes'] == ['missing_binding']
    source = voice_result(ready)['sources'][0]
    assert source['fact_kind'] == 'comparison'
    assert source['applicability']['status'] == 'resolved'
    assert source['context'][0]['text'] == part['source_text']
    assert source['context'][0]['role'] == 'exception'
    assert source['context'][0]['applicability'] == {
        'status': 'unknown', 'subjects': [], 'conditions': [], 'reason_codes': ['missing_binding']}
    assert 'review-1' not in json.dumps(source)  # provenance stays in audit


@pytest.mark.parametrize('issue', ['unmatched_reference', 'ambiguous_reference',
    'dependency_limit_exceeded', 'dependency_cycle', 'unsafe_dependency'])
async def test_dependency_issues_are_preserved_and_prevent_admitting_an_incomplete_group(issue):
    payload = latest_response()
    payload['hits'][0]['dependency_issues'] = [issue]
    # Even a contradictory complete flag must not hide an explicit issue.
    ready, ctx, calls = await consume(payload)
    assert not isinstance(ready, EvidenceReady) and len(calls) == 1
    assert ready.reason_code == 'CUEKB_VOICE_EVIDENCE_UNAVAILABLE'
    assert ctx.evidence['C1'].dependency_issues == [issue]


@pytest.mark.parametrize('selection', [None, {'method': 'adaptive', 'calibration_status': 'binding_mismatch',
    'stop_reason': 'candidates_exhausted', 'coverage_assessment': 'unassessed'}])
async def test_invalid_calibration_degraded_response_is_usable_without_claiming_sufficiency(selection):
    payload = latest_response()
    payload.update(retrieval_status='degraded', degraded_reasons=['relevance_profile_invalid'])
    payload['diagnostics']['selection'] = selection
    ready, _, calls = await consume(payload)
    assert isinstance(ready, EvidenceReady) and len(calls) == 1
    assert ready.envelope['retrieval_status'] == 'degraded'
    assert ready.envelope['evidence_status'] == 'unassessed'
    assert ready.envelope['partial'] is True


@pytest.mark.parametrize('fields', [
    {'selection_query': 'Product A'}, {'query_facets': ['storage']},
    {'selection_mode': 'adaptive', 'selection_query': '   '},
    {'selection_mode': 'adaptive', 'query_facets': ['']},
])
def test_selection_inputs_follow_upstream_adaptive_only_and_nonblank_rules(fields):
    with pytest.raises(ValidationError):
        CueKBSearchRequest(query='Product A', kb_ids=['11111111-1111-4111-8111-111111111111'], **fields)
