import json

from scripts.summarize_voice_timeline import summarize


def test_timeline_correlates_sequence_and_omits_other_sessions_and_payloads():
    lines = [
        '2026-10-21T01:00:00Z voice_input_transcribed conversation_id=chosen input_item_id=i2',
        '2026-10-21T01:00:01Z voice_ws_diagnostic ' + json.dumps({
            'event': 'asr.dispatch', 'sequence_id': 42, 'input_item_id': 'i2'}),
        '2026-10-21T01:00:02Z System prompt received for sequence 42: tool_names=search_knowledge_base chars=7082 sha256=abc',
        '2026-10-21T01:00:03Z Function call decoded from model for sequence 99: tool_names=other',
        '2026-10-21T01:00:04Z Function call decoded from model for sequence 42: tool_names=search_knowledge_base"',
        '2026-10-21T01:00:05Z voice_tool_call_received conversation_id=chosen call_id=c1 tool=search_knowledge_base input_item_id=i2 query=secret',
        '2026-10-21T01:00:06Z cuekb_response_validated conversation_id=other turn_id=t2 trace_id=x retrieval_status=ok hit_count=5 duration_ms=900',
        '2026-10-21T01:00:07Z cuekb_response_validated conversation_id=chosen turn_id=t1 trace_id=y retrieval_status=ok hit_count=5 duration_ms=102',
        '2026-10-21T01:00:08Z [seq 42] Fast response-token injection done, state: idle',
    ]
    header, rows = summarize(reversed(lines), 'chosen')
    assert header['sequence_ids'] == ['42']
    assert header['model_function_count'] == header['api_tool_calls'] == 1
    assert header['input_item_ids'] == ['i2']
    assert rows[-1]['event'] == 'backend.inject_done'
    assert 'secret' not in json.dumps(rows)
    assert '900' not in json.dumps(rows)
    assert next(r for r in rows if r['event'] == 'model.function')['tools'] == 'search_knowledge_base'


def test_missing_sequence_is_not_reported_as_zero_model_calls():
    header, rows = summarize([], 'chosen')
    assert header['session_match'] == 'unverified'
    assert header['model_function_count'] is None
    assert rows == []


def test_call_or_response_id_correlates_sequence_without_asr_diagnostic():
    header, rows = summarize([
        '2026-10-21T00:00:00Z voice_tool_call_received conversation_id=chosen input_item_id=i1 call_id=c1 tool=search_knowledge_base',
        '2026-10-21T00:00:01Z voice_ws_diagnostic ' + json.dumps({
            'event': 'function.batch_result', 'sequence_id': 123, 'call_id': 'c1', 'calls': 2}),
        '2026-10-21T00:00:02Z [seq 123] Fast response-token injection started',
    ], 'chosen')
    assert header['session_match'] == 'passed'
    assert header['sequence_ids'] == ['123']
    assert header['voicechat_diagnostic_events'] == 1
    assert rows[-1]['event'] == 'backend.inject_started'


def test_consumption_metrics_are_scoped_and_do_not_echo_tool_payloads():
    header, rows = summarize([
        '2026-10-25T00:00:00Z voice_input_transcribed conversation_id=chosen input_item_id=i1',
        '2026-10-25T00:00:01Z voice_ws_diagnostic ' + json.dumps({
            'event': 'asr.dispatch', 'sequence_id': 42, 'input_item_id': 'i1'}),
        '2026-10-25T00:00:02Z [seq 42] Tool audio buffered received_samples=2560 processed_frames=0 text=PRIVATE',
        '2026-10-25T00:00:03Z [seq 99] Tool response consumption tokens=999 completed=999 preparation_ms=1 append_wait_ms=1',
        '2026-10-25T00:00:04Z [seq 42] Tool response consumption tokens=400 completed=400 preparation_ms=8 append_wait_ms=9000 text=PRIVATE',
        '2026-10-25T00:00:05Z [seq 42] Tool audio resumed received_samples=12800 processed_frames=2',
    ], 'chosen')
    assert header['session_match'] == 'passed'
    profile = next(r for r in rows if r['event'] == 'backend.inject_profile')
    assert profile['tokens'] == profile['completed'] == 400
    assert profile['preparation_ms'] == 8 and profile['append_wait_ms'] == 9000
    assert next(r for r in rows if r['event'] == 'backend.audio_resumed')['processed_frames'] == 2
    assert 'PRIVATE' not in json.dumps(rows) and '999' not in json.dumps(rows)


def test_injection_finally_is_not_success_and_failure_metadata_survives_collection():
    from io import StringIO

    from scripts.run_native_cuekb_validation import backend_injection_line, read_voicechat_evidence

    logs = [
        '2026-10-28T00:00:00Z voice_input_transcribed conversation_id=chosen input_item_id=i1',
        '2026-10-28T00:00:01Z voice_ws_diagnostic ' + json.dumps({
            'event': 'asr.dispatch', 'sequence_id': 42, 'input_item_id': 'i1'}),
        '2026-10-28T00:00:02Z [seq 42] Fast response-token injection started',
        '2026-10-28T00:00:03Z [seq 420] Tool result injection failed: error_type=ValueError remaining_tokens=999',
        '2026-10-28T00:00:04Z [seq 42] Tool result injection failed: error_type=RuntimeError remaining_tokens=530 PRIVATE',
        '2026-10-28T00:00:05Z [seq 42] Fast response-token injection done, state: process_response',
    ]
    _, rows = summarize(logs, 'chosen')
    assert rows[-1]['state'] == 'process_response'
    assert rows[-2]['event'] == 'backend.inject_failed'
    assert rows[-2]['error_type'] == 'RuntimeError' and rows[-2]['remaining_tokens'] == 530
    assert 'PRIVATE' not in json.dumps(rows) and '999' not in json.dumps(rows)
    selected = read_voicechat_evidence(StringIO('\n'.join(logs)))
    line = backend_injection_line(selected, '42')
    assert 'status=failed' in line and 'failed=1' in line and 'done_idle=0' in line
