"""Real portal relay, controlled provider events; no claim of GPU verification."""
import base64
import copy
import json
import time

import httpx
import pytest
from app.main import create_app
from app.tools.adapters import CueKBAdapter
from app.voice.provider import MockVoiceAdapter, VoiceEvent
from fastapi.testclient import TestClient
from test_native_cuekb import configuration, evidence_v21, validate_v21

PCM = base64.b64encode(b'\x01\x00' * 1920).decode()


def test_barge_in_drops_old_answer_frames_and_suppressed_done_allows_idle_rotation(tmp_path):
    settings = configuration(tmp_path)
    app = create_app(settings)

    class Provider(MockVoiceAdapter):
        instance = None
        def __init__(self, settings):
            super().__init__(settings)
            Provider.instance = self

        async def connect(self, summary):
            for event in [
                VoiceEvent('input.state', {'state': 'speaking', 'item_id': 'i1'}),
                VoiceEvent('transcript.done', {'item_id': 'i1', 'text': 'Hello.'}),
                VoiceEvent('speech_text.delta', {'response_id': 'r1', 'input_item_id': 'i1', 'text': 'Hello'}),
                VoiceEvent('audio.delta', {'response_id': 'r1', 'input_item_id': 'i1', 'audio': PCM}),
            ]:
                await self.queue.put(event)

    with TestClient(app) as client:
        app.state.voice.provider_factory = Provider
        cid = client.post('/api/v1/conversations', json={}).json()['id']
        issued = client.post(f'/api/v1/conversations/{cid}/voice-sessions', json={}).json()
        with client.websocket_connect(issued['ws_url'], headers={'Origin': settings.public_origin}) as ws:
            while ws.receive_json()['type'] != 'portal.audio.delta':
                pass
            async def interrupt():
                for event in [
                    VoiceEvent('input.state', {'state': 'speaking', 'item_id': 'i2'}),
                    VoiceEvent('audio.delta', {'response_id': 'r1', 'input_item_id': 'i1', 'audio': PCM}),
                    VoiceEvent('speech_text.done', {'response_id': 'r1', 'input_item_id': 'i1', 'text': 'Hello again.'}),
                    VoiceEvent('audio.done', {'response_id': 'r1', 'input_item_id': 'i1'}),
                    VoiceEvent('transcript.done', {'item_id': 'i2', 'text': 'Thank you.'}),
                    VoiceEvent('input.state', {'state': 'quiet', 'item_id': 'i2'}),
                ]:
                    await Provider.instance.queue.put(event)
            client.portal.call(interrupt)
            events = [ws.receive_json() for _ in range(4)]
            assert events[0]['payload']['response_id'] == 'r1'
            assert [e['type'] for e in events] == [
                'portal.playback.clear', 'portal.input.state', 'portal.transcript.done', 'portal.input.state']
            # Advance only the configured rotation threshold, not global time.
            settings.voice_session_max_seconds = 0
            error = ws.receive_json()
            assert error['type'] == 'portal.error'
            assert error['payload']['code'] == 'VOICE_SESSION_ROTATION_REQUIRED'


