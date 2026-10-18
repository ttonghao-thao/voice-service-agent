"""Validate the bundled backend and execute its actual methods with CPU fixtures.

Token buffers, TTS outputs, codec and resampler are controlled fixtures. These
tests establish conditional completion and WebSocket ordering, not GPU accuracy,
speech quality, or a sufficient rendering budget for a real recording.
"""

import ast
import asyncio
import base64
import hashlib
import json
import os
import shutil
import subprocess
import threading
import uuid
from array import array
from collections import deque
from contextlib import nullcontext
from types import SimpleNamespace

import pytest
from app.config import ROOT

BACKEND = "opt/tritonserver/backends/nemotron-voicechat/model.py"
PAD, BOS, EOS, CONTENT, SOTC, EOTC = 0, 1, 2, 3, 10, 12


@pytest.fixture(scope="module")
def patched_backend(tmp_path_factory):
    manifest = json.loads((ROOT / "deploy/voicechat/pad-tail-completion-manifest.json").read_text())
    archive_manifest = json.loads((ROOT / "docs/voicechat/snapshot-manifest.json").read_text())
    hashes = manifest["files"][BACKEND]
    source = ROOT / "docs/voicechat" / BACKEND
    assert hashlib.sha256(source.read_bytes()).hexdigest() == hashes["after_sha256"]
    assert hashes["after_sha256"] == archive_manifest["files"][BACKEND]
    temporary = tmp_path_factory.mktemp("voicechat-pad-tail")
    target = temporary / BACKEND
    target.parent.mkdir(parents=True)
    shutil.copyfile(source, target)
    patch = ROOT / "deploy/voicechat/pad-tail-completion.patch"
    for arguments in (("--reverse", "--check"), ("--reverse",)):
        subprocess.run(["git", "apply", *arguments, str(patch)], cwd=temporary, check=True,
                       capture_output=True, text=True)
    assert hashlib.sha256(target.read_bytes()).hexdigest() == hashes["before_sha256"]
    for arguments in (("--check",), ()):
        subprocess.run(["git", "apply", *arguments, str(patch)], cwd=temporary, check=True,
                       capture_output=True, text=True)
    assert hashlib.sha256(target.read_bytes()).hexdigest() == hashes["after_sha256"]
    compile(target.read_text(), str(target), "exec")
    assert target.read_bytes() == source.read_bytes()
    return target


class Token(int):
    def item(self):
        return int(self)


class Window(list):
    def __eq__(self, value):
        return SimpleNamespace(any=lambda: any(token == value for token in self),
                               all=lambda: all(token == value for token in self))


class TokenBuffer:
    def __init__(self, tokens):
        self.tokens = list(tokens)

    def __getitem__(self, index):
        row, column = index
        assert row == 0
        return Window(self.tokens[column]) if isinstance(column, slice) else Token(self.tokens[column])

    def __setitem__(self, index, value):
        row, column = index
        assert row == 0
        self.tokens[column] = value


class TokenVector(list):
    def __ne__(self, value):
        return [token != value for token in self]

    def __getitem__(self, index):
        if isinstance(index, list):
            return [Token(value) for value, include in zip(self, index, strict=True) if include]
        return super().__getitem__(index)


class Tensor:
    def __init__(self, value):
        self.value = value

    def item(self):
        return self.value

    def unsqueeze(self, _dimension):
        return self

    def cpu(self):
        return self

    def __getitem__(self, _index):
        return self


class AudioTokens:
    def __init__(self):
        self.frames = {}

    def __setitem__(self, index, value):
        _batch, step = index
        self.frames[step] = value.value


