"""Network end-to-end tests with independently listening provider API fixtures."""

import asyncio
import base64
import json
import threading

import pytest
from app.storage.models import DeliveryAttempt, Event, Utterance
from sqlalchemy import event as sql_event
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from websockets.asyncio.client import connect
from websockets.exceptions import InvalidStatus

from scripts.simulate_full_flow import KB, PortalCall, SimulationHarness, VoicePlan


@pytest.fixture
async def simulation(tmp_path):
    async with SimulationHarness(tmp_path / "full-flow.db") as harness:
        yield harness


async def eventually(check, wait_seconds=5):
    async with asyncio.timeout(wait_seconds):
        while True:
            result = await check()
            if result:
                return result
            await asyncio.sleep(0.02)


async def sse_final(call, after=0):
    async with call.harness.client.stream(
        "GET", call.path + f"/events?after={after}", headers=call.headers
    ) as response:
        assert response.status_code == 200
        async for line in response.aiter_lines():
            if line.startswith("data:"):
                event = json.loads(line[5:])
                if event["type"] == "portal.answer.final":
                    return event
    raise AssertionError("SSE final not received")


SCENARIOS = [
    # name, provider controls, tool, status, reason, query/model counts
    ("direct", {}, "lookup_knowledge", "answered", None, 1, 0),
    ("reasoned", {}, "reason_over_knowledge", "answered", None, 1, 2),
    ("unassessed-escalation", {"evidence_status": "unassessed"}, "lookup_knowledge", "answered", None, 1, 1),
    (
        "conflict",
        {"evidence_status": "conflicting"},
        "lookup_knowledge",
        "insufficient_evidence",
        "CUEKB_EVIDENCE_UNSAFE",
        1,
        1,
    ),
    (
        "insufficient",
        {"evidence_status": "insufficient"},
        "lookup_knowledge",
        "insufficient_evidence",
        "CUEKB_EVIDENCE_UNSAFE",
        1,
        1,
    ),
    ("degraded", {"cuekb_status": "degraded"}, "lookup_knowledge", "answered", "CUEKB_DEGRADED", 1, 1),
    ("truncated", {"truncated": True}, "lookup_knowledge", "answered", None, 1, 1),
    ("too-many-hits", {"hit_count": 4}, "lookup_knowledge", "answered", None, 1, 1),
    (
        "not-found",
        {"cuekb_status": "not_found"},
        "lookup_knowledge",
        "insufficient_evidence",
        "CUEKB_NO_EVIDENCE",
        1,
        0,
    ),
    (
        "clarify",
        {"cuekb_status": "needs_clarification"},
        "lookup_knowledge",
        "needs_clarification",
        "CUEKB_NEEDS_CLARIFICATION",
        1,
        0,
    ),
    ("cuekb-401", {"cuekb_http_status": 401}, "lookup_knowledge", "failed", "CUEKB_AUTH_FAILED", 1, 0),
    ("cuekb-403", {"cuekb_http_status": 403}, "lookup_knowledge", "failed", "CUEKB_FORBIDDEN", 1, 0),
    ("cuekb-422", {"cuekb_http_status": 422}, "lookup_knowledge", "failed", "CUEKB_CONTRACT_ERROR", 1, 0),
    ("cuekb-429", {"cuekb_http_status": 429}, "lookup_knowledge", "failed", "CUEKB_RATE_LIMITED", 1, 0),
    ("cuekb-503-retry", {"cuekb_http_status": 503}, "lookup_knowledge", "failed", "TOOL_UNAVAILABLE", 2, 0),
    ("invalid-json", {"invalid_response": True}, "lookup_knowledge", "failed", "TOOL_BAD_RESPONSE", 1, 0),
    ("oversized-json", {"oversized_response": True}, "lookup_knowledge", "failed", "TOOL_BAD_RESPONSE", 1, 0),
    ("model-503", {"model_http_status": 503}, "reason_over_knowledge", "failed", "AGENT_FAILED", 0, 1),
    (
        "model-missed-tool",
        {"model_omit_tool": True},
        "reason_over_knowledge",
        "failed",
        "AGENT_REQUIRED_TOOL_NOT_CALLED",
        0,
        1,
    ),
    (
        "model-invalid-citation",
        {"invalid_citation": True},
        "reason_over_knowledge",
        "failed",
        "RAG_INVALID_CITATION",
        1,
        2,
    ),
]


