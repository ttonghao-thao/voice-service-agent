"""Full network flow: unpatched production adapters and independently running APIs."""

import asyncio
import json
import os
import signal
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

import httpx
import pytest
from network_stack import NetworkStack
from simulated_services import KB, PCM, QUESTION, SOURCE, SPOKEN
from websockets.asyncio.client import connect


@pytest.fixture(scope="module", params=["direct", "external"], ids=["mode_direct", "mode_external"])
def network_stack(tmp_path_factory, request):
    with NetworkStack(tmp_path_factory.mktemp("network-" + request.param), request.param) as stack:
        yield stack


@pytest.fixture
async def network(network_stack):
    async with httpx.AsyncClient(base_url=network_stack.api, trust_env=False, timeout=8) as client:
        for url in (network_stack.cuekb, network_stack.voice, network_stack.llm):
            assert (await client.delete(url + "/__test__/requests")).status_code == 200
        assert (await client.post(network_stack.voice + "/__test__/scenario", json={})).status_code == 200
        yield network_stack, client


async def new_call(client):
    response = await client.post("/api/v1/conversations", json={})
    assert response.status_code == 201
    body = response.json()
    return body["id"], {"Authorization": "Bearer " + body["access_token"]}


@asynccontextmanager
async def voice_call(stack, client, headers, cid, scenario="answer", questions=None):
    response = await client.post(
        stack.voice + "/__test__/scenario", json={"name": scenario, "questions": questions or [QUESTION]}
    )
    assert response.status_code == 200
    issued = await client.post(f"/api/v1/conversations/{cid}/voice-sessions", json={}, headers=headers)
    assert issued.status_code == 201, issued.text
    info = issued.json()
    async with connect(
        stack.api.replace("http:", "ws:") + info["ws_url"], origin="http://localhost:5173", proxy=None
    ) as ws:
        assert json.loads(await ws.recv())["type"] == "portal.session.ready"
        yield ws, info


async def audio(ws, epoch, seq=0):
    await ws.send(
        json.dumps(
            {
                "type": "portal.audio.append",
                "epoch": epoch,
                "seq": seq,
                "payload": {"format": "pcm16", "sample_rate": 24000, "audio": PCM},
            }
        )
    )


async def until(ws, event_type, response_id="answer-0"):
    result = []
    async with asyncio.timeout(6):
        while True:
            event = json.loads(await ws.recv())
            result.append(event)
            answer_boundary = (
                event_type not in ("portal.audio.done", "portal.audio.delta")
                or event["payload"].get("response_id") == response_id
            )
            if event["type"] == event_type and answer_boundary:
                return result
            assert event["type"] != "portal.error", event


async def messages(client, cid, headers, terminal=True):
    async with asyncio.timeout(6):
        while True:
            response = await client.get(f"/api/v1/conversations/{cid}/messages", headers=headers)
            assert response.status_code == 200
            result = response.json()
            if result["items"] and (
                not terminal or result["items"][-1]["status"] not in ("running", "awaiting_voice")
            ):
                return result
            await asyncio.sleep(0.02)


async def sse(client, cid, headers, after=0):
    events = []
    async with asyncio.timeout(6):
        async with client.stream(
            "GET", f"/api/v1/conversations/{cid}/events?after={after}", headers=headers
        ) as response:
            assert response.status_code == 200
            async for line in response.aiter_lines():
                if line.startswith("data: "):
                    event = json.loads(line[6:])
                    events.append(event)
                    if event["type"] == "portal.answer.final":
                        return events


async def stats(client, stack, kind):
    return (await client.get(getattr(stack, kind) + "/__test__/requests")).json()


