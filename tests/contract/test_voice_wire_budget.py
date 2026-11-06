"""Real packet admission with controlled counts; no deployed tokenizer claim."""
import asyncio
import hashlib
import json
from types import SimpleNamespace

import pytest
from app.agent_runtime.direct import EvidenceReady
from app.agent_runtime.native_cuekb import voice_result
from app.agent_runtime.voice_budget import fit_voice_result, measure_packet
from app.config import Settings
from app.contracts import DomainError
from app.voice.provider import NvidiaVoiceChatAdapter


def ready_fixture():
    items = [
        {'id': 'E1', 'for': 'Shelf life?', 'text': 'Shelf life is 45 days sealed.', 'conditions': ['After opening, refrigerate and consume within 3 days.']},
        {'id': 'E2', 'for': 'Shipping?', 'text': 'Shipping is available.', 'conditions': ['Only within the service area.']},
    ]
    return EvidenceReady({
        'schema_version': 'cuekb-evidence-v2.1', 'decision': 'answer',
        'model_view': {'decision': 'answer', 'evidence': items, 'missing': [], 'search': 'finished'},
        'instructions': 'Do not call another tool. Answer now.',
        'items': [{'citation_id': 'C1', 'text': items[0]['text'], 'context': items[0]['conditions'][0]},
                  {'citation_id': 'C2', 'text': items[1]['text'], 'context': items[1]['conditions'][0]}],
        'facets': [{'id': 'f1', 'text': 'Shelf life?', 'support': 'direct'}, {'id': 'f2', 'text': 'Shipping?', 'support': 'direct'}],
        'group_supports': {'E1': ['f1'], 'E2': ['f2']}, 'conflicts': [],
    }, ['C1', 'C2'], 0, 'answer')


async def test_exact_final_counts_admit_whole_groups_and_update_observer_evidence():
    ready = ready_fixture()
    original = json.dumps(ready.envelope['model_view'])
    seen = []
    async def measure(text):
        packet = json.loads(text)
        seen.append(packet)
        # Synthetic token response deliberately includes wrapper/control cost.
        return {'token_count': 100 + 100 * len(packet['evidence']), 'tokenizer_id': 'fixture-exact'}
    result = voice_result(ready)
    result['call_results'] = [{'index': '0', 'status': 'executed'}, {'index': '1', 'status': 'not_executed'}]
    fitted = await fit_voice_result(ready, result, measure, max_tokens=200, max_bytes=6000)
    packet = json.loads(fitted.text)
    assert fitted.token_count == 200 and fitted.retained_ids == ('E1',) and fitted.omitted_ids == ('E2',)
    assert packet['evidence'][0] == result['evidence'][0]
    assert packet['decision'] == 'partial_answer' and packet['missing'] == ['Shipping?']
    assert all('call_results' in candidate for candidate in seen)
    assert ready.citations == ['C1'] and len(ready.envelope['items']) == 1
    assert ready.envelope['decision'] == 'partial_answer'
    assert json.dumps(ready.envelope['provider_model_view']) == original


async def test_complete_group_that_cannot_fit_yields_measured_honest_refusal():
    ready = ready_fixture()
    async def measure(text):
        return {'token_count': 400 if json.loads(text)['evidence'] else 100, 'tokenizer_id': 'fixture-exact'}
    fitted = await fit_voice_result(ready, voice_result(ready), measure, max_tokens=200, max_bytes=6000)
    result = json.loads(fitted.text)
    assert result['decision'] == 'insufficient' and result['evidence'] == []
    assert 'cannot confirm' in result['instructions']
    assert fitted.retained_ids == () and fitted.omitted_ids == ('E1', 'E2')
    assert ready.citations == [] and ready.envelope['items'] == []
    assert ready.envelope['provider_decision'] == 'answer'


async def test_conflicting_groups_are_never_split_into_apparent_answer():
    ready = ready_fixture()
    ready.envelope['conflicts'] = [{'facet_id': 'f1', 'evidence_ids': ['E1', 'E2']}]
    ready.envelope['decision'] = ready.envelope['model_view']['decision'] = 'conflict'
    async def measure(text):
        return {'token_count': 100 + 100 * len(json.loads(text)['evidence']), 'tokenizer_id': 'fixture-exact'}
    fitted = await fit_voice_result(ready, voice_result(ready), measure, max_tokens=200, max_bytes=6000)
    assert not json.loads(fitted.text)['evidence']
    assert fitted.omitted_ids == ('E1', 'E2')