@pytest.mark.parametrize(
    "name,controls,tool,status,reason,queries,models", SCENARIOS, ids=[row[0] for row in SCENARIOS]
)
async def test_network_voice_business_flow(
    simulation, record_property, name, controls, tool, status, reason, queries, models
):
    for key, value in controls.items():
        setattr(simulation.services, key, value)
    call = await PortalCall(simulation).create()
    await call.open_voice()
    plan = VoicePlan(tool=tool)
    await call.speak(plan)
    turn = await call.answer()
    await call.until("portal.audio.done", lambda event: event["payload"]["response_id"] == plan.response_id)
    final = await sse_final(call)
    assert final["turn_id"] == turn["id"]
    assert final["payload"]["status"] == turn["answer"]["status"] == status
    assert turn["answer"]["reason_code"] == reason
    assert turn["selected_tool"] == tool
    assert turn["user_text"] == plan.question  # final ASR, never rewritten tool arguments
    assert turn["input_item_id"] == plan.input_id
    assert len(simulation.services.queries) == queries
    assert len(simulation.services.model_calls) == models
    assert len(simulation.services.outputs) == 1
    update = simulation.services.updates[0]["session"]
    assert [item["name"] for item in update["tools"]] == ["lookup_knowledge", "reason_over_knowledge"]
    assert "tool_choice" not in update
    assert all(
        query["kb_ids"] == [KB] and query["query"] == plan.question for query in simulation.services.queries
    )
    if models == 1 and queries == 1:
        assert simulation.services.model_calls[0]["tool_choice"] == "auto"
    if name == "direct":
        assert turn["answer"]["composition"] == "nano_grounded"
        assert turn["answer"]["verification_timing"] == "after_audio"
        assert simulation.services.outputs[0]["result"]["status"] == "evidence_ready"
    audio = [event for event in call.events if event["type"] == "portal.audio.delta"]
    assert audio and len(base64.b64decode(audio[0]["payload"]["audio"])) == 3840
    async with simulation.app.state.store.sessions() as db:
        finals = (await db.execute(select(Event).where(Event.conversation_id == call.cid))).scalars().all()
        assert sum(event.payload["type"] == "portal.answer.final" for event in finals) == 1
        attempt = (
            await db.execute(select(DeliveryAttempt).where(DeliveryAttempt.conversation_id == call.cid,
                                                         DeliveryAttempt.kind == "tool_result"))
        ).scalar_one()
        assert attempt.status == "sent" and attempt.response_id == plan.response_id
    record_property("evidence_mode", "simulated_independent_apis")
    record_property("real_service", "false")
    record_property("cuekb_http_requests", queries)
    record_property("text_model_http_requests", models)
    await call.close()
    assert (await simulation.client.get(call.path + "/messages", headers=call.headers)).status_code == 401