def model_harness(source, *, enabled=True, ratio=3.0, force_silence=True):
    tree = ast.parse(source.read_text())
    original = next(node for node in tree.body if isinstance(node, ast.ClassDef)
                    and node.name == "TritonPythonModel")
    names = {"_tts_pad_tail_budget_exhausted", "_apply_turn_taking_from_rnnt_states",
             "vllm_tts_step", "token_to_text_with_bos_eos", "generate_tokens",
             "parse_function_call_tokens", "_reset_fn_call_state"}
    methods = [node for node in original.body if isinstance(node, ast.FunctionDef) and node.name in names]
    assert len(methods) == len(names)
    for method in methods:
        method.decorator_list = []  # Timing decorators do not define the boundary behavior.
    namespace = {
        "pb_utils": SimpleNamespace(Logger=SimpleNamespace(log_info=lambda _: None, log_verbose=lambda _: None,
                                                          log_error=lambda _: None)),
        "FORCE_TURN_TAKING_THRESHOLD": 40, "RNNT_BOU_MIN_FRAMES_FIRST_TURN": 2,
        "RNNT_DENSITY_THRESHOLD": .15, "RNNT_DENSITY_LOW_MIN": 6, "RNNT_BOU_MIN_FRAMES": 3,
        "RNNT_NOISE_RESET_FRAMES": 10, "RNNT_EOS_SILENCE_FRAMES": 40,
        "RNNT_MAX_AGENT_RESPONSE_FRAMES": 0, "RNNT_TTS_RATIO_CAP": 0, "RNNT_BARGE_IN_FRAMES": 40,
        "torch": SimpleNamespace(float32="float32", Tensor=Tensor,
                                 full_like=lambda _input, value, **_kwargs: Tensor(value),
                                 ones_like=lambda _input, **_kwargs: Tensor(1)),
        "asyncio": SimpleNamespace(gather=lambda *requests: requests),
        "_time_block": lambda _name: nullcontext(), "deque": deque,
    }
    module = ast.Module(body=[
        ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
        ast.ClassDef(name="PatchedBackend", bases=[], keywords=[], body=methods, decorator_list=[], type_params=[]),
    ], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), str(source), "exec"), namespace)
    model = namespace["PatchedBackend"]()
    model.pad_id = PAD
    model.sotc_id, model.eotc_id = SOTC, EOTC
    tokenizer = SimpleNamespace(bos_id=BOS, eos_id=EOS, bos=BOS, eos=EOS,
        ids_to_tokens=lambda tokens: [{BOS: "<s>", EOS: "</s>"}[int(token)] for token in tokens],
        ids_to_text=lambda tokens: "".join("Hello" if int(token) == CONTENT else "" for token in tokens))
    model.model = SimpleNamespace(tokenizer=tokenizer, rnnt_decoder=None)
    model.options = SimpleNamespace(num_chunks_per_inference=2, max_chunks_for_inference=70)
    model._tts_close_on_pad_tail = enabled
    model._tts_force_silence_on_pad = force_silence
    model._tts_pad_tail_ratio = ratio
    model._tts_silence_codes = Tensor((0,) * 31)
    model._tts_loop_lock = threading.Lock()
    model._seq_mgr = {}
    model.diagnostics = []
    model._voice_diagnostic = lambda sequence, event, **fields: model.diagnostics.append(
        {"sequence_id": sequence.seq_id, "event": event, **fields})
    return model


def test_model_logs_decoded_tool_name_before_websocket_processing(patched_backend):
    tree = ast.parse(patched_backend.read_text())
    original = next(node for node in tree.body if isinstance(node, ast.ClassDef)
                    and node.name == "TritonPythonModel")
    method = next(node for node in original.body if isinstance(node, ast.FunctionDef)
                  and node.name == "sanitize_function_text")
    messages = []
    module = ast.Module(body=[
        ast.ClassDef(name="Model", bases=[], keywords=[], body=[method],
                     decorator_list=[], type_params=[]),
    ], type_ignores=[])
    namespace = {
        "json": json,
        "pb_utils": SimpleNamespace(Logger=SimpleNamespace(
            log_info=messages.append,
            log_error=messages.append,
        )),
    }
    exec(compile(ast.fix_missing_locations(module), str(patched_backend), "exec"), namespace)
    value = '<TOOLCALL>[{"name":"search_knowledge_base","arguments":{"query":"AX"}}]</TOOLCALL>'
    assert json.loads(namespace["Model"]().sanitize_function_text(42, value))[0]["name"] == "search_knowledge_base"
    assert messages == ["Function call decoded from model for sequence 42: tool_names=search_knowledge_base"]


def sequence(*, seq_id=42, text=PAD, function=PAD, previous_function=PAD, pads=7, content=2,
             function_state="idle", speaking=True, user_speech=0):
    return SimpleNamespace(
        seq_id=seq_id, decoder_global_step=1, audio_chunk_idx=1,
        generated_text_tokens=TokenBuffer([CONTENT if content > 0 else BOS, text]),
        generated_function_tokens=TokenBuffer([previous_function, function]), generated_audio_tokens=AudioTokens(),
        rnnt_rolling_density=.5, rnnt_first_turn=False, rnnt_agent_speaking=speaking,
        rnnt_user_speaking=False, rnnt_silent_frames=20, rnnt_nonblank_total=0,
        rnnt_consecutive_speech_frames=user_speech, rnnt_agent_talking_frames=20,
        rnnt_turn_text_tokens=content, tts_agent_idle=False, tts_in_turn_pads=pads,
        tts_in_turn_content=content, steps_exhausted=False, fn_call={"state": function_state},
    )


