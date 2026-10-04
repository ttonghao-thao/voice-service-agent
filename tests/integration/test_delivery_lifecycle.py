"""Delivery, presentation and recovery with independent socket APIs."""

import asyncio

import pytest
from app.storage.models import DeliveryAttempt
from sqlalchemy import select

from scripts.simulate_full_flow import PortalCall, SimulationHarness, VoicePlan


@pytest.fixture
async def simulation(tmp_path):
    async with SimulationHarness(tmp_path / "delivery.db", answer_timeout=2500) as harness:
        yield harness


async def eventually(check):
    async with asyncio.timeout(5):
        while True:
            value = await check()
            if value:
                return value
            await asyncio.sleep(.02)


async def attempts(harness, cid):
    async with harness.app.state.store.sessions() as db:
        return list((await db.execute(select(DeliveryAttempt).where(DeliveryAttempt.conversation_id == cid))).scalars())


@pytest.mark.parametrize("tool,model_calls", [(None, 0), ("lookup_knowledge", 0), ("reason_over_knowledge", 2)])
async def test_delivery_binds_actual_output_without_extra_model_calls(simulation, tool, model_calls):
    call = await PortalCall(simulation).create()
    await call.open_voice()
    plan = VoicePlan(tool=tool)
    await call.speak(plan)
    await call.until("portal.audio.done", lambda e: e["payload"]["response_id"] == plan.response_id)
    if tool:
        await call.answer()

    async def completed():
        rows = await attempts(simulation, call.cid)
        return next((r for r in rows if r.kind == "voice_audio" and r.response_id == plan.response_id and r.status == "completed"), None)

    presentation = await eventually(completed)
    assert presentation.input_item_id == plan.input_id
    assert presentation.sent_samples == 1920 and presentation.played_samples == 0
    assert presentation.finished_at and presentation.phase == "answer"
    assert (presentation.turn_id is not None) == (tool is not None)

    assert len(simulation.services.model_calls) == model_calls
    await call.close()


async def test_incomplete_audio_disconnect_is_unknown_and_is_not_replayed(simulation):
    call = await PortalCall(simulation).create()
    await call.open_voice()
    plan = VoicePlan(tool=None, no_answer_end=True)
    await call.speak(plan)
    await call.until("portal.audio.delta", lambda e: e["payload"]["response_id"] == plan.response_id)
    await call.ws.close()
    await call.reader

    async def unknown():
        return next((r for r in await attempts(simulation, call.cid)
                     if r.kind == "voice_audio" and r.status == "unknown"), None)

    row = await eventually(unknown)
    assert row.reason_code == "connection_closed" and row.finished_at
    previous_events = len(call.events)
    await call.open_voice()
    summary = simulation.services.updates[-1]["session"]["instructions"]
    assert "unknown" in summary and "do not replay" in summary
    assert plan.response_id in summary  # exact stored association, not text similarity
    assert not any(e["type"] == "portal.audio.delta" for e in call.events[previous_events:]
                   if e["epoch"] == call.issued["epoch"])
    await call.close()


async def test_stop_control_is_idempotent_and_preserves_network_completion(simulation):
    call = await PortalCall(simulation).create()
    await call.open_voice()
    plan = VoicePlan(tool=None)
    await call.speak(plan)
    await call.until("portal.audio.done", lambda e: e["payload"]["response_id"] == plan.response_id)
    for _ in range(2):
        response = await simulation.client.post(call.path + "/playback/stop", headers=call.headers,
            json={"expected_epoch": call.issued["epoch"], "expected_revision": call.issued["request_revision"], "response_id": plan.response_id})
        assert response.status_code == 200
    rows = await attempts(simulation, call.cid)
    controls = [r for r in rows if r.kind == "control"]
    assert len(controls) == 1 and controls[0].status == "applied" and controls[0].phase == "stop"
    audio = next(r for r in rows if r.kind == "voice_audio")
    assert audio.status == "completed" and audio.output_suppressed and audio.reason_code == "user_stopped"
    history = await call.history()
    assert history["items"] == []
    assert not any(r["kind"] == "run_observation" for r in history["records"])
    await call.close()


async def test_recovery_never_reopens_a_terminal_attempt(app, conversation):
    store = app.state.store
    await store.delivery(conversation, 0, "r1", "voice_audio", "write_started", response_id="r1", input_item_id="i1")
    await app.state.coordinator.recover()
    assert not await store.delivery(conversation, 0, "r1", "voice_audio", "sent", response_id="r1")
    rows = await attempts(type("Harness", (), {"app": app})(), conversation)
    assert rows[0].status == "unknown" and rows[0].reason_code == "service_restarted"


async def test_writer_cancellation_waits_for_database_commit_before_releasing_lock(app, conversation, monkeypatch):
    store = app.state.store
    entered, release = asyncio.Event(), asyncio.Event()
    original = store.sessions.begin

    class GatedCommit:
        def __init__(self):
            self.manager = original()

        async def __aenter__(self):
            return await self.manager.__aenter__()

        async def __aexit__(self, *error):
            entered.set()
            await release.wait()
            return await self.manager.__aexit__(*error)

    monkeypatch.setattr(store.sessions, "begin", GatedCommit)
    write = asyncio.create_task(store.delivery(conversation, 0, "r1", "voice_audio", "write_started"))
    await asyncio.wait_for(entered.wait(), 2)
    write.cancel()
    await asyncio.sleep(0)
    write.cancel()
    await asyncio.sleep(0)
    assert store.write_lock.locked() and not write.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await write
    assert not store.write_lock.locked()
    rows = await attempts(type("Harness", (), {"app": app})(), conversation)
    assert rows[0].status == "write_started"
    await store.settle_deliveries(conversation, 0, "connection_closed")
    rows = await attempts(type("Harness", (), {"app": app})(), conversation)
    assert rows[0].status == "unknown"