@pytest.mark.parametrize('decision', ['answer', 'insufficient', 'partial_answer', 'conflict', 'clarify'])
def test_completed_input_settles_late_call_without_second_answer_and_preserves_decision(tmp_path, decision):
    settings = configuration(tmp_path)
    app = create_app(settings)
    queries, outputs = [], []

    def http_handler(request):
        body = json.loads(request.content)
        if request.url.path.endswith('/query'):
            queries.append(body)
            data = evidence_v21(body, decision='answer' if decision in ('answer', 'partial_answer', 'conflict') else 'insufficient')
            data['decision'] = data['model_view']['decision'] = decision
            if decision == 'partial_answer':
                data['facets'][1].update(support='absent', evidence_ids=[])
                data['evidence'][0]['supports'] = ['f1']
                data['missing'] = data['model_view']['missing'] = ['f2']
            elif decision == 'conflict':
                group = copy.deepcopy(data['evidence'][0])
                group.update(id='E2', rank=2)
                group['statement'] = {'text': 'Product AX supports 20 connections.', 'source_refs': ['S2']}
                data['evidence'].append(group)
                data['sources']['S2'] = {**data['sources']['S1'], 'chunk_id': '00000000-0000-4000-8000-000000000202'}
                data['model_view']['evidence'].append({**data['model_view']['evidence'][0], 'id': 'E2', 'text': group['statement']['text']})
                for facet in data['facets']:
                    facet.update(support='conflicting', evidence_ids=['E1', 'E2'])
                data['conflicts'] = [{'facet_id': 'f1', 'evidence_ids': ['E1', 'E2'], 'reason': 'incompatible_values'}]
            elif decision == 'clarify':
                data['clarification'] = {'question': 'Which Product AX?', 'options': [
                    {'entity_id': '00000000-0000-4000-8000-000000000301', 'label': 'Product AX first edition'},
                    {'entity_id': '00000000-0000-4000-8000-000000000302', 'label': 'Product AX second edition'}]}
            return httpx.Response(200, json=data)
        return httpx.Response(200, json=validate_v21(body, queries[0]['kb_ids']))

    class Provider(MockVoiceAdapter):
        instance = None
        def __init__(self, settings):
            super().__init__(settings)
            Provider.instance = self
        async def connect(self, summary):
            for event in [
                VoiceEvent('input.state', {'state': 'speaking', 'item_id': 'i1'}),
                VoiceEvent('transcript.done', {'item_id': 'i1', 'text': 'What is Product AX connection limit? What is Product AX price?'}),
                VoiceEvent('tool', {'response_id': 'ack1', 'input_item_id': 'i1', 'call_id': 'c1',
                                   'name': 'search_knowledge_base', 'arguments': '{"query":"Product AX"}'}),
            ]:
                await self.queue.put(event)
        async def submit_tool_result(self, call_id, text, **kwargs):
            outputs.append(json.loads(text))
            rid = 'answer1' if call_id == 'c1' else 'duplicate'
            context = {'response_id': rid, 'input_item_id': 'i1', 'tool_call_ids': [call_id]}
            spoken = 'Product AX supports 10 connections.' if decision == 'answer' else 'I cannot confirm Product AX details from the available documentation.'
            for event in [VoiceEvent('speech_text.done', {**context, 'text': spoken}),
                          VoiceEvent('audio.delta', {**context, 'audio': PCM}),
                          VoiceEvent('audio.done', context)]:
                await self.queue.put(event)
            if call_id != 'c1':
                # Observable barrier after the duplicate was drained.
                await self.queue.put(VoiceEvent('transcript.done', {'item_id': 'i2', 'text': 'Thanks.'}))

    with TestClient(app) as client:
        app.state.voice.provider_factory = Provider
        http = httpx.AsyncClient(transport=httpx.MockTransport(http_handler))
        app.state.registry.adapters['cuekb_http'] = CueKBAdapter(settings, http)
        cid = client.post('/api/v1/conversations', json={}).json()['id']
        issued = client.post(f'/api/v1/conversations/{cid}/voice-sessions', json={}).json()
        with client.websocket_connect(issued['ws_url'], headers={'Origin': settings.public_origin}) as ws:
            while True:
                event = ws.receive_json()
                assert event['type'] != 'portal.error', event
                if event['type'] == 'portal.audio.done' and event['payload']['response_id'] == 'answer1':
                    break
            async def late_call():
                await Provider.instance.queue.put(VoiceEvent('tool', {
                    'response_id': 'ack2', 'input_item_id': 'i1', 'call_id': 'c2',
                    'name': 'search_knowledge_base', 'arguments': '{"query":"Product AX price"}'}))
            client.portal.call(late_call)
            while True:
                event = ws.receive_json()
                assert event['type'] != 'portal.error', event
                assert event['payload'].get('response_id') != 'duplicate'
                if event['type'] == 'portal.transcript.done':
                    break
            assert len(queries) == 1
            assert outputs[1]['status'] == 'not_executed'
            assert 'Wait for new user input' in outputs[1]['instructions']
            from app.storage.models import Turn
            from sqlalchemy import select
            async def stored():
                async with app.state.store.sessions() as db:
                    turn = (await db.execute(select(Turn).where(Turn.conversation_id == cid))).scalar_one()
                    return turn.answer
            for _ in range(50):
                answer = client.portal.call(stored)
                if answer:
                    break
                time.sleep(.01)
            assert answer['status'] == ('answered' if decision == 'answer' else 'needs_clarification' if decision == 'clarify' else 'insufficient_evidence')
            if decision == 'partial_answer':
                assert outputs[0]['missing'] == ['What is Product AX price?']
        client.portal.call(http.aclose)
