import asyncio
import json
import time

import httpx
import pytest
from app.agent_runtime.context import RunContext
from app.agent_runtime.direct import EvidenceReady
from app.agent_runtime.dispatch import NativeTool
from app.config import Settings
from app.contracts import AnswerBundle, DomainError, KnowledgeArguments, Principal, StrictModel
from app.main import create_app
from app.storage.models import DeliveryAttempt, Utterance
from app.voice.provider import MockVoiceAdapter, VoiceEvent, session_update
from fastapi.testclient import TestClient
from sqlalchemy import select

KB = "00000000-0000-4000-8000-000000000001"


def settings(tmp_path, **kwargs):
    return Settings(_env_file=None, auto_create_schema=True,
                    database_url=f"sqlite+aiosqlite:///{tmp_path}/qa.db",
                    qa_execution_mode="dual_tools", **kwargs)


def arguments(request="Find the integration sample"):
    return json.dumps({"user_request": request, "product_model": None, "software_version": None})


def cuekb_response(evidence="sufficient", status="ok"):
    return {
        "trace_id": "00000000-0000-4000-8000-000000000111",
        "retrieval_status": status, "evidence_status": evidence,
        "content_revisions": {KB: 1}, "timings_ms": {"total": 1},
        "hits": [{"document_id": "00000000-0000-4000-8000-000000000101",
                  "chunk_id": "00000000-0000-4000-8000-000000000102",
                  "version_id": "00000000-0000-4000-8000-000000000103",
                  "rank": 1, "source_text": "Product AX supports 10 connections.",
                  "metadata": {"product_model": "AX"}}] if status != "not_found" else [],
    }


async def context(app, cid, key="test"):
    p = Principal(user_id="dev-operator", scopes=frozenset({"knowledge:read"}), knowledge_base_ids=(KB,))
    turn, _, _ = await app.state.store.begin_turn(p, cid, key, "Find the integration sample", "voice", 0)
    return RunContext(p, cid, turn.id, 0, request_revision=turn.request_revision,
                      deadline=time.monotonic() + 5, retrieval_limit=2, answer_policy="general_qa")


async def install_cuekb(app, result, requests):
    app.state.settings.cuekb_mode = "real"
    app.state.settings.cuekb_base_url = "https://fixture.invalid"
    from pydantic import SecretStr
    app.state.settings.cuekb_api_key = SecretStr("fixture")

    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=result)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    app.state.registry.adapters["cuekb_http"].client = client
    return client


def test_two_native_tools_and_extensible_registration(tmp_path):
    app = create_app(settings(tmp_path))
    with TestClient(app) as client:
        dispatcher = app.state.coordinator.runtime.dispatcher
        definitions = dispatcher.definitions(tuple(dispatcher.tools))
        config = session_update("", definitions, "general_qa")
        assert [t["name"] for t in config["session"]["tools"]] == ["lookup_knowledge", "reason_over_knowledge"]
        assert set(config["session"]) == {"tools", "instructions", "audio"}
        assert "consult_service_agent" not in config["session"]["instructions"]
        assert config["session"]["tools"][0]["parameters"]["additionalProperties"] is False

        async def trusted_executor(request, ctx, history, progress=None):
            return AnswerBundle(status="answered", display_text="Registered", speech_text="Registered")

        dispatcher.register(NativeTool("future_tool", "A future authorized capability", KnowledgeArguments, trusted_executor))
        selected = dispatcher.resolve("future_tool", arguments(), ("future_tool",))
        assert selected.tool.executor is trusted_executor
        with pytest.raises(DomainError):
            dispatcher.resolve("future_tool", arguments(), ("lookup_knowledge",))
        with pytest.raises(ValueError):
            dispatcher.register(selected.tool)
        # A session keeps the set already issued; later registration cannot expand it.
        cid = client.post("/api/v1/conversations", json={}).json()["id"]
        issued = client.post(f"/api/v1/conversations/{cid}/voice-sessions", json={}).json()
        session = app.state.voice.sessions[issued["voice_session_id"]]
        dispatcher.register(NativeTool("later_tool", "Registered after this session", KnowledgeArguments, trusted_executor))
        assert "later_tool" not in session.registered_tools


