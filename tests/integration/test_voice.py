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
            await self.queue.put(
                VoiceEvent("input.state", {"state": "speaking", "item_id": "input-1"})
            )
            call = VoiceEvent(
                "tool",
                {
                    "response_id": "tool-response-1",
                    "call_id": "same",
                    "name": "consult_service_agent",
                    "arguments": json.dumps({"user_request": "Find the integration sample"}),
                },
            )
            await self.queue.put(call)
            await self.queue.put(call)
            await self.queue.put(
                VoiceEvent(
                    "transcript.done",
                    {"item_id": "input-1", "text": "Find the integration sample"},
                )
            )

        async def submit_tool_result(self, call_id, text):
            returns.append((call_id, json.loads(text)))
            await self.queue.put(
                VoiceEvent(
                    "speech_text.done",
                    {"response_id": "r1", "item_id": "spoken", "text": "Synthetic integration result returned."},
                )
            )

    with TestClient(app) as client:
        app.state.voice.provider_factory = Scripted
        cid = client.post("/api/v1/conversations", json={}).json()["id"]
        issued = client.post(f"/api/v1/conversations/{cid}/voice-sessions", json={}).json()
        with client.websocket_connect(issued["ws_url"], headers={"Origin": "http://localhost:5173"}) as ws:
            assert ws.receive_json()["type"] == "portal.session.ready"
            assert ws.receive_json()["type"] == "portal.input.state"
            assert ws.receive_json()["type"] == "portal.transcript.done"
            assert ws.receive_json()["type"] == "portal.speech_text.done"
            assert len(returns) == 1 and returns[0][0] == "same"
            assert "synthetic" in returns[0][1]["speech_text"].lower()
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
            await self.queue.put(
                VoiceEvent("input.state", {"state": "speaking", "item_id": "input-1"})
            )
            await self.queue.put(
                VoiceEvent("input.state", {"state": "quiet", "item_id": "input-1"})
            )

    with TestClient(app) as client:
        app.state.voice.provider_factory = Scripted
        cid = client.post("/api/v1/conversations", json={}).json()["id"]
        issued = client.post(f"/api/v1/conversations/{cid}/voice-sessions", json={}).json()
        with client.websocket_connect(issued["ws_url"], headers={"Origin": "http://localhost:5173"}) as ws:
            assert ws.receive_json()["type"] == "portal.session.ready"
            assert ws.receive_json()["payload"]["state"] == "speaking"
            assert ws.receive_json()["payload"]["state"] == "quiet"
            assert client.get(f"/api/v1/conversations/{cid}/messages").json()["items"] == []


def test_voice_response_after_user_speech_requires_business_bridge(tmp_path):
    app = create_app(
        Settings(
            _env_file=None,
            auto_create_schema=True,
            database_url=f"sqlite+aiosqlite:///{tmp_path}/required-bridge.db",
        )
    )

    class Scripted(MockVoiceAdapter):
        async def connect(self, summary):
            await self.queue.put(
                VoiceEvent("input.state", {"state": "speaking", "item_id": "input-1"})
            )
            await self.queue.put(
                VoiceEvent(
                    "speech_text.delta",
                    {"response_id": "unbridged", "text": "I can answer directly."},
                )
            )

    with TestClient(app) as client:
        app.state.voice.provider_factory = Scripted
        cid = client.post("/api/v1/conversations", json={}).json()["id"]
        issued = client.post(f"/api/v1/conversations/{cid}/voice-sessions", json={}).json()
        with client.websocket_connect(
            issued["ws_url"], headers={"Origin": "http://localhost:5173"}
        ) as ws:
            assert ws.receive_json()["type"] == "portal.session.ready"
            error = ws.receive_json()
            assert error["type"] == "portal.error"
            assert error["payload"]["code"] == "VOICE_TOOL_REQUIRED"


def test_tool_without_customer_input_is_settled_without_creating_turn(tmp_path):
    app = create_app(
        Settings(
            _env_file=None,
            auto_create_schema=True,
            database_url=f"sqlite+aiosqlite:///{tmp_path}/unbound-tool.db",
        )
    )
    returns = []

    class Scripted(MockVoiceAdapter):
        async def connect(self, summary):
            await self.queue.put(
                VoiceEvent(
                    "tool",
                    {
                        "response_id": "initial-response",
                        "call_id": "initial-call",
                        "name": "consult_service_agent",
                        "arguments": json.dumps({"user_request": "Invented request"}),
                    },
                )
            )

        async def submit_tool_result(self, call_id, text):
            returns.append((call_id, json.loads(text)))
            await self.queue.put(
                VoiceEvent("input.state", {"state": "quiet", "item_id": "idle-input"})
            )

    with TestClient(app) as client:
        app.state.voice.provider_factory = Scripted
        cid = client.post("/api/v1/conversations", json={}).json()["id"]
        issued = client.post(f"/api/v1/conversations/{cid}/voice-sessions", json={}).json()
        with client.websocket_connect(
            issued["ws_url"], headers={"Origin": "http://localhost:5173"}
        ) as ws:
            assert ws.receive_json()["type"] == "portal.session.ready"
            assert ws.receive_json()["type"] == "portal.input.state"
        assert returns[0][0] == "initial-call"
        assert returns[0][1]["status"] == "failed"
        assert client.get(f"/api/v1/conversations/{cid}/messages").json()["items"] == []


