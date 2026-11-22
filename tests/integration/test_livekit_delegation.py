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
        weather_mode="real", weather_base_url="https://weather.fixture", weather_api_key="weather-fixture",
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


@pytest.mark.parametrize(
    ("task_text", "transcript", "expected_tool", "expected_content", "expected_urls"),
    [
        ("CUEKB: Find the integration sample.", "", "search_knowledge_base", "search", ["/v2/evidence/query", "/v2/evidence/validate"]),
        ("CUEKB: Do not choose this rewrite.", "Find the integration sample.", "search_knowledge_base", "search", ["/v2/evidence/query", "/v2/evidence/validate"]),
        ("WEATHER: Beijing | today", "", "weather", "22.0 °C", ["/v1/places/resolve", "/v1/weather"]),
        ("WEB_SEARCH: latest Wi-Fi 7 standard | pm", "", "web_search", "https://example.org/wifi", ["/res/v1/web/search"]),
        ("WEB_SEARCH: confidential company notes", "", None, "澄清", []),
        ("WEATHER: | tomorrow", "", None, "澄清", []),
        ("WEB_SEARCH: public product launch", "", "web_search", "查询暂时失败", ["/res/v1/web/search"]),
        ("WEB_SEARCH: public outdated result | pm", "", "web_search", "不要播报旧问题", ["/res/v1/web/search"]),
    ],
)
async def test_room_delegation_executes_and_settles_once(
    tmp_path, task_text, transcript, expected_tool, expected_content, expected_urls,
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
                assert body["kb_ids"] == [config.knowledge_base_ids]
                return httpx.Response(200, json=evidence_v21(body))
            return httpx.Response(200, json=validate_v21(body, [config.knowledge_base_ids]))
        place = {"id": "bjs", "name": "Beijing", "country_code": "CN", "timezone": "Asia/Shanghai"}
        if request.url.path.endswith("/resolve"):
            return httpx.Response(200, json={"places": [place]})
        if request.url.path.endswith("/weather"):
            stamp = datetime.now(UTC).isoformat()
            return httpx.Response(200, json={
                "status": "ok", "place": place, "kind": "current", "valid_at": stamp, "fetched_at": stamp,
                "temperature": {"value": 22, "unit": "C"}, "condition": "Clear",
                "source": {"provider": "fixture", "is_mock": False}, "stale": False,
            })
        assert request.headers["X-Subscription-Token"] == "search-fixture"
        if "launch" in request.url.params["q"]:
            return httpx.Response(503)
        if "outdated" in request.url.params["q"]:
            call.inputs.update(True)
        assert request.url.params["freshness"] == "pm"
        return httpx.Response(200, json={"web": {"results": [
            {"url": "https://example.org/wifi", "title": "Wi-Fi 7", "description": "<strong>Public</strong> specifications"},
        ]}})

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.MockTransport(backend)) as upstream:
            app.state.registry.adapters["cuekb_http"].client = upstream
            app.state.registry.adapters["weather_http"].client = upstream
            app.state.registry.adapters["brave_http"].client = upstream
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                cid = (await client.post("/api/v1/conversations", json={})).json()["id"]
                issued = (await client.post(f"/api/v1/conversations/{cid}/voice-sessions")).json()
                session = app.state.voice.sessions[issued["voice_session_id"]]
                call = RoomCall(app.state.voice, None, session)
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
                    if expected_tool:
                        rows = (await client.get(f"/api/v1/conversations/{cid}/messages")).json()["items"]
                        assert len(rows) == 1 and rows[0]["selected_tool"] == expected_tool
                        if transcript:
                            assert rows[0]["user_text"] == transcript
                            assert "request_origin" not in rows[0]
                        else:
                            assert rows[0]["request_origin"] == "delegation"
                finally:
                    worker.cancel()
                    await asyncio.gather(worker, return_exceptions=True)
                    await call.room.disconnect()
