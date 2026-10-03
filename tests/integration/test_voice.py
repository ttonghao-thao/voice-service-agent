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
            _env_file=None, agent_provider="mock", auto_create_schema=True, database_url=f"sqlite+aiosqlite:///{tmp_path}/ws.db"
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
                VoiceEvent("transcript.delta", {"item_id": "input-1", "text": "Find the "})
            )
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
            await self.queue.put(
                VoiceEvent(
                    "speech_text.done",
                    {"response_id": "r1", "item_id": "spoken", "text": "A second spoken sentence."},
                )
            )

    with TestClient(app) as client:
        app.state.voice.provider_factory = Scripted
        cid = client.post("/api/v1/conversations", json={}).json()["id"]
        issued = client.post(f"/api/v1/conversations/{cid}/voice-sessions", json={}).json()
        with client.websocket_connect(issued["ws_url"], headers={"Origin": "http://localhost:5173"}) as ws:
            assert ws.receive_json()["type"] == "portal.session.ready"
            assert ws.receive_json()["type"] == "portal.input.state"
            assert ws.receive_json()["type"] == "portal.transcript.delta"
            assert ws.receive_json()["type"] == "portal.transcript.done"
            first_speech = ws.receive_json()
            second_speech = ws.receive_json()
            assert first_speech["type"] == second_speech["type"] == "portal.speech_text.done"
            assert [first_speech["payload"]["segment_index"], second_speech["payload"]["segment_index"]] == [0, 1]
            assert len(returns) == 1 and returns[0][0] == "same"
            assert "synthetic" in returns[0][1]["speech_text"].lower()
            with pytest.raises(WebSocketDisconnect):
                with client.websocket_connect(issued["ws_url"], headers={"Origin": "http://localhost:5173"}):
                    pass
        data = client.get(f"/api/v1/conversations/{cid}/messages").json()
        assert len(data["items"]) == 1
        assert data["items"][0]["input_item_id"] == "input-1"
        assert len([r for r in data["records"] if r["kind"] == "user_transcript"]) == 1
        spoken = [r for r in data["records"] if r["kind"] == "voicechat_transcript"]
        assert len(spoken) == 2
        assert all(r["payload"]["turn_id"] == data["items"][0]["id"] for r in spoken)
        assert all(r["payload"]["phase"] == "answer" for r in spoken)


