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
from app.agent_runtime.native_cuekb import voice_result
from app.config import ROOT, Settings
from app.contracts import CueKBArguments, DomainError, Principal
from app.main import create_app
from app.storage.models import Record, Turn
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
    ("ok", None), ("unassessed", None), ("five_hits", None), ("five_hits_bounded", None),
    ("not_found", "CUEKB_NO_EVIDENCE"),
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
    if scenario == "five_hits_bounded":
        s.qa_direct_evidence_max_bytes = 2200
    app = create_app(s)
    calls = []

    def handle(request):
        calls.append(request)
        if scenario == "auth":
            return httpx.Response(401)
        if scenario == "timeout":
            raise httpx.ReadTimeout("fixture timeout")
        data = evidence()
        if scenario in ("five_hits", "five_hits_bounded"):
            # Later ranked hits can carry the requested facts; the first
            # three headings/introductory excerpts are not necessarily enough.
            texts = ["Product AX supports 10 connections.", "Product AX overview.",
                     "Product AX launches in 2026.", "Product AX weighs 100 grams.",
                     "Product AX costs 20 dollars."]
            original = data["hits"][0]
            data["hits"] = [{**original,
                "chunk_id": f"00000000-0000-4000-8000-{index + 200:012d}",
                "source_text": text, "rank": index + 1,
            } for index, text in enumerate(texts)]
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
        assert body["top_k"] == s.cuekb_top_k == 5
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
            partial = scenario in ("partial", "five_hits_bounded")
            assert result.envelope["partial"] is partial
            wire = voice_result(result)
            assert wire["partial"] == ("true" if partial else "false")
            if scenario == "five_hits_bounded":
                assert 0 < len(wire["sources"]) < 5
            if scenario == "five_hits":
                assert len(wire["sources"]) == 5
                assert wire["sources"][-2]["text"] == "Product AX weighs 100 grams."
                assert wire["sources"][-1]["text"] == "Product AX costs 20 dollars."
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


@pytest.mark.parametrize("spoken,reason,context_mode", [
    ("Product AX supports 10 connections.", None, "none"),
    ("Product AX supports 99 connections.", "VOICE_UNSUPPORTED_NUMBER", "none"),
    ("Product AX supports 10 connections.", None, "truncated"),
    ("Product AX supports 99 connections.", "VOICE_UNSUPPORTED_NUMBER", "truncated"),
    ("Product AX supports 10 connections.", "VOICE_MISSING_CONDITION", "complete"),
    ("Product AX supports 10 connections only indoors.", None, "complete"),
    ("It supports 10 connections.", "VOICE_MISSING_CONDITION", "none"),
])
def test_pcm_through_real_adapter_native_call_cuekb_and_voice_reply(tmp_path, spoken, reason, context_mode):
    s = configuration(tmp_path, voice_provider="nvidia", voicechat_api_key="fixture-voice-key")
    app = create_app(s)
    pcm = base64.b64encode(b"\x01\x00" * 1920).decode()
    requests, results, inputs, registrations = [], [], [], []

    def search(request):
        requests.append(request)
        data = evidence()
        if context_mode != "none":
            hit = data["hits"][0]
            hit.update(context="Only 99 connections in an unrelated neighboring passage."
                       if context_mode == "truncated" else "Product AX supports 10 connections only indoors.",
                       context_truncated=context_mode == "truncated",
                       context_parts=[{"chunk_id": hit["chunk_id"], "source_text": hit["source_text"],
                                       "title_path": [], "anchor": {}}])
        return httpx.Response(200, json=data)

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
                assert turn["answer"]["status"] == ("failed" if reason else "answered")
                assert bool(turn["answer"]["citations"]) == (reason is None)
                history = client.get(f"/api/v1/conversations/{cid}/messages").json()
                audit = next(r["payload"] for r in history["records"] if r["kind"] == "native_answer_check")
                assert audit["reason_code"] == reason
                assert audit["evidence_basis"] == "delivered_sources"
                if reason == "VOICE_MISSING_CONDITION":
                    assert audit["condition_failures"][0]["rule"] == (
                        "source_signal" if context_mode == "complete" else "source_metadata")
                else:
                    assert audit["condition_failures"] == []
                from scripts.inspect_native_answer import inspect_answer
                inspected = client.portal.call(inspect_answer, s, cid, "i2")
                assert inspected["answer_text"] == spoken
                assert inspected["delivered_check"] == reason
                if context_mode == "truncated" and reason is None:
                    assert inspected["original_check"] == "VOICE_MISSING_CONDITION"
                    assert "context" not in json.loads(results[0]["output"])["sources"][0]
                    assert turn["answer"]["citations"][0]["context_truncated"] is True
                    assert "99" in turn["answer"]["citations"][0]["context"]
                assert len(requests) == len(results) == 1
                assert results[0]["call_id"] == "native-call"
                assert json.loads(results[0]["output"])["status"] == "ok"
                assert "trace_id" not in json.loads(results[0]["output"])
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
        "expected_tool": "search_knowledge_base", "query_terms": ["AX"]},
        {"case_id": "excluded", "wav": "not-loaded.wav", "expected_tool": None}]))
    async with serve(voicechat, "127.0.0.1", 0) as endpoint, http_server(cuekb) as cuekb_url:
        s = configuration(tmp_path, voice_provider="nvidia", voicechat_api_key="fixture",
            voicechat_ws_url=f"ws://127.0.0.1:{endpoint.sockets[0].getsockname()[1]}")
        s.cuekb_base_url = cuekb_url
        app = create_app(s)
        async with http_server(app) as base:
            report = await run(Namespace(base_url=base, origin=s.public_origin, cases=cases, case_id="fixture", case_timeout=10,
                audio_dir=tmp_path / "audio", output=tmp_path / "report.json"))
    assert report["status"] == "observed", report
    assert report["synthetic_tool_results"] is False
    row = report["cases"][0]
    assert len(report["cases"]) == 1
    assert row["answer_status"] == "answered" and row["answer_citation_count"] == 1
    assert row["answer_check"]["reason_code"] is None
    assert row["selection"] == row["arguments"] == row["retrieval"] == "passed"
    assert row["speech_transport"] == "passed" and row["spoken_faithfulness"] == "manual_review_required"
    assert received[0]["call_id"] == "call"
    assert len(searches) == 1 and searches[0]["kb_ids"] == [KB]
    assert json.loads(received[0]["output"])["sources"][0]["text"] == "Product AX supports 10 connections."
    stored = (tmp_path / "report.json").read_text()
    assert "Authorization" not in stored and "server-only" not in stored


