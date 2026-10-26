import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from app.storage.models import Base, Conversation, Record, Turn
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from scripts import collect_validation_summary as collector

ROOT = Path(__file__).resolve().parents[2]
CHOSEN = '11111111-1111-1111-1111-111111111111'
OTHER = '22222222-2222-2222-2222-222222222222'


def test_summary_separates_sessions_and_does_not_count_ack_as_answer(capsys):
    logs = [
        f'2026-10-21T00:00:00Z voice_input_transcribed conversation_id={CHOSEN} input_item_id=i1',
        '2026-10-21T00:00:01Z voice_ws_diagnostic ' + json.dumps({
            'sequence_id': 42, 'input_item_id': 'i1', 'event': 'asr.dispatch'}),
        '2026-10-21T00:00:02Z Function call decoded from model for sequence 42: tool_names=search_knowledge_base',
        '2026-10-21T00:00:02Z Function call decoded from model for sequence 99: tool_names=unrelated',
        f'2026-10-21T00:00:03Z voice_tool_call_received conversation_id={CHOSEN} call_id=c1 tool=search_knowledge_base input_item_id=i1 query=SECRET',
        f'2026-10-21T00:00:04Z voice_audio_first_sent conversation_id={CHOSEN} response_id=ack input_item_id=i1',
        f'2026-10-21T00:00:05Z voice_response_completed conversation_id={CHOSEN} response_id=ack input_item_id=i1 phase=ack samples=100',
        f'2026-10-21T00:00:06Z voice_tool_result_submitted conversation_id={CHOSEN} turn_id=t1 call_id=c1 bytes=200',
        f'2026-10-21T00:00:07Z voice_tool_call_received conversation_id={OTHER} call_id=c2 tool=unrelated input_item_id=i2',
        f'2026-10-21T00:00:08Z voice_audio_first_sent conversation_id={CHOSEN} response_id=answer input_item_id=i1',
        f'2026-10-21T00:00:09Z voice_text_first_sent conversation_id={CHOSEN} response_id=answer input_item_id=i1',
        f'2026-10-21T00:00:10Z voice_response_completed conversation_id={CHOSEN} response_id=answer input_item_id=i1 phase=answer samples=500',
    ]
    collector.log_summary('\n'.join(logs), CHOSEN)
    output = capsys.readouterr().out
    assert 'SECRET' not in output and 'unrelated' not in output and OTHER not in output
    assert '"model_function_count":1' in output
    assert '"last_result_to_audio_ms":2000' in output
    assert '"answer_audio":"2026-10-21T00:00:08Z"' in output
    assert '"answer_text":"2026-10-21T00:00:09Z"' in output
    assert '"text_before_done_ms":1000' in output


def test_unknown_sequence_and_missing_answer_remain_unverified(capsys):
    collector.log_summary('', CHOSEN)
    output = capsys.readouterr().out
    assert '"model_function_count":null' in output
    assert '"session_match":"unverified"' in output
    assert 'NO_TOOL_CALL_OBSERVED' in output
    assert collector.milliseconds(None, None) is None
    assert collector.clipped('x' * 5, 3) == {'text': 'xxx', 'truncated': True}


