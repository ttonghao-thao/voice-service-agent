"""Portal WS -> real Sophon adapter -> controlled WS and CueKB HTTP fixtures."""

import asyncio
import base64
import json
import struct
import time
from threading import Event

import httpx
import pytest
from app.main import create_app
from app.tools.adapters import CueKBAdapter
from fastapi.testclient import TestClient
from test_native_cuekb import configuration, evidence_v21, validate_v21
from websockets.asyncio.server import serve

VOICE = base64.b64encode(struct.pack("<1920h", *[1800] * 1920)).decode()
QUIET = base64.b64encode(bytes(3840)).decode()
QUESTION = "企业资料里 AX 网关经常离线是什么原因？"
ANSWER = "AX 网关可能因网线松动离线。"


@pytest.mark.parametrize("scenario", [
    "answer", "progress_then_answer", "barge_after_ack", "unmatched_ack", "insufficient", "unsupported", "missing_task",
    "missing_asr", "ambiguous_input", "stale_evidence", "superseded",
])
def test_sophon_voice_to_evidence_and_commentary(tmp_path, scenario):
    settings = configuration(tmp_path, voice_provider="sophon_live",
                             sophon_live_ws_url="ws://127.0.0.1:1/v1/live/sessions",
                             voice_native_answer_timeout_seconds=1 if scenario == "unmatched_ack" else 60)
    app = create_app(settings)
    calls, results, validated = [], [], []
    expected = "CUEKB: deliberately different model text"
    greeting = []
    query_started = Event()

    async def cuekb(request):
        if request.method == "GET":
            return httpx.Response(200, json=[])
        body = json.loads(request.content)
        if request.url.path.endswith("/query"):
            calls.append(body)
            query_started.set()
            if scenario == "superseded":
                await asyncio.sleep(.4)
            return httpx.Response(200, json=evidence_v21(
                body, statement=ANSWER, decision="insufficient" if scenario == "insufficient" else "answer"))
        assert request.url.path == "/v2/evidence/validate"
        validated.append(body)
        result = validate_v21(body, calls[0]["kb_ids"])
        if scenario == "stale_evidence":
            result["status"] = "stale"
        return httpx.Response(200, json=result)

    async def sophon(ws):
        assert ws.request.path == "/v1/live/sessions"
        assert "authorization" not in ws.request.headers
        handshake = json.loads(await ws.recv())
        assert handshake["type"] == "session.start"
        assert "中文" in handshake["session"]["instructions"]
        assert handshake["session"]["delegation"] == {"type": "client"}
        assert handshake["session"]["store"] is False
        await ws.send(json.dumps({"type": "session.started",
                                  "session": {**handshake["session"], "status": "active", "id": "sess_test"}}))
        ms, count, next_start = 0, 0, None

        async def send(kind, **body):
            await ws.send(json.dumps({"type": "session." + kind, **body}))

        async def audio(value):
            nonlocal ms
            await send("output_audio.delta", delta=value)
            ms += 80

        # First response is interrupted by the first user input. The client
        # leaves a short silence mid-question without creating a second turn.
        await audio(VOICE)
        try:
            async with asyncio.timeout(5):
                async for raw in ws:
                    event = json.loads(raw)
                    if event["type"] == "session.input_audio.append":
                        assert len(base64.b64decode(event["audio"])) == 3840
                        count += 1
                        if count == 13:
                            next_start = ms
                        await audio(VOICE if count <= 2 else QUIET)
                        if count == (24 if scenario == "ambiguous_input" else 12):
                            if scenario != "missing_asr":
                                await send("input_transcript.delta", delta=QUESTION[:8], start_ms=80, end_ms=260)
                            task = {"unsupported": "UNSUPPORTED: 查天气", "missing_task": None}.get(scenario, expected)
                            call = {"id": "item_first", "type": "delegation", "target": "client", "task": task}
                            await send("delegation.created", offset_ms=ms, delegation=call)
                            # Same ID and task are idempotent even with a later timestamp.
                            await send("delegation.created", offset_ms=ms+80, delegation=call)
                            if scenario != "missing_asr":
                                await asyncio.sleep(.04)
                                await send("input_transcript.delta", delta=QUESTION[8:], start_ms=260,
                                           end_ms=380 if scenario == "ambiguous_input" else 550)
                            if scenario == "ambiguous_input":
                                await send("input_transcript.delta", delta="另一条用户输入。", start_ms=1280, end_ms=1540)
                        if scenario in {"answer", "barge_after_ack"} and count == 24:
                            await send("input_transcript.delta", delta="谢谢。", start_ms=next_start,
                                       end_ms=next_start + 310)
                            await audio(VOICE)
                            await send("output_transcript.delta", delta="不客气。", start_ms=ms-80, end_ms=ms)
                            for _ in range(12):
                                await audio(QUIET)
                                await asyncio.sleep(.025)
                    if event["type"] == "session.commentary.append":
                        results.append(event)
                        await send("commentary.appended", start_ms=ms, end_ms=ms,
                                   client_event_id="not-the-result" if scenario == "unmatched_ack" else event["event_id"])
                        if scenario == "barge_after_ack":
                            continue
                        for _ in range(4):
                            await audio(QUIET)
                        spoken = ["目前没有足够资料。" if scenario == "insufficient" else ANSWER]
                        if scenario == "progress_then_answer":
                            spoken.insert(0, "我还在查，稍等一下。")
                        for line in spoken:
                            await audio(VOICE)
                            await send("output_transcript.delta", delta=line, start_ms=ms-80, end_ms=ms)
                            for _ in range(12):
                                await audio(QUIET)
                                await asyncio.sleep(.012)
                    if event["type"] == "session.close":
                        return
        except TimeoutError:
            await ws.close()

    async def start():
        return await serve(sophon, "127.0.0.1", 0)

    with TestClient(app) as client:
        backend = httpx.AsyncClient(transport=httpx.MockTransport(cuekb))
        app.state.registry.adapters["cuekb_http"] = CueKBAdapter(settings, backend)
        server = client.portal.call(start)
        settings.sophon_live_ws_url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}/v1/live/sessions"
        try:
            cid = client.post("/api/v1/conversations", json={}).json()["id"]
            issued = client.post(f"/api/v1/conversations/{cid}/voice-sessions", json={}).json()
            epoch = issued["epoch"]
            with client.websocket_connect(issued["ws_url"], headers={"Origin": settings.public_origin}) as ws:
                assert ws.receive_json()["type"] == "portal.session.ready"
                greeting.append(ws.receive_json()["payload"]["response_id"])
                def append(seq, audio):
                    ws.send_json({"type": "portal.audio.append", "epoch": epoch, "seq": seq, "payload": {
                        "audio": audio, "sample_rate": 24000, "format": "pcm16",
                    }})

                voiced_seq = (0, 1, 2, 3, 15, 16, 17, 18) if scenario == "ambiguous_input" else (0, 1, 4, 5)
                for seq in range(24 if scenario == "ambiguous_input" else 12):
                    append(seq, VOICE if seq in voiced_seq else QUIET)
                    time.sleep(.065)
                if scenario == "barge_after_ack":
                    for _ in range(60):
                        history = client.get(f"/api/v1/conversations/{cid}/messages").json()
                        if any(row["kind"] == "sophon_commentary_ack" for row in history["records"]):
                            break
                        time.sleep(.03)
                    else:
                        pytest.fail("Commentary receipt was not recorded")
                    for seq in range(12, 24):
                        append(seq, VOICE if seq < 16 else QUIET)
                        time.sleep(.065)
                if scenario == "superseded":
                    assert query_started.wait(2)
                    for seq in range(12, 14):
                        append(seq, VOICE)
                    for seq in range(14, 19):
                        append(seq, QUIET)
                        time.sleep(.055)
                events = []
                segment_count = 0
                for _ in range(50):
                    event = ws.receive_json()
                    assert event["type"] != "portal.error", event
                    events.append(event)
                    if event["type"] == "portal.audio.done":
                        segment_count += 1
                        if segment_count == (2 if scenario == "progress_then_answer" else 1):
                            break
                assert any(event["type"] == "portal.playback.clear" for event in events)
                assert events[-1]["type"] == "portal.audio.done"
                assert events[-1]["payload"]["response_id"] != greeting[0]
                cutoff = next(i for i, event in enumerate(events) if event["type"] == "portal.playback.clear")
                assert all(event["payload"].get("response_id") != greeting[0] for event in events[cutoff:])
                assert len(results) == 1 and results[0]["delegation_id"] == "item_first"
                if scenario in {"missing_asr", "ambiguous_input"}:
                    assert not calls and "重述" in results[0]["content"]
                elif scenario not in {"unsupported", "missing_task", "barge_after_ack"}:
                    assert [event["payload"]["text"] for event in events if event["type"] == "portal.transcript.done"] == [QUESTION]
                if scenario == "unsupported":
                    assert not calls and "不受支持" in results[0]["content"]
                if scenario == "missing_task":
                    assert not calls and "澄清" in results[0]["content"]
                if scenario == "barge_after_ack":
                    assert len(calls) == len(validated) == 1
                    captions = [e["payload"] for e in events if e["type"] == "portal.speech_text.done"]
                    assert [(p["text"], p["phase"]) for p in captions] == [("不客气。", "candidate")]
                    assert history["items"][0]["answer"]["reason_code"] == "VOICE_ANSWER_UNVERIFIED"
                if scenario in {"answer", "progress_then_answer", "unmatched_ack", "insufficient", "stale_evidence", "superseded"}:
                    assert len(calls) == len(validated) == 1
                    assert calls[0]["question"] == QUESTION
                    assert calls[0]["search_query"].startswith(QUESTION)
                    assert "deliberately" not in results[0]["content"]
                if scenario in {"answer", "progress_then_answer", "unmatched_ack", "insufficient"}:
                    packet = json.loads(results[0]["content"].split("\n", 1)[1])
                    assert packet["decision"] == ("insufficient" if scenario == "insufficient" else "answer")
                if scenario == "stale_evidence":
                    assert ANSWER not in results[0]["content"] and "无法取得" in results[0]["content"]
                if scenario == "superseded":
                    assert ANSWER not in results[0]["content"] and "不要播报旧问题" in results[0]["content"]
                if scenario in {"answer", "progress_then_answer", "unmatched_ack", "insufficient"}:
                    for _ in range(50):
                        history = client.get(f"/api/v1/conversations/{cid}/messages").json()
                        if history["items"][0].get("answer"):
                            break
                        time.sleep(.03)
                    answer = history["items"][0]["answer"]
                    assert answer["status"] == "failed"
                    assert answer["reason_code"] == (
                        "VOICE_COMMENTARY_UNCONFIRMED" if scenario == "unmatched_ack" else "VOICE_ANSWER_UNVERIFIED")
                    assert answer["speech_text"] == ""
                    assert [c["content"] for c in answer["citations"]] == ([] if scenario == "insufficient" else [ANSWER])
                    assert answer["validation_level"] == "provider_only"
                    assert not any(row["kind"] == "sophon_answer_observed" for row in history["records"])
                    if scenario != "unmatched_ack":
                        assert any(row["kind"] == "sophon_commentary_ack" for row in history["records"])
                    if scenario == "progress_then_answer":
                        captions = [e["payload"]["text"] for e in events if e["type"] == "portal.speech_text.done"]
                        assert captions == ["我还在查，稍等一下。", ANSWER]
                        assert all(e["payload"]["phase"] == "candidate" for e in events
                                   if e["type"] in {"portal.speech_text.done", "portal.audio.done"})
                if scenario == "answer":
                    rid = events[-1]["payload"]["response_id"]
                    played = [event for event in events if event["type"] == "portal.audio.delta"
                              and event["payload"]["response_id"] == rid]
                    ws.send_json({"type": "portal.playback.ack", "epoch": epoch, "payload": {
                        "response_id": rid, "finished": True,
                        "played_samples": sum(len(base64.b64decode(e["payload"]["audio"])) // 2 for e in played),
                    }})
                    for seq in range(12, 24):
                        append(seq, VOICE if seq < 16 else QUIET)
                        time.sleep(.065)
                    next_events = []
                    for _ in range(30):
                        event = ws.receive_json()
                        assert event["type"] != "portal.error", event
                        next_events.append(event)
                        if event["type"] == "portal.audio.done":
                            break
                    assert any(e["type"] == "portal.speech_text.done" and e["payload"]["text"] == "不客气。"
                               and e["payload"]["answer_kind"] == "general" for e in next_events)
                    assert len(results) == len(calls) == 1
                ws.send_json({"type": "portal.session.close", "epoch": epoch})
        finally:
            client.portal.call(backend.aclose)
            client.portal.call(server.close)
            client.portal.call(server.wait_closed)
