"""Stateless loaded-tokenizer preflight and ASR-ahead input ownership; CPU only."""
import ast
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from test_response_lifecycle import CALL, Socket, pending, server

BACKEND = Path(__file__).parents[2] / 'opt/tritonserver/backends/nemotron-voicechat/model.py'


def test_backend_preview_uses_final_wrapper_and_does_not_touch_sequence(tmp_path):
    cls = next(node for node in ast.parse(BACKEND.read_text()).body if isinstance(node, ast.ClassDef) and node.name == 'TritonPythonModel')
    methods = [node for node in cls.body if getattr(node, 'name', None) in {'_measure_tool_result', '_tool_tokenizer_identity', 'execute'}]
    for method in methods:
        method.decorator_list = []
    raw = '{"decision":"answer","evidence":[]}'
    calls = []
    pb = SimpleNamespace(get_input_tensor_by_name=lambda request, name: SimpleNamespace(as_numpy=lambda: np.array([[raw.encode()]], dtype=object)),
                         InferenceResponse=lambda **kw: kw, Tensor=lambda name, data: (name, data))
    namespace = {'pb_utils': pb, 'np': np, 'Path': Path, 'hashlib': hashlib, 'json': json,
                 'format_tool_response_for_nemo': lambda value: '<TOOL_RESPONSE>[' + value + ']</TOOL_RESPONSE>'}
    module = ast.Module(body=[ast.ClassDef(name='Backend', bases=[], keywords=[], body=methods, decorator_list=[])], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), str(BACKEND), 'exec'), namespace)
    (tmp_path / 'tokenizer.json').write_text('{"synthetic":true}')
    model = namespace['Backend']()
    def tokenize(value):
        calls.append(value)
        return list(value.encode())  # explicit controlled tokenizer
    model.model = SimpleNamespace(tokenizer=SimpleNamespace(text_to_ids=tokenize),
        model_cfg=SimpleNamespace(model=SimpleNamespace(stt=SimpleNamespace(model=SimpleNamespace(pretrained_llm=str(tmp_path))))))
    model._seq_mgr = object()  # any sequence lookup or write is an error
    request = SimpleNamespace(parameters=lambda: '{"voicechat_tool_result_measure":true}')
    response = model.execute([request])[0]
    metadata = json.loads(dict(response['output_tensors'])['output_function_text'][0])
    assert calls == ['<TOOL_RESPONSE>[' + raw + ']</TOOL_RESPONSE>']
    assert metadata['token_count'] == len(calls[0].encode())
    assert metadata['text_sha256'] == hashlib.sha256(raw.encode()).hexdigest()
    assert metadata['tokenizer_id'].startswith('voicechat:')


@pytest.mark.asyncio
async def test_old_native_call_stays_with_old_input_while_display_asr_is_ahead():
    session = server.AudioSession('ahead', Socket())
    session.tools = '[]'
    session._asr_input_start_frame = 0
    session._native_audio_frame_end = 4
    await session._emit_speech_started()
    original = session._input_item_id
    session._asr_input_start_frame = 10
    await session._emit_speech_started()
    followup = session._input_item_id
    await session._process_function_text(CALL)
    call = next(event for event in pending(session) if event['type'] == 'response.function_call_arguments.done')
    assert call['input_item_id'] == original
    assert session._function_inputs[call['call_id']] == original
    session._native_audio_frame_end = 10  # exclusive; new input not consumed yet
    assert session._native_input_id() == original
    session._native_audio_frame_end = 12
    assert session._native_input_id() == followup


@pytest.mark.asyncio
async def test_preflight_server_returns_metadata_without_enqueuing_function_or_audio():
    socket = Socket()
    session = server.AudioSession('budget', socket)
    session.sequence_started.set()
    seen = []
    raw = '{"decision":"insufficient"}'
    async def infer(**kw):
        seen.append(kw)
        assert kw['parameters'] == {'voicechat_tool_result_measure': True}
        assert not kw['sequence_start'] and not kw['sequence_end']
        result = {'token_count': 21, 'tokenizer_id': 'synthetic', 'serialization': 'nemo-tool-response-v1',
                  'text_sha256': hashlib.sha256(raw.encode()).hexdigest()}
        return SimpleNamespace(as_numpy=lambda name: np.array([json.dumps(result).encode()]))
    session.triton_client = SimpleNamespace(infer=infer)
    await session.handle_tool_result_measure({'event_id': 'probe', 'text': raw})
    assert len(seen) == 1 and not session.function_call_output
    assert session.triton_samples_submitted_16k == 0
    assert socket.events[0]['type'] == 'tool_result.measured'
    assert socket.events[0]['event_id'] == 'probe'


@pytest.mark.asyncio
async def test_adjacent_live_asr_end_start_keeps_transcripts_and_frame_owners_separate():
    session = server.AudioSession('adjacent', Socket())
    session._asr_input_start_frames = [0]
    await session._dispatch_asr_text('<s>First question')
    first = session._input_item_id
    session._asr_input_start_frames = [41]
    await session._dispatch_asr_text('</s><s>Second question')
    second = session._input_item_id
    session._asr_input_start_frames = []
    await session._dispatch_asr_text('</s>')
    completed = [event for event in pending(session) if event['type'] == 'conversation.item.input_audio_transcription.completed']
    assert [(event['item_id'], event['transcript']) for event in completed] == [
        (first, 'First question'), (second, 'Second question')]
    assert session._input_audio_intervals == [(0, first), (41, second)]


@pytest.mark.asyncio
async def test_asr_packet_can_carry_two_inputs_with_distinct_start_metadata():
    session = server.AudioSession('packed', Socket())
    session._asr_input_start_frames = [0, 41]
    await session._dispatch_asr_text('<s>First question</s><s>Second question</s>')
    completed = [event for event in pending(session) if event['type'] == 'conversation.item.input_audio_transcription.completed']
    assert [event['transcript'] for event in completed] == ['First question', 'Second question']
    assert session._input_audio_intervals == [(0, completed[0]['item_id']), (41, completed[1]['item_id'])]
