import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from app.agent_runtime.context import RunContext
from app.agent_runtime.direct import EvidenceReady
from app.agent_runtime.native_cuekb import NativeCueKBExecutor, voice_result
from app.config import Settings
from app.contracts import Principal
from app.tools.adapters import CueKBAdapter
from app.tools.evidence_contract import EvidenceQueryResponse
from app.tools.schemas import CueKBSearchInput
from pydantic import ValidationError

KB = "11111111-1111-4111-8111-111111111111"
FIXTURES = Path(__file__).parents[1] / "fixtures"


def provider_response():
    return json.loads((FIXTURES / "cuekb-evidence-query-v2.1-response.json").read_text())


def validate_response():
    return json.loads((FIXTURES / "cuekb-evidence-validate-v2.1-response.json").read_text())


def context(question):
    return RunContext(
        Principal(user_id="test", scopes=frozenset({"knowledge:read"}), knowledge_base_ids=(KB,)),
        "conversation", "turn", 0, request_revision=3, native_voice_search=True,
        tool_arguments={"query": "Product A"},
        task_context={
            "original_request": question,
            "resolved_request": question,
            "request_revision": 3,
            "input_source": {"input_item_id": "input-1"},
        },
    )


async def test_voice_uses_one_problem_query_then_validates_package_and_projects_only_model_view():
    question = "What is Product A input voltage?"
    query_payload = provider_response()
    query_payload.update(request_id="input-1", input_revision=3)
    query_payload["delivery"].update(profile="voice", token_budget=300)
    query_payload["facets"][0].update(id="f1", text=question)
    query_payload["evidence"][0]["supports"] = ["f1"]
    query_payload["model_view"]["evidence"][0]["for"] = question
    validation = validate_response()
    validation.update(request_id="input-1", status="valid")
    requests = []

    def handle(request):
        requests.append(request)
        if request.url.path == "/v2/evidence/query":
            return httpx.Response(200, json=query_payload)
        if request.url.path == "/v2/evidence/validate":
            return httpx.Response(200, json=validation)
        raise AssertionError(request.url.path)

    settings = Settings(_env_file=None, cuekb_mode="real", cuekb_base_url="https://cuekb.fixture",
                        cuekb_api_key="private", cuekb_voice_token_budget=300)
    ctx = context(question)
    ctx.task_context["resolved_request"] = question + "\nConfirmed user conditions: Product A."

    async def record(*args, **kwargs):
        pass

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        adapter = CueKBAdapter(settings, client)

        async def invoke(name, arguments, run_context):
            output = await adapter.invoke(CueKBSearchInput(**arguments), run_context)
            return adapter.output_adapter.validate_python(output).model_dump(mode="json")

        executor = NativeCueKBExecutor(settings, SimpleNamespace(invoke=invoke, store=SimpleNamespace(record=record)))
        ready = await executor.run("Product A", ctx, [])

    assert isinstance(ready, EvidenceReady)
    assert [request.url.path for request in requests] == ["/v2/evidence/query", "/v2/evidence/validate"]
    sent = json.loads(requests[0].content)
    assert sent["question"] == question
    assert sent["search_query"] == question + "\nConfirmed user conditions: Product A."
    assert sent["request_id"] == "input-1" and sent["input_revision"] == 3
    assert sent["delivery_profile"] == "voice" and sent["token_budget"] == 300
    assert sent["requested_facets"] == [{"id": "f1", "text": question, "question_span": [0, len(question)]}]
    assert json.loads(requests[1].content)["package_id"] == query_payload["package"]["id"]
    result = voice_result(ready)
    assert result["decision"] == "answer" and result["search_state"] == "finished"
    assert result["evidence"] == query_payload["model_view"]["evidence"]
    assert "sources" not in result and ready.citations[0].cuekb_schema_version == "2.1"


def test_provider_fixture_is_strict_and_unknown_fields_fail():
    payload = provider_response()
    assert EvidenceQueryResponse.model_validate(payload).contract_revision == "cuekb.evidence/2.1"
    payload["invented_field"] = True
    with pytest.raises(ValidationError):
        EvidenceQueryResponse.model_validate(payload)


