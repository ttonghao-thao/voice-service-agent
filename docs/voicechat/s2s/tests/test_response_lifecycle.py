"""CPU-only regressions: real server dispatch/resampling, controlled model outputs."""
import asyncio
import base64
import hashlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

spec = importlib.util.spec_from_file_location("voicechat_server", Path(__file__).parents[1] / "audio_server.py")
server = importlib.util.module_from_spec(spec)
spec.loader.exec_module(server)

ACK = "Please wait while I check the knowledge base."
CALL = json.dumps([{"name": "consult_service_agent", "arguments": {"user_request": "hello"}}])


@pytest.mark.asyncio
@pytest.mark.parametrize("followup_before_result", [False, True])
async def test_tool_answer_retains_owner_and_groups_followup_after_real_ack_boundary(followup_before_result):
    session = server.AudioSession("correlation", Socket())
    session.tools = json.dumps([{"name": "consult_service_agent", "ack_messages": [ACK]}])
    await session._emit_speech_started()
    original = session._input_item_id
    await session._dispatch_model_output(np.zeros(1764, np.float32), "<s>" + ACK, "", CALL)
    events = pending(session)
    call = next(event for event in events if event["type"] == "response.function_call_arguments.done")
    if followup_before_result:
        await session._emit_speech_started()
    await session.handle_function_call_output({"call_id": call["call_id"], "output": "facts"})
    session._activate_function_result(session.function_call_output.popleft())
    if not followup_before_result:
        await session._emit_speech_started()
    followup = session._input_item_id
    await session._dispatch_model_output(np.zeros(1764, np.float32), "</s>", "", "")
    assert session._awaiting_tool_answers  # ACK must never consume the answer obligation.
    await session._dispatch_model_output(np.zeros(1764, np.float32), "<s>Documented answer.</s>", "", "")
    answer = next(event for event in pending(session) if event.get("transcript") == "Documented answer.")
    assert answer["input_item_id"] == original
    assert answer["related_input_item_ids"] == [followup]
    assert answer["tool_call_ids"] == [call["call_id"]]
    assert not session._awaiting_tool_answers and not session._answer_inputs
    await session._emit_speech_started()
    await session._dispatch_model_output(np.zeros(1764, np.float32), "<s>Hello.</s>", "", "")
    general = next(event for event in pending(session) if event.get("transcript") == "Hello.")
    assert general["input_item_id"] == session._input_item_id and not general["tool_call_ids"]


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
async def test_fast_catchup_pcm_is_paced_without_loss_and_preserves_tool_and_done_order(monkeypatch):
    clock = [100.0]
    sleep = asyncio.sleep

    async def advance(delay):
        clock[0] += delay
        await sleep(0)

    monkeypatch.setattr(server, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(server.asyncio, 'sleep', advance)
    sent = []

    class PacedSocket(Socket):
        async def send_text(self, text):
            event = json.loads(text)
            sent.append((clock[0], event))
            self.events.append(event)

    socket = PacedSocket()
    session = server.AudioSession('paced', socket)
    # More than two seconds produced instantly, as after fast tool extraction.
    await session._dispatch_model_output(np.full(3528 * 13, .3, np.float32), '<s>' + ACK, '', CALL)
    await session._dispatch_model_output(np.full(1764, .2, np.float32), '</s>', '', '')
    await session._dispatch_model_output(np.full(1764, -.3, np.float32), '<s>Next</s>', '', '')
    expected = list(session.output_events._queue)
    writer = asyncio.create_task(session._send_loop())
    try:
        await asyncio.wait_for(session.output_events.join(), 2)
    finally:
        writer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await writer
    assert socket.events == expected  # every byte, ID and boundary in order
    audio = [(at, len(base64.b64decode(e['delta'])) // 2) for at, e in sent
             if e['type'] == 'response.output_audio.delta']
    assert sum(n for _, n in audio) > 24000 * 2
    queued, previous = 0, audio[0][0]
    for at, samples in audio:
        queued = max(0, queued - (at - previous) * 24000) + samples
        assert queued <= 3841  # below the unchanged one-second portal ring
        previous = at
    assert audio[-1][0] - audio[0][0] > 2
    assert next(i for i, e in enumerate(expected) if e['type'] == 'response.function_call_arguments.done') < next(
        i for i, e in enumerate(expected) if e['type'] == 'response.output_audio.delta')


@pytest.mark.asyncio
async def test_output_clock_has_no_credit_after_idle_and_wait_is_cancellable(monkeypatch):
    clock = [100.0]
    sleeps = []

    async def advance(delay):
        sleeps.append(delay)
        clock[0] += delay

    monkeypatch.setattr(server, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(server.asyncio, 'sleep', advance)
    session = server.AudioSession('clock', Socket())
    event = {'type': 'response.output_audio.delta', 'delta': base64.b64encode(bytes(3840)).decode()}
    await session._pace_output_audio(event)
    clock[0] += 5  # stalled connection or idle between replies
    await session._pace_output_audio(event)
    assert sleeps == []
    await session._pace_output_audio(event)
    assert sleeps == pytest.approx([.08])

    async def cancel(delay):
        raise asyncio.CancelledError

    monkeypatch.setattr(server.asyncio, 'sleep', cancel)
    with pytest.raises(asyncio.CancelledError):
        await session._pace_output_audio(event)


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


@pytest.mark.asyncio
async def test_prefix_fingerprints_follow_real_decode_resample_and_submission_without_changing_pcm(monkeypatch):
    monkeypatch.setattr(server, "VOICECHAT_DIAGNOSTICS", True)
    session = server.AudioSession("prefix", Socket())
    session.sequence_started.set()

    class Response:
        def as_numpy(self, name):
            return np.zeros(0, np.float32) if name == "output_audio" else np.array([b""])

    class Triton:
        async def infer(self, **kwargs):
            return Response()

    session.triton_client = Triton()
    original = np.arange(1920 * 60, dtype=np.int16)
    for offset in range(0, len(original), 1920):
        await session.queue_audio_chunk(base64.b64encode(original[offset:offset + 1920].astype("<i2").tobytes()).decode())
        while not session.inference_queue.empty():
            samples, event_id = session.inference_queue.get_nowait()
            before = samples.copy()
            await session.process_audio_chunk_with_triton(samples, event_id)
            assert np.array_equal(samples, before)
            session.inference_queue.task_done()
    entries = [e for _, e in session._diagnostic_events if e["event"] == "input.prefix"]
    assert len(entries) == 3
    prefixes = {e["stream"]: e for e in entries}
    assert prefixes["decoded"]["sha256"] == hashlib.sha256(original[:96000].astype("<i2").tobytes()).hexdigest()
    assert prefixes["resampled"]["sha256"] == prefixes["submitted"]["sha256"]
    assert prefixes["submitted"]["samples"] == 64000 and session.chunks_dropped == 0
    assert all(state["samples"] <= (96000 if name == "decoded" else 64000)
               for name, state in session._input_prefixes.items())


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [False, True])
async def test_asr_fingerprints_preserve_all_parts_and_input_ids_without_logging_text(monkeypatch, caplog, enabled):
    monkeypatch.setattr(server, "VOICECHAT_DIAGNOSTICS", enabled)
    caplog.set_level("INFO", logger=server.__name__)
    session = server.AudioSession("prefix", Socket())
    await session._dispatch_asr_text("<s>Hello, ")
    await session._dispatch_asr_text("how are you today?</s>")
    await session._dispatch_asr_text("<s>What is the difference between TCP and UDP?</s>")
    events = [e for e in pending(session) if e["type"] == "conversation.item.input_audio_transcription.completed"]
    assert [e["transcript"] for e in events] == ["Hello, how are you today?", "What is the difference between TCP and UDP?"]
    entries = [e for _, e in session._diagnostic_events if e["event"] == "asr.completed"]
    if enabled:
        assert [e["input_item_id"] for e in entries] == [e["item_id"] for e in events]
        assert entries[-1]["source_sha256_total"] == hashlib.sha256("".join(e["transcript"] for e in events).encode()).hexdigest()
    else:
        assert entries == []
        session._input_prefix("decoded", np.ones(96000, np.float32), 24000)
        assert session._input_prefixes == {}
    assert "Hello" not in caplog.text and "difference" not in caplog.text


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
async def test_completed_response_context_never_contaminates_the_next_user_transcript():
    session = server.AudioSession("ownership", Socket())
    await session._dispatch_model_output(
        np.zeros(1764, np.float32), "<s>First reply.</s>", "<s>First question</s>", ""
    )
    first = pending(session)
    reply = next(event for event in first if event["type"] == "response.output_audio_transcript.done")
    first_input = next(event["item_id"] for event in first
                       if event["type"] == "conversation.item.input_audio_transcription.completed")
    assert reply["input_item_id"] == first_input
    assert session._response_input_item_id is None

    await session._dispatch_model_output(np.zeros(1764, np.float32), "", "<s>Second question", "")
    second = pending(session)
    transcript = next(event for event in second
                      if event["type"] == "conversation.item.input_audio_transcription.delta")
    assert transcript["item_id"] != first_input
    assert not {"input_item_id", "tool_call_ids", "related_input_item_ids"} & transcript.keys()


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
async def test_distinct_model_queries_are_forwarded_without_result_replay():
    session = server.AudioSession("test", Socket())
    session.tools = json.dumps([{"name": "search_knowledge_base", "ack_messages": [ACK]}])
    for query in ("Product AX price", "Product BX storage", "Product AX price"):
        await session._process_function_text(json.dumps([{
            "name": "search_knowledge_base", "arguments": {"query": query},
        }]))
    calls = [e for e in pending(session) if e["type"] == "response.function_call_arguments.done"]
    assert len(calls) == len({e["call_id"] for e in calls}) == 3
    assert [json.loads(e["arguments"])["query"] for e in calls] == [
        "Product AX price", "Product BX storage", "Product AX price"]
    for call in calls:
        await session.handle_function_call_output({"call_id": call["call_id"], "output": "{}"})
    assert [item["call_id"] for item in session.function_call_output] == [e["call_id"] for e in calls]


@pytest.mark.asyncio
async def test_repeated_model_ack_and_following_answer_remain_visible():
    session = server.AudioSession("test", Socket())
    session.tools = json.dumps([{"name": "search_knowledge_base", "ack_messages": [ACK]}])
    call = json.dumps([{"name": "search_knowledge_base", "arguments": {"query": "Product AX"}}])
    for _ in range(2):
        await session._dispatch_model_output(np.ones(3528, np.float32) * .2,
                                            "<s>" + ACK + "</s>", "", call)
    await session._dispatch_model_output(np.ones(3528, np.float32) * .2,
                                        "<s>Product AX answer.</s>", "", "")
    events = pending(session)
    assert len([e for e in events if e["type"] == "response.function_call_arguments.done"]) == 2
    assert [e["transcript"] for e in events if e["type"] == "response.output_audio_transcript.done"] == [
        ACK, ACK, "Product AX answer."]
    assert not session.function_call_output


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


@pytest.mark.asyncio
async def test_native_batch_has_explicit_boundary_and_one_aggregate_injection():
    session = server.AudioSession("test", Socket())
    await session._process_function_text(json.dumps([
        {"name": "search_knowledge_base", "arguments": {"query": "Product price"}},
        {"name": "search_knowledge_base", "arguments": {"query": "Product storage"}},
    ]))
    calls = [e for e in pending(session) if e["type"] == "response.function_call_arguments.done"]
    assert len({e["native_batch_id"] for e in calls}) == 1
    assert [e["output_index"] for e in calls] == [0, 1]
    assert all(e["native_batch_size"] == 2 for e in calls)
    with pytest.raises(ValueError, match="complete aggregate"):
        await session.handle_function_call_output({"call_id": calls[0]["call_id"], "output": "{}"})
    assert not session.function_call_output
    result = {"call_id": calls[0]["call_id"], "native_batch_id": calls[0]["native_batch_id"],
              "native_call_ids": [e["call_id"] for e in calls], "output": '{"status":"ok","sources":[]}'}
    await session.handle_function_call_output(result)
    assert len(session.function_call_output) == 1
    assert session.function_call_output[0]["output"] == result["output"]
    assert not session.function_call_batches and not session.function_batches
    with pytest.raises(ValueError, match="already settled"):
        await session.handle_function_call_output(result)
    assert len(session.function_call_output) == 1
