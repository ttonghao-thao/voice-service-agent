"""Run archived backend methods on CPU; no inference engine or trained weights.

Distinct ramp samples expose dropped, duplicated and out-of-order audio windows.
Forced function embeddings are compared to the original per-step recurrence.
These tests do not establish GPU speed, native routing or speech accuracy.
"""

import ast
import asyncio
import copy
import threading
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

import pytest
import torch

BACKEND = Path(__file__).parents[2] / 'opt/tritonserver/backends/nemotron-voicechat/model.py'


@dataclass
class Result:
    generated_text: str
    generated_asr_text: str
    generated_function_text: str
    generated_audio: torch.Tensor
    skipped: bool = False
    error: str | None = None
    audio_frame_end: int = 0
    asr_frame_end: int = 0
    asr_input_start_frame: int | None = None
    asr_input_start_frames: list[int | None] = field(default_factory=list)


class Manager(dict):
    def is_started(self, seq):
        return seq in self and not self[seq].end


def state(seq=1):
    return SimpleNamespace(
        seq_id=seq, end=False, voice_diagnostics={}, audio_buffer=torch.empty(1, 0), audio_frames_processed=0,
        audio_chunk_idx=0, decoder_global_step=20, steps_exhausted=False, inference_error=None,
        pending_function_responses=deque(), pending_inference_output=None, generated_text_tokens=torch.zeros(1, 2048, dtype=torch.long),
        generated_function_tokens=torch.zeros(1, 2048, dtype=torch.long), awaiting_new_input=False,
        fn_call={'state': 'waiting_for_response', 'response_tokens': deque(), 'ack_tokens': deque(),
                 'extracting_tool': False, 'injecting_response': False},
    )


def harness(states=None):
    tree = ast.parse(BACKEND.read_text())
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'TritonPythonModel')
    names = {'infer', '_infer_audio_window', '_response_embeddings', '_inject_response_tokens',
             '_fast_inject_response_tokens', '_observe_audio_during_tool', '_observe_rnnt_display_frame',
             '_rnnt_step_transcript_display', '_append_rnnt_display_output', '_reset_rnnt_display_buffer',
             '_new_rnnt_display_state'}
    methods = [copy.deepcopy(n) for n in cls.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in names]
    for method in methods:
        method.decorator_list = []
    module = ast.Module(body=[ast.ClassDef(name='Backend', bases=[], keywords=[], body=methods, decorator_list=[])], type_ignores=[])
    messages = []
    namespace = {'torch': torch, 'deque': deque, 'InferenceResult': Result, 'List': list, 'SeqInfo': object,
                 'DTYPE': torch.float32, 'format_tool_response_for_nemo': lambda s: s,
                 'time': __import__('time'), 'VOICECHAT_DIAGNOSTICS': False,
                 'RNNT_EOS_SILENCE_FRAMES': 2, 'RNNT_DISPLAY_FALLBACK_CLEAR_FRAMES': 16, 'pb_utils': SimpleNamespace(Logger=SimpleNamespace(
                     log_info=messages.append, log_error=messages.append, log_verbose=lambda _: None))}
    exec(compile(ast.fix_missing_locations(module), str(BACKEND), 'exec'), namespace)
    model = namespace['Backend']()
    model._seq_mgr = Manager({s.seq_id: s for s in (states or [state()])})
    model.model = SimpleNamespace(device='cpu', tokenizer=SimpleNamespace(text_to_ids=lambda _: [4, 5]))
    model.options = SimpleNamespace(num_chunks_per_inference=2, num_samples_per_chunk=1,
                                    max_samples_for_inference=4)
    model._capture_tensor = lambda *a, **kw: None
    model._capture_enabled = lambda _: False
    model._resume_reply_for_tool_result = lambda _: None
    model._maybe_start_fast_path_threads = lambda _: None
    model.consumed = {seq: [] for seq in model._seq_mgr}
    model.windows = []
    model._last_audio = {}

    def perception(audio, lengths):
        model.windows.append([row[:int(length)].clone() for row, length in zip(audio, lengths, strict=True)])
        return audio, None

    def generate(audio, sequences, asr_emb=None):
        for row, seq in zip(audio, sequences, strict=True):
            s = model._seq_mgr[seq]
            length = min(s.audio_frames_processed + 2, 4)
            consumed = row[length - 2:length].tolist()
            model.consumed[seq].extend(consumed)
            model._last_audio[seq] = torch.tensor([consumed])
            s.audio_frames_processed += 2
            s.decoder_global_step += 2

    model.perception = perception
    model.generate_tokens = generate
    model.decode_output_from_tokens = lambda ids: (
        [''] * len(ids), [''] * len(ids), [''] * len(ids), [model._last_audio[i] for i in ids])
    return model, messages


