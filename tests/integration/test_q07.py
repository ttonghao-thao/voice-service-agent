"""Controlled Q07 execution/transport tests; no live model or CueKB calls."""

import asyncio
import json
from dataclasses import FrozenInstanceError

import httpx
import pytest
from app.agent_runtime.context import BusinessInput
from app.agent_runtime.direct import confirmed_filter
from app.api.routes import _answer_for_principal, _event_for_principal, _knowledge_for_principal
from app.config import Settings
from app.contracts import (
    Citation,
    KnowledgeBundle,
    NanoBridgeArguments,
    Principal,
    portal_server_event_adapter,
)
from app.main import create_app
from app.storage.models import Conversation, Event, ToolRun, Turn
from app.tools.schemas import CueKBSearchInput
from app.voice.provider import BRIDGE_ACK, KnowledgeWire, MockVoiceAdapter, VoiceEvent, direct_reply
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import select

KB = "00000000-0000-4000-8000-000000000001"


def principal():
    return Principal(user_id="dev-operator", scopes=frozenset({"knowledge:read"}), knowledge_base_ids=(KB,))


@pytest.fixture
async def direct_app(tmp_path, monkeypatch):
    from agents import Runner
    from openai import AsyncOpenAI

    def forbidden(*args, **kwargs):
        raise AssertionError("Direct mode must never construct or call an external model")

    monkeypatch.setattr(AsyncOpenAI, "__init__", forbidden)
    monkeypatch.setattr(Runner, "run", forbidden)
    monkeypatch.setattr(Runner, "run_streamed", forbidden)
    app = create_app(
        Settings(
            _env_file=None, auto_create_schema=True, database_url=f"sqlite+aiosqlite:///{tmp_path}/direct.db"
        )
    )
    async with app.router.lifespan_context(app):
        yield app


async def create_conversation(app):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        cid = (await client.post("/api/v1/conversations", json={})).json()["id"]
    return cid


async def submit(app, cid, key="one", text="Find the integration sample", query=None):
    return await app.state.coordinator.submit(
        principal(),
        cid,
        key,
        BusinessInput(text, query or CueKBSearchInput(query="Find the integration sample")),
        "voice",
        0,
        key,
        "input-" + key,
    )


async def deliver(app, turn):
    assert await app.state.store.tool_submitted(
        turn.conversation_id, turn.epoch, turn.request_revision, turn.id
    )
    assert app.state.coordinator.bind_voice_response(
        turn.conversation_id, turn.epoch, turn.request_revision, turn.id, "answer-" + turn.id
    )


async def done_record(app, turn, text, index=0):
    return await app.state.store.record(
        turn.conversation_id,
        turn.epoch,
        "voicechat_transcript",
        f"answer-{turn.id}:{index}",
        {
            "turn_id": turn.id,
            "request_revision": turn.request_revision,
            "response_id": "answer-" + turn.id,
            "phase": "answer",
            "segment_index": index,
            "text": text,
            "_authorized_kb_ids": [KB],
        },
        turn.id,
        turn.request_revision,
    )


async def finish(app, turn):
    task = app.state.coordinator.tasks[turn.conversation_id]
    assert await app.state.coordinator.complete_voice(
        turn.conversation_id, turn.epoch, turn.request_revision, turn.id, "answer-" + turn.id
    )
    result = await task
    await asyncio.sleep(0)
    return result


