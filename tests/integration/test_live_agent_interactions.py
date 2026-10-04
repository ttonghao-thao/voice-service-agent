"""P1/P2 regression. All provider speech/tool decisions remain scripted simulation."""

import asyncio
import base64
import json
import time
from types import SimpleNamespace

import pytest
from app.agent_runtime.evidence import EvidenceGate, presentation_contract
from app.api.routes import _event_for_principal, _record_for_principal
from app.contracts import Principal
from app.storage.models import DeliveryAttempt, Record
from app.voice.provider import NvidiaVoiceChatAdapter
from sqlalchemy import select

from scripts.simulate_full_flow import KB, PCM, PortalCall, SimulationHarness, VoicePlan


async def wait_for(check):
    async with asyncio.timeout(5):
        while True:
            if value := await check():
                return value
            await asyncio.sleep(.02)


async def rows(harness, cid, model=DeliveryAttempt):
    async with harness.app.state.store.sessions() as db:
        return list((await db.execute(select(model).where(model.conversation_id == cid))).scalars())


@pytest.mark.parametrize("spoken,expected", [
    ("Product AX supports 10 connections.", "matched"),
    ("Product AX supports 99 connections.", "failed"),
    ("AX has ten connections.", "failed"),
])
async def test_external_answer_is_bound_to_actual_spoken_transcript(tmp_path, spoken, expected):
    async with SimulationHarness(tmp_path / "spoken.db") as h:
        call = await PortalCall(h).create()
        await call.open_voice()
        plan = VoicePlan(tool="reason_over_knowledge", spoken=spoken)
        await call.speak(plan)
        written = await call.answer()
        event = await call.until("portal.presentation.updated", lambda e: e["payload"]["response_id"] == plan.response_id)
        assert event["payload"]["status"] == expected
        assert event["payload"]["answer_id"] == written["answer"]["answer_id"]
        assert event["payload"]["input_item_id"] == plan.input_id
        assert event["turn_id"] == written["id"]
        assert event["payload"]["check_basis"] == "provider_transcript"
        assert written["answer"]["status"] == "answered"
        assert written["answer"]["speech_text"] == "Product AX supports 10 connections."
        assert len(h.services.model_calls) == 2  # no additional LLM to judge presentation
        if expected == "failed":
            clear = await call.until("portal.playback.clear")
            assert clear["payload"]["response_id"] == plan.response_id
        record = next(r for r in await rows(h, call.cid, Record) if r.kind == "speech_validation")
        none = Principal(user_id="no-scope", roles=frozenset({"customer"}))
        assert _record_for_principal(record, none, h.settings) is None
        assert _event_for_principal({**event, "payload": {**event["payload"], "_authorized_kb_ids": [KB]}}, none, h.settings) is None
        await call.close()


async def test_planned_short_answer_falls_back_when_a_condition_is_missing(tmp_path):
    async with SimulationHarness(tmp_path / "conditions.db") as h:
        h.services.dynamic_conditions = True
        call = await PortalCall(h).create()
        await call.open_voice()
        # Exercise the finite gate directly with the real retrieved citations.
        plan = VoicePlan(question="For model AX100 version 1.0, how many connections?")
        await call.speak(plan)
        t = await call.answer()
        from app.contracts import AnswerBundle
        bundle = AnswerBundle.model_validate(t["answer"])
        bundle.speech_text = "It supports 10 connections."
        EvidenceGate.prepare(bundle, {"product_model": "AX100", "software_version": "1.0"})
        assert "written answer" in bundle.speech_text
        citation = bundle.citations[0]
        assert EvidenceGate.check_presentation("Product AX1000 version 1.0 supports 10 connections.",
            presentation_contract("grounded", {"product_model": "AX100"}), [citation]) == "VOICE_MISSING_CONDITION"
        citation.content = "Product AX100 must not reset and can connect only after version 1.0."
        assert EvidenceGate.check_spoken("Product AX100 can reset only after version 1.0.", [citation]) == "VOICE_MISSING_CONDITION"
        await call.close()