@pytest.mark.parametrize("extra", [{"kb_ids": [KB]}, {"url": "https://example.com"}, {"user_id": "other"}])
def test_native_tool_arguments_cannot_override_authority(extra):
    from app.agent_runtime.dispatch import knowledge_tools
    dispatcher = knowledge_tools(None, None)
    with pytest.raises(DomainError) as error:
        dispatcher.resolve("lookup_knowledge", json.dumps({**json.loads(arguments()), **extra}), ("lookup_knowledge",))
    assert error.value.code == "VOICE_TOOL_ARGUMENTS"


async def test_direct_uses_registry_without_external_llm(app, conversation):
    ctx = await context(app, conversation)
    requests = []
    client = await install_cuekb(app, cuekb_response(), requests)
    try:
        runtime = app.state.coordinator.runtime
        decision = runtime.dispatcher.resolve("lookup_knowledge", arguments(), ("lookup_knowledge",))
        ready = await runtime.run_selected(decision, "Find the integration sample", ctx, [])
        assert isinstance(ready, EvidenceReady)
        assert ready.envelope["status"] == "evidence_ready"
        assert len(requests) == ctx.retrieval_calls == 1
        assert requests[0]["kb_ids"] == [KB]
        assert ctx.effective_executor == "direct"
    finally:
        await client.aclose()


@pytest.mark.parametrize("evidence", ["unassessed", "insufficient", "conflicting"])
async def test_evidence_escalates_once_and_reuses_retrieval(app, conversation, evidence):
    ctx = await context(app, conversation)
    requests = []
    client = await install_cuekb(app, cuekb_response(evidence), requests)
    try:
        runtime = app.state.coordinator.runtime
        decision = runtime.dispatcher.resolve("lookup_knowledge", arguments(), ("lookup_knowledge",))
        result = await runtime.run_selected(decision, "Find the integration sample", ctx, [])
        assert isinstance(result, AnswerBundle)
        assert ctx.selected_tool == "lookup_knowledge" and ctx.effective_executor == "reasoned"
        assert ctx.escalation_reason == "evidence_requires_reasoning"
        assert len(requests) == 1
    finally:
        await client.aclose()


async def test_fabricated_model_filter_clarifies_without_search(app, conversation):
    ctx = await context(app, conversation)
    runtime = app.state.coordinator.runtime
    decision = runtime.dispatcher.resolve("lookup_knowledge", json.dumps({
        "user_request": "AX specifications", "product_model": "AX", "software_version": None,
    }), ("lookup_knowledge",))
    result = await runtime.run_selected(decision, "Find the integration sample", ctx, [])
    assert result.status == "needs_clarification" and ctx.retrieval_calls == 0


async def test_future_tool_keeps_its_own_schema_and_executor_arguments(app, conversation):
    class FutureArguments(StrictModel):
        case_id: str

    received = []
    async def executor(request, ctx, history, progress=None):
        received.append((request, ctx.tool_arguments))
        return AnswerBundle(status="answered", display_text="Acknowledged", speech_text="Acknowledged")

    runtime = app.state.coordinator.runtime
    runtime.dispatcher.register(NativeTool("future_case", "A trusted case capability", FutureArguments,
                                           executor, required_tools=frozenset()))
    decision = runtime.dispatcher.resolve("future_case", '{"case_id":"case-1"}', ("future_case",))
    ctx = await context(app, conversation)
    result = await runtime.run_selected(decision, "Inspect case-1", ctx, [])
    assert result.status == "answered"
    assert received == [("Inspect case-1", {"case_id": "case-1"})]
    assert ctx.retrieval_calls == 0


async def test_retrieval_budget_and_deadline_cannot_reset(app, conversation):
    ctx = await context(app, conversation)
    runtime = app.state.coordinator.runtime
    await runtime.prepare(ctx)
    ctx.retrieval_limit = 1
    await app.state.registry.invoke("search_knowledge", {"query": "integration sample"}, ctx)
    result = await app.state.registry.invoke("search_knowledge", {"query": "second query"}, ctx)
    assert result["code"] == "RETRIEVAL_LIMIT" and ctx.retrieval_calls == 1


