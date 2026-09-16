import json

import pytest
from app.config import Settings
from app.main import create_app
from app.voice.provider import MockVoiceAdapter, VoiceEvent
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect


def test_native_bridge_dedup_tool_before_transcript_and_ticket(tmp_path):
    app = create_app(
        Settings(
            _env_file=None, auto_create_schema=True, database_url=f"sqlite+aiosqlite:///{tmp_path}/ws.db"
        )
    )
    returns = []

    class Scripted(MockVoiceAdapter):
        async def connect(self, summary):
            call = VoiceEvent(
                "tool",
                {
                    "call_id": "same",
                    "name": "consult_service_agent",
                    "arguments": json.dumps({"user_request": "张先生的联调示例"}, ensure_ascii=False),
                },
            )
            await self.queue.put(call)
            await self.queue.put(call)
            await self.queue.put(
                VoiceEvent("transcript.done", {"item_id": "late", "text": "张先生的联调示例"})
            )

        async def submit_tool_result(self, call_id, text):
            returns.append((call_id, json.loads(text)))
            await self.queue.put(
                VoiceEvent(
                    "speech_text.done",
                    {"response_id": "r1", "item_id": "spoken", "text": "合成联调结果已返回。"},
                )
            )

    with TestClient(app) as client:
        app.state.voice.provider_factory = Scripted
        cid = client.post("/api/v1/conversations", json={}).json()["id"]
        issued = client.post(f"/api/v1/conversations/{cid}/voice-sessions", json={}).json()
        with client.websocket_connect(issued["ws_url"], headers={"Origin": "http://localhost:5173"}) as ws:
            assert ws.receive_json()["type"] == "portal.session.ready"
            assert ws.receive_json()["type"] == "portal.transcript.done"
            assert ws.receive_json()["type"] == "portal.speech_text.done"
            assert len(returns) == 1 and returns[0][0] == "same"
            assert "合成" in returns[0][1]["speech_text"]
            with pytest.raises(WebSocketDisconnect):
                with client.websocket_connect(issued["ws_url"], headers={"Origin": "http://localhost:5173"}):
                    pass
        data = client.get(f"/api/v1/conversations/{cid}/messages").json()
        assert len(data["items"]) == 1
        assert len([r for r in data["records"] if r["kind"] == "user_transcript"]) == 1


def test_input_activity_never_cancels_without_explicit_control(tmp_path):
    app = create_app(
        Settings(
            _env_file=None,
            auto_create_schema=True,
            database_url=f"sqlite+aiosqlite:///{tmp_path}/input-state.db",
        )
    )

    class Scripted(MockVoiceAdapter):
        async def connect(self, summary):
            await self.queue.put(VoiceEvent("input.state", {"state": "speaking"}))
            await self.queue.put(VoiceEvent("input.state", {"state": "quiet"}))

    with TestClient(app) as client:
        app.state.voice.provider_factory = Scripted
        cid = client.post("/api/v1/conversations", json={}).json()["id"]
        issued = client.post(f"/api/v1/conversations/{cid}/voice-sessions", json={}).json()
        with client.websocket_connect(issued["ws_url"], headers={"Origin": "http://localhost:5173"}) as ws:
            assert ws.receive_json()["type"] == "portal.session.ready"
            assert ws.receive_json()["payload"]["state"] == "speaking"
            assert ws.receive_json()["payload"]["state"] == "quiet"
            assert client.get(f"/api/v1/conversations/{cid}/messages").json()["items"] == []
