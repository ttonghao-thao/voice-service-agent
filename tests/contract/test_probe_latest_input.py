import asyncio
import json
from argparse import Namespace

import pytest

from scripts import validate_native_cuekb as probe


@pytest.mark.asyncio
async def test_probe_waits_for_second_input_in_one_wav(monkeypatch, tmp_path):
    class Response:
        def __init__(self, data):
            self.data = data

        def raise_for_status(self):
            pass

        def json(self):
            return self.data

    class HTTP:
        headers = {}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

        async def post(self, path, **_):
            return Response({'id': 'cid', 'access_token': 'token'} if path.endswith('conversations') else
                            {'epoch': 1, 'voice_session_id': 'sid', 'ws_url': '/ws', 'wait_interaction': False})

        async def get(self, path):
            return Response({'items': [], 'records': []} if path.endswith('messages') else {})

        async def delete(self, *_):
            return Response({})

    class WS:
        def __init__(self):
            self.queue = asyncio.Queue()
            self.sent = 0
            self.event('portal.session.ready')

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

        def event(self, kind, **payload):
            self.queue.put_nowait(json.dumps({'type': kind, 'payload': payload}))

        def answer(self, rid, item):
            self.event('portal.audio.delta', response_id=rid, audio='AAA=')
            self.event('portal.speech_text.done', response_id=rid, input_item_id=item, phase='answer', text='Answer.')
            self.event('portal.audio.done', response_id=rid, phase='answer')

        async def recv(self):
            return await self.queue.get()

        async def send(self, raw):
            if json.loads(raw)['type'] != 'portal.audio.append':
                return
            self.sent += 1
            if self.sent == 1:
                self.event('portal.transcript.done', item_id='first')
                self.answer('r1', 'first')
            elif self.sent == 2:
                self.event('portal.input.state', state='speaking')
            elif self.sent == 3:
                self.event('portal.transcript.done', item_id='second')
                self.answer('late-r1', 'first')
            elif self.sent == 4:
                self.answer('r2', 'second')

    ws = WS()
    monkeypatch.setattr(probe.httpx, 'AsyncClient', lambda **_: HTTP())
    monkeypatch.setattr(probe, 'connect', lambda *_, **__: ws)
    monkeypatch.setattr(probe, 'validate_voice_policy', lambda *_, **__: None)
    monkeypatch.setattr(probe, 'load_cases', lambda *_: [{
        'case_id': 'two-parts', 'expected_tool': None, 'input_sha256': 'hash', 'frames': [bytes(3840)] * 3}])
    report = await probe.run(Namespace(cases='unused', base_url='http://localhost', origin=None,
        case_timeout=2, audio_dir=None, output=tmp_path / 'report.json'))
    row = report['cases'][0]
    assert row['answer_response_id'] == 'r2', report
    assert row['answer_complete'] == 'passed'
    assert row['input_item_ids'] == ['first', 'second']
    assert ws.sent >= 4
