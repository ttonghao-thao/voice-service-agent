"""Serve the upstream Live Gateway with a deterministic test engine and ASR."""

import argparse
import asyncio
import importlib.util
import json
import logging
import sys
import types
from functools import partial
from pathlib import Path

import numpy as np
from websockets.asyncio.server import serve


class FixtureEngine:
    def __init__(self, *_, options):
        self.options = options
        self.q = asyncio.Queue()
        self.voiced_samples = 0
        self.delegated = self.replied = self.followed_up = False

    async def start(self, *, lang, tools, **_):
        assert lang == self.options.language and len(tools) == 1

    async def send_audio(self, pcm):
        if np.abs(pcm).max(initial=0) < 0.03:
            if self.replied and self.voiced_samples > 6000 and not self.followed_up:
                self.followed_up = True
                self.emit_reply("follow_up", "Okay, I am listening.", 770, 0.8)
            return
        self.voiced_samples += len(pcm)
        if self.voiced_samples >= 8500 and not self.delegated:
            self.delegated = True
            self.q.put_nowait({
                "t": "tool_call", "name": "delegate", "call_id": "fixture_call",
                "arguments": {"task": self.options.task},
            })

    def emit_reply(self, rid, text, hz, seconds):
        samples = np.arange(round(seconds * 16000))
        pcm = (0.2 * np.sin(2 * np.pi * hz * samples / 16000)).astype(np.float32)
        self.q.put_nowait({"t": "text", "delta": text, "rid": rid})
        self.q.put_nowait({"t": "audio", "pcm": pcm, "sr": 16000, "rid": rid})
        self.q.put_nowait({"t": "done", "rid": rid})

    async def tool_result(self, call_id, text, *, speak):
        assert call_id == "fixture_call" and self.options.expected_commentary in text and speak
        logging.info("Fixture accepted commentary")
        self.replied = True
        self.voiced_samples = 0
        self.emit_reply("answer", "The verification code is six four two nine.", 660, 3.5)
        return True

    async def close(self):
        logging.info("Fixture engine closed")


async def serve_fixture(options):
    # Load the original Gateway. Only its inference and ASR dependencies are replaced.
    package = types.ModuleType("probe_sophon")
    package.__path__ = []
    adapters = types.ModuleType("probe_sophon.adapters")
    adapters.VllmOmniAdapter = partial(FixtureEngine, options=options)
    realtime = types.ModuleType("probe_sophon.realtime")
    realtime.RealtimeSession = None
    sys.modules.update({module.__name__: module for module in (package, adapters, realtime)})
    spec = importlib.util.spec_from_file_location("probe_sophon.gateway", options.gateway)
    gateway = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gateway)

    def asr(_):
        return "What is the verification code?"

    async def handler(ws):
        assert ws.request.path == "/v1/live/sessions"
        assert "authorization" not in ws.request.headers
        session = gateway.LiveSession(ws, types.SimpleNamespace(engine="", bridge=""),
                                      None if options.no_input_transcript else asr, asyncio.Lock())
        try:
            async for raw in ws:
                await session.on_event(json.loads(raw))
        finally:
            await session.close("fixture_disconnected")

    async with serve(handler, "127.0.0.1", 18782):
        await asyncio.Future()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("gateway", type=Path, help="Source path of the pinned upstream Sophon Gateway")
    parser.add_argument("--language", choices=("en", "zh"), default="en")
    parser.add_argument("--task", default="Look up the verification code.")
    parser.add_argument("--expected-commentary", default="six four two nine")
    parser.add_argument("--no-input-transcript", action="store_true")
    logging.basicConfig(level=logging.INFO)
    asyncio.run(serve_fixture(parser.parse_args()))