async def test_general_answer_and_same_session_three_routes(simulation):
    call = await PortalCall(simulation).create()
    await call.open_voice()
    general = VoicePlan(question="What is MLO?", tool=None)
    await call.speak(general)
    await call.until(
        "portal.audio.done", lambda event: event["payload"]["response_id"] == general.response_id
    )
    history = await call.history()
    assert history["items"] == []
    assert len(simulation.services.queries) == len(simulation.services.model_calls) == 0
    async with simulation.app.state.store.sessions() as db:
        utterance = (
            await db.execute(select(Utterance).where(Utterance.conversation_id == call.cid))
        ).scalar_one()
        assert utterance.answer["answer_kind"] == "general"
        assert utterance.answer["validation_level"] == "provider_only"
    for tool in ("lookup_knowledge", "reason_over_knowledge"):
        plan = VoicePlan(tool=tool)
        await call.speak(plan)
        await call.until(
            "portal.audio.done",
            lambda event, expected=plan.response_id: event["payload"]["response_id"] == expected,
        )
        await eventually(lambda count=(1 if tool == "lookup_knowledge" else 2): has_turns(call, count))
    history = await call.history()
    assert [turn["selected_tool"] for turn in history["items"]] == [
        "lookup_knowledge",
        "reason_over_knowledge",
    ]
    assert len(simulation.services.queries) == len(simulation.services.model_calls) == 2
    await call.close()


async def has_turns(call, count):
    history = await call.history()
    return len(history["items"]) == count and all(turn["answer"] for turn in history["items"])


@pytest.mark.parametrize("mode,policy", [("legacy", None), ("dual_tools", "knowledge_required")])
async def test_legacy_and_strict_modes_use_external_chain(tmp_path, mode, policy):
    async with SimulationHarness(tmp_path / "mode.db", mode=mode, policy=policy) as simulation:
        call = await PortalCall(simulation).create()
        await call.open_voice()
        plan = VoicePlan(tool="consult_service_agent" if mode == "legacy" else "lookup_knowledge")
        await call.speak(plan)
        turn = await call.answer()
        assert turn["answer"]["status"] == "answered"
        assert turn["answer"]["composition"] == "external_llm"
        assert len(simulation.services.queries) == 1 and len(simulation.services.model_calls) == 2
        await call.close()


async def test_strict_mode_rejects_general_before_output(tmp_path):
    async with SimulationHarness(tmp_path / "strict.db", policy="knowledge_required") as simulation:
        call = await PortalCall(simulation).create()
        await call.open_voice()
        await call.speak(VoicePlan(tool=None, question="What is MLO?"))
        error = await call.until("portal.error")
        assert error["payload"]["code"] == "VOICE_TOOL_REQUIRED"
        assert not any(
            event["type"] in ("portal.audio.delta", "portal.speech_text.done") for event in call.events
        )
        assert not simulation.services.queries and not simulation.services.model_calls
        await call.close()


@pytest.mark.parametrize(
    "spoken,code",
    [
        ("Product AX supports 99 connections.", "VOICE_UNSUPPORTED_NUMBER"),
        ("Product AX supports 10 seconds.", "VOICE_UNSUPPORTED_UNIT"),
        ("It supports 10 connections.", "VOICE_MISSING_CONDITION"),
    ],
)
async def test_bad_native_answer_fails_and_clears_only_its_response(simulation, spoken, code):
    call = await PortalCall(simulation).create()
    await call.open_voice()
    plan = VoicePlan(spoken=spoken)
    await call.speak(plan)
    turn = await call.answer()
    assert turn["answer"]["status"] == "failed" and turn["answer"]["reason_code"] == code
    clear = await sse_type(
        call, "portal.playback.clear", lambda event: event["payload"].get("response_id") == plan.response_id
    )
    assert clear["payload"]["response_id"] == plan.response_id
    assert len(simulation.services.queries) == len(simulation.services.outputs) == 1
    await call.close()


async def sse_type(call, kind, predicate=lambda event: True):
    async with call.harness.client.stream("GET", call.path + "/events", headers=call.headers) as response:
        async for line in response.aiter_lines():
            if line.startswith("data:"):
                event = json.loads(line[5:])
                if event["type"] == kind and predicate(event):
                    return event
    raise AssertionError("SSE event not received: " + kind)


