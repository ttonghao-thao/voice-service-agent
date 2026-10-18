"""CPU-only regressions: real server dispatch/resampling, controlled model outputs."""
import asyncio
import base64
import hashlib
import importlib.util
import json
from pathlib import Path

import numpy as np
import pytest

spec = importlib.util.spec_from_file_location("voicechat_server", Path(__file__).parents[1] / "audio_server.py")
server = importlib.util.module_from_spec(spec)
spec.loader.exec_module(server)

ACK = "Please wait while I check the knowledge base."
CALL = json.dumps([{"name": "consult_service_agent", "arguments": {"user_request": "hello"}}])


class Socket:
    def __init__(self):
        self.events = []
        self.closed = False

    async def send_text(self, text):
        await asyncio.sleep(0)
        self.events.append(json.loads(text))

    async def close(self, **kwargs):
        self.closed = True


def pending(session):
    events = []
    while not session.output_events.empty():
        events.append(session.output_events.get_nowait())
        session.output_events.task_done()
    return events


def pcm(events):
    return np.frombuffer(b"".join(base64.b64decode(e["delta"]) for e in events
                                 if e["type"] == "response.output_audio.delta"), dtype="<i2")


@pytest.mark.asyncio
async def test_diagnostics_preserve_tool_before_ack_order_without_logging_content(monkeypatch, caplog):
    monkeypatch.setattr(server, "VOICECHAT_DIAGNOSTICS", True)
    caplog.set_level("INFO", logger=server.__name__)
    socket = Socket()
    session = server.AudioSession("test", socket)
    await session._dispatch_model_output(np.zeros(3528, np.float32), "<s>" + ACK, "<s>HELLO THAO MOONCAKE</s>", CALL)
    await session._dispatch_model_output(np.zeros(3528, np.float32), "</s>", "", "")
    writer = asyncio.create_task(session._send_loop())
    await asyncio.wait_for(session.output_events.join(), 2)
    writer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await writer
    for phase in ("queued", "sent"):
        entries = [e for _, e in session._diagnostic_events if e["event"] == "output." + phase]
        tool = next(e for e in entries if e["kind"] == "response.function_call_arguments.done")
        audio = next(e for e in entries if e["kind"] == "response.output_audio.delta")
        assert tool["output_seq"] < audio["output_seq"] and tool["response_id"] == audio["response_id"]
    assert session._output_queued_seq == session._output_sent_seq == len(socket.events)
    assert "HELLO" not in caplog.text and ACK not in caplog.text and '"user_request"' not in caplog.text


def test_ws_diagnostic_history_is_bounded_and_truncation_is_explicit(monkeypatch):
    monkeypatch.setattr(server, "VOICECHAT_DIAGNOSTICS", True)
    session = server.AudioSession("test", Socket())
    for index in range(500):
        session._voice_diagnostic("test.boundary", index=index)
    assert len(session._diagnostic_events) <= 256 and session._diagnostic_bytes <= 65536
    assert session._diagnostic_dropped > 0
    assert len(json.dumps([e for _, e in session._diagnostic_events], ensure_ascii=True).encode()) <= 65536
    assert session._diagnostic_bytes == sum(len(json.dumps(e, ensure_ascii=True).encode()) for _, e in session._diagnostic_events)


def test_isolated_capture_is_single_sequence_bounded_and_content_exact(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "VOICECHAT_CAPTURE", (tmp_path, 42, 1))
    session = server.AudioSession("capture", Socket())
    assert session.sequence_id == 42
    first = np.linspace(-0.5, 0.5, 160, dtype=np.float32)
    second = np.linspace(0.25, -0.25, 80, dtype=np.float32)
    session._capture_array("ws-decoded-24k", first, 24000)
    session._capture_array("ws-decoded-24k", second, 24000)
    session._finish_capture("test")
    capture = tmp_path / "sequence-42"
    payload = (capture / "ws-decoded-24k.f32le").read_bytes()
    assert np.array_equal(np.frombuffer(payload, dtype="<f4"), np.r_[first, second])
    summary = json.loads((capture / "ws-summary.json").read_text())
    assert summary["sequence_id"] == 42 and summary["reason"] == "test"
    assert summary["files"]["ws-decoded-24k"]["samples"] == 240
    assert summary["files"]["ws-decoded-24k"]["sha256"] == hashlib.sha256(payload).hexdigest()
    with pytest.raises(FileExistsError):
        server.AudioSession("second", Socket())


def test_isolated_capture_refuses_partial_or_unsafe_configuration(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "VOICECHAT_CAPTURE_DIR", str(tmp_path))
    monkeypatch.setattr(server, "VOICECHAT_CAPTURE_SEQUENCE_ID", "")
    with pytest.raises(RuntimeError, match="must be set together"):
        server._capture_configuration()
    monkeypatch.setattr(server, "VOICECHAT_CAPTURE_SEQUENCE_ID", "42")
    monkeypatch.setattr(server, "VOICECHAT_CAPTURE_DIR", "relative/path")
    with pytest.raises(RuntimeError, match="absolute non-symlink"):
        server._capture_configuration()