async def test_external_sdk_reuses_same_turn_evidence_without_second_search(app, conversation):
    from openai import AsyncOpenAI
    ctx = await context(app, conversation)
    queries, calls = [], []
    source = await install_cuekb(app, cuekb_response("unassessed"), queries)
    runtime = app.state.coordinator.runtime
    app.state.settings.agent_provider = "openai"
    app.state.settings.agent_model = "fixture"

    def handler(request):
        body = json.loads(request.content)
        calls.append(body)
        assert body["tool_choice"] == "auto"  # Backend only: a real retrieval already exists.
        assert "Server-retrieved evidence" in body["input"][-1]["content"]
        return httpx.Response(200, json={
            "id": "resp_fixture", "object": "response", "created_at": 1788600000,
            "model": "fixture", "status": "completed", "output": [{
                "type": "message", "id": "msg_fixture", "role": "assistant", "status": "completed",
                "content": [{"type": "output_text", "annotations": [], "text": json.dumps({
                    "status": "answered", "display_text": "Product AX supports 10 connections. [C1]",
                    "speech_text": "Product AX supports 10 connections.", "citation_ids": ["C1"],
                })}],
            }], "usage": {"input_tokens": 10, "output_tokens": 10, "total_tokens": 20},
        })

    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            runtime.client = AsyncOpenAI(api_key="fixture", http_client=http, max_retries=0)
            decision = runtime.dispatcher.resolve("lookup_knowledge", arguments(), ("lookup_knowledge",))
            result = await runtime.run_selected(decision, "Find the integration sample", ctx, [])
            assert result.status == "answered", result
        assert len(queries) == len(calls) == 1
        assert ctx.selected_tool == "lookup_knowledge" and ctx.effective_executor == "reasoned"
    finally:
        await source.aclose()


async def test_provider_timeout_and_cancellation_have_terminal_state(app, conversation):
    queries = []
    source = await install_cuekb(app, cuekb_response(), queries)
    runtime = app.state.coordinator.runtime
    p = Principal(user_id="dev-operator", scopes=frozenset({"knowledge:read"}), knowledge_base_ids=(KB,))
    app.state.settings.qa_provider_answer_timeout_ms = 100
    async with app.state.store.transaction() as db:
        c = await app.state.store.get(db, conversation)
        c.answer_policy = "general_qa"
    decision = runtime.dispatcher.resolve("lookup_knowledge", arguments(), ("lookup_knowledge",))
    try:
        turn, task = await app.state.coordinator.submit(p, conversation, "timeout", "Find AX", "voice", 0, "c1", "i1", decision)
        assert isinstance(await task, EvidenceReady)
        completion = app.state.coordinator.tasks[conversation]
        await completion
        async with app.state.store.sessions() as db:
            from app.storage.models import Turn
            saved = await db.get(Turn, turn.id)
            assert saved.answer["reason_code"] == "VOICE_ANSWER_TIMEOUT"
        turn, task = await app.state.coordinator.submit(p, conversation, "cancel", "Find AX", "voice", 0, "c2", "i2", decision)
        assert isinstance(await task, EvidenceReady)
        completion = app.state.coordinator.tasks[conversation]
        await app.state.coordinator.cancel_task(p, conversation, 0, turn.request_revision)
        await asyncio.gather(completion, return_exceptions=True)
        app.state.coordinator.provider_answer(turn.id, "Late answer")
        async with app.state.store.sessions() as db:
            saved = await db.get(Turn, turn.id)
            assert saved.status == "canceled" and saved.answer is None
        assert not app.state.coordinator.awaiting_answers
    finally:
        await source.aclose()


async def test_crash_recovery_marks_started_delivery_unknown(app, conversation):
    store = app.state.store
    await store.delivery(conversation, 0, "c1", "tool_result", "write_started")
    await store.delivery(conversation, 0, "c2", "tool_result", "prepared")
    await app.state.coordinator.recover()
    async with store.sessions() as db:
        attempts = (await db.execute(select(DeliveryAttempt))).scalars().all()
        assert {a.native_call_id: a.status for a in attempts} == {"c1": "unknown", "c2": "discarded"}


