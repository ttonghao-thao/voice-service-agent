"""Execute the archived service's real handshake methods without GPU imports.

Prompt rendering and Triton prefill are controlled fixtures. The real archived
method produces session.updated over a local WebSocket, then the API sends PCM.
"""

import ast
import asyncio
import base64
import hashlib
import json
import logging
import os
import uuid

import pytest
from app.config import ROOT, Settings
from app.voice.provider import NvidiaVoiceChatAdapter, session_update
from websockets.asyncio.server import serve


def snapshot_session_class():
    path = ROOT / "docs/voicechat/s2s/audio_server.py"
    manifest = json.loads((ROOT / "docs/voicechat/snapshot-manifest.json").read_text())
    assert hashlib.sha256(path.read_bytes()).hexdigest() == manifest["files"]["s2s/audio_server.py"]
    tree = ast.parse(path.read_text())
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "AudioSession")
    names = {"handle_session_update", "_normalize_format", "_format_to_structured",
             "_validate_input_rate", "get_instructions", "_emit_error"}
    methods = [node for node in cls.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
               and node.name in names]
    assert len(methods) == len(names)
    constants = {"MIN_INPUT_SAMPLE_RATE", "MAX_INPUT_SAMPLE_RATE", "MAX_INSTRUCTIONS_LENGTH"}
    definitions = [node for node in tree.body if isinstance(node, ast.Assign)
                   and any(isinstance(target, ast.Name) and target.id in constants for target in node.targets)]
    isolated = ast.Module(body=[
        ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
        *definitions,
        ast.ClassDef(name="SnapshotSession", bases=[], keywords=[], body=methods, decorator_list=[], type_params=[]),
    ], type_ignores=[])
    namespace = {"asyncio": asyncio, "json": json, "uuid": uuid, "os": os,
                 "logger": logging.getLogger("voicechat_snapshot_harness")}
    exec(compile(ast.fix_missing_locations(isolated), str(path), "exec"), namespace)
    return namespace["SnapshotSession"]


@pytest.mark.parametrize("mode", ["legacy", "dual_tools"])
async def test_api_accepts_actual_snapshot_echo_and_sends_first_pcm_during_prefill(mode):
    session_class = snapshot_session_class()
    prefill_started, prefill_finished, audio_seen = asyncio.Event(), asyncio.Event(), asyncio.Event()
    echoes = []
    pcm = base64.b64encode(bytes(3840)).decode()

    async def server(ws):
        class Socket:
            async def send_text(self, text):
                echoes.append(json.loads(text))
                await ws.send(text)

        async def prefill(prompt):
            prefill_started.set()
            await prefill_finished.wait()

        session = session_class()
        session.client_id = "snapshot-fixture"
        session.websocket = Socket()
        session.input_audio_format = session.output_audio_format = "pcm16"
        session.input_sample_rate = 24000
        session.sequence_started = asyncio.Event()
        session._prompt_task = None
        session.build_prompt = lambda instructions, tools: instructions  # No template/model capability claim.
        session.send_system_prompt_to_triton = prefill
        try:
            await ws.send(json.dumps({"type": "session.created"}))
            request = json.loads(await ws.recv())
            await session.handle_session_update(request["session"])
            async for raw in ws:
                event = json.loads(raw)
                assert event["type"] == "input_audio_buffer.append" and event["audio"] == pcm
                audio_seen.set()
                prefill_finished.set()
        finally:
            if session._prompt_task:
                session._prompt_task.cancel()
                await asyncio.gather(session._prompt_task, return_exceptions=True)

    async with serve(server, "127.0.0.1", 0) as endpoint:
        port = endpoint.sockets[0].getsockname()[1]
        adapter = NvidiaVoiceChatAdapter(Settings(_env_file=None, qa_execution_mode=mode,
            voicechat_ws_url=f"ws://127.0.0.1:{port}"))
        tools = None if mode == "legacy" else [
            {"name": name, "description": "Retrieve authorized company documentation.",
             "parameters": {"type": "object", "properties": {"user_request": {"type": "string"}},
                "required": ["user_request"], "additionalProperties": False}}
            for name in ("lookup_knowledge", "reason_over_knowledge")
        ]
        adapter.configuration = session_update("", tools)
        try:
            await adapter.connect("")
            echo = echoes[0]["session"]
            assert isinstance(echo["tools"], str)
            assert json.loads(echo["tools"]) == adapter.configuration["session"]["tools"]
            assert echo["instructions"] == adapter.configuration["session"]["instructions"]
            await asyncio.wait_for(prefill_started.wait(), 1)
            assert not prefill_finished.is_set()
            await adapter.send_audio(pcm)
            await asyncio.wait_for(audio_seen.wait(), 1)
        finally:
            await adapter.close()