async def test_playback_completion_requires_network_end_and_exact_sample_count(tmp_path):
    async with SimulationHarness(tmp_path / "playback.db") as h:
        call = await PortalCall(h).create()
        await call.open_voice()
        plan = VoicePlan(tool=None)
        await call.speak(plan)
        await call.until("portal.audio.done", lambda e: e["payload"]["response_id"] == plan.response_id)
        await call.ws.send(json.dumps({"type": "portal.playback.ack", "epoch": call.issued["epoch"],
            "payload": {"response_id": plan.response_id, "played_samples": 1920, "finished": True}}))

        async def drained():
            return next((r for r in await rows(h, call.cid) if r.response_id == plan.response_id and r.playback_finished_at), None)

        r = await wait_for(drained)
        assert r.status == "completed" and r.played_samples == r.sent_samples == 1920
        assert r.validation_status == "unverified"
        await call.ws.close()
        await call.reader

        async def ended():
            return next((r for r in await rows(h, call.cid, Record) if r.kind == "voice_session_end"), None)

        end = await wait_for(ended)
        assert end.payload == {"status": "confirmed", "basis": "upstream_close_handshake"}
        await call.close()


@pytest.mark.parametrize("samples", [0, 1919, 1921])
async def test_invalid_finished_ack_is_rejected(tmp_path, samples):
    async with SimulationHarness(tmp_path / "bad-ack.db") as h:
        call = await PortalCall(h).create()
        await call.open_voice()
        plan = VoicePlan(tool=None)
        await call.speak(plan)
        await call.until("portal.audio.done", lambda e: e["payload"]["response_id"] == plan.response_id)
        await call.ws.send(json.dumps({"type": "portal.playback.ack", "epoch": call.issued["epoch"],
            "payload": {"response_id": plan.response_id, "played_samples": samples, "finished": True}}))
        error = await call.until("portal.error")
        assert error["payload"]["code"] == "VOICE_PROTOCOL_ERROR"
        assert not any(r.playback_finished_at for r in await rows(h, call.cid))
        await call.close()


async def test_progress_while_waiting_has_no_extra_search_or_revision(tmp_path):
    async with SimulationHarness(tmp_path / "progress.db", wait_interaction=True) as h:
        h.services.cuekb_release = asyncio.Event()
        call = await PortalCall(h).create()
        await call.open_voice()
        assert call.issued["wait_interaction"] is True
        assert len(h.services.updates[-1]["session"]["tools"]) == 2
        original = VoicePlan()
        await call.speak(original)

        async def searching():
            return h.services.queries

        await wait_for(searching)
        before = await call.history()
        progress = VoicePlan(question="Have you finished checking?", operation="progress")
        await call.speak(progress)
        await call.until("portal.audio.done", lambda e: e["payload"]["response_id"] == progress.response_id)
        result = next(r["result"] for r in h.services.outputs if r["call_id"] == progress.call_id)
        assert result["speech_text"] == "I am still checking the authorized knowledge sources."
        after = await call.history()
        assert after["request_revision"] == before["request_revision"]
        assert len(after["items"]) == 1 and after["items"][0]["status"] == "running"
        assert len(h.services.queries) == 1 and h.services.model_calls == []
        h.services.cuekb_release.set()
        answer = await call.answer()
        assert answer["status"] == "answered" and answer["input_item_id"] == original.input_id
        await call.until("portal.audio.done", lambda e: e["payload"]["response_id"] == original.response_id)
        await call.close()


