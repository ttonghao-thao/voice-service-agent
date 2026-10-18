"""Phase-1 integration. HTTP/WS services below are fixtures, not real models."""

import base64
import json
import runpy
import time
import wave
from argparse import Namespace

import httpx
import pytest
from app.agent_runtime.context import RunContext
from app.agent_runtime.direct import EvidenceReady
from app.config import ROOT, Settings
from app.contracts import CueKBArguments, DomainError, Principal
from app.main import create_app
from app.storage.models import Record
from app.tools.adapters import CueKBAdapter
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import select
from websockets.asyncio.server import serve

KB = "00000000-0000-4000-8000-000000000001"


def configuration(tmp_path, **overrides):
    return Settings(_env_file=None, auto_create_schema=True, agent_provider="disabled",
        cuekb_mode="real", cuekb_base_url="https://cuekb.fixture", cuekb_api_key="server-only",
        qa_execution_mode="dual_tools", qa_answer_policy="general_qa",
        database_url=f"sqlite+aiosqlite:///{tmp_path}/native.db", **overrides)


def evidence(**overrides):
    return {"trace_id": "00000000-0000-4000-8000-000000000111", "retrieval_status": "ok",
        "evidence_status": "sufficient", "content_revisions": {KB: 1}, "timings_ms": {"total": 1},
        "hits": [{"document_id": "00000000-0000-4000-8000-000000000101",
                  "chunk_id": "00000000-0000-4000-8000-000000000102",
                  "version_id": "00000000-0000-4000-8000-000000000103", "rank": 1,
                  "source_text": "Product AX supports 10 connections.", "metadata": {"product_model": "AX"}}],
        **overrides}


def test_query_only_schema_rejects_authority_and_preserves_identifiers():
    value = "Hello, compare Product AX-2 firmware 1.5 and 2.0; do not omit restrictions."
    assert CueKBArguments(query=value).query == value
    assert set(CueKBArguments.model_json_schema()["properties"]) == {"query"}
    for extra in ({"kb_ids": [KB]}, {"api_key": "forged"}, {"product_model": "AX"}, {"permissions": ["all"]}):
        with pytest.raises(ValidationError):
            CueKBArguments(query=value, **extra)
    for query in ("", "  ", "x" * 2001, "产品 AX"):
        with pytest.raises(ValidationError):
            CueKBArguments(query=query)


