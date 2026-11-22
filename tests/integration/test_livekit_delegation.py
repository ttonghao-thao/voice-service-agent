"""Exercise the Room delegation router through the real coordinator and registry."""

import asyncio
import json
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest
from app.config import Settings
from app.main import create_app
from app.voice.livekit_room import RoomCall, RoomInputs, browser_credentials
from livekit import api
from test_native_cuekb import evidence_v21, validate_v21


def settings(tmp_path):
    return Settings(
        _env_file=None, auto_create_schema=True,
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'room.db'}",
        qa_execution_mode="dual_tools", agent_provider="disabled", voice_provider="sophon_live",
        sophon_live_ws_url="ws://127.0.0.1:18782/v1/live/sessions", livekit_url="ws://127.0.0.1:7880",
        livekit_api_key="test-key", livekit_api_secret="test-room-secret-with-at-least-32-bytes",
        cuekb_mode="real", cuekb_base_url="https://cuekb.fixture", cuekb_api_key="cuekb-fixture",
        weather_mode="real", weather_base_url="https://fixture.qweatherapi.com", weather_api_key="weather-fixture",
        web_search_api_key="search-fixture", enabled_tools="search_knowledge,weather,web_search",
    )


def test_room_token_allows_audio_only_in_its_own_room(tmp_path):
    config = settings(tmp_path)
    credentials = browser_credentials(config, "session-a")
    claims = api.TokenVerifier("test-key", config.livekit_api_secret.get_secret_value()).verify(credentials["token"])
    assert claims.identity == "caller"
    assert claims.video.room == "voice-session-a"
    assert not claims.video.can_publish_data
    assert claims.video.can_publish_sources == ["microphone"]


def test_stale_and_competing_speech_have_no_owner():
    import time

    inputs = RoomInputs()
    assert inputs.at_delegation(time.monotonic()) is None

    inputs.update(True)
    inputs.update(False)
    previous = inputs.latest
    assert inputs.at_delegation(time.monotonic()) is previous
    previous.ended -= 1
    inputs.update(True)
    inputs.update(False)
    assert inputs.at_delegation(time.monotonic()) is None

def test_only_exchange_settled_before_new_speech_releases_old_input():
    import time

    inputs = RoomInputs()
    inputs.update(True)
    inputs.update(False)
    old = inputs.latest
    old.claimed = True
    old.settled_at = time.monotonic()
    inputs.update(True)
    inputs.update(False)
    assert inputs.at_delegation(time.monotonic()) is inputs.latest
    # Captions may be delayed; do not reassign them just because a tool settled.
    assert inputs.at_delegation(time.monotonic(), exclude_settled=False) is None
    old.settled_at = None
    assert inputs.at_delegation(time.monotonic()) is None
    old.settled_at = inputs.latest.started + .01
    assert inputs.at_delegation(time.monotonic()) is None