def current_token(seq):
    return seq.generated_text_tokens[0, seq.decoder_global_step].item()


def install_tts_fixture(model, sequences):
    for seq in sequences:
        model._seq_mgr[seq.seq_id] = seq
        seq.generated_text_tokens.shape = (1, 100)
    captured = []
    model.model.tts = SimpleNamespace(
        create_stream=lambda _seq: None,
        stream_get_output=lambda _seq: (False, SimpleNamespace(outputs=[SimpleNamespace(
            custom_outputs={"acoustic_tokens": Tensor((9,) * 31)})])),
        stream_submit_request=lambda seq_id, inputs: captured.append((seq_id, inputs)),
        _audio_prompt_latent=Tensor(0), event_loop=SimpleNamespace(run_until_complete=lambda _requests: None),
    )
    return captured


def render_frame(model, seq):
    # Execute the actual TTS method while replacing only the engine/tensor I/O.
    captured = install_tts_fixture(model, [seq])
    model.vllm_tts_step([seq.seq_id])
    assert len(captured) == 1
    return seq.generated_audio_tokens.frames[seq.decoder_global_step]


@pytest.mark.parametrize("value,expected", [(None, False), ("false", False), ("true", True), ("1", True)])
def test_patched_initialization_is_disabled_by_default(patched_backend, monkeypatch, value, expected):
    if value is None:
        monkeypatch.delenv("S2S_INFERENCE_CLOSE_ON_PAD_TAIL", raising=False)
    else:
        monkeypatch.setenv("S2S_INFERENCE_CLOSE_ON_PAD_TAIL", value)
    tree = ast.parse(patched_backend.read_text())
    assignments = [node for node in ast.walk(tree) if isinstance(node, ast.Assign)
                   and any(isinstance(target, ast.Attribute) and target.attr == "_tts_close_on_pad_tail"
                           for target in node.targets)]
    assert len(assignments) == 1
    holder = SimpleNamespace()
    exec(compile(ast.Module(body=assignments, type_ignores=[]), str(patched_backend), "exec"),
         {"self": holder, "os": os})
    assert holder._tts_close_on_pad_tail is expected


@pytest.mark.parametrize("pads,expected", [(5, PAD), (6, PAD), (7, EOS)])
def test_completion_uses_previous_counter_and_strict_existing_boundary(patched_backend, pads, expected):
    model = model_harness(patched_backend)
    seq = sequence(pads=pads)
    model._apply_turn_taking_from_rnnt_states(seq.seq_id, seq)
    assert current_token(seq) == expected
    if expected == EOS:
        assert not seq.rnnt_agent_speaking and not seq.rnnt_user_speaking
        assert seq.rnnt_agent_talking_frames == seq.rnnt_turn_text_tokens == 0
        assert model.diagnostics == [{"sequence_id": 42, "event": "agent.eos", "source": "render_budget",
                                      "pad_tail_frames": 7, "content_tokens": 2, "pad_tail_ratio": 3.0}]
    else:
        assert model.diagnostics == []


@pytest.mark.parametrize("text,speaking,expected", [(BOS, False, BOS), (EOS, True, EOS),
                                                  (CONTENT, True, CONTENT)])
def test_original_non_pad_boundaries_and_content_are_preserved(patched_backend, text, speaking, expected):
    model = model_harness(patched_backend)
    seq = sequence(text=text, speaking=speaking)
    model._apply_turn_taking_from_rnnt_states(seq.seq_id, seq)
    assert current_token(seq) == expected
    assert not any(event.get("source") == "render_budget" for event in model.diagnostics)


@pytest.mark.parametrize("state", ["waiting_for_request", "request_received", "speaking_ack",
                                  "waiting_for_response", "process_response"])
def test_tool_lifecycle_is_not_completed_by_pad_budget(patched_backend, state):
    model = model_harness(patched_backend)
    seq = sequence(function_state=state)
    model._apply_turn_taking_from_rnnt_states(seq.seq_id, seq)
    assert current_token(seq) == PAD and seq.rnnt_agent_speaking
    assert model.diagnostics == []


