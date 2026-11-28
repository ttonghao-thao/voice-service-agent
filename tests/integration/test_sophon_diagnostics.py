"""Check the diagnostic projection through the Room, persistence and portal."""

import asyncio
import base64
import json
import logging
import struct
import time
from types import SimpleNamespace

import httpx
import pytest
from app.api.routes import events
from app.contracts import SOPHON_INTERACTION_EVENT, SOPHON_INTERACTION_RECORD, portal_server_event_adapter
from app.main import create_app
from app.voice.livekit_room import INPUT_LOOKBACK, RoomAgent, RoomCall
from app.voice.sophon_gpt_live import sophon_gpt_live
from pydantic import SecretStr
from test_livekit_delegation import settings
from websockets.asyncio.server import serve


@pytest.fixture
async def diagnostic_room(tmp_path):
    config = settings(tmp_path)
    config.auth_mode = "validation"
    app = create_app(config)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            conversation = (await client.post("/api/v1/conversations", json={})).json()
            client.headers["Authorization"] = "Bearer " + conversation["access_token"]
            cid = conversation["id"]
            issued = (await client.post(f"/api/v1/conversations/{cid}/voice-sessions")).json()
            call = RoomCall(app.state.voice, None, app.state.voice.sessions[issued["voice_session_id"]])
            try:
                yield call, client, app
            finally:
                await call.room.disconnect()


def native(task, call_id="d001", **extra):
    return {"type": "session.delegation.created",
            "delegation": {"id": call_id, "type": "delegation", "target": "client", "task": task, **extra}}


async def history(call, client, kind):
    response = await client.get(f"/api/v1/conversations/{call.cid}/messages")
    response.raise_for_status()
    return [row for row in response.json()["records"]
            if row["kind"] == SOPHON_INTERACTION_RECORD and row["payload"]["kind"] == kind]


@pytest.mark.parametrize("raw_task,received", [
    ({"credential": "fixture-hidden-nested-value"}, "object"),
    (None, "null"),
    (False, "boolean"),
    ("x" * 2051, "string"),
])
async def test_bad_task_keeps_original_shape_before_normalization(diagnostic_room, raw_task, received):
    call, client, _ = diagnostic_room
    call.input_state(True)
    call.input_state(False)
    call.receive(native(raw_task))
    call.delegate(SimpleNamespace(id="d001"))
    assert not call.closed.is_set()
    assert await call.diagnostics.flush()
    (row,) = await history(call, client, "delegation")
    diagnostic = row["payload"]
    assert diagnostic["input_item_id"] == call.inputs.latest.id
    assert diagnostic["structure"] == "invalid"
    assert diagnostic["field_issues"][0]["path"] == "delegation.task"
    assert diagnostic["field_issues"][0]["received"] == received
    assert diagnostic["intent"] is None
    assert "fixture-hidden-nested-value" not in json.dumps(row)
    assert "x" * 2051 not in json.dumps(row)


async def test_missing_id_and_protocol_close_are_persisted(diagnostic_room):
    call, client, _ = diagnostic_room
    broken = native("WEATHER: Beijing")
    del broken["delegation"]["id"]
    call.receive(broken)
    assert call.closed.is_set() and call.error.code == "VOICE_PROTOCOL_ERROR"
    assert call.calls.empty()
    await call.finish_diagnostics()
    (row,) = await history(call, client, "delegation")
    assert row["source_id"] == row["payload"]["interaction_id"]
    assert row["payload"]["delegation_id"] is None
    assert row["payload"]["input_item_id"] is None
    assert row["payload"]["field_issues"][0]["received"] == "missing"
    assert row["payload"]["reason_code"] == "VOICE_PROTOCOL_ERROR"