@pytest.mark.parametrize(("question", "texts"), [
    (
        "Can it be shipped? How long is its shelf life, and how should it be stored after opening?",
        ["Can it be shipped?", "How long is its shelf life", "how should it be stored after opening?"],
    ),
    (
        "What does the documentation say about price, weight, pastry layers, and best consumption time?",
        ["price", "weight", "pastry layers", "best consumption time?"],
    ),
])
def test_question_facets_preserve_exact_spans_for_compound_voice_questions(question, texts):
    facets = CueKBAdapter._question_facets(question)
    assert [facet.text for facet in facets] == texts
    assert all(question[start:end] == facet.text
               for facet, (start, end) in ((facet, facet.question_span) for facet in facets))


async def test_english_typography_is_projected_without_mutating_signed_evidence():
    from app.voice.provider import NvidiaVoiceChatAdapter
    original = {'decision': 'answer', 'evidence': [{'id': 'E1', 'for': 'Shelf life',
                'text': 'Monkey “Golden Yolk” — sealed: 45 days.', 'conditions': []}],
                'missing': [], 'search': 'finished'}
    ready = EvidenceReady({'schema_version': 'cuekb-evidence-v2.1', 'model_view': original,
                           'instructions': 'Answer now.'}, [], 0, 'answer')
    result = voice_result(ready)
    sent = []
    async def send(packet):
        sent.append(packet)
    adapter = NvidiaVoiceChatAdapter(Settings(_env_file=None))
    adapter.ws = SimpleNamespace(send=send)
    await adapter.submit_tool_result('call', json.dumps(result, ensure_ascii=True))
    assert len(sent) == 1
    assert '“Golden Yolk”' in original['evidence'][0]['text']
    assert result['evidence'][0]['text'] == 'Monkey "Golden Yolk" - sealed: 45 days.'


def test_context_bindings_require_unique_authorized_user_mentions():
    from app.tools.entity_context import context_binding
    entity = {'id': '11111111-1111-4111-8111-111111111112', 'name': 'Monkey Golden Yolk Mooncake',
              'aliases': ['Golden Yolk', 'golden yoke']}
    question = 'How long is its shelf life?'
    assert context_binding(question, [{'text': 'What about golden yoke mooncake?'}], [entity]) == [
        {'entity_id': entity['id'], 'source': 'confirmed_conversation_context'}]
    for prior in ['Not Golden Yolk.', 'Compare Golden Yolk and Black Pig.', 'What about another product?']:
        assert not context_binding(question, [{'text': prior}], [entity])
    duplicate = {**entity, 'id': '11111111-1111-4111-8111-111111111113'}
    assert not context_binding(question, [{'text': 'Golden Yolk'}], [entity, duplicate])
    assert not context_binding(question, [{'text': 'Golden Yolk'}], [])


@pytest.mark.parametrize('claim,code', [
    ('The sealed shelf life is ninety days.', 'VOICE_UNSUPPORTED_NUMBER'),
    ('The sealed shelf life is forty-five days.', None),
    ('After opening, eat within forty five days.', 'VOICE_MISSING_CONDITION'),
    ('After opening, store at room temperature and eat within three days.', 'VOICE_MISSING_CONDITION'),
    ('After opening, refrigerate and eat within three days.', None),
])
def test_spoken_numbers_and_conditions_are_checked_against_their_source(claim, code):
    from app.agent_runtime.evidence import EvidenceGate
    from app.contracts import Citation
    citation = Citation(citation_id='C1', document_id='d', chunk_id='c', title='Shelf life', version_id='v', trace_id='t', retrieval_id='r', rank=1,
                        content='Sealed shelf life: 45 days. After opening, refrigerate and consume within 3 days.')
    assert EvidenceGate.check_spoken(claim, [citation]) == code


async def test_pronoun_uses_authorized_catalog_and_user_history_not_model_query():
    ctx = context('How long is its shelf life?')
    ctx.task_context['recent_inputs'] = [{'text': 'What about golden yoke mooncake?'}]
    seen = []
    def handle(request):
        seen.append(request)
        assert request.url.path == f'/knowledge-bases/{KB}/entities'
        assert request.headers['authorization'] == 'Bearer private'
        return httpx.Response(200, json=[{'id': '11111111-1111-4111-8111-111111111112',
            'kb_id': KB, 'name': 'Monkey Golden Yolk Mooncake', 'aliases': ['golden yoke']}])
    settings = Settings(_env_file=None, cuekb_mode='real', cuekb_base_url='https://cuekb.fixture', cuekb_api_key='private')
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        adapter = CueKBAdapter(settings, client)
        binding = await adapter._context_bindings(ctx, ctx.task_context['original_request'])
    assert len(seen) == 1
    assert binding == [{'entity_id': '11111111-1111-4111-8111-111111111112', 'source': 'confirmed_conversation_context'}]