@pytest.mark.asyncio
async def test_idle_codec_arrays_do_not_create_or_poison_a_response():
    session = server.AudioSession("test", Socket())
    for _ in range(8):
        await session._dispatch_model_output(np.zeros(3528, np.float32), "", "", "")
    assert pending(session) == []
    assert not session._response_active
    await session._dispatch_model_output(np.ones(3528, np.float32) * .3, "<s>Hello</s>", "", "")
    events = pending(session)
    assert events[0]["type"] == "response.created"
    assert events[-1]["type"] == "response.done"
    assert len(pcm(events)) == 3840
    assert not session._response_active


@pytest.mark.asyncio
async def test_tool_precedes_same_batch_ack_and_completed_asr_binds_one_input():
    session = server.AudioSession("test", Socket())
    await session._dispatch_model_output(np.zeros(3528, np.float32), "<s>" + ACK, "<s>hello</s>", CALL)
    await session._dispatch_model_output(np.zeros(3528, np.float32), "</s>", "", "")
    events = pending(session)
    kinds = [e["type"] for e in events]
    tool = next(e for e in events if e["type"] == "response.function_call_arguments.done")
    asr = [e for e in events if e["type"].startswith(("input_audio_buffer.", "conversation.item.input_audio"))]
    assert len({e["item_id"] for e in asr}) == 1
    assert kinds.index("conversation.item.input_audio_transcription.completed") < kinds.index(tool["type"])
    assert kinds.index(tool["type"]) < kinds.index("response.output_audio.delta")
    assert {e["response_id"] for e in events if "response_id" in e} == {tool["response_id"]}
    assert json.loads(tool["arguments"]) == {"user_request": "hello"}
    assert kinds[-1] == "response.done"


@pytest.mark.asyncio
async def test_slow_writer_preserves_packet_ownership_done_order_and_short_audio_tail():
    socket = Socket()
    session = server.AudioSession("test", socket)
    for value, text in [(0.4, "One"), (-0.4, "Two")]:
        await session._dispatch_model_output(np.full(2000, value, np.float32), "<s>" + text + "</s>", "", "")
    # Both response IDs have already been retired before the sender starts.
    writer = asyncio.create_task(session._send_loop())
    await asyncio.wait_for(session.output_events.join(), 2)
    writer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await writer
    events = socket.events
    ids = [e["response"]["id"] for e in events if e["type"] == "response.created"]
    assert len(ids) == 2 and ids[0] != ids[1]
    for response, sign in zip(ids, [1, -1], strict=True):
        response_events = [e for e in events if e.get("response_id") == response]
        audio = pcm(response_events)
        assert len(audio) == round(2000 * 24000 / 22050)
        assert np.mean(audio[100:-100]) * sign > 12000
        audio_indexes = [i for i, e in enumerate(events) if e.get("response_id") == response and e["type"] == "response.output_audio.delta"]
        done_index = next(i for i, e in enumerate(events) if e.get("response_id") == response and e["type"] == "response.output_audio.done")
        assert max(audio_indexes) < done_index
    assert events[-1]["type"] == "response.done"


@pytest.mark.asyncio
async def test_adjacent_eos_bos_uses_the_exact_80ms_codec_boundary():
    session = server.AudioSession("test", Socket())
    await session._dispatch_model_output(np.full(1764, .4, np.float32), "<s>First", "", "")
    await session._dispatch_model_output(np.r_[np.full(1764, .4, np.float32), np.full(1764, -.4, np.float32)], "</s><s>", "", "")
    await session._dispatch_model_output(np.full(1764, -.4, np.float32), "Second</s>", "", "")
    events = pending(session)
    ids = [e["response"]["id"] for e in events if e["type"] == "response.created"]
    assert len(ids) == 2
    for response, sign in zip(ids, [1, -1], strict=True):
        audio = pcm([e for e in events if e.get("response_id") == response])
        assert len(audio) == 3840
        assert np.mean(audio[100:-100]) * sign > 12000


@pytest.mark.asyncio
async def test_malformed_batch_and_output_backpressure_fail_explicitly():
    session = server.AudioSession("test", Socket())
    with pytest.raises(ValueError, match="Ambiguous"):
        await session._dispatch_model_output(np.zeros(100, np.float32), "</s><s>", "", "")
    session.output_events = asyncio.Queue(maxsize=1)
    session._queue_output_event({"type": "first"})
    with pytest.raises(RuntimeError, match="bounded capacity"):
        session._queue_output_event({"type": "second"})


@pytest.mark.asyncio
@pytest.mark.parametrize("arguments", [{"user_request": "hello"}, '{"user_request":"hello"}'])
async def test_tool_argument_object_and_json_string_have_one_wire_encoding(arguments):
    session = server.AudioSession("test", Socket())
    await session._process_function_text(json.dumps([{"name": "consult_service_agent", "arguments": arguments}]))
    event = pending(session)[-1]
    assert json.loads(event["arguments"]) == {"user_request": "hello"}