@pytest.mark.parametrize("function", [SOTC, EOTC, CONTENT])
def test_unparsed_native_function_frame_is_not_completed(patched_backend, function):
    model = model_harness(patched_backend)
    seq = sequence(function=function)
    model._apply_turn_taking_from_rnnt_states(seq.seq_id, seq)
    assert current_token(seq) == PAD and model.diagnostics == []


@pytest.mark.parametrize("chunks,position", [(2, 0), (4, 0), (4, 2)])
@pytest.mark.parametrize("previous_function", [SOTC, EOTC, CONTENT])
def test_earlier_unparsed_function_frame_in_same_batch_is_protected(
    patched_backend, previous_function, chunks, position,
):
    model = model_harness(patched_backend)
    model.options.num_chunks_per_inference = chunks
    seq = sequence()
    seq.decoder_global_step = chunks - 1
    seq.generated_text_tokens = TokenBuffer([CONTENT] + [PAD] * (chunks - 1))
    function_tokens = [PAD] * chunks
    function_tokens[position] = previous_function
    seq.generated_function_tokens = TokenBuffer(function_tokens)
    assert seq.fn_call["state"] == "idle" and current_token(seq) == PAD
    model._apply_turn_taking_from_rnnt_states(seq.seq_id, seq)
    assert current_token(seq) == PAD and model.diagnostics == []


def test_function_token_outside_recent_batch_window_does_not_block_completion(patched_backend):
    model = model_harness(patched_backend)
    seq = sequence()
    seq.decoder_global_step = 2
    seq.generated_text_tokens = TokenBuffer([BOS, CONTENT, PAD])
    seq.generated_function_tokens = TokenBuffer([SOTC, PAD, PAD])
    model._apply_turn_taking_from_rnnt_states(seq.seq_id, seq)
    assert current_token(seq) == EOS and model.diagnostics[-1]["source"] == "render_budget"


@pytest.mark.parametrize("setting,value", [("enabled", False), ("force_silence", False),
                                          ("ratio", 0), ("ratio", -1)])
def test_disabled_completion_and_silence_policy_leave_pad_unchanged(patched_backend, setting, value):
    model = model_harness(patched_backend, **{setting: value})
    seq = sequence()
    model._apply_turn_taking_from_rnnt_states(seq.seq_id, seq)
    assert current_token(seq) == PAD and model.diagnostics == []


@pytest.mark.parametrize("setting", ["tts_idle", "agent_idle", "user_speech"])
def test_idle_agent_and_new_user_speech_are_excluded(patched_backend, setting):
    model = model_harness(patched_backend)
    seq = sequence(user_speech=1 if setting == "user_speech" else 0,
                   speaking=setting != "agent_idle")
    seq.tts_agent_idle = setting == "tts_idle"
    model._apply_turn_taking_from_rnnt_states(seq.seq_id, seq)
    assert current_token(seq) == PAD and model.diagnostics == []


def test_short_user_speech_followed_by_blank_preserves_pending_speech(patched_backend):
    model = model_harness(patched_backend)
    seq = sequence(user_speech=1)
    seq.rnnt_nonblank_total = 1
    model._apply_turn_taking_from_rnnt_states(seq.seq_id, seq)
    seq.rnnt_consecutive_speech_frames = 0  # Next RNNT frame is blank; cumulative speech remains.
    model._apply_turn_taking_from_rnnt_states(seq.seq_id, seq)
    assert current_token(seq) == PAD and seq.rnnt_nonblank_total == 1
    assert seq.rnnt_agent_speaking and model.diagnostics == []


def test_confirmed_user_turn_is_not_erased_by_completion_budget(patched_backend):
    model = model_harness(patched_backend)
    seq = sequence()
    seq.rnnt_user_speaking = True
    model._apply_turn_taking_from_rnnt_states(seq.seq_id, seq)
    assert current_token(seq) == PAD and seq.rnnt_user_speaking
    assert seq.rnnt_agent_speaking and model.diagnostics == []


def test_completion_does_not_mutate_another_sequence(patched_backend):
    model = model_harness(patched_backend)
    expired, rendering = sequence(seq_id=42), sequence(seq_id=43, pads=6)
    model._apply_turn_taking_from_rnnt_states(expired.seq_id, expired)
    model._apply_turn_taking_from_rnnt_states(rendering.seq_id, rendering)
    assert current_token(expired) == EOS and current_token(rendering) == PAD
    assert not expired.rnnt_agent_speaking and rendering.rnnt_agent_speaking
    assert [event["sequence_id"] for event in model.diagnostics] == [42]


