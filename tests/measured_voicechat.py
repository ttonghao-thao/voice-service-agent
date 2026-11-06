"""Explicit synthetic preflight for controlled websocket provider fixtures.

Fixed counts test the protocol, never production tokenization or GPU budgets.
"""
import hashlib
import json

from websockets.asyncio.server import serve as websocket_serve


class MeasuredSocket:
    def __init__(self, socket):
        self.socket = socket

    def __getattr__(self, name):
        return getattr(self.socket, name)

    async def send(self, raw):
        data = json.loads(raw)
        if data.get('type') == 'session.updated':
            data['session']['capabilities'] = {'tool_result_measurement': 'nemo-tool-response-v1'}
            raw = json.dumps(data)
        await self.socket.send(raw)

    async def recv(self):
        while True:
            raw = await self.socket.recv()
            data = json.loads(raw)
            if data.get('type') != 'tool_result.measure':
                return raw
            await self.socket.send(json.dumps({'type': 'tool_result.measured', 'event_id': data['event_id'],
                'text_sha256': hashlib.sha256(data['text'].encode()).hexdigest(), 'token_count': 32,
                'tokenizer_id': 'synthetic-fixture', 'serialization': 'nemo-tool-response-v1'}))

    def __aiter__(self):
        return self

    async def __anext__(self):
        from websockets.exceptions import ConnectionClosedOK
        try:
            return await self.recv()
        except ConnectionClosedOK:
            raise StopAsyncIteration from None


def serve(handler, *args, **kwargs):
    async def measured(socket):
        await handler(MeasuredSocket(socket))
    return websocket_serve(measured, *args, **kwargs)