@pytest.mark.parametrize("answer_available", [True, False])
async def test_continuous_native_conversation_survives_delayed_answers_and_repeated_acks(tmp_path, answer_available):
    """Real API/WS transport, controlled provider. No claim about GPU semantics."""
    import asyncio

    from scripts.simulate_full_flow import http_server
    from scripts.validate_native_cuekb import passed, run

    queries, results, registrations, silence_during_wait = [], [], [], []
    cuekb = FastAPI()

    @cuekb.post("/v1/search")
    async def search(request: Request):
        body = await request.json()
        queries.append(body["query"])
        return evidence()

    async def voicechat(ws):
        await ws.send(json.dumps({"type": "session.created"}))
        registration = json.loads(await ws.recv())["session"]
        registrations.append(registration)
        await ws.send(json.dumps({"type": "session.updated", "session": registration}))
        seen, workers = set(), set()
        waiting = False

        async def speak(rid, text):
            await ws.send(json.dumps({"type": "response.output_audio_transcript.done", "response_id": rid, "transcript": text}))
            await ws.send(json.dumps({"type": "response.output_audio.delta", "response_id": rid,
                                      "delta": base64.b64encode(b"\x01\x00" * 1920).decode()}))
            await ws.send(json.dumps({"type": "response.output_audio.done", "response_id": rid}))

        async def answer(n):
            nonlocal waiting
            waiting = True
            # Exceeds the old 100ms provider answer deadline while audio keeps flowing.
            await asyncio.sleep(.3)
            await speak(f"ack-{n}-1", "Let me check the documentation.")
            await speak(f"ack-{n}-2", "Let me check the documentation.")
            if n == 4:
                # Exercise the idle-rotation boundary after every ACK has ended.
                # Shorten only this test's live setting; the hard cap stays bounded.
                s.voice_session_max_seconds = 1
                await asyncio.sleep(1.2)
            if answer_available:
                await speak(f"answer-{n}", "Product AX supports 10 connections.")
            waiting = False

        try:
            async for raw in ws:
                event = json.loads(raw)
                if event["type"] == "input_audio_buffer.append":
                    n = base64.b64decode(event["audio"])[0]
                    if n == 0 and waiting:
                        silence_during_wait.append(True)
                    if n == 0 or n in seen:
                        continue
                    seen.add(n)
                    await ws.send(json.dumps({"type": "input_audio_buffer.speech_started", "item_id": f"i{n}"}))
                    utterances = {1: "Hello.", 2: "How many connections does Product AX support?",
                                  3: "Thank you.", 4: "What was its connection limit again?"}
                    await ws.send(json.dumps({"type": "conversation.item.input_audio_transcription.completed",
                                              "item_id": f"i{n}", "transcript": utterances[n]}))
                    await ws.send(json.dumps({"type": "input_audio_buffer.speech_stopped", "item_id": f"i{n}"}))
                    if n in (1, 3):
                        await speak(f"answer-{n}", "Hello." if n == 1 else "You are welcome.")
                    else:
                        call = {"type": "response.function_call_arguments.done", "response_id": f"tool-{n}",
                                "call_id": f"call-{n}", "name": "search_knowledge_base",
                                "arguments": json.dumps({"query": "Product AX connection limit"})}
                        await ws.send(json.dumps(call))
                        await ws.send(json.dumps(call))  # same ID must execute only once
                        await speak(f"tool-{n}", "Let me check the documentation.")
                elif event["type"] == "conversation.item.create":
                    results.append(event["item"])
                    if answer_available and event["item"]["call_id"] == "call-2":
                        # A new native ID with a refined query must execute,
                        # even while the previous answer observer is pending.
                        await ws.send(json.dumps({
                            "type": "response.function_call_arguments.done", "response_id": "refine-2",
                            "call_id": "refined-2", "name": "search_knowledge_base",
                            "arguments": json.dumps({"query": "Product AX maximum simultaneous connections"}),
                        }))
                        await speak("refine-2", "Let me check the documentation.")
                        continue
                    task = asyncio.create_task(answer(int(event["item"]["call_id"].split("-")[1])))
                    workers.add(task)
                    task.add_done_callback(workers.discard)
        finally:
            for task in workers:
                task.cancel()
            await asyncio.gather(*workers, return_exceptions=True)

    cases = []
    for n in range(1, 5):
        with wave.open(str(tmp_path / f"input-{n}.wav"), "wb") as wav:
            wav.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
            wav.writeframes(bytes([n, 0]) * 1920)
        cases.append({"case_id": str(n), "wav": f"input-{n}.wav",
                      "expected_tool": "search_knowledge_base" if n in (2, 4) else None})
    case_file = tmp_path / "cases.json"
    case_file.write_text(json.dumps(cases))
    async with serve(voicechat, "127.0.0.1", 0) as endpoint, http_server(cuekb) as cuekb_url:
        s = configuration(tmp_path, voice_provider="nvidia", voicechat_api_key="fixture",
            voicechat_ws_url=f"ws://127.0.0.1:{endpoint.sockets[0].getsockname()[1]}",
            qa_provider_answer_timeout_ms=100)
        s.cuekb_base_url = cuekb_url
        app = create_app(s)
        async with http_server(app) as base:
            report = await run(Namespace(base_url=base, origin=s.public_origin, cases=case_file,
                case_timeout=5 if answer_available else 1, audio_dir=tmp_path / "audio", output=tmp_path / "report.json"))
            # Closing the connection also settles a pending answer observer.
            for _ in range(100):
                if not app.state.voice.sessions and not app.state.coordinator.awaiting_answers:
                    break
                await asyncio.sleep(.01)
            assert not app.state.voice.sessions
            assert not app.state.coordinator.awaiting_answers
    if not answer_available:
        assert not passed(report)
        assert report["error_type"] == "TimeoutError"
        assert len(report["cases"]) == 2
        assert report["cases"][-1]["speech_transport"] == "passed"
        assert report["cases"][-1]["answer_complete"] == "unverified"
        return
    assert passed(report), report
    assert len(registrations) == 1
    assert len(queries) == len(results) == 3
    assert queries[1].splitlines()[0] == "Product AX maximum simultaneous connections"
    assert [r["call_id"] for r in results] == ["call-2", "refined-2", "call-4"]
    assert silence_during_wait
    assert all(row["answer_complete"] == "passed" for row in report["cases"])
    assert [r["answer_response_id"] for r in report["cases"]] == [f"answer-{n}" for n in range(1, 5)]
    assert all(row.get("reason_code") is None for row in report["cases"])