def test_actual_batch_generation_preserves_sotc_until_post_batch_parser(patched_backend):
    model = model_harness(patched_backend)
    seq = sequence()
    seq.generated_text_tokens = TokenBuffer([CONTENT, PAD, PAD])
    seq.generated_function_tokens = TokenBuffer([PAD, PAD, PAD])
    captured = install_tts_fixture(model, [seq])
    trace = []

    def llm_step(_encoded, seq_ids, step_offset):
        assert seq_ids == [42]
        trace.append((step_offset, seq.fn_call["state"]))
        seq.generated_function_tokens[0, seq.decoder_global_step] = SOTC if step_offset == 0 else PAD

    model._llm_step = llm_step  # Controlled native output, not model/tool selection evidence.
    model.generate_tokens(None, [42])
    assert trace == [(0, "idle"), (1, "idle")]
    assert seq.generated_text_tokens.tokens == [CONTENT, PAD, PAD]
    assert [inputs["text_tokens"].item() for _seq_id, inputs in captured] == [PAD, PAD]
    assert seq.decoder_global_step == 3 and seq.fn_call["state"] == "idle"
    assert model.parse_function_call_tokens(42, TokenVector([SOTC, PAD])) == ""
    assert seq.fn_call["state"] == "waiting_for_request"
    assert not any(event.get("source") == "render_budget" for event in model.diagnostics)


def test_actual_tts_batch_keeps_each_sequences_boundary_and_codec(patched_backend):
    model = model_harness(patched_backend)
    expired, rendering = sequence(seq_id=42), sequence(seq_id=43, pads=6)
    captured = install_tts_fixture(model, [expired, rendering])
    model._apply_turn_taking_from_rnnt_states(expired.seq_id, expired)
    model._apply_turn_taking_from_rnnt_states(rendering.seq_id, rendering)
    model.vllm_tts_step([43, 42])
    assert [(seq_id, inputs["text_tokens"].item()) for seq_id, inputs in captured] == [(43, PAD), (42, EOS)]
    assert rendering.generated_audio_tokens.frames[1] == (9,) * 31
    assert expired.generated_audio_tokens.frames[1] == (0,) * 31


@pytest.mark.parametrize("content,pads", [(2, 7), (1, 4)])
def test_forced_eos_frame_uses_the_same_codec_codes_as_existing_pad_silence(patched_backend, content, pads):
    original = model_harness(patched_backend, enabled=False)
    patched = model_harness(patched_backend)
    old_seq, new_seq = sequence(content=content, pads=pads), sequence(content=content, pads=pads)
    original._apply_turn_taking_from_rnnt_states(old_seq.seq_id, old_seq)
    patched._apply_turn_taking_from_rnnt_states(new_seq.seq_id, new_seq)
    assert current_token(old_seq) == PAD and current_token(new_seq) == EOS
    assert render_frame(original, old_seq) == render_frame(patched, new_seq) == (0,) * 31
    assert new_seq.tts_agent_idle and new_seq.tts_in_turn_content == new_seq.tts_in_turn_pads == 0


def test_pure_bos_pad_turn_is_silenced_without_claiming_answer_completion(patched_backend):
    model = model_harness(patched_backend)
    seq = sequence(content=0, pads=1)
    model._apply_turn_taking_from_rnnt_states(seq.seq_id, seq)
    assert current_token(seq) == PAD and seq.rnnt_agent_speaking
    assert model.diagnostics == []
    assert render_frame(model, seq) == (0,) * 31
    assert not seq.tts_agent_idle


def test_rendering_at_exact_budget_keeps_acoustic_output(patched_backend):
    model = model_harness(patched_backend)
    seq = sequence(pads=6)
    model._apply_turn_taking_from_rnnt_states(seq.seq_id, seq)
    assert render_frame(model, seq) == (9,) * 31


class Audio(list):
    @property
    def size(self):
        return len(self)


class ControlledResampler:
    """Hold two samples so the real server must flush its final tail."""

    def __init__(self, *_args, **_kwargs):
        self.pending = Audio()

    def resample_chunk(self, values, last=False):
        self.pending.extend(values)
        count = len(self.pending) if last else max(0, len(self.pending) - 2)
        output, self.pending = Audio(self.pending[:count]), Audio(self.pending[count:])
        return output