@pytest.mark.parametrize("spoken,normalized", [
    ("three point seven five percent", "3.75%"),
    ("forty-five days", "45 days"),
    ("one hundred and five", "105"),
])
def test_numeric_word_normalization_preserves_values(spoken, normalized):
    from app.agent_runtime.evidence import normalize_numbers
    assert normalize_numbers(spoken) == normalized


async def test_tokenizer_identity_mismatch_is_rejected_before_package_delivery():
    from app.contracts import DomainError
    question = 'What is Product A input voltage?'
    payload = provider_response()
    payload.update(request_id='input-1', input_revision=3)
    payload['delivery'].update(profile='voice', token_budget=300)
    payload['facets'][0].update(id='f1', text=question)
    payload['evidence'][0]['supports'] = ['f1']
    seen = []
    def handle(request):
        seen.append(request.url.path)
        return httpx.Response(200, json=payload)
    settings = Settings(_env_file=None, cuekb_mode='real', cuekb_base_url='https://cuekb.fixture',
                        cuekb_voice_token_budget=300, cuekb_voice_tokenizer_id='different-tokenizer')
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(DomainError) as caught:
            await CueKBAdapter(settings, client).invoke(CueKBSearchInput(query=question), context(question))
    assert caught.value.code == 'CUEKB_CONTRACT_ERROR'
    assert seen == ['/v2/evidence/query']


@pytest.mark.parametrize('spoken,normalized', [
    ('forty-five and three days', '45 and 3 days'),
    ('one hundred and five days', '105 days'),
])
def test_numeric_list_items_are_not_added_together(spoken, normalized):
    from app.agent_runtime.evidence import normalize_numbers
    assert normalize_numbers(spoken) == normalized


@pytest.mark.parametrize('claim,code', [
    ('The voltage is twelve volts.', None),
    ('The price is sixty five RMB.', 'VOICE_UNSUPPORTED_UNIT'),
    ('The weight is forty two grams.', 'VOICE_UNSUPPORTED_UNIT'),
    ('The weight is sixty five grams and the price is forty two RMB.', None),
])
def test_numbers_cannot_move_between_different_units(claim, code):
    from app.agent_runtime.evidence import EvidenceGate
    from app.contracts import Citation
    source = Citation(citation_id='C1', document_id='d', chunk_id='c', title='Specification',
                      version_id='v', trace_id='t', retrieval_id='r', rank=1,
                      content='Voltage: 12 V. Weight: 65 g. Price: RMB 42.')
    assert EvidenceGate.check_spoken(claim, [source]) == code


def test_subject_property_negations_and_explicit_corrections_preserve_the_correct_binding():
    from app.tools.entity_context import context_binding
    first = {'id': '11111111-1111-4111-8111-111111111112', 'name': 'Product Alpha', 'aliases': []}
    second = {'id': '11111111-1111-4111-8111-111111111113', 'name': 'Product Beta', 'aliases': []}
    rows = [{'text': 'Tell me about Product Alpha.'}]
    assert context_binding('Can it not be shipped?', rows, [first, second])[0]['entity_id'] == first['id']
    assert context_binding('If refrigerated, how long can it last?', rows, [first, second])[0]['entity_id'] == first['id']
    assert context_binding('How long is its shelf life?', [*rows, {'text': 'Not Product Alpha, Product Beta.'}],
                           [first, second])[0]['entity_id'] == second['id']
    assert not context_binding('What about this other product?', rows, [first, second])
    assert context_binding('How long is its shelf life?', [*rows, {'text': 'My new model is Product Beta.'}],
                           [first, second])[0]['entity_id'] == second['id']
    assert not context_binding('How long is its shelf life?', [*rows, {'text': 'Forget Product Alpha.'}], [first, second])