async def test_end_to_end_api_audio_knowledge_speech_history_and_sse(network):
    stack, client = network
    ready = (await client.get("/health/ready")).json()
    assert ready["status"] == "ready" and ready["execution_mode"] == stack.mode
    caps = (await client.get("/api/v1/capabilities")).json()
    assert caps["text_available"] == (stack.mode == "external")
    cid, headers = await new_call(client)
    async with voice_call(stack, client, headers, cid) as (ws, info):
        live_sse = asyncio.create_task(sse(client, cid, headers))
        try:
            await audio(ws, info["epoch"])
            events = await until(ws, "portal.audio.done")
            snapshot = await messages(client, cid, headers)
            stream = await live_sse
        finally:
            live_sse.cancel()
            await asyncio.gather(live_sse, return_exceptions=True)
        turn = snapshot["items"][0]
        assert turn["user_text"] == QUESTION
        assert turn["execution_mode"] == stack.mode
        assert turn["status"] == ("voice_completed" if stack.mode == "direct" else "answered")
        answer = turn["answer"]
        assert answer["speech_text"] == SPOKEN
        assert answer["citations"][0]["content"] == SOURCE
        assert answer["citations"][0]["version_id"].endswith("103")
        assert answer["answer_origin"] == ("voicechat" if stack.mode == "direct" else "business_runtime")
        assert any(e["type"] == "portal.transcript.done" and e["payload"]["text"] == QUESTION for e in events)
        assert any(e["type"] == "portal.speech_text.done" and e["payload"]["text"] == SPOKEN for e in events)
        assert any(e["type"] == "portal.audio.delta" and e["payload"]["audio"] == PCM for e in events)
        seqs = [e["server_seq"] for e in stream]
        assert seqs == sorted(set(seqs))
        assert stream[-1]["payload"]["speech_text"] == SPOKEN
        knowledge_events = [e for e in stream if e["type"] == "portal.knowledge.ready"]
        assert bool(knowledge_events) == (stack.mode == "direct")
        if knowledge_events:
            assert "authorized_kb_ids" not in knowledge_events[0]["payload"]
            assert "_authorized_kb_ids" not in knowledge_events[0]
        replay = await sse(client, cid, headers, after=seqs[-2])
        assert [e["server_seq"] for e in replay] == [seqs[-1]]
        await ws.send(
            json.dumps(
                {
                    "type": "portal.playback.ack",
                    "epoch": info["epoch"],
                    "payload": {"response_id": "answer-0", "played_samples": 1920},
                }
            )
        )
        requests = await stats(client, stack, "cuekb")
        assert len(requests) == 1
        assert requests[0]["kb_ids"] == [KB] and requests[0]["include_context"] is True
        if stack.mode == "direct":
            assert requests[0]["filters"]["product_model"] == "AX"
            assert requests[0]["filters"]["software_version"] == "3.2"
        provider_requests = await stats(client, stack, "voice")
        assert len([r for r in provider_requests if r["type"] == "conversation.item.create"]) == 1
        assert len(await stats(client, stack, "llm")) == (0 if stack.mode == "direct" else 2)
    assert (
        await client.post(
            f"/api/v1/conversations/{cid}/interrupt",
            json={"expected_epoch": snapshot["epoch"]},
            headers=headers,
        )
    ).status_code == 200
    refreshed = (await client.get(f"/api/v1/conversations/{cid}/messages", headers=headers)).json()
    assert refreshed["items"][0]["answer"]["speech_text"] == SPOKEN


@pytest.mark.parametrize(
    "query,status,reason",
    [
        ("Find no evidence", "insufficient_evidence", None),
        ("Find unauthorized knowledge", "failed", "CUEKB_AUTH_FAILED"),
        ("Find forbidden knowledge", "failed", "CUEKB_FORBIDDEN"),
        ("Find rate limited knowledge", "failed", "CUEKB_RATE_LIMITED"),
        ("Find service error knowledge", "failed", "TOOL_UNAVAILABLE"),
        ("Find malformed knowledge", "failed", "TOOL_BAD_RESPONSE"),
    ],
)
async def test_knowledge_failures_remain_distinct_over_http(network, query, status, reason):
    stack, client = network
    cid, headers = await new_call(client)
    async with voice_call(stack, client, headers, cid, questions=[query]) as (ws, info):
        await audio(ws, info["epoch"])
        await until(ws, "portal.audio.done")
        turn = (await messages(client, cid, headers))["items"][0]
        assert turn["status"] == status, turn
        assert not turn["answer"]["citations"]
        if reason:
            assert turn["answer"]["reason_code"] == reason
        requests = await stats(client, stack, "cuekb")
        assert len(requests) == (2 if "service error" in query else 1)