def feed(model, values, responses=None):
    ids = list(model._seq_mgr)
    return model.infer(torch.tensor(values, dtype=torch.float32), responses or [''] * len(ids),
                       SimpleNamespace(sequence_ids=torch.tensor(ids), start_flags=torch.zeros(len(ids)),
                                       seq_to_batch_idx={seq: i for i, seq in enumerate(ids)}))


@pytest.mark.parametrize('busy', ['extracting_tool', 'injecting_response'])
def test_busy_decoder_keeps_every_sample_and_catches_up_in_order(busy):
    model, _ = harness()
    s = model._seq_mgr[1]
    s.fn_call[busy] = True
    for index in range(10):
        result = feed(model, [[index * 2 + 1, index * 2 + 2]])[0]
        assert result.skipped
    assert s.audio_buffer.tolist() == [list(range(1, 21))]
    assert s.audio_frames_processed == 0 and s.decoder_global_step == 20
    # Hundreds of tool steps must not require hundreds of fake audio frames.
    s.decoder_global_step += 500
    s.fn_call[busy] = False
    feed(model, [[21, 22]])
    assert model.consumed[1] == list(range(1, 9))  # bounded four-window catch-up
    feed(model, [[]])
    feed(model, [[]])
    assert model.consumed[1] == list(range(1, 23))
    assert s.audio_frames_processed == 22 and s.decoder_global_step == 542
    assert s.audio_buffer.tolist() == [list(range(1, 23))]
    assert model.windows[2][0].tolist() == [3, 4, 5, 6]


def test_busy_result_is_queued_and_submitted_once_when_decoder_resumes():
    model, messages = harness()
    s = model._seq_mgr[1]
    s.fn_call['injecting_response'] = True
    feed(model, [[1, 2]], ['result'])
    assert list(s.pending_function_responses) == ['result']
    s.fn_call['injecting_response'] = False
    feed(model, [[3, 4]])
    feed(model, [[5, 6]])
    assert not s.pending_function_responses
    assert sum('response_chars=' in m for m in messages) == 1
    assert model.consumed[1] == [1, 2, 3, 4, 5, 6]


def test_one_busy_sequence_does_not_block_or_mix_another_sequences_input():
    model, _ = harness([state(1), state(2)])
    model._seq_mgr[1].fn_call['extracting_tool'] = True
    result = feed(model, [[1, 2], [101, 102]])
    assert result[0].skipped and not result[1].skipped
    assert model.consumed == {1: [], 2: [101, 102]}
    model._seq_mgr[1].fn_call['extracting_tool'] = False
    feed(model, [[3, 4], [103, 104]])
    assert model.consumed == {1: [1, 2, 3, 4], 2: [101, 102, 103, 104]}


def test_pending_result_overflow_fails_explicitly_without_replaying():
    model, _ = harness()
    s = model._seq_mgr[1]
    s.pending_function_responses.extend(['result'] * 16)
    result = feed(model, [[1, 2]], ['overflow'])[0]
    assert result.error and len(s.pending_function_responses) == 16
    assert not model.consumed[1]