@pytest.mark.parametrize('patch', [
    {'id': 'bad-uuid'}, {'aliases': [12]}, {'aliases': ['']}, {'kb_id': '22222222-2222-4222-8222-222222222222'},
])
async def test_malformed_or_cross_scope_entity_rows_are_contract_errors(patch):
    from app.contracts import DomainError
    row = {'id': '11111111-1111-4111-8111-111111111112', 'kb_id': KB,
           'name': 'Product Alpha', 'aliases': []}
    ctx = context('How long is its shelf life?')
    ctx.task_context['recent_inputs'] = [{'text': 'Product Alpha'}]
    def handle(request):
        return httpx.Response(200, json=[{**row, **patch}])
    settings = Settings(_env_file=None, cuekb_mode='real', cuekb_base_url='https://cuekb.fixture')
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(DomainError) as caught:
            await CueKBAdapter(settings, client)._context_bindings(ctx, ctx.task_context['original_request'])
    assert caught.value.code == 'CUEKB_CONTRACT_ERROR'


@pytest.mark.parametrize('mutation', ['decision', 'missing', 'conflict'])
def test_inconsistent_evidence_decisions_and_missing_items_are_rejected(mutation):
    payload = provider_response()
    if mutation == 'decision':
        payload['decision'] = payload['model_view']['decision'] = 'insufficient'
    elif mutation == 'missing':
        payload['missing'] = payload['model_view']['missing'] = [payload['facets'][0]['id']]
    else:
        payload['conflicts'] = [{'facet_id': payload['facets'][0]['id'], 'evidence_ids': ['E1', 'E2'],
                                 'reason': 'incompatible_values'}]
    with pytest.raises(ValidationError):
        EvidenceQueryResponse.model_validate(payload)


@pytest.mark.parametrize('text,decision,code', [
    ('I cannot confirm the requested information from the available documentation.', 'insufficient', None),
    ('Which product do you mean?', 'clarify', None),
    ('It can be shipped.', 'insufficient', 'VOICE_ANSWER_UNGROUNDED'),
    ('I cannot confirm that. It can be shipped.', 'insufficient', 'VOICE_ANSWER_UNGROUNDED'),
    ('I cannot confirm a forty five day shelf life.', 'insufficient', 'VOICE_UNSUPPORTED_NUMBER'),
])
def test_no_evidence_disposition_does_not_authorize_positive_facts(text, decision, code):
    from app.agent_runtime.evidence import EvidenceGate
    assert EvidenceGate.check_disposition(text, decision) == code



async def test_long_indivisible_question_requests_clarification_without_http_or_truncation():
    from app.contracts import DomainError
    question = 'What is ' + 'a' * 493
    assert len(question) == 501
    calls = []
    def handle(request):
        calls.append(request)
        raise AssertionError('No incomplete question may be submitted')
    settings = Settings(_env_file=None, cuekb_mode='real', cuekb_base_url='https://cuekb.fixture')
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(DomainError) as caught:
            await CueKBAdapter(settings, client).invoke(CueKBSearchInput(query=question), context(question))
    assert caught.value.code == 'CUEKB_QUESTION_TOO_LONG'
    assert calls == []
    assert CueKBAdapter._question_facets(question[:-1])[0].text == question[:-1]



@pytest.mark.parametrize('provider,admitted,expected_reason', [
    ('answer', 'partial_answer', 'VOICE_EVIDENCE_BUDGET'),
    ('partial_answer', 'insufficient', 'VOICE_EVIDENCE_BUDGET'),
    ('partial_answer', 'partial_answer', 'CUEKB_PARTIAL_ANSWER'),
])
def test_local_delivery_omissions_are_not_reported_as_provider_retrieval_failures(provider, admitted, expected_reason):
    from app.sessions.coordinator import evidence_outcome
    status, reason = evidence_outcome({'decision': admitted, 'provider_decision': provider,
                                      'wire_admission': {'omitted_ids': ['E2']}})
    assert status == 'insufficient_evidence'
    assert reason == expected_reason


def test_speech_consumer_rejects_incomplete_selected_group():
    payload = provider_response()
    payload['evidence'][0]['completeness'] = 'incomplete'
    with pytest.raises(ValidationError, match='voice_evidence_requires_complete_groups'):
        EvidenceQueryResponse.model_validate(payload)