async def test_direct_two_stage_actual_transcript_and_no_external_calls(direct_app):
    app = direct_app
    cid = await create_conversation(app)
    for i in range(2):
        turn, ready = await submit(app, cid, key=str(i), text="Find it for the integration sample")
        reply = await ready
        wire = KnowledgeWire.model_validate_json(reply.tool_output)
        assert wire.directive == "answer_from_evidence" and wire.is_mock
        async with app.state.store.sessions() as db:
            stored = await db.get(Turn, turn.id)
            c = await db.get(Conversation, cid)
            assert stored.status == "awaiting_voice" and stored.answer is None
            assert c.history == [] if i == 0 else len(c.history) == 2
            assert stored.user_text == "Find it for the integration sample"
        assert len(app.state.coordinator.all_tasks) == 1
        assert not await app.state.coordinator.complete_voice(
            cid, 0, turn.request_revision, turn.id, "unbound"
        )
        await deliver(app, turn)
        await done_record(app, turn, "Actual first sentence.", 0)
        assert not await done_record(app, turn, "Duplicate content.", 0)
        await done_record(app, turn, "Actual second sentence.", 1)
        assert not await app.state.coordinator.complete_voice(cid, 0, turn.request_revision, turn.id, "wrong")
        bundle = await finish(app, turn)
        assert bundle.status == "voice_completed" and bundle.answer_origin == "voicechat"
        assert bundle.evidence_role == "retrieved_context"
        assert bundle.display_text == "Actual first sentence. Actual second sentence."
        assert not await app.state.coordinator.complete_voice(
            cid, 0, turn.request_revision, turn.id, "answer-" + turn.id
        )
        assert not app.state.coordinator.all_tasks and not app.state.coordinator.executions
    async with app.state.store.sessions() as db:
        c = await db.get(Conversation, cid)
        assert len(c.history) == 4 and c.history[-1]["content"] == bundle.display_text
        runs = (await db.execute(select(ToolRun))).scalars().all()
        assert len(runs) == 2
        events = (await db.execute(select(Event).order_by(Event.server_seq))).scalars().all()
        assert [e.payload["type"] for e in events].count("portal.knowledge.ready") == 2
        assert [e.payload["type"] for e in events].count("portal.answer.final") == 2
    # Changing environment/settings after startup cannot enable text or external calls.
    app.state.settings.agent_provider = "openai"
    assert app.state.profile.mode == "direct" and app.state.coordinator.runtime.client is None
    with pytest.raises(FrozenInstanceError):
        app.state.profile.mode = "external"


async def test_direct_text_rejected_without_turn_revision_or_voice_change(direct_app):
    app = direct_app
    cid = await create_conversation(app)
    turn, ready = await submit(app, cid)
    await ready
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        result = await client.post(
            f"/api/v1/conversations/{cid}/messages",
            json={"text": "hello"},
            headers={"Idempotency-Key": "text"},
        )
        assert result.status_code == 409 and result.json()["code"] == "TEXT_INPUT_UNAVAILABLE"
        assert (
            await client.post(
                "/api/v1/conversations/other/messages",
                json={"text": "hello"},
                headers={"Idempotency-Key": "text"},
            )
        ).status_code == 404
        caps = (await client.get("/api/v1/capabilities")).json()
        assert (
            caps["execution_mode"] == "direct"
            and not caps["text_available"]
            and not caps["external_llm_enabled"]
        )
        assert caps["native_tool_phase_barge_in"] is False
        assert (await client.get("/health/ready")).json()["status"] == "ready"
        view = (await client.get(f"/api/v1/conversations/{cid}/messages")).json()
        assert view["request_revision"] == turn.request_revision and len(view["items"]) == 1
        assert "authorized_kb_ids" not in view["items"][0]["knowledge_result"]
        assert "tool_version" not in view["items"][0]["knowledge_result"]


@pytest.mark.parametrize(
    "query, expected, status",
    [
        (CueKBSearchInput(query="no matching policy"), "report_insufficient", "insufficient_evidence"),
        (
            CueKBSearchInput(query="Find the integration sample", product_model="GUESS"),
            "ask_clarification",
            "needs_clarification",
        ),
    ],
)
async def test_direct_no_evidence_and_unconfirmed_filter(direct_app, query, expected, status):
    app = direct_app
    cid = await create_conversation(app)
    turn, ready = await submit(app, cid, query=query)
    reply = await ready
    assert reply.result.directive == expected
    if expected == "ask_clarification":
        assert reply.result.reason_code == "KNOWLEDGE_FILTER_UNCONFIRMED"
        async with app.state.store.sessions() as db:
            assert not (await db.execute(select(ToolRun))).scalars().all()
    await deliver(app, turn)
    await done_record(app, turn, "Please clarify the question.")
    assert (await finish(app, turn)).status == status