async def test_evidence_is_not_final_until_native_answer_and_duplicate_safe(simulation):
    call = await PortalCall(simulation).create()
    await call.open_voice()
    plan = VoicePlan(tool_before_asr=True, duplicate_tool=True, hold_reply=True)
    await call.speak(plan)
    await asyncio.wait_for(plan.output_received.wait(), 5)
    history = await call.history()
    assert len(history["items"]) == 1
    assert history["items"][0]["answer"] is None
    assert history["items"][0]["execution_phase"] == "awaiting_provider_answer"
    assert len(simulation.services.queries) == len(simulation.services.outputs) == 1
    plan.reply_release.set()
    assert (await call.answer())["answer"]["status"] == "answered"
    await call.close()


@pytest.mark.parametrize("tool", [None, "lookup_knowledge"])
async def test_native_missing_end_times_out(tmp_path, tool):
    async with SimulationHarness(tmp_path / "timeout.db", answer_timeout=150) as simulation:
        call = await PortalCall(simulation).create()
        await call.open_voice()
        plan = VoicePlan(tool=tool, no_answer_end=True)
        await call.speak(plan)
        if tool:
            answer = (await call.answer())["answer"]
            assert answer["status"] == "failed" and answer["reason_code"] == "VOICE_ANSWER_TIMEOUT"
        else:
            error = await call.until("portal.error")
            assert error["payload"]["code"] == "VOICE_ANSWER_TIMEOUT"
            assert (await call.history())["items"] == []
            assert not simulation.services.queries and not simulation.services.model_calls
        await call.close()


@pytest.mark.parametrize(
    "tool,args,code,results",
    [
        ("unregistered_tool", None, "VOICE_PROTOCOL_ERROR", 0),
        ("lookup_knowledge", {"user_request": "x", "kb_ids": [KB]}, "VOICE_TOOL_REQUIRED", 1),
    ],
)
async def test_gateway_rejects_invalid_native_calls(simulation, tool, args, code, results):
    call = await PortalCall(simulation).create()
    await call.open_voice()
    plan = VoicePlan(tool=tool, arguments=args)
    await call.speak(plan)
    error = await call.until("portal.error")
    assert error["payload"]["code"] == code
    assert not simulation.services.queries and not simulation.services.model_calls
    assert len(simulation.services.outputs) == results
    if results:
        assert simulation.services.outputs[0]["result"]["status"] == "failed"
    assert (await call.history())["items"] == []
    assert not any(
        event["type"] == "portal.audio.delta" and event["payload"]["response_id"] == plan.response_id
        for event in call.events
    )
    await call.close()


async def test_fabricated_filter_is_clarified_without_query(simulation):
    call = await PortalCall(simulation).create()
    await call.open_voice()
    await call.speak(VoicePlan(arguments={"user_request": "AX specs", "product_model": "ZX"}))
    turn = await call.answer()
    assert turn["answer"]["status"] == "needs_clarification"
    assert not simulation.services.queries and not simulation.services.model_calls
    await call.close()


async def test_acknowledgement_plays_while_independent_search_is_pending(simulation):
    simulation.services.cuekb_release = asyncio.Event()
    call = await PortalCall(simulation).create()
    await call.open_voice()
    plan = VoicePlan()
    await call.speak(plan)
    ack = await call.until("portal.speech_text.done", lambda event: event["payload"]["phase"] == "status")
    await call.until(
        "portal.audio.delta", lambda event: event["payload"]["response_id"] == ack["payload"]["response_id"]
    )

    async def search_started():
        return simulation.services.queries

    await eventually(search_started)
    assert simulation.services.outputs == []
    history = await call.history()
    assert history["items"][0]["answer"] is None
    assert history["items"][0]["execution_phase"] == "executing"
    simulation.services.cuekb_release.set()
    assert (await call.answer())["answer"]["status"] == "answered"
    await call.close()