async def test_natural_revision_settles_old_call_and_drops_late_output(tmp_path):
    async with SimulationHarness(tmp_path / "revision.db", wait_interaction=True) as h:
        h.services.cuekb_release = asyncio.Event()
        h.services.dynamic_conditions = True
        call = await PortalCall(h).create()
        await call.open_voice()
        original = VoicePlan(question="For model AX100 version 1.0, how many connections?")
        await call.speak(original)

        async def searching():
            return h.services.queries

        await wait_for(searching)
        old = (await call.history())["items"][0]
        old_deadline = h.app.state.coordinator.contexts[old["id"]].deadline
        revised = VoicePlan(question="Actually use model AX200 version 2.0 instead.", operation="revise")
        await call.speak(revised)
        await asyncio.wait_for(original.output_received.wait(), 3)
        old_result = next(r["result"] for r in h.services.outputs if r["call_id"] == original.call_id)
        assert old_result["status"] == "canceled" and old_result["speech_text"] == ""
        history = await call.history()
        new = history["items"][-1]
        assert new["parent_task_id"] == old["id"] and new["request_revision"] == old["request_revision"] + 1
        assert h.app.state.coordinator.contexts[new["id"]].deadline == old_deadline
        assert new["user_text"] == revised.question  # preserves raw correction, not tool paraphrase
        # Force an obsolete response under a previously unseen ID while the new
        # call is pending. It must not consume the new continuation grant.
        native = h.services.native_connections[-1]
        await native.send(json.dumps({"type": "response.output_audio_transcript.done", "response_id": "late-old",
            "parent_call_id": original.call_id, "transcript": "Product AX100 supports 99 connections."}))
        await native.send(json.dumps({"type": "response.output_audio.delta", "response_id": "late-old",
                                      "parent_call_id": original.call_id, "delta": PCM}))
        h.services.cuekb_release.set()
        answer = await call.answer()
        assert answer["status"] == "answered" and "AX200" in answer["answer"]["display_text"]
        await call.until("portal.audio.done", lambda e: e["payload"]["response_id"] == revised.response_id)
        history = await call.history()
        assert history["items"][0]["status"] == "superseded"
        assert history["items"][0]["answer"] is None
        assert h.services.queries[-1]["filters"] == {"document_ids": [], "product_model": "AX200", "software_version": "2.0"}
        assert not any(e["payload"].get("response_id") == "late-old" for e in call.events)
        settlements = [r for r in await rows(h, call.cid) if r.kind == "tool_settlement"]
        assert len(settlements) == 1 and settlements[0].status == "sent"
        assert h.services.model_calls == []
        await call.close()


async def test_unverified_native_provider_keeps_wait_disabled_but_portal_progress_works(tmp_path):
    async with SimulationHarness(tmp_path / "native.db") as h:
        assert not NvidiaVoiceChatAdapter.capabilities.wait_interaction
        h.services.cuekb_release = asyncio.Event()
        call = await PortalCall(h).create()
        await call.open_voice()
        assert call.issued["wait_interaction"] is False
        assert all("operation" not in t["parameters"]["properties"] for t in h.services.updates[-1]["session"]["tools"])
        await call.speak(VoicePlan())

        async def searching():
            return h.services.queries

        await wait_for(searching)
        history = await call.history()
        response = await h.client.post(call.path + "/tasks/current/progress", headers=call.headers,
            json={"expected_epoch": history["epoch"], "expected_revision": history["request_revision"]})
        assert response.status_code == 200 and response.json()["status"] == "running"
        assert len(h.services.queries) == 1 and h.services.model_calls == []
        stale = await h.client.post(call.path + "/tasks/current/progress", headers=call.headers,
            json={"expected_epoch": history["epoch"], "expected_revision": history["request_revision"] - 1})
        assert stale.status_code == 409
        await call.speak(VoicePlan(question="Any progress?", arguments={"operation": "progress", "user_request": "Any progress?"}))
        error = await call.until("portal.error")
        assert error["payload"]["code"] == "VOICE_PROTOCOL_ERROR"
        assert len(h.services.queries) == 1
        await call.close()


async def test_interleaved_progress_and_answer_use_their_own_call_grants(tmp_path):
    async with SimulationHarness(tmp_path / "grants.db", wait_interaction=True) as h:
        h.services.cuekb_release = asyncio.Event()
        call = await PortalCall(h).create()
        await call.open_voice()
        original = VoicePlan()
        await call.speak(original)

        async def searching():
            return h.services.queries

        await wait_for(searching)
        progress = VoicePlan(question="Any progress?", operation="progress", hold_reply=True)
        await call.speak(progress)
        await asyncio.wait_for(progress.output_received.wait(), 3)
        h.services.cuekb_release.set()
        written = await call.answer()
        event = await call.until("portal.presentation.updated", lambda e: e["payload"]["response_id"] == original.response_id)
        assert event["turn_id"] == written["id"] and event["payload"]["mode"] == "grounded"
        progress.reply_release.set()
        event = await call.until("portal.presentation.updated", lambda e: e["payload"]["response_id"] == progress.response_id)
        assert event["turn_id"] is None and event["payload"]["input_item_id"] == progress.input_id
        assert event["payload"]["mode"] == "verbatim" and event["payload"]["status"] == "matched"
        assert len(h.services.queries) == 1 and h.services.model_calls == []
        await call.close()