@pytest.mark.parametrize(
    ("task_text", "transcript", "private_context", "expected_tool", "expected_content", "expected_urls"),
    [
        ("CUEKB: Find the integration sample.", "", False, "search_knowledge_base", "search", ["/v2/evidence/query", "/v2/evidence/validate"]),
        ("CUEKB: Find the integration sample.", "model is WRONG9. What is the weather?", False, "search_knowledge_base", "search", ["/v2/evidence/query", "/v2/evidence/validate"]),
        ("CUEKB:", "Find the integration sample.", False, None, "澄清", []),
        ("WEATHER: Beijing | today", "What is the weather in London?", False, "weather", "22.0 °C", ["/geo/v2/city/lookup", "/weather/v1/current/39.9/116.4"]),
        ("WEB_SEARCH: latest Wi-Fi 7 standard | pm", "confidential transcription error", False, "web_search", "https://example.org/wifi", ["/search"]),
        ("WEB_SEARCH: latest Wi-Fi 7 standard | pm", "latest Wi-Fi 7 standard", True, None, "开启新会话", []),
        ("WEB_SEARCH: confidential company notes", "", False, None, "澄清", []),
        ("WEATHER: | tomorrow", "", False, None, "澄清", []),
        ("WEB_SEARCH: public product launch", "", False, "web_search", "查询暂时失败", ["/search"]),
        ("WEB_SEARCH: public outdated result | pm", "", False, "web_search", "不要播报旧问题", ["/search"]),
    ],
)
async def test_room_delegation_executes_and_settles_once(
    tmp_path, task_text, transcript, private_context, expected_tool, expected_content, expected_urls,
):
    config = settings(tmp_path)
    app = create_app(config)
    requests = []

    async def backend(request):
        requests.append(request.url.path)
        if request.url.host == "cuekb.fixture":
            body = json.loads(request.content)
            assert request.headers["Authorization"] == "Bearer cuekb-fixture"
            if request.url.path.endswith("/query"):
                assert body["question"] == "Find the integration sample."
                assert body["search_query"] == body["question"]
                assert body["kb_ids"] == [config.knowledge_base_ids]
                return httpx.Response(200, json=evidence_v21(body))
            return httpx.Response(200, json=validate_v21(body, [config.knowledge_base_ids]))
        if request.url.host == "fixture.qweatherapi.com":
            assert request.headers["X-QW-Api-Key"] == "weather-fixture"
            if request.url.path.endswith("lookup"):
                assert request.url.params["location"] == "Beijing"
                return httpx.Response(200, json={"code": "200", "location": [{
                    "id": "bjs", "name": "Beijing", "country": "China", "tz": "Asia/Shanghai", "lat": "39.9", "lon": "116.4",
                }]})
            return httpx.Response(200, json={
                "validTime": datetime.now(UTC).isoformat(), "temperature": {"value": 22, "unit": "°C"},
                "condition": {"text": "Clear"}, "metadata": {"attributions": ["https://www.qweather.com"]},
            })
        assert request.headers["Authorization"] == "Bearer search-fixture"
        assert request.method == "POST" and request.url.host == "api.tavily.com"
        search_body = json.loads(request.content)
        if "launch" in search_body["query"]:
            return httpx.Response(503)
        if "outdated" in search_body["query"]:
            call.inputs.update(True)
        else:
            assert search_body["query"] == "latest Wi-Fi 7 standard"
        assert search_body["time_range"] == "month"
        return httpx.Response(200, json={"results": [
            {"url": "https://example.org/wifi", "title": "Wi-Fi 7", "content": "<strong>Public</strong> specifications"},
        ]})

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.MockTransport(backend)) as upstream:
            app.state.registry.adapters["cuekb_http"].client = upstream
            app.state.registry.adapters["weather_http"].client = upstream
            app.state.registry.adapters["tavily_http"].client = upstream
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                cid = (await client.post("/api/v1/conversations", json={})).json()["id"]
                issued = (await client.post(f"/api/v1/conversations/{cid}/voice-sessions")).json()
                session = app.state.voice.sessions[issued["voice_session_id"]]
                call = RoomCall(app.state.voice, None, session)
                call.sent_private_content = private_context
                call.inputs.update(True)
                call.inputs.update(False)
                results = asyncio.Queue()

                def append(content, *, delegation_id):
                    call.sent({"type": "session.commentary.append", "delegation_id": delegation_id, "event_id": "event1"})
                    call.receive({"type": "session.commentary.appended", "client_event_id": "event1"})
                    results.put_nowait(content)

                call.duplex = SimpleNamespace(append_commentary=append)
                raw = {"type": "session.delegation.created",
                       "delegation": {"id": "task1", "type": "delegation", "target": "client", "task": task_text}}
                if transcript:
                    call.receive({"type": "session.input_transcript.delta", "delta": transcript, "start_ms": 100, "end_ms": 900})
                call.receive(raw)
                event = SimpleNamespace(id="task1", pending_transcript=transcript)
                call.delegate(event)
                call.delegate(event)
                worker = asyncio.create_task(call.run_tools())
                try:
                    content = await asyncio.wait_for(results.get(), 4)
                    assert expected_content in content
                    if expected_tool == "search_knowledge_base":
                        assert json.loads(content[content.index("{"):])["search"] == "finished"
                    assert requests == expected_urls
                    assert call.calls.empty()
                    if transcript:
                        call.transcript_finished(SimpleNamespace(is_final=True, transcript=transcript))
                        events = [call.outgoing.get_nowait() for _ in range(call.outgoing.qsize())]
                        assert [e["payload"]["text"] for e in events if e["type"] == "portal.transcript.done"] == [transcript]
                    if expected_tool:
                        rows = (await client.get(f"/api/v1/conversations/{cid}/messages")).json()["items"]
                        assert len(rows) == 1 and rows[0]["selected_tool"] == expected_tool
                        assert rows[0]["user_text"] == task_text.split(":", 1)[1].strip()
                        assert rows[0]["request_origin"] == "delegation"
                finally:
                    worker.cancel()
                    await asyncio.gather(worker, return_exceptions=True)
                    await call.room.disconnect()