def injection_harness():
    model, messages = harness()
    s = model._seq_mgr[1]
    model.pad_id = 0
    model._encoded_audio_dim = 4
    table = torch.nn.Embedding(32, 4)
    with torch.no_grad():
        table.weight.copy_(torch.arange(128).reshape(32, 4) / 10)
    model.model.embed_tokens = table
    model.model.model_cfg = SimpleNamespace(model=SimpleNamespace(stt=SimpleNamespace(
        model={'duplex_text_channel_weight': 0.7, 'duplex_function_channel_weight': 1.3})))
    model.model.llm_custom_input_specs = [SimpleNamespace(dtype='float32')]
    model._injection_threads = {1: object()}
    model._voice_diagnostic = lambda *a, **kw: None
    s.fn_call.update(state='process_response', response_tokens=deque([4, 5, 6]), injecting_response=True)
    s.generated_text_tokens[0, 19] = 2  # preserve ACK EOS on the first forced step
    s.generated_function_tokens[0, 19] = 9
    s.audio_buffer = torch.tensor([[1., 2.]])
    model.appended = []
    model.schedules = 0

    async def append(seq, ids, embeds):
        model.appended.append(embeds[0].clone())
        return {'custom_outputs': object()}  # No logits access or text sampling allowed.

    def run(coro):
        model.schedules += 1
        return asyncio.run(coro)

    def reset(fn, state):
        fn['state'] = 'idle'
        fn['response_tokens'].clear()

    model._get_vllm_llm_next_token = append
    model._run_coro = run
    model._reset_fn_call_state = reset
    return model, s, messages


@pytest.mark.parametrize("dtype", [torch.float32, torch.bfloat16])
def test_forced_embeddings_match_original_channel_recurrence_without_audio_steps(dtype):
    model, s, messages = injection_harness()
    model._response_embeddings.__func__.__globals__["DTYPE"] = dtype
    model.model.llm_custom_input_specs[0].dtype = str(dtype).split(".")[-1]
    model._fast_inject_response_tokens(1)
    expected = []
    for text, function in [(2, 9), (0, 4), (0, 5)]:
        value = torch.zeros(4, dtype=dtype)
        value += model.model.embed_tokens(torch.tensor(text)).detach().to(dtype) * 0.7
        value += model.model.embed_tokens(torch.tensor(function)).detach().to(dtype) * 1.3
        expected.append(value)
    assert torch.allclose(torch.cat(model.appended), torch.stack(expected))
    assert model.schedules == 1 and len(model.appended) == 3
    assert s.generated_function_tokens[0, 20:23].tolist() == [4, 5, 6]
    assert s.generated_text_tokens[0, 20:23].tolist() == [0, 0, 0]
    assert s.decoder_global_step == 23 and s.audio_frames_processed == 0
    assert s.audio_chunk_idx == 0 and s.audio_buffer.tolist() == [[1, 2]]
    assert s.fn_call['state'] == 'idle' and not s.fn_call['injecting_response']
    assert not model._injection_threads


def test_failed_partial_injection_closes_instead_of_hanging_or_replaying():
    model, s, _ = injection_harness()
    append = model._get_vllm_llm_next_token

    async def fail_second(*args):
        if model.appended:
            raise RuntimeError('engine failed')
        return await append(*args)

    model._get_vllm_llm_next_token = fail_second
    model._fast_inject_response_tokens(1)
    assert s.inference_error and not s.fn_call['injecting_response']
    assert len(model.appended) == 1 and list(s.fn_call['response_tokens']) == [5, 6]
    assert feed(model, [[3, 4]])[0].error
    assert len(model.appended) == 1


@pytest.mark.parametrize('fail_second', [False, True])
def test_inference_tensor_history_is_updated_on_real_background_loop_without_mode_leak(fail_second):
    # Production execute() creates history under inference_mode; _run_coro
    # submits to a separate event-loop thread. asyncio.run on the caller thread
    # hides the missing thread-local mode and used to let this regression pass.
    with torch.inference_mode():
        model, s, messages = injection_harness()
    assert s.generated_text_tokens.is_inference()
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever)
    thread.start()
    append = model._get_vllm_llm_next_token

    async def checked_append(*args):
        assert not torch.is_inference_mode_enabled()
        await asyncio.sleep(0)
        assert not torch.is_inference_mode_enabled()
        if fail_second and model.appended:
            raise RuntimeError('engine failed')
        return await append(*args)

    async def mode_after():
        return torch.is_inference_mode_enabled()

    model._get_vllm_llm_next_token = checked_append
    model._run_coro = lambda coro: asyncio.run_coroutine_threadsafe(coro, loop).result(timeout=5)
    try:
        model._fast_inject_response_tokens(1)
        assert not model._run_coro(mode_after())
        if fail_second:
            assert s.inference_error and s.decoder_global_step == 21
            assert len(model.appended) == 1 and list(s.fn_call['response_tokens']) == [5, 6]
            assert any('error_type=RuntimeError remaining_tokens=2' in message for message in messages)
        else:
            assert s.inference_error is None and s.fn_call['state'] == 'idle'
            assert s.generated_function_tokens[0, 20:23].tolist() == [4, 5, 6]
            assert s.decoder_global_step == 23 and len(model.appended) == 3
        assert not s.fn_call['injecting_response'] and not model._injection_threads
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=5)
        loop.close()