@pytest.mark.parametrize("scenario,code", [
    ("ok", None), ("unassessed", None), ("not_found", "CUEKB_NO_EVIDENCE"),
    ("insufficient", "CUEKB_EVIDENCE_UNSAFE"), ("conflicting", "CUEKB_EVIDENCE_UNSAFE"),
    ("needs_clarification", "CUEKB_NEEDS_CLARIFICATION"), ("auth", "CUEKB_AUTH_FAILED"),
    ("timeout", "TOOL_UNAVAILABLE"), ("non_ascii", "CUEKB_VOICE_EVIDENCE_UNAVAILABLE"),
    ("truncated_context", "CUEKB_VOICE_EVIDENCE_UNAVAILABLE"),
    ("truncated_context_with_source_part", None),
    ("budget", "CUEKB_VOICE_EVIDENCE_UNAVAILABLE"), ("partial", None),
    ("typographic_ascii", None),
    ("duplicate_context", None), ("bounded_source_part", None),
    ("omitted_without_source_part", None),
    ("omitted_unassessed", "CUEKB_VOICE_EVIDENCE_UNAVAILABLE"),
    ("oversized", "CUEKB_NO_EVIDENCE"),
])
async def test_native_query_reaches_cuekb_without_an_answer_model(tmp_path, monkeypatch, scenario, code):
    s = configuration(tmp_path)
    if scenario == "budget":
        s.qa_direct_evidence_max_bytes = 1024
    app = create_app(s)
    calls = []

    def handle(request):
        calls.append(request)
        if scenario == "auth":
            return httpx.Response(401)
        if scenario == "timeout":
            raise httpx.ReadTimeout("fixture timeout")
        data = evidence()
        if scenario in ("unassessed", "insufficient", "conflicting"):
            data["evidence_status"] = scenario
        if scenario in ("not_found", "needs_clarification"):
            data.update(retrieval_status=scenario, hits=[])
        if scenario == "non_ascii":
            data["hits"][0]["source_text"] = "产品 AX: 10 connections."
        if scenario == "typographic_ascii":
            data["hits"][0].update(
                source_text="Product AX supports \u226510 connections \u2014 it\u2019s documented.",
                title_path=["Guide \u00b7 \u201cProduct AX\u201d"],
            )
        if scenario == "duplicate_context":
            source = data["hits"][0]["source_text"]
            data["hits"][0].update(context=source, context_parts=[{
                "chunk_id": data["hits"][0]["chunk_id"],
                "source_text": source,
                "anchor": {},
                "title_path": [],
            }])
        if scenario in ("bounded_source_part", "omitted_without_source_part", "omitted_unassessed"):
            source = data["hits"][0]["source_text"]
            neighbor = "Additional complete neighboring context. " * 200
            source_part = {
                "chunk_id": data["hits"][0]["chunk_id"],
                "source_text": source,
                "anchor": {},
                "title_path": [],
            }
            neighbor_part = {
                "chunk_id": "00000000-0000-4000-8000-000000000199",
                "source_text": neighbor,
                "anchor": {},
                "title_path": [],
            }
            data["hits"][0].update(
                context=source + "\n\n" + neighbor,
                context_parts=([source_part, neighbor_part]
                               if scenario == "bounded_source_part" else [neighbor_part]),
            )
            if scenario == "omitted_unassessed":
                data["evidence_status"] = "unassessed"
        if scenario == "oversized":
            data["hits"][0]["source_text"] = "No. " * 2000
        if scenario in ("truncated_context", "truncated_context_with_source_part"):
            data["hits"][0]["context_truncated"] = True
        if scenario == "truncated_context_with_source_part":
            source = data["hits"][0]["source_text"]
            data["hits"][0].update(
                context=source + "\n\nPartial surrounding context",
                context_parts=[{
                    "chunk_id": data["hits"][0]["chunk_id"],
                    "source_text": source,
                    "anchor": {},
                    "title_path": [],
                }],
            )
        if scenario == "partial":
            data["scope_limited"] = True
        return httpx.Response(200, json=data)

    async def forbidden(*args, **kwargs):
        raise AssertionError("No external model may run in the native path")

    async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        runtime = app.state.coordinator.runtime
        assert runtime.client is None
        monkeypatch.setattr(runtime, "run", forbidden)
        monkeypatch.setattr(runtime.direct, "reasoned", forbidden)
        app.state.registry.adapters["cuekb_http"] = CueKBAdapter(s, http)
        snapshot = await runtime.catalog.snapshot()
        assert list(snapshot.tools) == ["search_knowledge_base"]
        for old_name in ("search_cueb", "search_company_documents"):
            with pytest.raises(DomainError) as rejected:
                runtime.dispatcher.resolve(
                    old_name,
                    json.dumps({"query": "Product AX"}),
                    tuple(snapshot.tools),
                    catalog=snapshot.tools,
                )
            assert rejected.value.code == "VOICE_TOOL_UNKNOWN"
        definition = snapshot.definitions[0]
        assert definition["ack_messages"] == ["Let me check the documentation."]
        assert set(definition["parameters"]["properties"]) == {"query"}
        description = definition["description"].lower()
        expected_routing_terms = (
            "price",
            "weight",
            "shipping",
            "storage",
            "shelf life",
            "lack access",
        )
        assert all(value in description for value in expected_routing_terms)
        p = Principal(user_id="dev-operator", scopes=frozenset({"knowledge:read"}), knowledge_base_ids=(KB,))
        async with app.state.store.transaction() as db:
            from app.storage.models import Conversation
            db.add(Conversation(id="cid", title="Native test", owner_id=p.user_id,
                                qa_execution_mode="dual_tools", answer_policy="general_qa"))
        turn, _, _ = await app.state.store.begin_turn(p, "cid", "id", "Configure it", "voice", 0)
        ctx = RunContext(p, "cid", turn.id, 0, request_revision=turn.request_revision,
                         answer_policy="general_qa", deadline=time.monotonic() + 5)
        query = "How do I configure Product AX firmware version 2?"
        decision = runtime.dispatcher.resolve("search_knowledge_base", json.dumps({"query": query}),
            tuple(snapshot.tools), catalog=snapshot.tools)
        result = await runtime.run_selected(decision, "Configure it", ctx, [])
        body = json.loads(calls[0].content)
        assert body["query"] == query  # Do not replace the model's resolved query with ASR.
        assert body["kb_ids"] == [KB]
        assert calls[0].headers["authorization"] == "Bearer server-only"
        assert str(calls[0].url) == "https://cuekb.fixture/v1/search"
        if code:
            assert result.reason_code == code
            assert result.status != "answered"
        else:
            assert isinstance(result, EvidenceReady)
            expected_text = (
                "Product AX supports >=10 connections - it's documented."
                if scenario == "typographic_ascii"
                else "Product AX supports 10 connections."
            )
            assert result.envelope["items"][0]["text"] == expected_text
            assert result.envelope["items"][0]["document_id"].endswith("101")
            assert result.envelope["trace_id"].endswith("111")
            if scenario == "truncated_context_with_source_part":
                assert result.envelope["items"][0]["context"] is None
            assert len(json.dumps(result.envelope).encode()) <= s.qa_direct_evidence_max_bytes
            assert json.dumps(result.envelope, ensure_ascii=False).isascii()
            assert result.envelope["partial"] is (scenario == "partial")
        assert ctx.effective_executor == "native_cuekb" and ctx.escalation_reason is None
        assert ctx.retrieval_calls == 1
        if scenario in ("truncated_context", "truncated_context_with_source_part"):
            async with app.state.store.sessions() as db:
                audit = (await db.execute(select(Record).where(
                    Record.conversation_id == "cid",
                    Record.kind == "native_cuekb_voice_evidence",
                ))).scalar_one().payload
            if scenario == "truncated_context":
                assert audit["rejected_truncated_context_count"] == 1
                assert audit["accepted_truncated_source_count"] == 0
            else:
                assert audit["rejected_truncated_context_count"] == 0
                assert audit["accepted_truncated_source_count"] == 1