async def test_direct_text_rejection_or_external_streaming_tool_loop(network):
    stack, client = network
    cid, headers = await new_call(client)
    headers = {**headers, "Idempotency-Key": "synthetic-text"}
    response = await client.post(
        f"/api/v1/conversations/{cid}/messages", headers=headers, json={"text": QUESTION}
    )
    if stack.mode == "direct":
        assert response.status_code == 409
        snapshot = (await client.get(f"/api/v1/conversations/{cid}/messages", headers=headers)).json()
        assert snapshot["request_revision"] == 0 and not snapshot["items"]
        assert not await stats(client, stack, "cuekb")
        assert not await stats(client, stack, "llm")
    else:
        assert response.status_code == 202
        turn = (await messages(client, cid, headers))["items"][0]
        assert turn["status"] == "answered" and turn["answer"]["citations"]
        requests = await stats(client, stack, "llm")
        assert len(requests) == 2 and all(r["stream"] for r in requests)
        repeated = await client.post(
            f"/api/v1/conversations/{cid}/messages", headers=headers, json={"text": QUESTION}
        )
        assert repeated.json()["turn_id"] == response.json()["turn_id"]
        assert len(await stats(client, stack, "llm")) == 2


async def test_call_token_isolation_ticket_single_use_and_end_revocation(network):
    stack, client = network
    cid, headers = await new_call(client)
    other_cid, other_headers = await new_call(client)
    assert headers != other_headers
    assert (await client.get(f"/api/v1/conversations/{cid}/messages", headers=other_headers)).status_code in (
        401,
        403,
        404,
    )
    assert (await client.get("/api/v1/admin/status", headers=headers)).status_code in (401, 403, 404)
    async with voice_call(stack, client, headers, cid) as (ws, info):
        await audio(ws, info["epoch"])
        await until(ws, "portal.audio.done")
        await messages(client, cid, headers)
        from websockets.exceptions import InvalidStatus

        with pytest.raises(InvalidStatus):
            async with connect(
                stack.api.replace("http:", "ws:") + info["ws_url"], origin="http://localhost:5173", proxy=None
            ):
                pass
    assert not (
        await client.get(f"/api/v1/conversations/{other_cid}/messages", headers=other_headers)
    ).json()["items"]
    assert (await client.delete(f"/api/v1/conversations/{cid}", headers=headers)).status_code == 200
    assert (await client.get(f"/api/v1/conversations/{cid}/messages", headers=headers)).status_code in (
        401,
        403,
        404,
    )


async def test_duplicate_native_call_queries_and_delivers_once(network):
    stack, client = network
    cid, headers = await new_call(client)
    async with voice_call(stack, client, headers, cid, "duplicate") as (ws, info):
        await audio(ws, info["epoch"])
        await until(ws, "portal.audio.done")
        assert len((await messages(client, cid, headers))["items"]) == 1
        assert len(await stats(client, stack, "cuekb")) == 1
        assert (
            len([r for r in await stats(client, stack, "voice") if r["type"] == "conversation.item.create"])
            == 1
        )


async def test_stop_playback_preserves_answer_and_allows_followup(network):
    stack, client = network
    cid, headers = await new_call(client)
    async with voice_call(stack, client, headers, cid, "delayed_answer", [QUESTION, QUESTION]) as (ws, info):
        await audio(ws, info["epoch"])
        await until(ws, "portal.audio.delta")
        await ws.send(
            json.dumps(
                {
                    "type": "portal.playback.stop",
                    "epoch": info["epoch"],
                    "payload": {"response_id": "answer-0"},
                }
            )
        )
        await until(ws, "portal.playback.clear")
        await until(ws, "portal.speech_text.done")
        first = await messages(client, cid, headers)
        assert first["items"][0]["answer"]["speech_text"] == SPOKEN
        assert any(
            r["kind"] == "voicechat_transcript" and r["payload"].get("text") == SPOKEN
            for r in first["records"]
        )
        await asyncio.sleep(0.2)
        await audio(ws, info["epoch"], 1)
        await until(ws, "portal.audio.done", response_id="answer-1")
        second = await messages(client, cid, headers)
        assert len(second["items"]) == 2
        assert second["items"][1]["answer"]["speech_text"] == SPOKEN
        assert len(await stats(client, stack, "cuekb")) == 2