def test_final_transcript_is_authoritative_for_voice_turn(tmp_path):
    app = create_app(
        Settings(
            _env_file=None,
            auto_create_schema=True,
            database_url=f"sqlite+aiosqlite:///{tmp_path}/authoritative-transcript.db",
        )
    )

    class Scripted(MockVoiceAdapter):
        async def connect(self, summary):
            await self.queue.put(
                VoiceEvent("input.state", {"state": "speaking", "item_id": "input-1"})
            )
            await self.queue.put(
                VoiceEvent(
                    "tool",
                    {
                        "response_id": "tool-response",
                        "call_id": "call-1",
                        "name": "consult_service_agent",
                        "arguments": json.dumps({"user_request": "Wrong rewritten request"}),
                    },
                )
            )
            await self.queue.put(
                VoiceEvent(
                    "transcript.done",
                    {"item_id": "input-1", "text": "Find the integration sample"},
                )
            )

        async def submit_tool_result(self, call_id, text):
            await self.queue.put(
                VoiceEvent(
                    "speech_text.done",
                    {"response_id": "spoken-response", "text": "Synthetic result."},
                )
            )

    with TestClient(app) as client:
        app.state.voice.provider_factory = Scripted
        cid = client.post("/api/v1/conversations", json={}).json()["id"]
        issued = client.post(f"/api/v1/conversations/{cid}/voice-sessions", json={}).json()
        with client.websocket_connect(
            issued["ws_url"], headers={"Origin": "http://localhost:5173"}
        ) as ws:
            assert ws.receive_json()["type"] == "portal.session.ready"
            assert ws.receive_json()["type"] == "portal.input.state"
            assert ws.receive_json()["type"] == "portal.transcript.done"
            assert ws.receive_json()["type"] == "portal.speech_text.done"
        turn = client.get(f"/api/v1/conversations/{cid}/messages").json()["items"][0]
        assert turn["user_text"] == "Find the integration sample"


def test_new_input_state_does_not_reject_authorized_tool_output(tmp_path):
    app = create_app(
        Settings(
            _env_file=None,
            auto_create_schema=True,
            database_url=f"sqlite+aiosqlite:///{tmp_path}/authorized-output.db",
        )
    )

    class Scripted(MockVoiceAdapter):
        async def connect(self, summary):
            await self.queue.put(
                VoiceEvent("input.state", {"state": "speaking", "item_id": "input-1"})
            )
            await self.queue.put(
                VoiceEvent(
                    "tool",
                    {
                        "response_id": "tool-response",
                        "call_id": "call-1",
                        "name": "consult_service_agent",
                        "arguments": json.dumps({"user_request": "Find the integration sample"}),
                    },
                )
            )
            await self.queue.put(
                VoiceEvent(
                    "transcript.done",
                    {"item_id": "input-1", "text": "Find the integration sample"},
                )
            )

        async def submit_tool_result(self, call_id, text):
            await self.queue.put(
                VoiceEvent("input.state", {"state": "speaking", "item_id": "input-2"})
            )
            await self.queue.put(
                VoiceEvent(
                    "speech_text.delta",
                    {"response_id": "spoken-response", "text": "Authorized result"},
                )
            )

    with TestClient(app) as client:
        app.state.voice.provider_factory = Scripted
        cid = client.post("/api/v1/conversations", json={}).json()["id"]
        issued = client.post(f"/api/v1/conversations/{cid}/voice-sessions", json={}).json()
        with client.websocket_connect(
            issued["ws_url"], headers={"Origin": "http://localhost:5173"}
        ) as ws:
            assert ws.receive_json()["type"] == "portal.session.ready"
            assert ws.receive_json()["type"] == "portal.input.state"
            assert ws.receive_json()["type"] == "portal.transcript.done"
            assert ws.receive_json()["type"] == "portal.input.state"
            spoken = ws.receive_json()
            assert spoken["type"] == "portal.speech_text.delta"
            assert spoken["payload"]["text"] == "Authorized result"
