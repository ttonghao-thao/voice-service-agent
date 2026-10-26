"""Run archived backend methods on CPU; no inference engine or trained weights.

Distinct ramp samples expose dropped, duplicated and out-of-order audio windows.
Forced function embeddings are compared to the original per-step recurrence.
These tests do not establish GPU speed, native routing or speech accuracy.
"""

import ast
import asyncio
import copy
from collections import deque
from dataclasses import dataclass
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
             '_fast_inject_response_tokens'}
    methods = [copy.deepcopy(n) for n in cls.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in names]
    for method in methods:
        method.decorator_list = []
    module = ast.Module(body=[ast.ClassDef(name='Backend', bases=[], keywords=[], body=methods, decorator_list=[])], type_ignores=[])
    messages = []
    namespace = {'torch': torch, 'deque': deque, 'InferenceResult': Result, 'List': list, 'SeqInfo': object,
                 'DTYPE': torch.float32, 'format_tool_response_for_nemo': lambda s: s,
                 'time': __import__('time'), 'pb_utils': SimpleNamespace(Logger=SimpleNamespace(
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