@pytest.mark.parametrize("spoken,reason", [
    ("Product AX supports 10 connections.", None),
    ("Product AX supports 99 connections.", "VOICE_UNSUPPORTED_NUMBER"),
])
def test_pcm_through_real_adapter_native_call_cuekb_and_voice_reply(tmp_path, spoken, reason):
    s = configuration(tmp_path, voice_provider="nvidia", voicechat_api_key="fixture-voice-key")
    app = create_app(s)
    pcm = base64.b64encode(b"\x01\x00" * 1920).decode()
    requests, results, inputs, registrations = [], [], [], []

    def search(request):
        requests.append(request)
        return httpx.Response(200, json=evidence())

    async def voicechat(ws):
        await ws.send(json.dumps({"type": "session.created"}))
        registration = json.loads(await ws.recv())["session"]
        registrations.append(registration)
        await ws.send(json.dumps({"type": "session.updated", "session": {
            **registration, "tools": json.dumps(registration["tools"])}}))
        async for raw in ws:
            event = json.loads(raw)
            if event["type"] == "input_audio_buffer.append":
                inputs.append(event["audio"])
                n = len(inputs)
                await ws.send(json.dumps({"type": "input_audio_buffer.speech_started", "item_id": f"i{n}"}))
                request = "I use Product AX." if n == 1 else "How many connections does it support?"
                await ws.send(json.dumps({"type": "conversation.item.input_audio_transcription.completed",
                    "item_id": f"i{n}", "transcript": request}))
                if n == 1:
                    await ws.send(json.dumps({"type": "response.output_audio_transcript.done", "response_id": "direct",
                                              "transcript": "Understood."}))
                    await ws.send(json.dumps({"type": "response.output_audio.delta", "response_id": "direct", "delta": pcm}))
                    await ws.send(json.dumps({"type": "response.output_audio.done", "response_id": "direct"}))
                else:
                    call = {"type": "response.function_call_arguments.done", "response_id": "tool",
                        "call_id": "native-call", "name": "search_knowledge_base",
                        "arguments": json.dumps({"query": "How many connections does Product AX support?"})}
                    await ws.send(json.dumps(call))
                    await ws.send(json.dumps(call))
            elif event["type"] == "conversation.item.create":
                results.append(event["item"])
                await ws.send(json.dumps({"type": "response.output_audio_transcript.done", "response_id": "answer",
                                          "transcript": spoken}))
                await ws.send(json.dumps({"type": "response.output_audio.delta", "response_id": "answer", "delta": pcm}))
                await ws.send(json.dumps({"type": "response.output_audio.done", "response_id": "answer"}))

    with TestClient(app) as client:
        http = httpx.AsyncClient(transport=httpx.MockTransport(search))
        app.state.registry.adapters["cuekb_http"] = CueKBAdapter(s, http)
        async def start():
            return await serve(voicechat, "127.0.0.1", 0)
        server = client.portal.call(start)
        s.voicechat_ws_url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}"
        try:
            caps = client.get("/api/v1/capabilities").json()
            assert caps["native_tools"] == ["search_knowledge_base"] and caps["text_configured"] is False
            assert client.get("/health/ready").json()["status"] == "ready"
            cid = client.post("/api/v1/conversations", json={}).json()["id"]
            issued = client.post(f"/api/v1/conversations/{cid}/voice-sessions", json={}).json()
            assert issued["wait_interaction"] is False
            with client.websocket_connect(issued["ws_url"], headers={"Origin": s.public_origin}) as ws:
                assert ws.receive_json()["type"] == "portal.session.ready"
                for seq in range(2):
                    ws.send_json({"type": "portal.audio.append", "epoch": issued["epoch"], "seq": seq,
                        "payload": {"format": "pcm16", "sample_rate": 24000, "audio": pcm}})
                    events = []
                    for _ in range(15):
                        event = ws.receive_json()
                        events.append(event)
                        assert event["type"] != "portal.error", event
                        if event["type"] == "portal.audio.done":
                            break
                    else:
                        pytest.fail("Voice reply did not end")
                    assert any(e["type"] == "portal.audio.delta" for e in events)
                    if seq == 0:
                        assert not requests and not results
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline:
                    turn = client.get(f"/api/v1/conversations/{cid}/messages").json()["items"][0]
                    if turn["answer"]:
                        break
                    time.sleep(0.01)
                assert turn["selected_tool"] == "search_knowledge_base"
                assert turn["effective_executor"] == "native_cuekb"
                assert turn["answer"]["reason_code"] == reason
                assert turn["answer"]["verification_timing"] == "after_audio"
                assert len(requests) == len(results) == 1
                assert results[0]["call_id"] == "native-call"
                assert json.loads(results[0]["output"])["status"] == "evidence_ready"
                query = json.loads(requests[0].content)
                assert "How many connections does Product AX support?" in query["query"]
                assert query["filters"]["product_model"] == "AX"
                assert [t["name"] for t in registrations[0]["tools"]] == ["search_knowledge_base"]
                records = client.get(f"/api/v1/conversations/{cid}/messages").json()["records"]
                audit = next(r for r in records if r["kind"] == "native_tool_arguments")
                assert audit["payload"]["matches_final_transcript"] is False
                assert audit["payload"]["arguments"] == {"query": "How many connections does Product AX support?"}
                assert audit["payload"]["name"] == "search_knowledge_base"
                source = next(r for r in records if r["kind"] == "native_cuekb_result")
                assert source["payload"]["status"] == "ok" and source["payload"]["trace_id"].endswith("111")
                assert "_authorized_kb_ids" not in source["payload"]
                voice_evidence = next(
                    r for r in records if r["kind"] == "native_cuekb_voice_evidence"
                )["payload"]
                assert voice_evidence["candidate_count"] == voice_evidence["selected_count"] == 1
                assert sum(value for key, value in voice_evidence.items() if key.startswith("rejected_")) == 0
        finally:
            client.portal.call(http.aclose)
            server.close()
            client.portal.call(server.wait_closed)