async def test_two_fast_turns_can_repeat_query_without_reexecuting_duplicate_ids(tmp_path):
    from pydantic import SecretStr

    config = settings(tmp_path)
    config.cuekb_base_url, config.cuekb_api_key = "", SecretStr("")
    config.enabled_tools = "weather,web_search"
    app = create_app(config)
    requests = []

    def backend(request):
        requests.append(request)
        return httpx.Response(200, json={"results": [
            {"title": "Public standard", "url": "https://example.org/standard", "content": "Published public information"},
        ]})

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.MockTransport(backend)) as upstream:
            app.state.registry.adapters["tavily_http"].client = upstream
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                cid = (await client.post("/api/v1/conversations", json={})).json()["id"]
                issued = (await client.post(f"/api/v1/conversations/{cid}/voice-sessions")).json()
                call = RoomCall(app.state.voice, None, app.state.voice.sessions[issued["voice_session_id"]])
                results = asyncio.Queue()

                def append(content, *, delegation_id):
                    event_id = "reply-" + delegation_id
                    call.sent({"type": "session.commentary.append", "delegation_id": delegation_id, "event_id": event_id})
                    call.receive({"type": "session.commentary.appended", "client_event_id": event_id})
                    results.put_nowait((delegation_id, content))

                call.duplex = SimpleNamespace(append_commentary=append)
                worker = asyncio.create_task(call.run_tools())
                try:
                    for index in range(3):
                        call.inputs.update(True)
                        call.inputs.update(False)
                        call_id = f"task-{index}"
                        call.receive({"type": "session.delegation.created", "delegation": {
                            "id": call_id, "type": "delegation", "target": "client",
                            "task": ["WEB_SEARCH:", "CUEKB: public standards", "WEB_SEARCH: public standards | pm"][index],
                        }})
                        event = SimpleNamespace(id=call_id, pending_transcript="")
                        call.delegate(event)
                        call.delegate(event)
                        _, content = await asyncio.wait_for(results.get(), 3)
                        assert ("澄清" if index == 0 else "https://example.org/standard") in content
                        async with asyncio.timeout(3):
                            while call.inputs.latest.settled_at is None:
                                call.inputs.changed.clear()
                                await call.inputs.changed.wait()
                        assert call.inputs.latest.settled_at is not None
                    assert len(requests) == 2 and call.calls.empty()
                    rows = (await client.get(f"/api/v1/conversations/{cid}/messages")).json()["items"]
                    assert len(rows) == 2 and {row["selected_tool"] for row in rows} == {"web_search"}
                finally:
                    worker.cancel()
                    await asyncio.gather(worker, return_exceptions=True)
                    await call.room.disconnect()