class ControlledCodec:
    def __init__(self, *_args):
        self.pending = Audio()

    def encode(self, values):
        self.pending.extend(values)

    def read_encoded_chunk(self):
        if len(self.pending) < 4:
            return None
        values, self.pending = self.pending[:4], Audio(self.pending[4:])
        return array("h", [round(value * 100) for value in values]).tobytes()

    def flush_encoded_chunk(self):
        if not self.pending:
            return None
        values, self.pending = self.pending, Audio()
        return array("h", [round(value * 100) for value in values]).tobytes()


def websocket_harness():
    source = ROOT / "docs/voicechat/s2s/audio_server.py"
    tree = ast.parse(source.read_text())
    original = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "AudioSession")
    names = {"_dispatch_model_output", "_finish_spoken_response", "_flush_response_audio",
             "_emit_response_created", "_emit_response_done", "_emit_audio_transcript_delta",
             "_emit_audio_transcript_done", "_queue_output_event", "_queue_audio_chunk",
             "encode_audio_output", "_create_codec"}
    methods = [node for node in original.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
               and node.name in names]
    assert len(methods) == len(names)
    module = ast.Module(body=[
        ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
        ast.ClassDef(name="RealDispatch", bases=[], keywords=[], body=methods, decorator_list=[], type_params=[]),
    ], type_ignores=[])
    namespace = {"asyncio": asyncio, "uuid": uuid, "base64": base64, "SEND_OUTPUT_TEXT_EVENTS": False,
                 "MODEL_OUTPUT_SAMPLE_RATE": 22050, "OUTPUT_SAMPLE_RATE": 24000,
                 "PCM16Codec": ControlledCodec,
                 "soxr": SimpleNamespace(ResampleStream=ControlledResampler),
                 "np": SimpleNamespace(array=lambda values, **_kwargs: Audio(values), float32="float32",
                                       clip=lambda values, lower, upper: Audio(
                                           min(upper, max(lower, value)) for value in values))}
    exec(compile(ast.fix_missing_locations(module), str(source), "exec"), namespace)
    session = namespace["RealDispatch"]()
    session.output_events = asyncio.Queue(maxsize=32)
    session._output_queued_seq = 0
    session._trace_output_event = lambda *_args: None
    session._response_active = session._response_has_speech = False
    session._response_id = session._output_item_id = None
    session.accumulated_agent_transcript = []
    return session


async def test_patched_eos_produces_ordered_done_after_final_audio_tail(patched_backend):
    model = model_harness(patched_backend)
    seq = sequence()
    model._apply_turn_taking_from_rnnt_states(seq.seq_id, seq)
    terminal_text = model.token_to_text_with_bos_eos(TokenVector([PAD, current_token(seq)]))
    assert terminal_text == "</s>"
    session = websocket_harness()
    await session._dispatch_model_output(Audio([.5] * 8), "<s>Hello", "", "")
    await session._dispatch_model_output(Audio([.25] * 3), terminal_text, "", "")
    events = []
    while not session.output_events.empty():
        events.append(session.output_events.get_nowait())
    kinds = [event["type"] for event in events]
    pcm = array("h")
    pcm.frombytes(b"".join(base64.b64decode(event["delta"]) for event in events
                            if event["type"] == "response.output_audio.delta"))
    assert pcm.tolist() == [50] * 8 + [25] * 3
    assert kinds[-5:] == ["response.output_audio_transcript.done", "response.output_audio.done",
                         "response.content_part.done", "response.output_item.done", "response.done"]
    assert events[-5]["transcript"] == "Hello"
    assert events[-1]["response"]["status"] == "completed"
    assert max(index for index, kind in enumerate(kinds) if kind == "response.output_audio.delta") < len(kinds) - 5
    assert len({event["response_id"] for event in events if "response_id" in event}) == 1
    assert not session._response_active and session._response_id is None


def test_completion_patch_can_be_reversed_to_exact_archived_backend(patched_backend, tmp_path):
    target = tmp_path / BACKEND
    target.parent.mkdir(parents=True)
    shutil.copyfile(patched_backend, target)
    patch = ROOT / "deploy/voicechat/pad-tail-completion.patch"
    subprocess.run(["git", "apply", "--reverse", "--check", str(patch)], cwd=tmp_path, check=True,
                   capture_output=True, text=True)
    subprocess.run(["git", "apply", "--reverse", str(patch)], cwd=tmp_path, check=True,
                   capture_output=True, text=True)
    manifest = json.loads((ROOT / "deploy/voicechat/pad-tail-completion-manifest.json").read_text())
    assert hashlib.sha256(target.read_bytes()).hexdigest() == manifest["files"][BACKEND]["before_sha256"]