async def test_reused_ended_response_id_is_explicitly_rejected(tmp_path):
    async with SimulationHarness(tmp_path / "reused.db") as h:
        call = await PortalCall(h).create()
        await call.open_voice()
        plan = VoicePlan(tool="reason_over_knowledge")
        plan.response_id = "ack-" + plan.input_id
        await call.speak(plan)
        error = await call.until("portal.error")
        assert error["payload"]["code"] == "VOICE_PROTOCOL_ERROR"
        r = next(r for r in await rows(h, call.cid) if r.kind == "voice_audio")
        assert r.status == "completed" and r.phase == "status" and r.sent_samples == 1920
        assert r.validation_status is None
        await call.close()


async def test_abnormal_upstream_close_is_unknown_even_after_some_audio(tmp_path):
    async with SimulationHarness(tmp_path / "abnormal.db") as h:
        call = await PortalCall(h).create()
        await call.open_voice()
        plan = VoicePlan(tool=None, no_answer_end=True)
        await call.speak(plan)
        await call.until("portal.audio.delta", lambda e: e["payload"]["response_id"] == plan.response_id)
        h.services.native_connections[-1].transport.abort()
        await call.until("portal.error")
        await call.reader

        async def ended():
            return next((r for r in await rows(h, call.cid, Record) if r.kind == "voice_session_end"), None)

        end = await wait_for(ended)
        assert end.payload["status"] == "unknown"
        assert any(r.kind == "voice_audio" and r.status == "unknown" for r in await rows(h, call.cid))
        await call.close()


async def test_enhanced_provider_cannot_guess_a_missing_call_association(tmp_path):
    async with SimulationHarness(tmp_path / "missing-binding.db", wait_interaction=True) as h:
        call = await PortalCall(h).create()
        await call.open_voice()
        plan = VoicePlan(hold_reply=True)
        await call.speak(plan)
        await asyncio.wait_for(plan.output_received.wait(), 3)
        await h.services.native_connections[-1].send(json.dumps({
            "type": "response.output_audio_transcript.done", "response_id": plan.response_id,
            "transcript": "Product AX supports 10 connections.",
        }))
        error = await call.until("portal.error")
        assert error["payload"]["code"] == "VOICE_PROTOCOL_ERROR"
        assert not any(e["payload"].get("response_id") == plan.response_id and e["type"] == "portal.audio.delta" for e in call.events)
        await call.close()


async def test_rotation_waits_for_browser_drain_estimate(tmp_path, monkeypatch):
    from app.voice import gateway
    clock = {"offset": 0}
    monkeypatch.setattr(gateway, "time", SimpleNamespace(
        monotonic=lambda: time.monotonic() + clock["offset"], time=time.time))
    async with SimulationHarness(tmp_path / "rotation.db") as h:
        h.settings.voice_session_max_seconds = 10
        call = await PortalCall(h).create()
        await call.open_voice()
        plan = VoicePlan(tool=None)
        await call.speak(plan)
        await call.until("portal.audio.done", lambda e: e["payload"]["response_id"] == plan.response_id)
        clock["offset"] = 11
        await call.ws.send(json.dumps({"type": "portal.audio.append", "epoch": call.issued["epoch"],
            "seq": call.seq, "payload": {"format": "pcm16", "sample_rate": 24000,
                                         "audio": base64.b64encode(bytes(3840)).decode()}}))
        await asyncio.sleep(1.1)  # cross a watchdog tick with playback still unconfirmed
        assert not any(e["type"] == "portal.error" for e in call.events)
        await call.ws.send(json.dumps({"type": "portal.playback.ack", "epoch": call.issued["epoch"],
            "payload": {"response_id": plan.response_id, "played_samples": 1920, "finished": True}}))
        error = await call.until("portal.error")
        assert error["payload"]["code"] == "VOICE_SESSION_ROTATION_REQUIRED"
        await call.close()