@pytest.mark.asyncio
async def test_function_channel_forwards_registered_tool_name_without_rewriting():
    session = server.AudioSession("test", Socket())
    await session._process_function_text(json.dumps([{
        "name": "search_knowledge_base",
        "arguments": {"query": "Product AX storage requirements"},
    }]))
    event = pending(session)[-1]
    assert event["type"] == "response.function_call_arguments.done"
    assert event["name"] == "search_knowledge_base"


@pytest.mark.asyncio
async def test_duplicate_tool_call_reuses_one_api_call_and_cached_result_per_input():
    session = server.AudioSession("test", Socket())
    session.tools = json.dumps([{"name": "search_knowledge_base"}])
    session._input_item_id = "input-1"
    call_text = json.dumps([{
        "name": "search_knowledge_base",
        "arguments": {"query": "Black Pig Crisp Heart details"},
    }])

    await session._process_function_text(call_text)
    await session._process_function_text(json.dumps([{
        "name": "search_knowledge_base",
        "arguments": {"query": "Please find the Black Pig product details"},
    }]))
    calls = [event for event in pending(session)
             if event["type"] == "response.function_call_arguments.done"]
    assert len(calls) == 1

    await session.handle_function_call_output({
        "type": "function_call_output",
        "call_id": calls[0]["call_id"],
        "output": '{"status":"evidence_ready"}',
    })
    await session._process_function_text(json.dumps([{
        "name": "search_knowledge_base",
        "arguments": {"query": "Black Pig price and storage"},
    }]))
    assert not [event for event in pending(session)
                if event["type"] == "response.function_call_arguments.done"]
    assert len(session.function_call_output) == 1

    signature = session._function_call_signatures[calls[0]["call_id"]]
    session.function_call_output.clear()
    session._function_signature_states[signature] = "delivered"
    await session._process_function_text(call_text)
    assert len(session.function_call_output) == 1
    assert session.function_call_output[0]["output"] == '{"status":"evidence_ready"}'

    session.function_call_output.clear()
    session._function_signature_states[signature] = "delivered"
    with pytest.raises(ValueError, match="cached result replay limit"):
        await session._process_function_text(call_text)

    await session._emit_speech_started()
    pending(session)
    await session._process_function_text(call_text)
    next_turn = [event for event in pending(session)
                 if event["type"] == "response.function_call_arguments.done"]
    assert len(next_turn) == 1 and next_turn[0]["call_id"] != calls[0]["call_id"]
    assert json.loads(next_turn[0]["arguments"]) == {"query": "Black Pig Crisp Heart details"}


def test_pcm16_clips_full_scale_and_flushes_partial_little_endian_frame():
    codec = server.PCM16Codec(chunk_size=2)
    codec.encode(np.array([-2., 1., 2.], dtype=np.float32))
    assert np.frombuffer(codec.read_encoded_chunk(), dtype="<i2").tolist() == [-32768, 32767]
    assert np.frombuffer(codec.flush_encoded_chunk(), dtype="<i2").tolist() == [32767]
    assert codec.flush_encoded_chunk() is None


@pytest.mark.parametrize("use_jinja", [False, True])
def test_jinja_preserves_application_routing_and_legacy_default_is_explicit(
    monkeypatch, caplog, use_jinja
):
    monkeypatch.setattr(server, "USE_JINJA_TEMPLATE_PROMPT", use_jinja)
    caplog.set_level("INFO", logger=server.__name__)
    tools = [{"name": "search_knowledge_base", "ack_messages": [ACK],
              "parameters": {"type": "object"}}]
    instruction = "Use search_knowledge_base for company documentation."
    prompt = server.AudioSession.build_prompt(instruction, json.dumps(tools))
    assert instruction in prompt
    assert ("answer directly from your own knowledge" not in prompt) == use_jinja
    assert "<AVAILABLE_TOOLS>" in prompt and "<TOOLCALL>" in prompt
    assert "<TOOL_ACK_MESSAGES>" in prompt and ACK in prompt
    assert "tool_names=search_knowledge_base" in caplog.text
    assert f"prompt_sha256={hashlib.sha256(prompt.encode()).hexdigest()}" in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary_text", ["</s>", "<s>" + ACK])
async def test_tool_never_reuses_an_initial_greeting_response(boundary_text):
    session = server.AudioSession("test", Socket())
    await session._dispatch_model_output(np.zeros(3528, np.float32), "<s>Welcome", "", "")
    greeting = session._response_id
    await session._dispatch_model_output(np.zeros(3528, np.float32), boundary_text, "<s>hello</s>", CALL)
    events = pending(session)
    call = next(e for e in events if e["type"] == "response.function_call_arguments.done")
    assert call["response_id"] != greeting
    greeting_done = next(i for i, e in enumerate(events) if e["type"] == "response.done" and e["response"]["id"] == greeting)
    tool_index = events.index(call)
    assert greeting_done < tool_index