@pytest.mark.parametrize(
    "reason", ["VOICE_CONNECTION_ENDED", "VOICE_SESSION_EXPIRED", "VOICE_TOOL_SEND_FAILED"]
)
async def test_direct_voice_failure_retains_evidence_without_history(direct_app, reason):
    app = direct_app
    cid = await create_conversation(app)
    turn, ready = await submit(app, cid)
    await ready
    task = app.state.coordinator.tasks[cid]
    await deliver(app, turn)
    await done_record(app, turn, "Partial reply.")
    assert await app.state.coordinator.fail_voice(cid, 0, turn.request_revision, turn.id, reason)
    assert not await app.state.coordinator.fail_voice(cid, 0, turn.request_revision, turn.id, reason)
    result = await task
    assert result.reason_code == reason and result.status == "failed"
    async with app.state.store.sessions() as db:
        t = await db.get(Turn, turn.id)
        c = await db.get(Conversation, cid)
        assert t.delivery_status == "voice_failed" and t.knowledge_result and c.history == []
    assert not app.state.coordinator.executions


async def test_direct_total_deadline_and_shielded_ready(direct_app):
    app = direct_app
    app.state.settings.agent_deadline_ms = 100
    cid = await create_conversation(app)
    turn, ready = await submit(app, cid)
    waiter = asyncio.ensure_future(asyncio.shield(ready))
    waiter.cancel()
    await asyncio.gather(waiter, return_exceptions=True)
    assert (await ready).result.kind == "knowledge"
    task = app.state.coordinator.tasks[cid]
    result = await task
    assert result.reason_code == "VOICE_ANSWER_TIMEOUT"
    assert not app.state.coordinator.executions


async def test_direct_stop_continues_cancel_awaiting_releases_capacity(direct_app):
    app = direct_app
    cid = await create_conversation(app)
    turn, ready = await submit(app, cid)
    await ready
    await app.state.coordinator.stop_playback(principal(), cid, 0, turn.request_revision)
    assert app.state.coordinator.active_voice(cid)
    async with app.state.store.sessions() as db:
        assert not (await db.get(Turn, turn.id)).output_suppressed
    task = app.state.coordinator.tasks[cid]
    _, changed = await app.state.coordinator.cancel_task(principal(), cid, 0, turn.request_revision)
    assert changed
    await asyncio.gather(task, return_exceptions=True)
    assert not app.state.coordinator.executions
    async with app.state.store.sessions() as db:
        assert (await db.get(Turn, turn.id)).status == "canceled"


async def test_direct_empty_done_and_aggregate_length(direct_app):
    app = direct_app
    cid = await create_conversation(app)
    turn, ready = await submit(app, cid)
    await ready
    await deliver(app, turn)
    await done_record(app, turn, " ")
    assert (await finish(app, turn)).reason_code == "VOICE_ANSWER_MISSING"
    turn, ready = await submit(app, cid, "two")
    await ready
    await deliver(app, turn)
    await done_record(app, turn, "x" * 8001)
    assert (await finish(app, turn)).reason_code == "VOICE_ANSWER_TOO_LARGE"


async def test_direct_tool_revoked_after_evidence_cannot_finalize(direct_app):
    from app.storage.models import ToolConfig

    app = direct_app
    cid = await create_conversation(app)
    turn, ready = await submit(app, cid)
    await ready
    await deliver(app, turn)
    await done_record(app, turn, "Reply based on revoked evidence.")
    async with app.state.store.transaction() as db:
        db.add(ToolConfig(name="search_knowledge", enabled=False, revision=2))
    result = await finish(app, turn)
    assert result.status == "failed" and result.reason_code == "FORBIDDEN" and not result.citations


def citation(**changes):
    data = dict(
        citation_id="C1",
        document_id="document",
        chunk_id="chunk",
        version_id="version",
        title="Title",
        content="Source text.",
        trace_id="trace",
        retrieval_id="trace",
        rank=1,
        authorized_kb_ids=(KB,),
    )
    return Citation(**{**data, **changes})


def knowledge(**changes):
    return KnowledgeBundle(
        **{
            **dict(
                directive="answer_from_evidence",
                retrieval_status="ok",
                evidence_status="unassessed",
                citations=[citation()],
                authorized_kb_ids=[KB],
                tool_version=1,
            ),
            **changes,
        }
    )


