"""Controlled adapter tests; never connect to VoiceChat or infer acoustic accuracy."""

import asyncio
import base64
import importlib.util
import json
import wave
from pathlib import Path
from types import SimpleNamespace

import pytest
from app.voice.provider import BRIDGE_NAME, VoiceEvent

spec = importlib.util.spec_from_file_location("voice_probe", Path(__file__).parents[2] / "scripts/probe_voicechat.py")
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


def arguments(tmp_path, **overrides):
    path = tmp_path / "authorized.wav"
    with wave.open(str(path), "wb") as wav:
        wav.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
        wav.writeframes(b"\x01\x00" * 3840)
    values = dict(wav=str(path), api_version="controlled", image_digest="fixture", diagnostic=True,
                  barge_in_wav=None, tool_delay_seconds=0.02, observation_seconds=0.4,
                  tool_wait_timeout=0.4, post_result_seconds=0.2, total_timeout=2,
                  output=str(tmp_path / "report.json"), output_wav=None)
    return SimpleNamespace(**(values | overrides))


class ControlledProvider:
    def __init__(self, settings):
        self.queue = asyncio.Queue()
        self.frames = []
        self.results = []
        self.closed = False
        self.writing = False
        self.closed_after_first_frame = False

    async def connect(self, summary):
        pass

    async def send_audio(self, audio):
        assert not self.writing, "Audio and tool results must share one writer"
        self.writing = True
        await asyncio.sleep(0)
        self.frames.append(base64.b64decode(audio))
        self.writing = False
        if len(self.frames) == 1:
            await self.queue.put(VoiceEvent("audio.delta", {"response_id": "unbridged", "audio": "AAA="}))
            if self.closed_after_first_frame:
                await self.queue.put(None)
        if len(self.frames) == 4:
            await self.queue.put(VoiceEvent("tool", {
                "call_id": "c1", "response_id": "ack", "name": BRIDGE_NAME,
                "arguments": json.dumps({"user_request": "HELLO THAO MOONCAKE"}),
            }))

    async def submit_tool_result(self, call_id, text):
        assert not self.writing
        self.writing = True
        await asyncio.sleep(0)
        self.results.append((call_id, json.loads(text)))
        self.writing = False

    async def events(self):
        while True:
            event = await self.queue.get()
            if event is None:
                return
            yield event

    async def close(self):
        self.closed = True


def configure(monkeypatch, provider):
    monkeypatch.setattr(probe, "Settings", lambda: SimpleNamespace(voicechat_ws_url="ws://fixture.invalid"))
    monkeypatch.setattr(probe, "NvidiaVoiceChatAdapter", lambda settings: provider)


async def test_full_input_and_silence_continue_after_unbridged_output_and_deliver_late_tool(tmp_path, monkeypatch):
    provider = ControlledProvider(None)
    configure(monkeypatch, provider)
    args = arguments(tmp_path)
    report = await probe.probe(args)
    assert report["input_complete"] and report["tool_seen"] and provider.closed
    assert report["exit_reason"] == "observation_complete"
    assert report["audio_evidence"]["primary_input_samples"] == 3840
    assert report["audio_evidence"]["silence_input_samples"] >= 3 * 1920
    assert report["audio_evidence"]["pre_tool_output_samples"] == 1
    assert provider.frames[:2] == [b"\x01\x00" * 1920] * 2
    assert all(frame == bytes(3840) for frame in provider.frames[2:])
    assert len(provider.results) == 1 and provider.results[0][1]["is_mock"]
    assert report["status"] == "partial" and report["synthetic_tool_result"]
    assert report["unsent_input_samples"]["primary"] == 0
    assert "HELLO THAO MOONCAKE" not in await asyncio.to_thread(Path(args.output).read_text)


async def test_peer_close_retains_unsent_input_and_cleans_up(tmp_path, monkeypatch):
    provider = ControlledProvider(None)
    provider.closed_after_first_frame = True
    configure(monkeypatch, provider)
    report = await probe.probe(arguments(tmp_path))
    assert report["exit_reason"] == "peer_closed" and provider.closed
    assert not report["input_complete"] and report["unsent_input_samples"]["primary"] == 1920
    assert report["checks"]["input_fully_sent"] == "unverified"


async def test_pending_tool_result_is_cancelled_on_observation_end(tmp_path, monkeypatch):
    provider = ControlledProvider(None)
    configure(monkeypatch, provider)
    report = await probe.probe(arguments(tmp_path, tool_delay_seconds=0.8))
    assert report["tool_seen"] and provider.results == []
    await asyncio.sleep(0.45)
    assert provider.results == []
    assert not any("delayed_result" in task.get_coro().__qualname__ for task in asyncio.all_tasks())


async def test_connection_budget_includes_handshake_and_closes_on_timeout(tmp_path, monkeypatch):
    provider = ControlledProvider(None)

    async def stalled_connect(summary):
        await asyncio.sleep(10)

    provider.connect = stalled_connect
    configure(monkeypatch, provider)
    report = await probe.probe(arguments(tmp_path, total_timeout=0.2, observation_seconds=0))
    assert report["exit_reason"] == "total_timeout" and provider.closed
    assert not report["input_complete"] and not report["real_service_connected"]


@pytest.mark.parametrize("overrides", [
    {"total_timeout": 61}, {"total_timeout": float("nan")}, {"observation_seconds": 3},
])
async def test_invalid_budgets_are_rejected_before_connect(tmp_path, monkeypatch, overrides):
    monkeypatch.setattr(probe, "NvidiaVoiceChatAdapter", lambda settings: pytest.fail("Must not connect"))
    report = await probe.probe(arguments(tmp_path, **overrides))
    assert report["status"] == "failed" and not report["real_service_connected"]


def test_invalid_wav_format_is_rejected_and_short_tail_is_padded(tmp_path):
    path = tmp_path / "sample.wav"
    with wave.open(str(path), "wb") as wav:
        wav.setparams((2, 2, 24000, 0, "NONE", "not compressed"))
        wav.writeframes(bytes(100))
    with pytest.raises(ValueError):
        probe.read_wav(str(path))
    with wave.open(str(path), "wb") as wav:
        wav.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
        wav.writeframes(bytes(200))
    chunks, _ = probe.read_wav(str(path))
    assert len(chunks) == 1 and len(chunks[0]) == 3840