async def test_native_websocket_wire_registers_tools_and_returns_result():
    from app.agent_runtime.dispatch import knowledge_tools
    from app.voice.provider import NvidiaVoiceChatAdapter
    from websockets.asyncio.server import serve
    dispatcher = knowledge_tools(None, None)
    config = session_update("", dispatcher.definitions(tuple(dispatcher.tools)), "general_qa")
    received = []

    async def server(ws):
        await ws.send(json.dumps({"type": "session.created"}))
        update = json.loads(await ws.recv())
        received.append(update)
        assert set(update["session"]) == {"audio", "instructions", "tools"}
        await ws.send(json.dumps({"type": "session.updated", "session": update["session"]}))
        await ws.send(json.dumps({"type": "response.function_call_arguments.done", "call_id": "call-wire",
                                  "response_id": "ack", "name": "lookup_knowledge", "arguments": arguments()}))
        result = json.loads(await ws.recv())
        received.append(result)
        await ws.send(json.dumps({"type": "response.output_audio_transcript.done", "response_id": "r1",
                                  "transcript": "A native continuation."}))
        await ws.send(json.dumps({"type": "response.output_audio.done", "response_id": "r1"}))

    async with serve(server, "127.0.0.1", 0) as endpoint:
        port = endpoint.sockets[0].getsockname()[1]
        adapter = NvidiaVoiceChatAdapter(Settings(_env_file=None, voicechat_ws_url=f"ws://127.0.0.1:{port}"))
        adapter.configuration = config
        await adapter.connect("")
        iterator = adapter.events()
        tool = await anext(iterator)
        assert tool.kind == "tool" and tool.payload["name"] == "lookup_knowledge"
        await adapter.submit_tool_result("call-wire", '{"status":"evidence_ready","items":[]}')
        assert (await anext(iterator)).kind == "speech_text.done"
        assert (await anext(iterator)).kind == "audio.done"
        await adapter.close()
    assert len(received[0]["session"]["tools"]) == 2
    assert received[1]["item"]["type"] == "function_call_output"
    assert received[1]["item"]["call_id"] == "call-wire"


def wait_answer(client, cid):
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        items = client.get(f"/api/v1/conversations/{cid}/messages").json()["items"]
        if items and items[0]["answer"]:
            return items[0]
        time.sleep(0.01)
    raise AssertionError("Answer did not reach a terminal state")


@pytest.mark.parametrize("spoken,code", [
    ("Product AX supports 10 connections.", None),
    ("Product AX supports 99 connections.", "VOICE_UNSUPPORTED_NUMBER"),
    ("Product AX supports 10 seconds.", "VOICE_UNSUPPORTED_UNIT"),
    ("It supports 10 connections.", "VOICE_MISSING_CONDITION"),
])
def test_evidence_ready_is_not_final_and_native_reply_commits_once(tmp_path, spoken, code):
    app = create_app(settings(tmp_path))
    returns = []

    class Scripted(MockVoiceAdapter):
        async def connect(self, summary):
            assert len(self.configuration["session"]["tools"]) == 2
            await self.queue.put(VoiceEvent("input.state", {"state": "speaking", "item_id": "i1"}))
            await self.queue.put(VoiceEvent("transcript.done", {"item_id": "i1", "text": "Find AX integration sample documentation"}))
            await self.queue.put(VoiceEvent("tool", {"call_id": "c1", "response_id": "ack",
                "name": "lookup_knowledge", "arguments": arguments("Find AX integration sample documentation")}))

        async def submit_tool_result(self, call_id, text):
            returns.append(json.loads(text))
            # This runs before a response exists; evidence must not have become a final answer.
            async with app.state.store.sessions() as db:
                from app.storage.models import Turn
                turn = (await db.execute(select(Turn))).scalar_one()
                assert turn.status == "running" and turn.answer is None
                assert turn.execution_phase == "awaiting_provider_answer"
            await self.queue.put(VoiceEvent("speech_text.done", {"response_id": "r1", "text": spoken}))
            await self.queue.put(VoiceEvent("audio.done", {"response_id": "r1"}))

    with TestClient(app) as client:
        from app.tools.adapters import CueKBAdapter
        class Source(CueKBAdapter):
            async def invoke(self, args, ctx):
                result = await super().invoke(args, ctx)
                for citation in ctx.evidence.values():
                    citation.content = "Product AX supports 10 connections."
                    citation.metadata = {"product_model": "AX"}
                    citation.evidence_status = "sufficient"
                for hit in result["hits"]:
                    hit.update(content="Product AX supports 10 connections.", metadata={"product_model": "AX"}, evidence_status="sufficient")
                result["evidence_status"] = "sufficient"
                ctx.retrievals[-1]["evidence_status"] = "sufficient"
                return result
        app.state.registry.adapters["cuekb_http"] = Source(app.state.settings, app.state.client)
        app.state.voice.provider_factory = Scripted
        cid = client.post("/api/v1/conversations", json={}).json()["id"]
        issued = client.post(f"/api/v1/conversations/{cid}/voice-sessions", json={}).json()
        with client.websocket_connect(issued["ws_url"], headers={"Origin": "http://localhost:5173"}) as ws:
            while ws.receive_json()["type"] != "portal.audio.done":
                pass
            turn = wait_answer(client, cid)
            assert turn["answer"]["reason_code"] == code
            assert turn["answer"]["verification_timing"] == "after_audio"
            assert turn["answer"]["status"] == ("failed" if code else "answered")
            assert len(returns) == 1 and returns[0]["status"] == "evidence_ready"
            assert turn["selected_tool"] == "lookup_knowledge"