def test_projection_budget_identity_and_non_ascii():
    sources = [
        citation(
            citation_id=f"C{i}",
            rank=i,
            content="A" * 1100,
            context="B" * 1100,
            metadata={"product_model": "AX", "secret": "do not send"},
        )
        for i in range(1, 9)
    ]
    result = direct_reply(knowledge(citations=sources, hits_omitted=2))
    wire = KnowledgeWire.model_validate_json(result.tool_output)
    assert len(result.tool_output.encode()) <= 8192 and len(wire.evidence) <= 5
    assert sum(len(e.source_text) + len(e.context or "") for e in wire.evidence) <= 6000
    assert [e.source_id for e in wire.evidence] == [c.citation_id for c in result.result.citations]
    assert result.result.hits_omitted == 5 and result.result.application_limited
    assert all(e.source_text == "A" * 1100 for e in wire.evidence)
    assert "secret" not in result.tool_output and "authorized_kb_ids" not in result.tool_output
    assert "document_id" not in result.tool_output and "chunk_id" not in result.tool_output
    bad = [
        citation(content="Chinese 中文"),
        citation(citation_id="C2", relations=[{"conditions": {"product_model": "中文"}}]),
    ]
    filtered = direct_reply(knowledge(citations=bad))
    assert filtered.result.directive == "report_insufficient"
    assert filtered.result.reason_code == "VOICE_EVIDENCE_NON_ASCII"
    assert not filtered.result.citations
    title = direct_reply(knowledge(citations=[citation(title="中文", context="Source text.")]))
    assert title.result.citations[0].title == "" and title.result.citations[0].context is None


def test_projection_drops_whole_relations_and_preserves_conflict_directive():
    result = direct_reply(
        knowledge(
            directive="report_insufficient",
            evidence_status="conflicting",
            citations=[
                citation(
                    relations=[
                        {"stance": "supports", "conditions": {"product_model": "AX" * 5000}},
                        {"stance": "refutes", "conditions": {"software_version": "3.2"}},
                    ]
                )
            ],
        )
    )
    assert result.result.directive == "report_insufficient" and not result.result.citations
    assert result.result.hits_omitted == 1


@pytest.mark.parametrize(
    "value,text,slot,expected",
    [
        ("3.2", "Use version 3.2.", None, True),
        ("3.2", "Use 13.20", None, False),
        ("3.2", "Use 3.2.1", None, False),
        ("AX", "Use AX-1", None, False),
        ("AX-1", "Use ax-1.", None, True),
        ("AX", "Use something", "ax", True),
        ("3.2", "Use 3.2/4", None, False),
        ("AX  1", "Use ax 1", None, True),
    ],
)
def test_filter_origin_boundaries(value, text, slot, expected):
    assert confirmed_filter(value, text, slot) is expected


@pytest.mark.parametrize(
    "changes",
    [
        {"kb_ids": [KB]},
        {"mode": "external"},
        {"query": "中文"},
        {"query": "bad\tquery"},
        {"product_model": "中文"},
    ],
)
def test_nano_arguments_strict_and_decoded_ascii(changes):
    with pytest.raises(ValidationError):
        NanoBridgeArguments.model_validate({"user_request": "request", "query": "query", **changes})


def test_revocation_hides_knowledge_sse_and_direct_answer():
    settings = Settings(_env_file=None)
    revoked = Principal(user_id="dev-operator", knowledge_base_ids=())
    bundle = knowledge(citations=[])
    assert (
        _knowledge_for_principal(bundle.model_dump(), revoked, settings)["reason_code"] == "KB_ACCESS_REVOKED"
    )
    event = dict(
        type="portal.knowledge.ready", payload=bundle.public_view().model_dump(), _authorized_kb_ids=[KB]
    )
    visible = _event_for_principal(event, revoked, settings)
    assert visible["payload"]["directive"] == "report_failure" and "_authorized_kb_ids" not in visible
    assert "authorized_kb_ids" not in visible["payload"]
    answer = dict(
        answer_origin="voicechat",
        authorized_kb_ids=[KB],
        status="voice_completed",
        display_text="secret",
        citations=[],
    )
    assert _answer_for_principal(answer, revoked, settings)["display_text"] != "secret"
    valid = portal_server_event_adapter.validate_python(
        dict(
            type="portal.knowledge.ready",
            conversation_id="cid",
            epoch=0,
            turn_id="turn",
            server_seq=1,
            payload=bundle.public_view().model_dump(),
        )
    )
    assert valid.payload.kind == "knowledge"


