"""Real API/relay/DB with controlled HTTP evidence and token-count receipts.

This verifies final admission provenance, not deployed tokenizer/GPU behavior.
"""

import asyncio
import base64
import copy
import hashlib
import json

import httpx
import pytest
from app.main import create_app
from app.storage.models import Record, Turn
from app.tools.adapters import CueKBAdapter
from app.tools.evidence_contract import EvidenceQueryResponse
from app.voice.provider import MockVoiceAdapter, VoiceEvent
from fastapi.testclient import TestClient
from sqlalchemy import select
from test_native_cuekb import configuration, evidence_v21, validate_v21

PCM = base64.b64encode(b'\x01\x00' * 1920).decode()
QUESTION = 'What is Product AX connection limit? What is Product AX price?'


@pytest.mark.parametrize('uses_omitted_fact', [False, True])
def test_wire_budget_admission_controls_spoken_check_and_saved_citations(tmp_path, uses_omitted_fact):
    settings = configuration(tmp_path, voice_tool_result_max_tokens=200)
    app = create_app(settings)
    queries, validations, measured_packets, injected_packets = [], [], [], []

    def handle(request):
        body = json.loads(request.content)
        if request.url.path == '/v2/evidence/query':
            queries.append(body)
            assert body['question'] == QUESTION
            assert len(body['requested_facets']) == 2
            data = evidence_v21(body, statement='Product AX supports 10 connections.')
            data['facets'][0]['evidence_ids'] = ['E1']
            data['facets'][1]['evidence_ids'] = ['E2']
            data['evidence'][0]['supports'] = ['f1']
            data['model_view']['evidence'][0]['for'] = body['requested_facets'][0]['text']
            second = copy.deepcopy(data['evidence'][0])
            second.update(id='E2', rank=2, supports=['f2'])
            second['statement'] = {'text': 'Product AX price is 99.', 'source_refs': ['S2']}
            data['evidence'].append(second)
            data['model_view']['evidence'].append({
                'id': 'E2', 'for': body['requested_facets'][1]['text'],
                'text': second['statement']['text'], 'conditions': [],
            })
            data['sources']['S2'] = {
                **data['sources']['S1'],
                'chunk_id': '00000000-0000-4000-8000-000000000202',
                'node_id': '00000000-0000-4000-8000-000000000204',
            }
            data['retrieval'].update(candidate_groups=2, selected_groups=2)
            # Exercise the strict consumer contract before the API receives it.
            EvidenceQueryResponse.model_validate(data)
            return httpx.Response(200, json=data)
        if request.url.path == '/v2/evidence/validate':
            validations.append(body)
            return httpx.Response(200, json=validate_v21(body, queries[0]['kb_ids']))
        raise AssertionError(request.url.path)

    class Provider(MockVoiceAdapter):
        async def connect(self, summary):
            for event in [
                VoiceEvent('input.state', {'state': 'speaking', 'item_id': 'input'}),
                VoiceEvent('transcript.done', {'item_id': 'input', 'text': QUESTION}),
                VoiceEvent('input.state', {'state': 'quiet', 'item_id': 'input'}),
                VoiceEvent('tool', {'response_id': 'ack', 'input_item_id': 'input', 'call_id': 'call',
                                   'name': 'search_knowledge_base', 'arguments': '{"query":"Product AX"}'}),
            ]:
                await self.queue.put(event)

        async def measure_tool_result(self, text):
            packet = json.loads(text)
            measured_packets.append(packet)
            # Controlled final count includes synthetic fixed wrapper overhead;
            # two groups exceed the limit, one whole group fits exactly.
            return {'token_count': 100 + 100 * len(packet.get('evidence', [])),
                    'text_sha256': hashlib.sha256(text.encode()).hexdigest(),
                    'tokenizer_id': 'controlled-final-packet-counter',
                    'serialization': 'nemo-tool-response-v1'}

        async def submit_tool_result(self, call_id, text, **kwargs):
            injected_packets.append(json.loads(text))
            spoken = ('Product AX supports 10 connections and costs 99.' if uses_omitted_fact else
                      'Product AX supports 10 connections. I cannot confirm its price.')
            context = {'response_id': 'answer', 'input_item_id': 'input', 'tool_call_ids': ['call']}
            for event in [
                VoiceEvent('speech_text.done', {**context, 'text': spoken}),
                VoiceEvent('audio.delta', {**context, 'audio': PCM}),
                VoiceEvent('audio.done', context),
            ]:
                await self.queue.put(event)

    with TestClient(app) as client:
        app.state.voice.provider_factory = Provider
        http = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        app.state.registry.adapters['cuekb_http'] = CueKBAdapter(settings, http)
        cid = client.post('/api/v1/conversations', json={}).json()['id']
        issued = client.post(f'/api/v1/conversations/{cid}/voice-sessions', json={}).json()
        with client.websocket_connect(issued['ws_url'], headers={'Origin': settings.public_origin}) as ws:
            while True:
                event = ws.receive_json()
                assert event['type'] != 'portal.error', event
                if event['type'] == 'portal.audio.done' and event['payload']['response_id'] == 'answer':
                    break

            async def committed_answer():
                for _ in range(200):
                    async with app.state.store.sessions() as db:
                        turn = (await db.execute(select(Turn).where(Turn.conversation_id == cid))).scalar_one()
                        if turn.answer:
                            records = (await db.execute(select(Record).where(
                                Record.conversation_id == cid, Record.kind == 'native_wire_admission'))).scalars().all()
                            return turn.answer, [record.payload for record in records], turn.evidence
                    await asyncio.sleep(.01)
                raise AssertionError('Spoken answer was not committed')

            answer, admissions, evidence = client.portal.call(committed_answer)
            assert [item['citation_id'] for item in evidence['envelope']['items']] == ['C1']
            assert evidence['envelope']['decision'] == 'partial_answer'
            assert evidence['envelope']['provider_decision'] == 'answer' 
            assert len(queries) == len(validations) == len(injected_packets) == 1
            packet = injected_packets[0]
            assert packet['decision'] == 'partial_answer'
            assert packet['missing'] == ['What is Product AX price?']
            assert [group['id'] for group in packet['evidence']] == ['E1']
            assert packet['evidence'][0]['text'] == 'Product AX supports 10 connections.'
            assert all(group['text'] != 'Product AX price is 99.' for group in packet['evidence'])
            assert any(len(candidate['evidence']) == 2 for candidate in measured_packets)
            assert [citation['citation_id'] for citation in answer['citations']] == ['C1']
            assert all('99' not in citation['content'] for citation in answer['citations'])
            if uses_omitted_fact:
                assert answer['status'] == 'failed'
                assert answer['reason_code'] == 'VOICE_UNSUPPORTED_NUMBER'
            else:
                assert answer['status'] == 'insufficient_evidence'
                assert answer['reason_code'] == 'VOICE_EVIDENCE_BUDGET'
            assert len(admissions) == 1
            assert admissions[0]['provider_decision'] == 'answer'
            assert admissions[0]['decision'] == 'partial_answer'
            assert admissions[0]['retained_ids'] == ['E1']
            assert admissions[0]['omitted_ids'] == ['E2']
            assert admissions[0]['token_count'] == admissions[0]['token_budget'] == 200
        client.portal.call(http.aclose)