@pytest.mark.parametrize(
    "tool,code", [("lookup_knowledge", "AGENT_TIMEOUT"), ("reason_over_knowledge", "AGENT_TIMEOUT")]
)
async def test_shared_business_deadline_stops_slow_independent_search(tmp_path, tool, code):
    async with SimulationHarness(tmp_path / "deadline.db", deadline=1000) as simulation:
        simulation.services.cuekb_delay = 1.5
        call = await PortalCall(simulation).create()
        await call.open_voice()
        await call.speak(VoicePlan(tool=tool))
        answer = (await call.answer())["answer"]
        assert answer["status"] == "failed" and answer["reason_code"] == code
        assert len(simulation.services.queries) == 1
        assert len(simulation.services.model_calls) == (1 if tool == "reason_over_knowledge" else 0)
        assert len(simulation.services.outputs) == 1
        await call.close()


async def test_independent_voice_disconnect_cancels_unfinished_native_answer(simulation):
    call = await PortalCall(simulation).create()
    await call.open_voice()
    plan = VoicePlan(hold_reply=True)
    await call.speak(plan)
    await asyncio.wait_for(plan.output_received.wait(), 3)
    await simulation.services.native_connections[-1].close(code=1011, reason="Injected simulation failure")

    async def canceled():
        history = await call.history()
        return history["items"] and history["items"][0]["status"] == "canceled"

    await eventually(canceled)
    assert (await call.history())["items"][0]["answer"] is None
    assert not any(
        event["type"] == "portal.audio.delta" and event["payload"]["response_id"] == plan.response_id
        for event in call.events
    )
    await call.open_voice()
    await call.speak(VoicePlan())
    assert (await call.answer())["answer"]["status"] == "answered"
    await call.close()


async def test_text_streaming_idempotency_and_sse_replay(simulation):
    call = await PortalCall(simulation).create()
    headers = {**call.headers, "Idempotency-Key": "same-network-question"}
    body = {"text": "Find AX integration sample documentation"}
    first = await simulation.client.post(call.path + "/messages", json=body, headers=headers)
    assert first.status_code == 202
    final = await sse_final(call)
    assert final["payload"]["status"] == "answered"
    again = await simulation.client.post(call.path + "/messages", json=body, headers=headers)
    assert again.json()["turn_id"] == first.json()["turn_id"]
    conflict = await simulation.client.post(
        call.path + "/messages", json={"text": "Different question"}, headers=headers
    )
    assert conflict.status_code == 409
    replay = await sse_final(call, final["server_seq"] - 1)
    assert replay["event_id"] == final["event_id"]
    assert len(simulation.services.queries) == 1 and len(simulation.services.model_calls) == 2
    assert all(body["stream"] for body in simulation.services.model_calls)
    await call.close()


async def test_sse_disconnect_finishes_database_cleanup_before_next_call(simulation, monkeypatch, caplog):
    """Disconnect during actual SQLite work, then verify cleanup and the next write."""
    call = await PortalCall(simulation).create()
    entered, release, closed = threading.Event(), threading.Event(), asyncio.Event()
    cleanup_errors = []

    def hold_query():
        entered.set()
        release.wait(3)
        return 1

    store = simulation.app.state.store
    await store.engine.dispose()

    def install_function(connection, _):
        connection.run_async(lambda driver: driver.create_function("simulation_hold", 0, hold_query))

    sql_event.listen(store.engine.sync_engine, "connect", install_function)

    class DelayedCloseSession(AsyncSession):
        async def execute(self, statement, *args, **kwargs):
            if str(statement).lstrip().startswith("SELECT events."):
                self.event_reader = True
                await super().execute(text("SELECT simulation_hold()"))
            return await super().execute(statement, *args, **kwargs)

        async def close(self):
            if getattr(self, "event_reader", False):
                try:
                    await super().close()
                except BaseException as exc:
                    cleanup_errors.append(type(exc).__name__)
                    raise
                finally:
                    closed.set()
            else:
                await super().close()

    monkeypatch.setattr(
        store,
        "sessions",
        async_sessionmaker(store.engine, class_=DelayedCloseSession, expire_on_commit=False),
    )

    async def query_entered():
        return entered.is_set()

    try:
        async with simulation.client.stream("GET", call.path + "/events", headers=call.headers) as response:
            assert response.status_code == 200
            await eventually(query_entered)
        # Allow the server's disconnect cancel scope to run during the SQLite call.
        await asyncio.sleep(0.05)
    finally:
        release.set()
    await asyncio.wait_for(closed.wait(), 3)
    assert cleanup_errors == []
    assert "Exception terminating connection" not in caplog.text

    async def reader_gone():
        return call.cid not in store.event_listeners

    await eventually(reader_gone)
    fresh = await PortalCall(simulation).create()
    assert fresh.cid != call.cid
    assert store.engine.pool.checkedout() == 0
    await fresh.close()
    await call.close()