@pytest.mark.parametrize("same_response", [True, False])
@pytest.mark.parametrize("stop", [True, False])
def test_direct_transport_send_race_ack_and_stop(tmp_path, same_response, stop):
    app = create_app(
        Settings(
            _env_file=None, auto_create_schema=True, database_url=f"sqlite+aiosqlite:///{tmp_path}/race.db"
        )
    )
    outputs = []

    class Scripted(MockVoiceAdapter):
        async def connect(self, summary):
            await self.queue.put(VoiceEvent("input.state", {"state": "speaking", "item_id": "input"}))
            await self.queue.put(
                VoiceEvent("transcript.done", {"item_id": "input", "text": "Find the integration sample"})
            )
            await self.queue.put(
                VoiceEvent(
                    "tool",
                    {
                        "response_id": "tool",
                        "call_id": "call",
                        "name": "consult_service_agent",
                        "arguments": json.dumps(
                            {"user_request": "ASR description", "query": "Find the integration sample"}
                        ),
                    },
                )
            )

        async def submit_tool_result(self, call_id, text):
            outputs.append(json.loads(text))
            if stop:
                s = next(iter(app.state.voice.sessions.values()))
                await app.state.voice.suppress_playback(s.conversation_id, s.epoch)
            await self.queue.put(VoiceEvent("speech_text.done", {"response_id": "tool", "text": BRIDGE_ACK}))
            await self.queue.put(VoiceEvent("audio.done", {"response_id": "tool"}))
            response = "tool" if same_response else "answer"
            await self.queue.put(
                VoiceEvent("speech_text.done", {"response_id": response, "text": "Actual spoken answer."})
            )
            await self.queue.put(VoiceEvent("audio.done", {"response_id": response}))
            # Let the receiver consume all outputs while send is still suspended.
            await asyncio.sleep(0.05)

    with TestClient(app) as client:
        app.state.voice.provider_factory = Scripted
        cid = client.post("/api/v1/conversations", json={}).json()["id"]
        issued = client.post(f"/api/v1/conversations/{cid}/voice-sessions", json={}).json()
        with client.websocket_connect(issued["ws_url"], headers={"Origin": "http://localhost:5173"}) as ws:
            for _ in range(12):
                event = ws.receive_json()
                assert event["type"] != "portal.error", event
                if event["type"] == "portal.audio.done" and event["payload"]["phase"] == "answer":
                    break
            else:
                pytest.fail("Missing answer completion")

            # Read after coordinator commit, independently of the transport queue.
            async def committed():
                task = app.state.coordinator.tasks.get(cid)
                if task:
                    await task

            client.portal.call(committed)
            view = client.get(f"/api/v1/conversations/{cid}/messages").json()
            assert view["items"][0]["status"] == "voice_completed", view
            assert view["items"][0]["answer"]["display_text"] == "Actual spoken answer."
            assert [r["payload"]["text"] for r in view["records"] if r["kind"] == "voicechat_transcript"] == [
                "Actual spoken answer."
            ]
        assert len(outputs) == 1 and outputs[0]["kind"] == "knowledge"


async def test_direct_concurrent_completion_signals_are_idempotent(direct_app):
    app = direct_app
    cid = await create_conversation(app)
    turn, ready = await submit(app, cid)
    await ready
    await deliver(app, turn)
    await done_record(app, turn, "Single final reply.")
    task = app.state.coordinator.tasks[cid]
    results = await asyncio.gather(
        *(
            app.state.coordinator.complete_voice(cid, 0, turn.request_revision, turn.id, "answer-" + turn.id)
            for _ in range(2)
        )
    )
    assert sorted(results) == [False, True]
    assert (await task).status == "voice_completed"