async def test_saved_records_exclude_prior_epoch_outside_window(tmp_path, monkeypatch, capsys):
    from datetime import UTC, datetime
    from types import SimpleNamespace

    url = 'sqlite+aiosqlite:///' + str(tmp_path / 'records.db')
    engine = create_async_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine)() as db:
        db.add(Conversation(id=CHOSEN, owner_id='test', title='Validation', epoch=2))
        for day, text in ((20, 'OLD_PRIVATE_TEXT'), (21, 'current question')):
            db.add(Record(conversation_id=CHOSEN, epoch=day, kind='user_transcript', source_id=str(day),
                          payload={'item_id': str(day), 'text': text}, created_at=datetime(2026, 10, day, tzinfo=UTC)))
        db.add(Record(conversation_id=CHOSEN, epoch=2, kind='native_tool_arguments', source_id='query',
                      payload={'input_item_id': 'current', 'arguments': {'query': 'Can Product AX be shipped?'}},
                      created_at=datetime(2026, 10, 21, tzinfo=UTC)))
        db.add(Record(conversation_id=CHOSEN, epoch=2, kind='native_answer_check', source_id='answer-turn',
                      payload={'input_item_id': 'current', 'response_id': 'checked-response',
                               'reason_code': 'VOICE_MISSING_CONDITION',
                               'condition_failures': [{'value': str(i)} for i in range(4)],
                               'unlisted_debug_field': 'PRIVATE_DEBUG_DO_NOT_PRINT'},
                      created_at=datetime(2026, 10, 21, tzinfo=UTC)))
        db.add(Record(conversation_id=CHOSEN, epoch=2, kind='native_tool_batch', source_id='batch',
                      payload={'input_item_id': 'current', 'new_sources': 0,
                               'retained_sources': 5, 'result_injections': 1},
                      created_at=datetime(2026, 10, 21, tzinfo=UTC)))
        db.add(Turn(conversation_id=CHOSEN, epoch=2, request_revision=1, input_item_id='current',
                    idempotency_key='test-query', request_hash='test', user_text='Can it be shipped?', channel='voice',
                    status='answered', answer={'status': 'answered'},
                    evidence={'envelope': {'items': [{'citation_id': 'C1', 'text': 'Ship locally only.',
                        'conditions': {'product_model': 'AX'}}]},
                              'citations': [{'context': 'EXCLUDED_CONTEXT_MUST_NOT_APPEAR', 'trace_id': 'trace'}]},
                    created_at=datetime(2026, 10, 21, tzinfo=UTC)))
        await db.commit()
    await engine.dispose()
    monkeypatch.setattr(collector, 'Settings', lambda: SimpleNamespace(database_url=url))
    await collector.saved_answers(CHOSEN, datetime(2026, 10, 21, tzinfo=UTC), datetime(2026, 10, 22, tzinfo=UTC))
    output = capsys.readouterr().out
    assert 'current question' in output and 'OLD_PRIVATE_TEXT' not in output
    assert 'Can Product AX be shipped?' in output
    assert 'EXCLUDED_CONTEXT_MUST_NOT_APPEAR' not in output
    assert 'PRIVATE_DEBUG_DO_NOT_PRINT' not in output
    check = json.loads(next(row.split('=', 1)[1] for row in output.splitlines() if row.startswith('answer_check=')))
    assert check['response_id'] == 'checked-response' and check['omitted'] == 1
    assert len(check['condition_failures']) == 3
    assert '"new_sources":0,"retained_sources":5' in output
    admitted = json.loads(next(row.split('=', 1)[1] for row in output.splitlines() if row.startswith('admitted_evidence=')))
    assert admitted['items'] == [{'citation_id': 'C1', 'text': 'Ship locally only.', 'conditions': {'product_model': 'AX'}}]
    assert admitted['omitted'] == 0 and admitted['coverage'] == 'manual_review_required'
    monkeypatch.setattr(collector, 'inspect_prefix', lambda text, cid, case, transcripts, fixtures, **kwargs: {
        'transcripts': transcripts, 'case': case, **kwargs})
    prefix = await collector.saved_prefix('logs', CHOSEN, datetime(2026, 10, 21, tzinfo=UTC),
                                          datetime(2026, 10, 22, tzinfo=UTC), 'greeting')
    assert prefix['transcripts'] == [{'item_id': '21', 'text': 'current question'}]
    assert prefix['records_truncated'] is False