def test_catchup_preserves_two_frame_eos_bos_packets_and_buffers_new_input():
    model, _ = harness()
    s = model._seq_mgr[1]
    s.audio_buffer = torch.tensor([[1., 2., 3., 4.]])
    decode = model.decode_output_from_tokens

    def boundary_on_second_window(ids):
        text, function, asr, audio = decode(ids)
        if s.audio_frames_processed == 4:
            text[0] = "</s><s>"
        return text, function, asr, audio

    model.decode_output_from_tokens = boundary_on_second_window
    first = feed(model, [[]])[0]
    assert first.generated_text == "" and first.generated_audio.tolist() == [[1, 2]]
    second = feed(model, [[5, 6]])[0]
    assert second.generated_text == "</s><s>"
    assert second.generated_audio.tolist() == [[3, 4]]
    assert s.audio_frames_processed == 4 and s.audio_buffer.tolist() == [[1, 2, 3, 4, 5, 6]]
    assert feed(model, [[]])[0].generated_audio.tolist() == [[5, 6]]


def live_display_harness():
    model, messages = harness()
    seq = model._seq_mgr[1]
    seq.rnnt_display_state = None
    seq.rnnt_display_pending_text = ''
    seq.rnnt_display_emitted_len = 0
    seq.rnnt_display_turn_open = False
    seq.rnnt_display_frames_processed = 0
    seq.rnnt_display_live_turn = False
    model.model.rnnt_decoder = object()
    model.model.tokenizer.bos_id = 1
    model.options.max_chunks_for_inference = 4
    model._voice_diagnostic = lambda *a, **kw: None
    model._capture_reference_rnnt = lambda *a, **kw: None
    model._rnnt_display_decode_text = lambda ids: ''.join('word' + str(token) for token in ids)
    model.decoded_display_frames = []

    def perception(audio, lengths):
        model.windows.append([row[:int(length)].clone() for row, length in zip(audio, lengths, strict=True)])
        return audio, audio.unsqueeze(-1)
    model.perception = perception

    def decode(embedding, batch, seq_id, frame):
        state = model._seq_mgr[seq_id]
        display = state.rnnt_display_state or model._new_rnnt_display_state()
        value = int(embedding[batch, frame, 0])
        model.decoded_display_frames.append(value)
        if value:
            display['y_sequence'].append(value)
            display['nonblank_total'] += 1
            display['blank_count'] = 0
            display['speech_confirmed'] = True
        else:
            display['blank_count'] += 1
        state.rnnt_display_state = display
        return [value] if value else []
    model._rnnt_display_decode_frame = decode
    original_output = model.decode_output_from_tokens
    def output(ids):
        text, function, _, audio = original_output(ids)
        asr = [model._seq_mgr[seq_id].rnnt_display_pending_text for seq_id in ids]
        for seq_id in ids:
            model._seq_mgr[seq_id].rnnt_display_pending_text = ''
        return text, function, asr, audio
    model.decode_output_from_tokens = output
    return model, seq