async def test_tokenizer_change_or_terminal_budget_failure_never_mutates_evidence():
    ready = ready_fixture()
    calls = 0
    async def measure(text):
        nonlocal calls
        calls += 1
        return {'token_count': 400, 'tokenizer_id': f'changed-{calls}'}
    with pytest.raises(DomainError) as caught:
        await fit_voice_result(ready, voice_result(ready), measure, max_tokens=200, max_bytes=6000)
    assert caught.value.code == 'VOICE_PROTOCOL_ERROR'
    assert ready.citations == ['C1', 'C2'] and 'wire_admission' not in ready.envelope
    async def fixed_measure(text):
        return {'token_count': 400, 'tokenizer_id': 'fixture-exact'}
    with pytest.raises(DomainError) as caught:
        await fit_voice_result(ready, voice_result(ready), fixed_measure, max_tokens=200, max_bytes=6000)
    assert caught.value.code == 'VOICE_EVIDENCE_BUDGET'
    assert ready.citations == ['C1', 'C2']


async def test_non_evidence_settlement_is_counted_in_final_form():
    seen = []
    async def measure(text):
        seen.append(json.loads(text))
        return {'token_count': 30, 'tokenizer_id': 'fixture-exact'}
    packet = await measure_packet({'status': 'not_executed', 'instructions': 'Wait for new input.'}, measure,
                                  max_tokens=40, max_bytes=6000)
    assert packet.token_count == 30 and len(seen) == 1


@pytest.mark.parametrize('corrupt', [False, True])
async def test_provider_preflight_is_correlated_and_does_not_consume_audio_events(corrupt):
    adapter = NvidiaVoiceChatAdapter(Settings(_env_file=None))
    adapter.tool_result_measurement = 'nemo-tool-response-v1'
    queue = asyncio.Queue()
    class WebSocket:
        def __aiter__(self):
            return self
        async def __anext__(self):
            return await queue.get()
        async def send(self, raw):
            event = json.loads(raw)
            receipt = {'type': 'tool_result.measured', 'event_id': event['event_id'], 'token_count': 73,
                       'text_sha256': 'wrong' if corrupt else hashlib.sha256(event['text'].encode()).hexdigest(),
                       'tokenizer_id': 'fixture-exact', 'serialization': 'nemo-tool-response-v1'}
            await queue.put(json.dumps(receipt))
            await queue.put(json.dumps({'type': 'input_audio_buffer.speech_started', 'item_id': 'new-input'}))
    adapter.ws = WebSocket()
    reader = asyncio.create_task(anext(adapter.events()))
    if corrupt:
        with pytest.raises(DomainError) as caught:
            await adapter.measure_tool_result('{"decision":"answer"}')
        assert caught.value.code == 'VOICE_PROTOCOL_ERROR'
    else:
        receipt = await adapter.measure_tool_result('{"decision":"answer"}')
        assert receipt['token_count'] == 73
    assert (await reader).payload['item_id'] == 'new-input'


async def test_provider_requires_explicit_exact_measurement_capability():
    adapter = NvidiaVoiceChatAdapter(Settings(_env_file=None))
    adapter.ws = SimpleNamespace()
    with pytest.raises(DomainError) as caught:
        await adapter.measure_tool_result('{}')
    assert caught.value.code == 'VOICE_TOKENIZER_UNAVAILABLE'


async def test_oversized_clarification_is_a_local_budget_limit_even_with_no_groups():
    from app.sessions.coordinator import evidence_outcome
    ready = ready_fixture()
    ready.envelope.update(decision='clarify', items=[])
    ready.envelope['model_view'].update(decision='clarify', evidence=[])
    ready.envelope['clarification'] = {'question': 'Which edition?', 'options': ['First', 'Second']}
    ready.citations = []
    async def measure(text):
        return {'token_count': 300 if 'clarification' in json.loads(text) else 100, 'tokenizer_id': 'fixture'}
    await fit_voice_result(ready, voice_result(ready), measure, max_tokens=200, max_bytes=6000)
    assert evidence_outcome(ready.envelope) == ('insufficient_evidence', 'VOICE_EVIDENCE_BUDGET')