@pytest.fixture
def fake_docker(tmp_path):
    docker = tmp_path / 'docker'
    docker.write_text(f'#!{sys.executable}\n' + '''import hashlib, os, pathlib, sys
args = sys.argv[1:]
with open(os.environ['DOCKER_TRACE'], 'a') as f:
    f.write(repr(args) + '\\n')
mode = os.environ.get('FAKE_MODE', '')
root = pathlib.Path(os.environ['REPO_ROOT'])
if args[0] == 'compose':
    print('api-container')
elif args[0] == 'inspect':
    print('runtime container=api-container image=fake')
elif args[0] == 'logs':
    print('2026-10-21T00:00:00Z voice_input_transcribed conversation_id=11111111-1111-1111-1111-111111111111 input_item_id=i1')
elif 'sha256sum' in args:
    path = args[-1]
    if path.startswith('/app/validation/native-cuekb/'):
        path = root / 'artifacts/native-cuekb' / pathlib.Path(path).name
    elif path.startswith('/app/'):
        path = root / path[5:]
    else:
        path = root / 'docs/voicechat' / path[1:]
    print(('bad' if mode == 'mismatch' else hashlib.sha256(path.read_bytes()).hexdigest()) + '  file')
elif '/app/scripts/run_native_cuekb_validation.py' in args:
    print('preflight=passed')
    print('validation=failed fault=EXAMPLE')
    sys.exit(1)
elif '/app/scripts/collect_validation_summary.py' in args:
    sys.stdin.read()
    print('collection=complete business_acceptance=unassessed')
elif '-c' in args:
    code = args[args.index('-c') + 1]
    if '/v1/realtime/health' in code:
        if mode == 'voice-unready':
            sys.exit(1)
    elif 'json.load' in code:
        if mode == 'no-report':
            print('not a report', file=sys.stderr)
            sys.exit(1)
        print('2026-10-21T00:00:00+00:00 2026-10-21T00:01:00+00:00 11111111-1111-1111-1111-111111111111')
    elif 'datetime.now' in code:
        print('2026-10-21T00:00:00+00:00')
''')
    docker.chmod(0o755)
    return {**os.environ, 'PATH': str(tmp_path) + ':' + os.environ['PATH'], 'TMPDIR': str(tmp_path),
            'DOCKER_TRACE': str(tmp_path / 'trace'), 'REPO_ROOT': str(ROOT)}


def run_host(env, *args):
    return subprocess.run(['bash', str(ROOT / 'scripts/validate_deployment.sh'), *args],
                          env=env, capture_output=True, text=True, timeout=15)


def test_failed_validation_still_collects_matching_window(fake_docker):
    result = run_host(fake_docker, 'auto')
    assert result.returncode == 1
    assert 'host_exit_code=1' in result.stdout
    assert 'collection=complete' in result.stdout
    assert 'operation=auto case=golden_yolk' in result.stdout
    assert 'Traceback' not in result.stdout
    trace = Path(fake_docker['DOCKER_TRACE']).read_text()
    assert '--conversation-id' in trace and '--window-start' in trace


def test_core_run_adds_same_connection_followup_and_collects_both_products_once(fake_docker):
    result = run_host(fake_docker, 'auto', 'core')
    assert result.returncode == 1  # The mock fails; collecting both must not hide it.
    assert result.stdout.count('operation=auto case=') == 2
    trace = Path(fake_docker['DOCKER_TRACE']).read_text()
    assert trace.count("'--case-id', 'golden_yolk'") == 1
    assert trace.count("'--case-id', 'black_pig_crisp_heart'") == 1
    assert trace.count("'--followup-case-id', 'greeting'") == 1


def test_prefix_runs_each_small_case_once_and_collects_even_after_failure(fake_docker):
    result = run_host(fake_docker, 'prefix')
    assert result.returncode == 1
    assert 'operation=prefix case=greeting' in result.stdout
    assert 'operation=prefix case=general_tech' in result.stdout
    trace = Path(fake_docker['DOCKER_TRACE']).read_text()
    assert trace.count("'--case-id', 'greeting'") == 1
    assert trace.count("'--case-id', 'general_tech'") == 1
    assert trace.count("'--input-prefix-case'") == 2
    assert "'--case-id', 'golden_yolk'" not in trace
    assert 'docker cp' not in trace and 'Traceback' not in result.stdout