def test_live_display_asr_advances_during_blocked_tool_then_llm_replays_once():
    busy = 'injecting_response'
    model, seq = live_display_harness()
    seq.fn_call[busy] = True
    first = feed(model, [[1, 2]])[0]
    assert first.generated_asr_text == '<s>word1word2'
    assert (first.audio_frame_end, first.asr_frame_end, first.asr_input_start_frame) == (0, 2, 0)
    assert first.asr_input_start_frames == [0]
    assert not first.generated_audio.numel() and not first.generated_text
    assert seq.fn_call[busy]  # The tool writer is still blocked/running.
    assert seq.audio_frames_processed == 0 and seq.decoder_global_step == 20
    ended = feed(model, [[0, 0]])[0]
    assert ended.generated_asr_text == '</s>'
    followup = feed(model, [[3, 4]])[0]
    assert followup.generated_asr_text == '<s>word3word4'
    assert (followup.audio_frame_end, followup.asr_frame_end, followup.asr_input_start_frame) == (0, 6, 4)
    assert seq.rnnt_display_frames_processed == 6
    assert seq.audio_buffer.tolist() == [[1, 2, 0, 0, 3, 4]]

    # A stale BOS from old LLM audio may not close the new live follow-up.
    model._observe_rnnt_display_frame(torch.tensor([[[9.]]]), 0, 1, 0, 0, agent_bos_fired=True)
    assert seq.rnnt_display_turn_open and not seq.rnnt_display_pending_text
    assert model.decoded_display_frames == [1, 2, 0, 0, 3, 4]

    def replay(audio, sequences, asr_emb=None):
        for batch, seq_id in enumerate(sequences):
            state = model._seq_mgr[seq_id]
            end = min(state.audio_frames_processed + 2, 4)
            values = audio[batch, end - 2:end].tolist()
            model.consumed[seq_id].extend(values)
            model._last_audio[seq_id] = torch.tensor([values])
            for offset in range(2):
                model._rnnt_step_transcript_display(asr_emb[batch:batch + 1], [seq_id], 3, offset)
                state.audio_chunk_idx = min(3, state.audio_chunk_idx + 1)
                state.audio_frames_processed += 1
                state.decoder_global_step += 1
    model.generate_tokens = replay
    seq.fn_call[busy] = False
    replayed = feed(model, [[0, 0]])[0]
    assert replayed.generated_asr_text == ''  # Earlier replay frames precede this boundary.
    assert feed(model, [[]])[0].generated_asr_text == '</s>'
    assert model.decoded_display_frames == [1, 2, 0, 0, 3, 4, 0, 0]
    assert model.consumed[1] == [1, 2, 0, 0, 3, 4, 0, 0]
    assert seq.audio_frames_processed == seq.rnnt_display_frames_processed == 8
    assert seq.decoder_global_step == 28
    assert not seq.rnnt_display_turn_open


def test_live_asr_runs_while_actual_injection_coroutine_is_waiting_for_engine():
    model, seq = live_display_harness()
    template, _, _ = injection_harness()
    model.model.embed_tokens = template.model.embed_tokens
    model.model.model_cfg = template.model.model_cfg
    model.model.llm_custom_input_specs = template.model.llm_custom_input_specs
    model._encoded_audio_dim = template._encoded_audio_dim
    model.pad_id = 0
    model._injection_threads = {1: object()}
    seq.fn_call.update(state='process_response', response_tokens=deque([4, 5]), injecting_response=True)
    entered, release = threading.Event(), threading.Event()
    appended = []

    async def append(seq_id, ids, embeds):
        entered.set()
        await asyncio.to_thread(release.wait, 5)
        appended.append(embeds[0].clone())
    model._get_vllm_llm_next_token = append
    model._run_coro = asyncio.run
    model._reset_fn_call_state = lambda fn, state: fn.update(state='idle')
    worker = threading.Thread(target=model._fast_inject_response_tokens, args=(1,))
    worker.start()
    try:
        assert entered.wait(3)
        result = feed(model, [[1, 2]])[0]
        assert worker.is_alive() and seq.fn_call['injecting_response']
        assert result.generated_asr_text == '<s>word1word2'
        assert seq.decoder_global_step == 20 and seq.audio_frames_processed == 0
        assert not appended
    finally:
        release.set()
        worker.join(timeout=5)
    assert not worker.is_alive() and not seq.fn_call['injecting_response']
    assert seq.inference_error is None and len(appended) == 2
    assert seq.decoder_global_step == 22 and seq.audio_frames_processed == 0
    assert seq.audio_buffer.tolist() == [[1, 2]]


def test_live_asr_end_start_window_snapshots_each_published_input_onset():
    model, seq = live_display_harness()
    seq.fn_call['injecting_response'] = True
    first = feed(model, [[1, 0]])[0]
    assert first.generated_asr_text == '<s>word1'
    assert first.asr_input_start_frames == [0]
    adjacent = feed(model, [[0, 2]])[0]
    assert adjacent.generated_asr_text == '</s><s>word2'
    assert adjacent.asr_input_start_frames == [3]
    assert adjacent.audio_frame_end == 0 and adjacent.asr_frame_end == 4
    assert feed(model, [[0, 0]])[0].asr_input_start_frames == []