def test_input_activity_never_cancels_without_explicit_control(tmp_path):
    app = create_app(
        Settings(
            _env_file=None, agent_provider="mock",
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
            _env_file=None, agent_provider="mock",
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
            if error["type"] == "portal.input.state":
                error = ws.receive_json()
            assert error["type"] == "portal.error"
            assert error["payload"]["code"] == "VOICE_TOOL_REQUIRED"


def test_tool_without_customer_input_is_settled_without_creating_turn(tmp_path):
    app = create_app(
        Settings(
            _env_file=None, agent_provider="mock",
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
            _env_file=None, agent_provider="mock",
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
            _env_file=None, agent_provider="mock",
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


@pytest.mark.parametrize("closed_initial", [False, True])
def test_initial_continuous_response_does_not_silently_swallow_customer_reply(tmp_path, closed_initial):
    """Mirror VoiceChat's audio-before-ASR ordering and session-long response ID."""
    import base64

    app = create_app(
        Settings(
            _env_file=None, agent_provider="mock",
            auto_create_schema=True,
            database_url=f"sqlite+aiosqlite:///{tmp_path}/continuous-response.db",
        )
    )

    class Scripted(MockVoiceAdapter):
        async def connect(self, summary):
            await self.queue.put(VoiceEvent("audio.delta", {
                "response_id": "initial-stream",
                "audio": base64.b64encode(bytes(3840)).decode(),
            }))
            await self.queue.put(VoiceEvent("speech_text.done", {"response_id": "initial-stream", "text": ""}))
            if closed_initial:
                await self.queue.put(VoiceEvent("audio.done", {"response_id": "initial-stream"}))
            await self.queue.put(VoiceEvent("input.state", {
                "state": "speaking", "item_id": "user-1",
            }))
            await self.queue.put(VoiceEvent("speech_text.delta", {
                "response_id": "new-reply" if closed_initial else "initial-stream",
                "text": "An unverified spoken reply.",
            }))

    with TestClient(app) as client:
        app.state.voice.provider_factory = Scripted
        cid = client.post("/api/v1/conversations", json={}).json()["id"]
        issued = client.post(f"/api/v1/conversations/{cid}/voice-sessions", json={}).json()
        with client.websocket_connect(issued["ws_url"], headers={"Origin": "http://localhost:5173"}) as ws:
            for _ in range(4):
                event = ws.receive_json()
                assert event["type"] not in ("portal.audio.delta", "portal.speech_text.delta")
                if event["type"] == "portal.error":
                    break
            else:
                pytest.fail("Incompatible or unbridged output must be surfaced")
            expected = "VOICE_TOOL_REQUIRED" if closed_initial else "VOICE_PROTOCOL_ERROR"
            assert event["payload"]["code"] == expected
        assert client.get(f"/api/v1/conversations/{cid}/messages").json()["items"] == []


@pytest.mark.parametrize("original_is_ack", [False, True])
@pytest.mark.parametrize("stopped_original", [False, True])
def test_fast_tool_result_during_ack_preserves_exactly_one_answer_permission(tmp_path, original_is_ack, stopped_original):
    from app.voice.provider import BRIDGE_ACK

    app = create_app(Settings(
        _env_file=None, agent_provider="mock", auto_create_schema=True,
        database_url=f"sqlite+aiosqlite:///{tmp_path}/fast-result.db",
    ))

    class Scripted(MockVoiceAdapter):
        async def connect(self, summary):
            await self.queue.put(VoiceEvent("input.state", {"state": "speaking", "item_id": "input"}))
            await self.queue.put(VoiceEvent("transcript.done", {
                "item_id": "input", "text": "Find the integration sample",
            }))
            await self.queue.put(VoiceEvent("tool", {
                "response_id": "tool-response", "call_id": "call", "name": "consult_service_agent",
                "arguments": json.dumps({"user_request": "Find the integration sample"}),
            }))

        async def submit_tool_result(self, call_id, text):
            # Deliberately produce the ACK after the fast business result was sent.
            if stopped_original:
                session = next(iter(app.state.voice.sessions.values()))
                await app.state.voice.suppress_playback(session.conversation_id, session.epoch, "tool-response")
            await self.queue.put(VoiceEvent("speech_text.delta", {
                "response_id": "tool-response",
                "text": "Please wait " if original_is_ack else "The verified ",
            }))
            await self.queue.put(VoiceEvent("speech_text.done", {
                "response_id": "tool-response",
                "text": BRIDGE_ACK if original_is_ack else "The verified answer.",
            }))
            await self.queue.put(VoiceEvent("audio.done", {"response_id": "tool-response"}))
            if original_is_ack:
                await self.queue.put(VoiceEvent("speech_text.done", {
                    "response_id": "answer", "text": "The verified answer.",
                }))
                await self.queue.put(VoiceEvent("audio.done", {"response_id": "answer"}))
            # Neither original-response nor follow-up-response mode may leave
            # a spare grant that would authorize an unrelated answer.
            await self.queue.put(VoiceEvent("speech_text.delta", {
                "response_id": "unrelated", "text": "An unbridged answer.",
            }))

    with TestClient(app) as client:
        app.state.voice.provider_factory = Scripted
        cid = client.post("/api/v1/conversations", json={}).json()["id"]
        issued = client.post(f"/api/v1/conversations/{cid}/voice-sessions", json={}).json()
        with client.websocket_connect(issued["ws_url"], headers={"Origin": "http://localhost:5173"}) as ws:
            spoken = []
            for _ in range(12):
                event = ws.receive_json()
                if event["type"] == "portal.speech_text.done":
                    spoken.append(event["payload"]["text"])
                if event["type"] == "portal.error":
                    assert event["payload"]["code"] == "VOICE_TOOL_REQUIRED"
                    break
            else:
                pytest.fail("Unrelated output must be rejected")
            # The writer may be cancelled when the deliberate final failure arrives;
            # use stored transcript evidence to verify accepted responses reliably.
        records = client.get(f"/api/v1/conversations/{cid}/messages").json()["records"]
        spoken = [r["payload"]["text"] for r in records if r["kind"] == "voicechat_transcript"]
        assert "The verified answer." in spoken  # Stop clears audio, retaining authorized spoken text.
        assert BRIDGE_ACK not in spoken



def test_initial_greeting_overlapping_user_input_is_suppressed_without_false_lifecycle_error(tmp_path):
    app = create_app(Settings(
        _env_file=None, agent_provider="mock", auto_create_schema=True,
        database_url=f"sqlite+aiosqlite:///{tmp_path}/greeting-overlap.db",
    ))

    class Scripted(MockVoiceAdapter):
        async def connect(self, summary):
            for event in [
                VoiceEvent("speech_text.delta", {"response_id": "greeting", "text": "Welcome"}),
                VoiceEvent("input.state", {"state": "speaking", "item_id": "input"}),
                VoiceEvent("speech_text.delta", {"response_id": "greeting", "text": " back."}),
                VoiceEvent("speech_text.done", {"response_id": "greeting", "text": "Welcome back."}),
                VoiceEvent("audio.done", {"response_id": "greeting"}),
                VoiceEvent("transcript.done", {"item_id": "input", "text": "hello"}),
            ]:
                await self.queue.put(event)

    with TestClient(app) as client:
        app.state.voice.provider_factory = Scripted
        cid = client.post("/api/v1/conversations", json={}).json()["id"]
        issued = client.post(f"/api/v1/conversations/{cid}/voice-sessions", json={}).json()
        with client.websocket_connect(issued["ws_url"], headers={"Origin": "http://localhost:5173"}) as ws:
            assert ws.receive_json()["type"] == "portal.session.ready"
            assert ws.receive_json()["type"] == "portal.input.state"
            assert ws.receive_json()["type"] == "portal.transcript.done"