async def test_capability_isolation_origin_ticket_and_revocation(simulation):
    first = await PortalCall(simulation).create()
    second = await PortalCall(simulation).create()
    assert first.headers != second.headers
    assert (await simulation.client.get(second.path + "/messages", headers=first.headers)).status_code == 401
    assert (await simulation.client.get(first.path + "/messages")).status_code == 401
    denied = await simulation.client.post(
        first.path + "/voice-sessions", headers={**first.headers, "Origin": "http://evil.invalid"}
    )
    assert denied.status_code == 403
    await first.open_voice()
    url = simulation.url.replace("http:", "ws:") + first.issued["ws_url"]
    with pytest.raises(InvalidStatus) as duplicate:
        async with connect(url, origin=simulation.settings.public_origin):
            pass
    assert duplicate.value.response.status_code == 403
    await first.close()
    assert (await simulation.client.get(first.path + "/messages", headers=first.headers)).status_code == 401
    await second.close()


async def test_cancel_pending_search_then_new_epoch_has_no_old_answer(simulation):
    simulation.services.cuekb_delay = 0.4
    call = await PortalCall(simulation).create()
    await call.open_voice()
    old_epoch = call.issued["epoch"]
    old = VoicePlan()
    await call.speak(old)

    async def pending():
        history = await call.history()
        return history if history["items"] and simulation.services.queries else None

    history = await eventually(pending)
    canceled = await simulation.client.post(
        call.path + "/tasks/current/cancel",
        headers=call.headers,
        json={"expected_epoch": old_epoch, "expected_revision": history["request_revision"]},
    )
    assert canceled.status_code == 200 and canceled.json()["status"] == "canceled"
    await call.ws.close()
    await call.reader
    simulation.services.cuekb_delay = 0
    await call.open_voice()
    assert call.issued["epoch"] > old_epoch
    fresh = VoicePlan()
    await call.speak(fresh)
    fresh_turn = await call.answer()
    assert fresh_turn["status"] == "answered" and fresh_turn["input_item_id"] == fresh.input_id
    history = await call.history()
    assert history["items"][0]["status"] == "canceled" and history["items"][0]["answer"] is None
    assert all(result["call_id"] != old.call_id for result in simulation.services.outputs)
    assert not any(
        event["type"] == "portal.audio.delta" and event["payload"]["response_id"] == old.response_id
        for event in call.events
    )
    await call.close()


async def test_stop_playback_keeps_result_and_next_answer_plays(simulation):
    call = await PortalCall(simulation).create()
    await call.open_voice()
    first = VoicePlan()
    await call.speak(first)
    turn = await call.answer()
    response = await simulation.client.post(
        call.path + "/playback/stop",
        headers=call.headers,
        json={
            "expected_epoch": turn["epoch"],
            "expected_revision": turn["request_revision"],
            "response_id": first.response_id,
        },
    )
    assert response.status_code == 200
    assert (await call.history())["items"][0]["answer"]["status"] == "answered"
    second = VoicePlan(tool="reason_over_knowledge")
    await call.speak(second)
    await call.until(
        "portal.audio.delta", lambda event: event["payload"]["response_id"] == second.response_id
    )
    await eventually(lambda: has_turns(call, 2))
    assert (await call.history())["items"][1]["answer"]["status"] == "answered"
    await call.close()