async def test_cancel_slow_query_suppresses_late_evidence_and_output(network):
    stack, client = network
    cid, headers = await new_call(client)
    async with voice_call(stack, client, headers, cid, questions=["Find slow synthetic knowledge"]) as (
        ws,
        info,
    ):
        await audio(ws, info["epoch"])
        async with asyncio.timeout(5):
            for _ in range(250):
                if await stats(client, stack, "cuekb"):
                    break
                await asyncio.sleep(0.02)
            else:
                pytest.fail("CueKB did not receive the slow request")
        snapshot = await messages(client, cid, headers, terminal=False)
        response = await client.post(
            f"/api/v1/conversations/{cid}/tasks/current/cancel",
            headers=headers,
            json={"expected_epoch": snapshot["epoch"], "expected_revision": snapshot["request_revision"]},
        )
        assert response.status_code == 200 and response.json()["status"] == "canceled"
        await asyncio.sleep(1.7)
        canceled = await messages(client, cid, headers)
        assert canceled["items"][0]["status"] == "canceled"
        assert not canceled["items"][0]["knowledge_result"]
        assert not [r for r in await stats(client, stack, "voice") if r["type"] == "conversation.item.create"]


@pytest.mark.parametrize("scenario", ["disconnect", "voice_error", "no_completion"])
async def test_direct_voice_failure_retains_evidence_without_completed_answer(network, scenario):
    stack, client = network
    if stack.mode != "direct":
        pytest.skip("External answers finalize before voice; direct owns the completion waiter")
    cid, headers = await new_call(client)
    async with voice_call(stack, client, headers, cid, scenario) as (ws, info):
        await audio(ws, info["epoch"])
        snapshot = await messages(client, cid, headers)
        turn = snapshot["items"][0]
        assert turn["status"] == "failed" and turn["delivery_status"] == "voice_failed"
        assert turn["knowledge_result"]["citations"][0]["content"] == SOURCE
        assert turn["answer"]["status"] == "failed"
        assert turn["answer"]["answer_origin"] == "voicechat"


async def test_unbridged_voice_is_rejected_without_knowledge_access(network):
    stack, client = network
    cid, headers = await new_call(client)
    async with voice_call(stack, client, headers, cid, "unbridged") as (ws, info):
        await audio(ws, info["epoch"])
        events = await until(ws, "portal.error")
        assert events[-1]["payload"]["code"] == "VOICE_TOOL_REQUIRED"
        assert not await stats(client, stack, "cuekb")
        assert not await stats(client, stack, "llm")


async def test_degraded_evidence_remains_visible_with_service_reason(network):
    stack, client = network
    cid, headers = await new_call(client)
    async with voice_call(stack, client, headers, cid, questions=["Find degraded synthetic AX guide"]) as (
        ws,
        info,
    ):
        await audio(ws, info["epoch"])
        await until(ws, "portal.audio.done")
        turn = (await messages(client, cid, headers))["items"][0]
        assert turn["status"] == ("voice_completed" if stack.mode == "direct" else "answered")
        citation = turn["answer"]["citations"][0]
        assert citation["retrieval_status"] == "degraded"
        assert citation["degraded_reasons"] == ["synthetic-stage-unavailable"]


async def test_parallel_calls_have_separate_turns_tokens_and_upstream_sessions(network):
    stack, client = network
    first, second = await asyncio.gather(new_call(client), new_call(client))

    async def run(call):
        cid, headers = call
        async with voice_call(stack, client, headers, cid) as (ws, info):
            await audio(ws, info["epoch"])
            await until(ws, "portal.audio.done")
            result = await messages(client, cid, headers)
            assert len(result["items"]) == 1
            return result["items"][0]["id"]

    turns = await asyncio.gather(run(first), run(second))
    assert turns[0] != turns[1]
    assert len(await stats(client, stack, "cuekb")) == 2
    sessions = {r["session_id"] for r in await stats(client, stack, "voice") if r["type"] == "connected"}
    assert len(sessions) == 2