def test_web_prefix_marker_keeps_case_and_mode_in_one_window(fake_docker):
    assert run_host(fake_docker, 'web-start', 'greeting', 'prefix').returncode == 0
    assert run_host(fake_docker, 'web-end').returncode == 0
    trace = Path(fake_docker['DOCKER_TRACE']).read_text()
    assert "'--input-prefix-case', 'greeting'" in trace


def test_missing_current_report_never_reads_old_report_or_collects(fake_docker):
    result = run_host({**fake_docker, 'FAKE_MODE': 'no-report'}, 'auto', 'black_pig_crisp_heart')
    assert result.returncode == 1 and 'NO_CURRENT_REPORT' in result.stdout
    trace = Path(fake_docker['DOCKER_TRACE']).read_text()
    assert "['logs'" not in trace
    assert '/tmp/native-cuekb/report.json' not in trace


def test_mismatch_stops_before_audio_and_pending_web_blocks_auto(fake_docker):
    mismatch = run_host({**fake_docker, 'FAKE_MODE': 'mismatch'}, 'auto')
    assert mismatch.returncode == 2 and 'files_not_current' in mismatch.stdout
    assert '--case-id' not in Path(fake_docker['DOCKER_TRACE']).read_text()
    assert run_host(fake_docker, 'web-start', 'golden_yolk').returncode == 0
    blocked = run_host(fake_docker, 'auto')
    assert blocked.returncode == 2 and 'web_capture_pending' in blocked.stdout
    ended = run_host(fake_docker, 'web-end')
    assert ended.returncode == 0 and 'collection=complete' in ended.stdout
    assert 'web_capture_not_started' in run_host(fake_docker, 'web-end').stdout


def test_timing_reports_first_reply_after_last_result_and_counts_later_reopenings(capsys):
    events = [
        ('00:00:01', f'voice_audio_first_sent conversation_id={CHOSEN} response_id=early input_item_id=i1'),
        ('00:00:02', f'voice_response_completed conversation_id={CHOSEN} response_id=early input_item_id=i1 phase=answer samples=100'),
        ('00:00:03', f'voice_tool_call_received conversation_id={CHOSEN} call_id=c1 tool=search_knowledge_base input_item_id=i1'),
        ('00:00:03', f'voice_tool_result_submitted conversation_id={CHOSEN} call_id=c1 bytes=200'),
        ('00:00:04', f'voice_audio_first_sent conversation_id={CHOSEN} response_id=final input_item_id=i1'),
        ('00:00:05', f'voice_text_first_sent conversation_id={CHOSEN} response_id=final input_item_id=i1'),
        ('00:00:08', f'voice_response_completed conversation_id={CHOSEN} response_id=final input_item_id=i1 phase=answer samples=300'),
        ('00:00:09', f'voice_audio_first_sent conversation_id={CHOSEN} response_id=repeat input_item_id=i1'),
        ('00:00:12', f'voice_response_completed conversation_id={CHOSEN} response_id=repeat input_item_id=i1 phase=answer samples=300'),
    ]
    collector.log_summary('\n'.join(f'2026-10-21T{at}Z {row}' for at, row in events), CHOSEN)
    output = capsys.readouterr().out
    assert '"answer_audio":"2026-10-21T00:00:04Z"' in output
    assert '"text_before_done_ms":3000' in output
    assert '"answer_responses":3' in output
    assert '"additional_answers_after_result":1' in output
    assert '"last_result_to_audio_ms":1000' in output
    assert '"last_answer_done":"2026-10-21T00:00:12Z"' in output


def test_unready_voicechat_stops_before_sending_audio(fake_docker):
    result = run_host({**fake_docker, 'FAKE_MODE': 'voice-unready'}, 'auto')
    assert result.returncode == 2
    assert 'stage=voicechat_health' in result.stdout
    assert '--case-id' not in Path(fake_docker['DOCKER_TRACE']).read_text()