async def test_official_gpt_live_sdk_keeps_bad_wire_task_and_settles_once(diagnostic_room):
    call, client, _ = diagnostic_room
    trigger = asyncio.Event()
    commentary = []

    async def gateway(ws):
        start = json.loads(await ws.recv())
        session = {**start["session"], "id": "controlled-sophon", "status": "active", "store": False}
        await ws.send(json.dumps({"type": "session.started", "session": session}))
        await trigger.wait()
        raw = json.dumps(native({"bad": "task was an object"}))
        await ws.send(raw)
        await ws.send(raw)
        async for raw in ws:
            event = json.loads(raw)
            if event["type"] == "session.commentary.append":
                commentary.append(event["delegation_id"])
                await ws.send(json.dumps({"type": "session.commentary.appended",
                                          "client_event_id": event["event_id"]}))
            elif event["type"] == "session.close":
                await ws.send(json.dumps({"type": "session.closed",
                                          "session": {**session, "status": "closed"}}))
                return

    async with serve(gateway, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        async with sophon_gpt_live(f"ws://127.0.0.1:{port}/v1/live/sessions", "") as model:
            duplex = model.session()

            class TestAgent(RoomAgent):
                @property
                def duplex_session(self):
                    return duplex

            await TestAgent(call, "SDK test").on_enter()
            await duplex._update_session(instructions="SDK test", tools=[])
            worker = asyncio.create_task(call.run_tools())
            try:
                async with asyncio.timeout(4):
                    await call.ready.wait()
                    call.input_state(True)
                    call.input_state(False)
                    trigger.set()
                    while not any(item.status == "finished" for item in call.diagnostics.delegations.values()):
                        call.diagnostics.changed.clear()
                        await call.diagnostics.changed.wait()
            finally:
                trigger.set()
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)
                await duplex.aclose()
    await call.finish_diagnostics()
    (row,) = await history(call, client, "delegation")
    item = row["payload"]
    assert item["structure"] == "invalid" and item["field_issues"][0]["received"] == "object"
    assert item["duplicates"] == 1 and commentary == ["d001"]
    assert item["intent"]["reason_code"] == "invalid_task"
    assert item["tool_executions"] == []
    assert item["delivery"]["status"] == "gateway_received"


async def test_raw_repeats_and_conflicts_do_not_enqueue_two_calls(diagnostic_room):
    call, client, _ = diagnostic_room
    call.input_state(True)
    call.input_state(False)
    call.receive(native({}))
    call.delegate(SimpleNamespace(id="d001"))
    call.receive(native({}))
    call.delegate(SimpleNamespace(id="d001"))
    call.receive(native(None))
    call.delegate(SimpleNamespace(id="d001"))
    assert call.calls.qsize() == 1
    await call.finish_diagnostics()
    first, conflict = [row["payload"] for row in await history(call, client, "delegation")]
    assert first["duplicates"] == 1
    assert conflict["reason_code"] == "CONFLICTING_DELEGATION_ID"
    assert conflict["field_issues"][0]["received"] == "null"


@pytest.mark.parametrize("scenario,expected", [
    ("voiced_reply", ["sophon_replied"]),
    ("silence", ["no_reply_or_delegation"]),
    ("welcome_overlaps_input", ["attribution_uncertain"]),
    ("competing_inputs", ["attribution_uncertain", "attribution_uncertain"]),
    ("disconnect", ["observation_interrupted"]),
])
async def test_input_observation_without_asr(diagnostic_room, monkeypatch, scenario, expected):
    call, client, _ = diagnostic_room
    clock = [time.monotonic()]
    monkeypatch.setattr("app.voice.livekit_room.time", SimpleNamespace(monotonic=lambda: clock[0]))
    voiced = base64.b64encode(struct.pack("<h", 1000) * 480).decode()
    silence = base64.b64encode(bytes(960)).decode()
    if scenario == "welcome_overlaps_input":
        call.receive({"type": "session.output_audio.delta", "delta": voiced})
        assert not call.diagnostics.inputs
    call.input_state(True)
    clock[0] += .5
    call.input_state(False)
    if scenario == "competing_inputs":
        clock[0] += 1
        call.input_state(True)
        call.input_state(False)
    await call.diagnostics.flush()
    assert {row["payload"]["status"] for row in await history(call, client, "input")} == {"awaiting_sophon"}
    if scenario == "disconnect":
        await call.finish_diagnostics()
    else:
        call.receive({"type": "session.output_audio.delta", "delta": silence if scenario == "silence" else voiced})
        clock[0] += INPUT_LOOKBACK + 1
        call.advance_observations()
        await call.diagnostics.flush()
    rows = await history(call, client, "input")
    assert [row["payload"]["status"] for row in rows] == expected
    assert all(row["payload"]["observation_complete"] for row in rows)
    if "attribution_uncertain" in expected:
        assert len(await history(call, client, "unassigned")) == 1


