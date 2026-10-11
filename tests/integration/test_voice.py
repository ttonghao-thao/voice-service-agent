import json
import logging

import pytest
from app.config import Settings
from app.main import create_app
from app.voice.provider import BRIDGE_ACK, MockVoiceAdapter, VoiceEvent
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect


@pytest.fixture
def voice_logs(caplog):
    logger = logging.getLogger("app.voice.gateway")
    logger.addHandler(caplog.handler)
    try:
        yield caplog
    finally:
        logger.removeHandler(caplog.handler)


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


@pytest.mark.parametrize("output_kind", ["speech_text.delta", "audio.delta"])
@pytest.mark.parametrize("empty_prefix_count", [0, 40])
def test_voice_response_after_user_speech_requires_business_bridge(
    tmp_path, voice_logs, output_kind, empty_prefix_count,
):
    app = create_app(
        Settings(
            _env_file=None,
            auto_create_schema=True,
            database_url=f"sqlite+aiosqlite:///{tmp_path}/required-bridge.db",
        )
    )

    class Scripted(MockVoiceAdapter):
        async def events(self):
            for index in range(empty_prefix_count):
                yield VoiceEvent("audio.done", {"response_id": f"empty-{index}"})
            async for event in super().events():
                yield event

        async def connect(self, summary):
            await self.queue.put(
                VoiceEvent("input.state", {"state": "speaking", "item_id": "input-1"})
            )
            await self.queue.put(
                VoiceEvent(
                    output_kind,
                    {
                        "response_id": "unbridged",
                        "text": "I can answer directly.",
                        # Even all-zero, nonempty PCM requires authorization.
                        "audio": "AAA=",
                    },
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
            # The writer can deliver the already queued input state before the
            # receiver rejects the following unbridged response.
            if error["type"] == "portal.input.state":
                assert error["payload"] == {"state": "speaking"}
                error = ws.receive_json()
            assert error["type"] == "portal.error"
            assert error["payload"]["code"] == "VOICE_TOOL_REQUIRED"

    rejection = next(r.message for r in voice_logs.records if "voice_unbridged_response_rejected" in r.message)
    diagnostic = json.loads(rejection.split("diagnostic=", 1)[1])
    assert diagnostic["customer_input_seen"] is True
    assert diagnostic["recent_events"][-1]["kind"] == output_kind
    assert diagnostic["recent_events"][-1]["response_id"] == "unbridged"
    assert diagnostic["recent_events"][-2]["item_id"] == "input-1"
    assert len(diagnostic["recent_events"]) == min(32, empty_prefix_count + 2)
    assert diagnostic["recent_events"][-1]["seq"] == empty_prefix_count + 2
    assert "I can answer directly." not in rejection
    assert "AAA=" not in rejection


@pytest.mark.parametrize("after_tool_result", [False, True])
@pytest.mark.parametrize("empty_event", [
    VoiceEvent("audio.done", {"response_id": "answer"}),
    VoiceEvent("audio.delta", {"response_id": "answer", "audio": ""}),
    VoiceEvent("speech_text.delta", {"response_id": "answer", "text": " "}),
    VoiceEvent("speech_text.done", {"response_id": "answer", "text": ""}),
])
def test_empty_unowned_output_neither_disconnects_nor_spends_answer_grant(
    tmp_path, voice_logs, after_tool_result, empty_event,
):
    app = create_app(Settings(
        _env_file=None, auto_create_schema=True,
        database_url=f"sqlite+aiosqlite:///{tmp_path}/empty-output.db",
    ))

    class Scripted(MockVoiceAdapter):
        async def connect(self, summary):
            await self.queue.put(VoiceEvent("input.state", {"state": "speaking", "item_id": "input"}))
            if not after_tool_result:
                await self.queue.put(empty_event)
            await self.queue.put(VoiceEvent("transcript.done", {
                "item_id": "input", "text": "Find the integration sample",
            }))
            await self.queue.put(VoiceEvent("tool", {
                "response_id": "tool-response", "call_id": "call", "name": "consult_service_agent",
                "arguments": json.dumps({"user_request": "Find the integration sample"}),
            }))

        async def submit_tool_result(self, call_id, text):
            if after_tool_result:
                await self.queue.put(VoiceEvent("speech_text.done", {
                    "response_id": "tool-response", "text": BRIDGE_ACK,
                }))
                await self.queue.put(VoiceEvent("audio.done", {"response_id": "tool-response"}))
                # A repeated ACK end must not consume the answer permission.
                await self.queue.put(VoiceEvent("audio.done", {"response_id": "tool-response"}))
                await self.queue.put(empty_event)
                # A second orphan ID exposes accidental consumption of a grant,
                # even when the first empty event used the eventual answer ID.
                await self.queue.put(VoiceEvent("audio.done", {"response_id": "orphan"}))
            await self.queue.put(VoiceEvent("speech_text.done", {
                "response_id": "answer", "text": "The verified answer.",
            }))
            await self.queue.put(VoiceEvent("audio.done", {"response_id": "answer"}))
            await self.queue.put(VoiceEvent("speech_text.delta", {
                "response_id": "unrelated", "text": "An unbridged answer.",
            }))

    with TestClient(app) as client:
        app.state.voice.provider_factory = Scripted
        cid = client.post("/api/v1/conversations", json={}).json()["id"]
        issued = client.post(f"/api/v1/conversations/{cid}/voice-sessions", json={}).json()
        with client.websocket_connect(issued["ws_url"], headers={"Origin": "http://localhost:5173"}) as ws:
            for _ in range(10):
                event = ws.receive_json()
                if event["type"] == "portal.error":
                    assert event["payload"]["code"] == "VOICE_TOOL_REQUIRED"
                    break
                assert event["payload"].get("response_id") != "orphan"
            else:
                pytest.fail("Unrelated output must still be rejected")
        records = client.get(f"/api/v1/conversations/{cid}/messages").json()["records"]
        spoken = [r["payload"]["text"] for r in records if r["kind"] == "voicechat_transcript"]
        assert spoken == ["The verified answer."]
    rejection = next(r.message for r in voice_logs.records if "voice_unbridged_response_rejected" in r.message)
    diagnostic = json.loads(rejection.split("diagnostic=", 1)[1])
    events = diagnostic["recent_events"]
    tool_event = next(e for e in events if e["kind"] == "tool")
    result_event = next(e for e in events if e["kind"] == "tool.result.submitted")
    assert tool_event["call_id"] == result_event["call_id"] == "call"
    assert tool_event["response_id"] == result_event["response_id"] == "tool-response"
    assert tool_event["seq"] < result_event["seq"] < events[-1]["seq"]
    assert events[-1]["response_id"] == "unrelated"
    assert diagnostic["pending_tools"] == {"running": 0, "ready": 0, "sent": 1}
    assert "Find the integration sample" not in rejection
    assert "The verified answer." not in rejection
    assert BRIDGE_ACK not in rejection


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


@pytest.mark.parametrize("closed_initial", [False, True])
def test_initial_continuous_response_does_not_silently_swallow_customer_reply(tmp_path, closed_initial):
    """Mirror VoiceChat's audio-before-ASR ordering and session-long response ID."""
    import base64

    app = create_app(
        Settings(
            _env_file=None,
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
        _env_file=None, auto_create_schema=True,
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
        assert ("The verified answer." in spoken) == (original_is_ack or not stopped_original)
        assert BRIDGE_ACK not in spoken



def test_initial_greeting_overlapping_user_input_is_suppressed_without_false_lifecycle_error(tmp_path):
    app = create_app(Settings(
        _env_file=None, auto_create_schema=True,
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