async def test_direct_shutdown_finishes_waiter_and_restart_recovers_active_turn(direct_app):
    app = direct_app
    cid = await create_conversation(app)
    turn, ready = await submit(app, cid)
    await ready
    task = app.state.coordinator.tasks[cid]
    await app.state.coordinator.close()
    assert (await task).reason_code == "VOICE_SERVICE_SHUTDOWN"
    assert not app.state.coordinator.executions
    async with app.state.store.transaction() as db:
        saved = await db.get(Turn, turn.id)
        saved.status = "awaiting_voice"  # persisted process crash before completion
        c = await db.get(Conversation, cid)
        c.voice_session_id = "crashed"
    await app.state.coordinator.recover()
    async with app.state.store.sessions() as db:
        assert (await db.get(Turn, turn.id)).status == "expired"
        assert (await db.get(Conversation, cid)).epoch == 1


@pytest.mark.parametrize("failure", ["send", "buffer", "invalid", "no_text", "disconnect"])
def test_direct_transport_failures_release_execution(tmp_path, failure):
    app = create_app(
        Settings(
            _env_file=None,
            auto_create_schema=True,
            agent_deadline_ms=500,
            database_url=f"sqlite+aiosqlite:///{tmp_path}/failure.db",
        )
    )
    outputs = []

    class Scripted(MockVoiceAdapter):
        async def connect(self, summary):
            await self.queue.put(VoiceEvent("input.state", {"state": "speaking", "item_id": "input"}))
            await self.queue.put(
                VoiceEvent("transcript.done", {"item_id": "input", "text": "Find the integration sample"})
            )
            args = {"user_request": "Find the integration sample", "query": "Find the integration sample"}
            if failure == "invalid":
                args["kb_ids"] = [KB]
            await self.queue.put(
                VoiceEvent(
                    "tool",
                    {
                        "response_id": "tool",
                        "call_id": "call",
                        "name": "consult_service_agent",
                        "arguments": json.dumps(args),
                    },
                )
            )

        async def submit_tool_result(self, call_id, text):
            outputs.append(json.loads(text))
            if failure == "invalid":
                await self.queue.put(VoiceEvent("session.ended"))
            elif failure == "send":
                raise OSError("controlled send failure")
            elif failure == "buffer":
                for _ in range(17):
                    await self.queue.put(
                        VoiceEvent("audio.delta", {"response_id": "answer", "audio": "AAA="})
                    )
                await asyncio.sleep(0.05)
            elif failure == "no_text":
                await self.queue.put(VoiceEvent("audio.done", {"response_id": "answer"}))
            elif failure == "disconnect":
                await self.queue.put(
                    VoiceEvent("speech_text.done", {"response_id": "answer", "text": "Partial spoken text."})
                )
                await self.queue.put(VoiceEvent("session.ended"))

    with TestClient(app) as client:
        app.state.voice.provider_factory = Scripted
        if failure == "disconnect":
            submitted = app.state.store.tool_submitted

            async def slow_delivery(*args):
                await asyncio.sleep(0.05)
                return await submitted(*args)

            app.state.store.tool_submitted = slow_delivery
        cid = client.post("/api/v1/conversations", json={}).json()["id"]
        issued = client.post(f"/api/v1/conversations/{cid}/voice-sessions", json={}).json()
        from starlette.websockets import WebSocketDisconnect

        with client.websocket_connect(issued["ws_url"], headers={"Origin": "http://localhost:5173"}) as ws:
            try:
                while True:
                    ws.receive_json()
            except WebSocketDisconnect:
                pass
        assert not app.state.coordinator.executions
        view = client.get(f"/api/v1/conversations/{cid}/messages").json()
        assert len(outputs) == 1
        if failure == "invalid":
            assert outputs[0]["directive"] == "report_failure" and not view["items"]

            async def no_runs():
                async with app.state.store.sessions() as db:
                    return not (await db.execute(select(ToolRun))).scalars().all()

            assert client.portal.call(no_runs)
        else:
            assert view["items"][0]["status"] == "failed", view
            assert view["items"][0]["delivery_status"] == "voice_failed"
            assert view["items"][0]["knowledge_result"]