async def test_reconnect_rotates_epoch_and_passes_completed_history(network):
    stack, client = network
    cid, headers = await new_call(client)
    async with voice_call(stack, client, headers, cid) as (ws, first):
        await audio(ws, first["epoch"])
        await until(ws, "portal.audio.done")
        await messages(client, cid, headers)
    async with voice_call(stack, client, headers, cid) as (ws, second):
        assert second["epoch"] > first["epoch"]
        updates = [r for r in await stats(client, stack, "voice") if r["type"] == "session.update"]
        assert QUESTION in updates[-1]["session"]["instructions"]
        assert SPOKEN in updates[-1]["session"]["instructions"]
        await audio(ws, second["epoch"])
        await until(ws, "portal.audio.done")
        snapshot = await messages(client, cid, headers)
        assert len(snapshot["items"]) == 2
        assert {t["epoch"] for t in snapshot["items"]} == {first["epoch"], second["epoch"]}


async def test_provider_handshake_rejects_incorrect_audio_format(network):
    stack, client = network
    cid, headers = await new_call(client)
    await client.post(stack.voice + "/__test__/scenario", json={"name": "bad_format"})
    issued = (
        await client.post(f"/api/v1/conversations/{cid}/voice-sessions", json={}, headers=headers)
    ).json()
    async with connect(
        stack.api.replace("http:", "ws:") + issued["ws_url"], origin="http://localhost:5173", proxy=None
    ) as ws:
        event = json.loads(await ws.recv())
        assert event["type"] == "portal.error" and event["payload"]["code"] == "VOICE_PROTOCOL_ERROR"
    assert not await stats(client, stack, "cuekb")


async def test_process_crash_recovers_pending_turn_and_allows_new_call(network):
    stack, client = network
    cid, headers = await new_call(client)
    direct = stack.mode == "direct"
    async with voice_call(
        stack,
        client,
        headers,
        cid,
        "no_completion" if direct else "answer",
        [QUESTION if direct else "Find slow synthetic knowledge"],
    ) as (ws, info):
        await audio(ws, info["epoch"])
        async with asyncio.timeout(5):
            for _ in range(250):
                if direct:
                    ready = any(
                        r["type"] == "conversation.item.create" for r in await stats(client, stack, "voice")
                    )
                else:
                    ready = bool(await stats(client, stack, "cuekb"))
                if ready:
                    break
                await asyncio.sleep(0.01)
            else:
                pytest.fail("Pending turn did not reach the crash checkpoint")
        pending = (await messages(client, cid, headers, terminal=False))["items"][0]
        assert pending["status"] == ("awaiting_voice" if direct else "running")
        process = stack.processes[-1]
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=5)
    stack.api = await asyncio.to_thread(
        stack.start, "portal_app", port=urlsplit(stack.api).port, health="/health/ready"
    )
    recovered = await messages(client, cid, headers)
    previous = recovered["items"][0]
    assert previous["status"] == "expired" and previous["cancellation_reason"] == "service_restarted"
    assert previous["delivery_status"] == "discarded" and previous["answer"] is None
    if direct:
        assert previous["knowledge_result"]["citations"][0]["content"] == SOURCE
    async with voice_call(stack, client, headers, cid) as (ws, fresh):
        assert fresh["epoch"] > info["epoch"]
        updates = [r for r in await stats(client, stack, "voice") if r["type"] == "session.update"]
        assert SPOKEN not in updates[-1]["session"]["instructions"]
        await audio(ws, fresh["epoch"])
        await until(ws, "portal.audio.done")
        completed = await messages(client, cid, headers)
        assert len(completed["items"]) == 2
        assert completed["items"][-1]["answer"]["speech_text"] == SPOKEN
