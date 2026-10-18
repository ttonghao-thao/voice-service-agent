"""Protocol fixtures exercise probe liveness; they make no model quality claim."""

import asyncio
import base64
import json
import wave
from pathlib import Path
from types import SimpleNamespace

import pytest
from app.config import Settings
from app.voice.provider import NvidiaVoiceChatAdapter, session_update
from websockets.asyncio.server import serve

from scripts import probe_voicechat as probe


@pytest.fixture
def args(tmp_path):
    source = tmp_path / "question.wav"
    with wave.open(str(source), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(24000)
        wav.writeframes(b"\x01\x00" * 1920)
    return SimpleNamespace(wav=str(source), barge_in_wav=None, api_version="fixture",
        image_digest="fixture", session_update=None, tool_delay_seconds=0.04,
        tool_wait_timeout=0.15, post_result_seconds=0.04, output=str(tmp_path / "report.json"),
        output_wav=None)


async def run_fixture(monkeypatch, args, behavior="tool"):
    seen = {"frames": [], "results": [], "during_delay": 0, "after_result": 0}
    configuration = session_update(tools=[{
        "name": "custom_catalog_tool", "description": "A configured tool, not the legacy bridge.",
        "parameters": {"type": "object", "properties": {"request": {"type": "string"}},
            "required": ["request"], "additionalProperties": False},
    }])
    snapshot = args.output + ".session.json"
    await asyncio.to_thread(Path(snapshot).write_text, json.dumps(configuration))
    args.session_update = snapshot

    async def handler(ws):
        await ws.send(json.dumps({"type": "session.created"}))
        update = json.loads(await ws.recv())
        assert update["session"] == configuration["session"]
        await ws.send(json.dumps({"type": "session.updated", "session": update["session"]}))
        announced = False
        async for raw in ws:
            event = json.loads(raw)
            if event["type"] == "input_audio_buffer.append":
                pcm = base64.b64decode(event["audio"])
                seen["frames"].append(pcm)
                if announced:
                    seen["after_result" if seen["results"] else "during_delay"] += 1
                if len(seen["frames"]) == 4:
                    if behavior == "ended":
                        await ws.send(json.dumps({"type": "session.end"}))
                        return
                    if behavior == "disconnect":
                        await ws.close()
                        return
                    if behavior == "no_tool":
                        continue
                    announced = True
                    tool = {"type": "response.function_call_arguments.done", "call_id": "native-call",
                        "response_id": "response-1", "name": "custom_catalog_tool",
                        "arguments": json.dumps({"request": "private content"})}
                    if behavior == "invalid_arguments":
                        tool["arguments"] = "{}"
                    await ws.send(json.dumps(tool))
                    await ws.send(json.dumps(tool))  # A repeated notification must not execute twice.
                    if behavior == "conflicting_call":
                        tool["arguments"] = json.dumps({"request": "different"})
                        await ws.send(json.dumps(tool))
            if event["type"] == "conversation.item.create":
                seen["results"].append(event["item"])
                await ws.send(json.dumps({"type": "conversation.item.input_audio_transcription.completed",
                    "item_id": "input-1", "transcript": "private transcript"}))
                for _ in range(5):
                    await ws.send(json.dumps({"type": "response.output_audio.delta", "response_id": "response-1",
                        "delta": base64.b64encode(b"\x01\x00" * 1920).decode()}))

    monkeypatch.setattr(probe, "FRAME_SECONDS", 0.005)
    async with serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        monkeypatch.setattr(probe, "Settings", lambda: Settings(_env_file=None,
            qa_execution_mode="dual_tools", voicechat_ws_url=f"ws://127.0.0.1:{port}"))
        report = await probe.probe(args)
    return report, seen


async def test_input_continues_until_call_through_delay_and_after_result(monkeypatch, args):
    report, seen = await run_fixture(monkeypatch, args)
    assert report["status"] == "partial" and report["phase"] == "finished"
    assert seen["frames"][0] == b"\x01\x00" * 1920
    assert all(frame == bytes(3840) for frame in seen["frames"][1:])
    assert seen["during_delay"] > 0 and seen["after_result"] > 0
    assert len(seen["results"]) == 1 and seen["results"][0]["call_id"] == "native-call"
    assert report["registered_tool_names"] == ["custom_catalog_tool"]
    assert report["audio_evidence"]["primary_input_samples"] == 1920
    assert report["audio_evidence"]["silence_input_samples"] >= 1920 * 3
    assert "native-call" in json.dumps(report) and "private" not in json.dumps(report)


@pytest.mark.parametrize("behavior,reason", [
    ("no_tool", "TIMEOUT_WAITING_NATIVE_TOOL"), ("ended", "PROVIDER_SESSION_ENDED"),
    ("disconnect", "PROVIDER_EVENT_STREAM_ENDED"), ("invalid_arguments", "ValidationError"),
    ("conflicting_call", "CONFLICTING_CALL_ID"),
])
async def test_failure_is_specific_and_evidence_is_saved(monkeypatch, args, behavior, reason):
    report, seen = await run_fixture(monkeypatch, args, behavior)
    assert report["reason"] == reason
    assert report["phase"] != "finished"
    assert len(seen["frames"]) >= 4
    assert json.loads(await asyncio.to_thread(Path(args.output).read_text))["reason"] == reason


@pytest.mark.parametrize("method", ["send_audio", "submit_tool_result"])
async def test_background_failure_stops_probe(monkeypatch, args, method):
    async def fail(*_):
        raise OSError("private connection details")
    monkeypatch.setattr(NvidiaVoiceChatAdapter, method, fail)
    report, _ = await run_fixture(monkeypatch, args)
    assert report["status"] == "failed" and report["reason"] == "OSError"
    assert "private connection" not in json.dumps(report)


async def test_barge_in_has_one_audio_writer_and_actual_overlap(monkeypatch, args):
    args.barge_in_wav = args.wav
    report, seen = await run_fixture(monkeypatch, args)
    assert report["checks"]["barge_in_audio_sent_while_tool_pending"] == "passed"
    assert seen["frames"].count(b"\x01\x00" * 1920) == 2
    assert report["audio_evidence"]["barge_in_input_samples"] == 1920


async def test_capture_is_bounded_and_truncation_explicit(monkeypatch, args):
    monkeypatch.setattr(probe, "MAX_EVENTS", 3)
    monkeypatch.setattr(probe, "MAX_OUTPUT_BYTES", 3840)
    args.output_wav = args.output + ".wav"
    report, _ = await run_fixture(monkeypatch, args)
    assert len(report["event_timeline"]) == 3 and report["timeline_dropped"] > 0
    assert report["audio_evidence"]["output_samples"] == 5 * 1920
    assert report["audio_evidence"]["output_wav_truncated"]
    with wave.open(args.output_wav) as wav:
        assert wav.getnframes() == 1920


def test_dual_tools_cannot_silently_test_legacy_bridge(args):
    with pytest.raises(probe.ProbeFailure, match="DUAL_TOOLS_REQUIRES_SESSION_SNAPSHOT"):
        probe.load_configuration(args, Settings(_env_file=None, qa_execution_mode="dual_tools"))


def test_console_summary_contains_only_decision_fields():
    report = {
        "status": "observed", "phase": "finished", "reason": None,
        "real_service_connected": True, "registered_tool_names": ["search_knowledge_base"],
        "checks": {"tool_result_submitted": "passed"},
        "audio_evidence": {"output_samples": 1920, "output_wav_truncated": False},
        "pending_call_ids_at_close": [], "timeline_dropped": 0,
    }

    lines = list(probe.summary_lines(report))

    assert len(lines) == 3
    assert lines[0] == (
        "probe_status=observed phase=finished reason=None connected=True "
        "tools=['search_knowledge_base']"
    )
    assert lines[1] == 'checks={"tool_result_submitted":"passed"}'
    assert lines[2] == (
        "audio_output_samples=1920 pending_calls=[] timeline_dropped=0 "
        "output_wav_truncated=False"
    )


@pytest.mark.parametrize("duration", [float("nan"), float("inf"), -1, 121])
def test_invalid_duration_is_bounded(args, duration):
    args.tool_wait_timeout = duration
    with pytest.raises(probe.ProbeFailure, match="INVALID_PROBE_DURATION"):
        probe.validate_durations(args)


def test_input_padding_preserves_prefix_and_rejects_empty_wav(args):
    chunks, _ = probe.read_wav(args.wav)
    assert chunks == [b"\x01\x00" * 1920]
    with wave.open(args.wav, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(24000)
    with pytest.raises(probe.ProbeFailure, match="INPUT_DURATION_OUT_OF_BOUNDS"):
        probe.read_wav(args.wav)