async def test_wrong_qwen_route_and_missing_gateway_ack_are_independently_visible(diagnostic_room):
    call, client, app = diagnostic_room
    call.settings.delegation_router_provider = "openai_compatible"
    call.settings.delegation_router_base_url = "https://intent.fixture/v1"
    call.settings.delegation_router_api_key = SecretStr("intent-fixture")
    call.settings.delegation_router_model = "test-router"
    task = "Check the weather in Beijing tomorrow"
    submitted = []

    def upstream(request):
        submitted.append(request.url.host)
        if request.url.host == "intent.fixture":
            return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {
                "content": json.dumps({"route": "WEB_SEARCH", "query": task, "place": None,
                                       "date": None, "units": None, "freshness": None}),
            }}]})
        return httpx.Response(200, json={"results": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as fixture:
        app.state.registry.client = fixture
        app.state.registry.adapters["tavily_http"].client = fixture
        call.input_state(True)
        call.input_state(False)
        appended = []
        call.duplex = SimpleNamespace(append_commentary=lambda content, **kwargs: appended.append(kwargs))
        call.receive(native(f"  {task}  "))
        call.delegate(SimpleNamespace(id="d001"))
        worker = asyncio.create_task(call.run_tools())
        try:
            observed = call.raw_tasks["d001"].observed
            async with asyncio.timeout(5):
                while observed.status != "finished":
                    call.diagnostics.changed.clear()
                    await call.diagnostics.changed.wait()
            assert submitted == ["intent.fixture", "api.tavily.com"] and len(appended) == 1
            assert await call.diagnostics.flush()
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
    (row,) = await history(call, client, "delegation")
    item = row["payload"]
    assert item["native_event"]["delegation"]["task"] == f"  {task}  "
    assert item["intent"]["initial_route"] == item["intent"]["route"] == "WEB_SEARCH"
    assert item["intent"]["parameters"]["query"] == task
    assert item["tool_executions"][0]["result_status"] == "no_results"
    assert item["result_status"] == "insufficient_evidence"
    assert item["delivery"]["status"] == "receipt_unknown"


@pytest.mark.parametrize("tool_failure", ["disabled", "invalid_output"])
async def test_observation_distinguishes_a_blocked_tool_from_an_adapter_failure(
    diagnostic_room, monkeypatch, tool_failure,
):
    call, client, app = diagnostic_room
    if tool_failure == "disabled":
        call.settings.enabled_tools = "search_knowledge,web_search"
    else:
        async def invalid_output(args, ctx):
            return []

        monkeypatch.setattr(app.state.registry.adapters["weather_http"], "invoke", invalid_output)
    call.input_state(True)
    call.input_state(False)

    def reply(content, *, delegation_id):
        call.sent({"type": "session.commentary.append", "delegation_id": delegation_id, "event_id": "reply"})
        call.receive({"type": "session.commentary.appended", "client_event_id": "reply"})

    call.duplex = SimpleNamespace(append_commentary=reply)
    call.receive(native("WEATHER: Beijing | today"))
    call.delegate(SimpleNamespace(id="d001"))
    worker = asyncio.create_task(call.run_tools())
    try:
        observed = call.raw_tasks["d001"].observed
        async with asyncio.timeout(2):
            while observed.status != "finished":
                call.diagnostics.changed.clear()
                await call.diagnostics.changed.wait()
        assert await call.diagnostics.flush()
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
    (row,) = await history(call, client, "delegation")
    item = row["payload"]
    assert item["intent"]["selected_tool"] == "weather"
    if tool_failure == "disabled":
        assert item["tool_executions"] == []
        assert item["result_reason"] == "FORBIDDEN"
        assert item["delivery"]["kind"] == "disposition"
    else:
        (adapter,) = item["tool_executions"]
        assert adapter["entered"] and adapter["status"] == "failed"
        assert adapter["reason_code"] == item["result_reason"] == "TOOL_BAD_RESPONSE"
        assert item["delivery"]["kind"] == "tool_result"


async def test_replay_rechecks_scope_and_logs_exclude_task_text(diagnostic_room, monkeypatch, caplog):
    call, client, app = diagnostic_room
    monkeypatch.setattr(logging.getLogger("app"), "propagate", True)
    with caplog.at_level(logging.INFO):
        call.receive(native("Find the release notes", extra_field="do-not-echo"))
        call.receive(native("Look up API key sk-synthetic-example", call_id="d002"))
        await call.finish_diagnostics()
    rows = await history(call, client, "delegation")
    assert rows[0]["payload"]["native_event"]["delegation"]["task"] == "Find the release notes"
    assert rows[0]["payload"]["omitted_fields"] == 1
    assert rows[1]["payload"]["redacted_fields"] == ["delegation.task"]
    assert "Find the release notes" not in caplog.text
    assert "sk-synthetic-example" not in json.dumps(rows) + caplog.text
    assert "do-not-echo" not in json.dumps(rows) + caplog.text
    assert '"tool_executed": false' in caplog.text

    async def connected():
        return False

    request = SimpleNamespace(app=app, path_params={"cid": call.cid}, headers=client.headers,
                              url=SimpleNamespace(path="/api/v1/conversations/fixture/events"),
                              is_disconnected=connected)
    original_scope = call.session.principal
    cursor = str(rows[0]["server_seq"] - 1)
    first_stream = (await events(call.cid, request, original_scope, after=0, last_event_id=cursor)).body_iterator
    frame = await asyncio.wait_for(anext(first_stream), 1)
    event = json.loads(frame.split("data: ")[1])
    assert event["type"] == SOPHON_INTERACTION_EVENT
    portal_server_event_adapter.validate_python(event)
    assert event["payload"]["native_event"] and "_required_scopes" not in event["payload"]
    assert event["server_seq"] == rows[0]["server_seq"]
    await first_stream.aclose()

    app.state.settings.knowledge_base_ids = ""
    with pytest.raises(StopAsyncIteration):
        stale_stream = (await events(call.cid, request, original_scope, after=0, last_event_id=cursor)).body_iterator
        await asyncio.wait_for(anext(stale_stream), 1)
    reduced_scope = await app.state.auth.principal(request)
    reduced = await history(call, client, "delegation")
    assert reduced[0]["payload"]["details_restricted"] and reduced[0]["payload"]["native_event"] is None
    replay = (await events(call.cid, request, reduced_scope, after=0, last_event_id=cursor)).body_iterator
    event = json.loads((await asyncio.wait_for(anext(replay), 1)).split("data: ")[1])
    assert event["payload"] == reduced[0]["payload"]
    await replay.aclose()


async def test_failed_diagnostic_write_preserves_pending_history(diagnostic_room, monkeypatch):
    call, client, app = diagnostic_room
    write = app.state.store.write_sophon_interactions

    async def fail(*args):
        raise RuntimeError("Temporary database failure")

    monkeypatch.setattr(app.state.store, "write_sophon_interactions", fail)
    call.input_state(True)
    assert not await call.diagnostics.flush()
    assert not call.closed.is_set()
    assert not await history(call, client, "input")
    monkeypatch.setattr(app.state.store, "write_sophon_interactions", write)
    await call.finish_diagnostics()
    (row,) = await history(call, client, "input")
    assert row["payload"]["status"] == "observation_interrupted"