def test_general_answer_has_no_business_turn_or_retrieval(tmp_path):
    app = create_app(settings(tmp_path))

    class Scripted(MockVoiceAdapter):
        async def connect(self, summary):
            await self.queue.put(VoiceEvent("input.state", {"state": "speaking", "item_id": "i1"}))
            await self.queue.put(VoiceEvent("transcript.done", {"item_id": "i1", "text": "What is MLO?"}))
            await self.queue.put(VoiceEvent("speech_text.done", {"response_id": "r1", "text": "MLO means multi-link operation."}))
            await self.queue.put(VoiceEvent("audio.done", {"response_id": "r1"}))

    with TestClient(app) as client:
        app.state.voice.provider_factory = Scripted
        cid = client.post("/api/v1/conversations", json={}).json()["id"]
        issued = client.post(f"/api/v1/conversations/{cid}/voice-sessions", json={}).json()
        with client.websocket_connect(issued["ws_url"], headers={"Origin": "http://localhost:5173"}) as ws:
            events = []
            while not events or events[-1]["type"] != "portal.audio.done":
                events.append(ws.receive_json())
            spoken = next(e for e in events if e["type"] == "portal.speech_text.done")
            assert spoken["turn_id"] is None and spoken["payload"]["input_item_id"] == "i1"
            data = client.get(f"/api/v1/conversations/{cid}/messages").json()
            assert data["items"] == []
            assert any(r["kind"] == "voicechat_transcript" for r in data["records"])
            async def inspect():
                async with app.state.store.sessions() as db:
                    utterance = (await db.execute(select(Utterance))).scalar_one()
                    assert utterance.answer["composition"] == "provider_general"
                    assert not (await db.execute(select(DeliveryAttempt))).scalars().all()
            client.portal.call(inspect)


def test_strict_policy_rejects_native_general_output(tmp_path):
    app = create_app(settings(tmp_path, qa_answer_policy="knowledge_required"))

    class Scripted(MockVoiceAdapter):
        async def connect(self, summary):
            await self.queue.put(VoiceEvent("input.state", {"state": "speaking", "item_id": "i1"}))
            await self.queue.put(VoiceEvent("speech_text.done", {"response_id": "r1", "text": "The account is online."}))

    with TestClient(app) as client:
        app.state.voice.provider_factory = Scripted
        cid = client.post("/api/v1/conversations", json={}).json()["id"]
        issued = client.post(f"/api/v1/conversations/{cid}/voice-sessions", json={}).json()
        with client.websocket_connect(issued["ws_url"], headers={"Origin": "http://localhost:5173"}) as ws:
            event = ws.receive_json()
            while event["type"] != "portal.error":
                event = ws.receive_json()
            assert event["payload"]["code"] == "VOICE_TOOL_REQUIRED"


def test_general_response_without_end_times_out_without_starting_retrieval(tmp_path):
    app = create_app(settings(tmp_path, qa_provider_answer_timeout_ms=100))

    class Scripted(MockVoiceAdapter):
        async def connect(self, summary):
            await self.queue.put(VoiceEvent("input.state", {"state": "speaking", "item_id": "i1"}))
            await self.queue.put(VoiceEvent("transcript.done", {"item_id": "i1", "text": "What is MLO?"}))
            await self.queue.put(VoiceEvent("speech_text.delta", {"response_id": "r1", "text": "MLO means "}))

    with TestClient(app) as client:
        app.state.voice.provider_factory = Scripted
        cid = client.post("/api/v1/conversations", json={}).json()["id"]
        issued = client.post(f"/api/v1/conversations/{cid}/voice-sessions", json={}).json()
        with client.websocket_connect(issued["ws_url"], headers={"Origin": "http://localhost:5173"}) as ws:
            event = ws.receive_json()
            while event["type"] != "portal.error":
                event = ws.receive_json()
            assert event["payload"]["code"] == "VOICE_ANSWER_TIMEOUT"
        assert client.get(f"/api/v1/conversations/{cid}/messages").json()["items"] == []