async def test_catalog_upgrade_is_explicit_atomic_and_idempotent(tmp_path):
    s = configuration(tmp_path)
    app = create_app(s)
    configure = runpy.run_path(ROOT / "scripts/configure_native_cuekb.py")["configure"]
    async with app.router.lifespan_context(app):
        runtime = app.state.coordinator.runtime
        from app.agent_runtime.tool_catalog import NativeToolEntry
        old = [NativeToolEntry(name="lookup_knowledge", implementation="lookup_knowledge", description="Legacy lookup")]
        await runtime.catalog.update(old, 0, "fixture")
        preview = await configure(s)
        assert preview["previous_names"] == ["lookup_knowledge"] and preview["applied"] is False
        assert (await runtime.catalog.snapshot()).revision == 1
        applied = await configure(s, apply=True)
        assert applied["revision"] == 2
        assert list((await runtime.catalog.snapshot()).tools) == ["search_knowledge_base"]
        assert (await configure(s, apply=True))["changed"] is False
        assert (await runtime.catalog.snapshot()).revision == 2


async def test_recording_validator_uses_portal_business_results_not_synthetic_returns(tmp_path):
    from scripts.simulate_full_flow import http_server
    from scripts.validate_native_cuekb import run
    pcm = b"\x01\x00" * 1920
    received = []
    searches = []
    cuekb = FastAPI()

    @cuekb.post("/v1/search")
    async def search(request: Request):
        assert request.headers["authorization"] == "Bearer server-only"
        searches.append(await request.json())
        return evidence()

    async def voicechat(ws):
        await ws.send(json.dumps({"type": "session.created"}))
        session = json.loads(await ws.recv())["session"]
        await ws.send(json.dumps({"type": "session.updated", "session": session}))
        called = False
        async for raw in ws:
            event = json.loads(raw)
            if event["type"] == "input_audio_buffer.append" and not called:
                called = True
                await ws.send(json.dumps({"type": "input_audio_buffer.speech_started", "item_id": "input"}))
                await ws.send(json.dumps({"type": "conversation.item.input_audio_transcription.completed",
                    "item_id": "input", "transcript": "What is the connection limit for Product AX?"}))
                await ws.send(json.dumps({"type": "response.function_call_arguments.done", "response_id": "tool",
                    "call_id": "call", "name": "search_knowledge_base",
                    "arguments": json.dumps({"query": "What is the connection limit for Product AX?"})}))
            elif event["type"] == "conversation.item.create":
                received.append(event["item"])
                await ws.send(json.dumps({"type": "response.output_audio_transcript.done", "response_id": "answer",
                    "transcript": "Product AX supports 10 connections."}))
                await ws.send(json.dumps({"type": "response.output_audio.delta", "response_id": "answer",
                    "delta": base64.b64encode(pcm).decode()}))
                await ws.send(json.dumps({"type": "response.output_audio.done", "response_id": "answer"}))

    with wave.open(str(tmp_path / "input.wav"), "wb") as wav:
        wav.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
        wav.writeframes(pcm)
    cases = tmp_path / "cases.json"
    cases.write_text(json.dumps([{"case_id": "fixture", "wav": "input.wav",
        "expected_tool": "search_knowledge_base", "query_terms": ["AX"]}]))
    async with serve(voicechat, "127.0.0.1", 0) as endpoint, http_server(cuekb) as cuekb_url:
        s = configuration(tmp_path, voice_provider="nvidia", voicechat_api_key="fixture",
            voicechat_ws_url=f"ws://127.0.0.1:{endpoint.sockets[0].getsockname()[1]}")
        s.cuekb_base_url = cuekb_url
        app = create_app(s)
        async with http_server(app) as base:
            report = await run(Namespace(base_url=base, origin=s.public_origin, cases=cases, case_timeout=10,
                audio_dir=tmp_path / "audio", output=tmp_path / "report.json"))
    assert report["status"] == "observed", report
    assert report["synthetic_tool_results"] is False
    row = report["cases"][0]
    assert row["selection"] == row["arguments"] == row["retrieval"] == "passed"
    assert row["speech_transport"] == "passed" and row["spoken_faithfulness"] == "manual_review_required"
    assert received[0]["call_id"] == "call"
    assert len(searches) == 1 and searches[0]["kb_ids"] == [KB]
    assert json.loads(received[0]["output"])["schema_version"] == "cuekb-evidence-v1"
    stored = (tmp_path / "report.json").read_text()
    assert "Authorization" not in stored and "server-only" not in stored