@pytest.mark.parametrize("scenario", ["supplement", "empty", "duplicate", "post_terminal", "revoked", "one_search", "batch", "batch_revoked"])
async def test_native_input_search_budget_and_cumulative_answer(tmp_path, scenario):
    """Real API/HTTP/WS, deterministic provider; verifies control, not GPU speech."""
    from scripts.simulate_full_flow import http_server
    from scripts.validate_native_cuekb import passed, run

    queries, results, registrations, histories = [], [], [], []
    cuekb = FastAPI()

    @cuekb.post("/v1/search")
    async def search(request: Request):
        queries.append((await request.json())["query"])
        value = evidence(trace_id=f"00000000-0000-4000-8000-{len(queries):012d}")
        if len(queries) == 2 and scenario == "empty":
            value.update(retrieval_status="not_found", evidence_status="insufficient", hits=[])
        elif len(queries) == 2 and scenario in ("supplement", "post_terminal"):
            value["hits"][0].update(chunk_id="00000000-0000-4000-8000-000000000202",
                                    source_text="Product AX has 18 layers.")
        if len(queries) == 2 and scenario == "batch_revoked":
            app.state.registry.specs["search_knowledge"].enabled = False
        return value

    async def voicechat(ws):
        await ws.send(json.dumps({"type": "session.created"}))
        registration = json.loads(await ws.recv())["session"]
        registrations.append(registration)
        await ws.send(json.dumps({"type": "session.updated", "session": registration}))
        seen = set()

        async def speak(rid, text):
            await ws.send(json.dumps({"type": "response.output_audio_transcript.done", "response_id": rid, "transcript": text}))
            await ws.send(json.dumps({"type": "response.output_audio.delta", "response_id": rid,
                                      "delta": base64.b64encode(b"\x01\x00" * 1920).decode()}))
            await ws.send(json.dumps({"type": "response.output_audio.done", "response_id": rid}))

        async def call(cid, query):
            event = {"type": "response.function_call_arguments.done", "response_id": cid, "call_id": cid,
                     "name": "search_knowledge_base", "arguments": json.dumps({"query": query})}
            await ws.send(json.dumps(event))
            await ws.send(json.dumps(event))  # duplicate transport event is not another request
            await speak(cid, "Let me check the documentation.")

        async for raw in ws:
            event = json.loads(raw)
            if event["type"] == "input_audio_buffer.append":
                n = base64.b64decode(event["audio"])[0]
                if not n or n in seen:
                    continue
                seen.add(n)
                await ws.send(json.dumps({"type": "input_audio_buffer.speech_started", "item_id": f"i{n}"}))
                await ws.send(json.dumps({"type": "conversation.item.input_audio_transcription.completed",
                                          "item_id": f"i{n}", "transcript": "Tell me about Product AX."}))
                await ws.send(json.dumps({"type": "input_audio_buffer.speech_stopped", "item_id": f"i{n}"}))
                await call(f"initial-{n}", "Product AX connection limit")
            elif event["type"] == "conversation.item.create":
                results.append(event["item"])
                cid = event["item"]["call_id"]
                if cid == "initial-1" and scenario != "one_search":
                    if scenario == "revoked":
                        app.state.registry.specs["search_knowledge"].enabled = False
                    if scenario in ("batch", "batch_revoked"):
                        # A single native output contains two requests. Both
                        # arrive before the second query result is available.
                        for index, (call_id, query) in enumerate([
                            ("second-1", "Product AX layer count"), ("third-1", "Product AX price")]):
                            call_event = {"type": "response.function_call_arguments.done", "response_id": "batch-ack",
                                          "call_id": call_id, "name": "search_knowledge_base",
                                          "arguments": json.dumps({"query": query}),
                                          "native_batch_id": "batch-1", "native_batch_size": 2, "output_index": index}
                            await ws.send(json.dumps(call_event))
                            await ws.send(json.dumps(call_event))
                        await speak("batch-ack", "Let me check the documentation.")
                    else:
                        await call("second-1", "Product AX connection limit" if scenario in ("duplicate", "revoked")
                                   else "Product AX layer count")
                elif cid == "second-1" and scenario == "post_terminal":
                    await call("third-1", "Product AX price")
                else:
                    # Inspect the exact stored evidence used by the answer observer.
                    async with app.state.store.sessions() as db:
                        turns = (await db.execute(select(Turn).where(Turn.conversation_id == conversation_id()))).scalars()
                        histories.append([{"input_item_id": t.input_item_id, "answer": t.answer} for t in turns])
                    text = ("Product AX supports 10 connections and has 18 layers."
                            if cid == "second-1" and scenario == "supplement"
                            else "Product AX supports 10 connections. I cannot confirm the price from these excerpts.")
                    await speak(f"answer-{cid}", text)

    def conversation_id():
        return next(iter(app.state.voice.sessions.values())).conversation_id

    cases = []
    for n in (1, 2):
        path = tmp_path / f"budget-{n}.wav"
        with wave.open(str(path), "wb") as wav:
            wav.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
            wav.writeframes(bytes([n, 0]) * 1920)
        cases.append({"case_id": str(n), "wav": path.name, "expected_tool": "search_knowledge_base"})
    case_file = tmp_path / "cases.json"
    case_file.write_text(json.dumps(cases))
    async with serve(voicechat, "127.0.0.1", 0) as endpoint, http_server(cuekb) as cuekb_url:
        s = configuration(tmp_path, voice_provider="nvidia", voicechat_api_key="fixture",
                          voicechat_ws_url=f"ws://127.0.0.1:{endpoint.sockets[0].getsockname()[1]}",
                          voice_max_retrievals_per_input=1 if scenario == "one_search" else 2)
        s.cuekb_base_url = cuekb_url
        app = create_app(s)
        async with http_server(app) as base:
            report = await run(Namespace(base_url=base, origin=s.public_origin, cases=case_file,
                case_timeout=4, audio_dir=None, output=tmp_path / "report.json"))
    assert len(registrations) == 1
    if scenario == "post_terminal":
        assert not passed(report)
        assert report["cases"][0]["portal_error"] == "VOICE_TOOL_LOOP"
        assert report["cases"][0]["post_terminal_calls"] == 1
        assert len(results) == len(queries) == 2
        return
    if scenario == "batch_revoked":
        assert not passed(report)
        assert len(queries) == 2 and len(results) == 1
        return
    if scenario == "revoked":
        assert not passed(report)
        assert len(results) == len(queries) == 1
        return
    assert passed(report), report
    first = report["cases"][0]
    assert first["reason_code"] is None
    assert first["terminal_results_submitted"] == 1
    assert first["post_terminal_calls"] == 0
    if scenario == "batch":
        assert first["queued_calls_settled"] == 1
        assert first["tool_batches_submitted"] == 2
        assert len(results) == 3  # two batches for input 1; one for the next input
        assert len(queries) == 3  # two actual queries + next input; never the price query
        batch_result = results[1]
        assert batch_result["native_batch_id"] == "batch-1"
        assert batch_result["native_call_ids"] == ["second-1", "third-1"]
        outcomes = json.loads(batch_result["output"])["call_results"]
        assert [row["status"] for row in outcomes] == ["executed", "not_executed"]
    assert first["retrieval_executions"] == (1 if scenario in ("duplicate", "one_search") else 2)
    assert first["duplicate_queries"] == int(scenario == "duplicate")
    assert report["cases"][1]["retrieval_executions"] == 1  # new input, same query, fresh budget
    final = json.loads(results[0 if scenario == "one_search" else 1]["output"])
    assert final["search_state"] == "finished"
    assert "Do not call another tool" in final["instructions"]
    assert "10 connections" in final["sources"][0]["text"]
    if scenario == "supplement":
        assert len(final["sources"]) == 2
        assert "18 layers" in final["sources"][1]["text"]
    if scenario == "empty":
        assert final["partial"] == "true"
        assert first["latest_retrieval_status"] == "not_found"
        assert first["retrieval"] == "passed" and first["cumulative_sources"] == 1
    # The completed first answer, not just the wire payload, retains old facts.
    previous = next(t for t in histories[-1] if t["input_item_id"] == "i1" and t["answer"])
    assert previous["answer"]["status"] == "answered"
    assert len(previous["answer"]["citations"]) == (2 if scenario == "supplement" else 1)
