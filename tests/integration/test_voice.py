import asyncio
import base64
import json

import pytest
from app.config import Settings
from app.main import create_app
from app.voice.provider import BRIDGE_ACK, QA_ACK, MockVoiceAdapter, VoiceEvent
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

pytestmark = pytest.mark.usefixtures("legacy_native_catalog")


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


@pytest.mark.parametrize("qa_mode", ["legacy", "dual_tools"])
@pytest.mark.parametrize("output_kind", ["speech_text.delta", "speech_text.done", "audio.delta"])
@pytest.mark.parametrize(
    "user_request,model_reply",
    [
        pytest.param("Hello.", "Hello! How can I help?", id="greeting"),
        pytest.param(
            "Hello, what is the documented connection limit for Product AX?",
            "Hello! Product AX supports ten connections.",
            id="greeting-and-product-question",
        ),
        pytest.param(
            "Is my account online right now?", "I cannot access live account data.", id="no-matching-tool"
        ),
        pytest.param("Hello.", None, id="unassociated-tool-ack"),
    ],
)
def test_voice_response_after_user_speech_requires_business_bridge(
    tmp_path, qa_mode, output_kind, user_request, model_reply,
):
    app = create_app(
        Settings(
            _env_file=None,
            auto_create_schema=True,
            qa_execution_mode=qa_mode,
            qa_answer_policy="knowledge_required",
            database_url=f"sqlite+aiosqlite:///{tmp_path}/required-bridge.db",
        )
    )
    pcm = base64.b64encode(b"\x01\x00" * 1920).decode()
    reply = model_reply if model_reply is not None else QA_ACK if qa_mode == "dual_tools" else BRIDGE_ACK

    class Scripted(MockVoiceAdapter):
        async def connect(self, summary):
            await self.queue.put(
                VoiceEvent("input.state", {"state": "speaking", "item_id": "input-1"})
            )
            await self.queue.put(
                VoiceEvent(
                    "transcript.done", {"item_id": "input-1", "text": user_request},
                )
            )
            await self.queue.put(VoiceEvent(output_kind, {"response_id": "unbridged", **(
                {"text": reply} if output_kind.startswith("speech_text") else {"audio": pcm}
            )}))

    class Recovery(MockVoiceAdapter):
        async def connect(self, summary):
            await self.queue.put(VoiceEvent("input.state", {"state": "speaking", "item_id": "retry-input"}))
            await self.queue.put(VoiceEvent("transcript.done", {
                "item_id": "retry-input", "text": "Find the integration sample",
            }))
            await self.queue.put(VoiceEvent("tool", {
                "response_id": "retry-tool-response", "call_id": "retry-call",
                "name": "lookup_knowledge" if qa_mode == "dual_tools" else "consult_service_agent",
                "arguments": json.dumps({"user_request": "Find the integration sample", **(
                    {"product_model": None, "software_version": None} if qa_mode == "dual_tools" else {})}),
            }))

        async def submit_tool_result(self, call_id, text):
            assert call_id == "retry-call"
            await self.queue.put(VoiceEvent("speech_text.done", {
                "response_id": "retry-answer", "text": json.loads(text)["speech_text"],
            }))
            await self.queue.put(VoiceEvent("audio.delta", {"response_id": "retry-answer", "audio": pcm}))
            await self.queue.put(VoiceEvent("audio.done", {"response_id": "retry-answer"}))

    with TestClient(app) as client:
        app.state.voice.provider_factory = Scripted
        cid = client.post("/api/v1/conversations", json={}).json()["id"]
        issued = client.post(f"/api/v1/conversations/{cid}/voice-sessions", json={}).json()
        with client.websocket_connect(
            issued["ws_url"], headers={"Origin": "http://localhost:5173"}
        ) as ws:
            events = []
            while not events or events[-1]["type"] != "portal.error":
                events.append(ws.receive_json())
            assert events[-1]["payload"]["code"] == "VOICE_TOOL_REQUIRED"
            assert events[-1]["payload"]["retryable"] is True
            assert any(e["type"] == "portal.transcript.done" and e["payload"]["text"] == user_request
                       for e in events)
            assert not any(e["type"].startswith(("portal.speech_text.", "portal.audio.")) for e in events)
            with pytest.raises(WebSocketDisconnect):
                ws.receive_json()
        data = client.get(f"/api/v1/conversations/{cid}/messages").json()
        assert data["items"] == []
        assert not any(r["kind"] == "voicechat_transcript" for r in data["records"])
        assert any(r["kind"] == "user_transcript" and r["payload"]["text"] == user_request
                   for r in data["records"])

        # A controlled failure must leave the conversation usable for a fresh
        # voice session with a properly authorized knowledge answer.
        app.state.voice.provider_factory = Recovery
        retried = client.post(f"/api/v1/conversations/{cid}/voice-sessions", json={})
        assert retried.status_code == 201
        retry_session = retried.json()
        assert retry_session["epoch"] > issued["epoch"]
        with client.websocket_connect(
            retry_session["ws_url"], headers={"Origin": "http://localhost:5173"}
        ) as ws:
            events = []
            while not events or events[-1]["type"] != "portal.audio.done":
                event = ws.receive_json()
                assert event["type"] != "portal.error", event
                events.append(event)
            assert any(e["type"] == "portal.speech_text.done" for e in events)
            assert any(e["type"] == "portal.audio.delta" and e["payload"]["audio"] == pcm for e in events)
        turns = client.get(f"/api/v1/conversations/{cid}/messages").json()["items"]
        assert len(turns) == 1
        assert turns[0]["input_item_id"] == "retry-input"
        assert turns[0]["answer"]["status"] == "answered"


@pytest.mark.parametrize("after_result", [False, True])
@pytest.mark.parametrize("qa_mode", ["legacy", "dual_tools"])
def test_empty_output_does_not_require_or_consume_a_tool_grant(tmp_path, after_result, qa_mode):
    app = create_app(Settings(_env_file=None, auto_create_schema=True,
        qa_execution_mode=qa_mode, qa_answer_policy="knowledge_required",
        database_url=f"sqlite+aiosqlite:///{tmp_path}/empty-output.db"))

    class Scripted(MockVoiceAdapter):
        async def empty_output(self):
            await self.queue.put(VoiceEvent("speech_text.delta", {"response_id": "empty", "text": ""}))
            await self.queue.put(VoiceEvent("speech_text.done", {"response_id": "empty", "text": " "}))
            await self.queue.put(VoiceEvent("audio.done", {"response_id": "empty"}))

        async def connect(self, summary):
            await self.queue.put(VoiceEvent("input.state", {"state": "speaking", "item_id": "input-1"}))
            await self.queue.put(VoiceEvent("transcript.done", {"item_id": "input-1", "text": "Find the integration sample"}))
            if not after_result:
                await self.empty_output()
            await self.queue.put(VoiceEvent("tool", {
                "response_id": "tool-response", "call_id": "call-1",
                "name": "lookup_knowledge" if qa_mode == "dual_tools" else "consult_service_agent",
                "arguments": json.dumps({"user_request": "Find the integration sample", **(
                    {"product_model": None, "software_version": None} if qa_mode == "dual_tools" else {})}),
            }))

        async def submit_tool_result(self, call_id, text):
            if after_result:
                # Neither an unrelated empty response nor the tool's empty end
                # can spend the one-response permission for the actual answer.
                await self.empty_output()
                await self.queue.put(VoiceEvent("audio.done", {"response_id": "tool-response"}))
            speech = json.loads(text)["speech_text"]
            first, rest = speech.split(" ", 1)
            for chunk in (first, " ", rest):
                await self.queue.put(VoiceEvent("speech_text.delta", {"response_id": "answer", "text": chunk}))
            await self.queue.put(VoiceEvent("speech_text.done", {
                "response_id": "answer", "text": speech,
            }))
            await self.queue.put(VoiceEvent("audio.done", {"response_id": "answer"}))

    with TestClient(app) as client:
        app.state.voice.provider_factory = Scripted
        cid = client.post("/api/v1/conversations", json={}).json()["id"]
        issued = client.post(f"/api/v1/conversations/{cid}/voice-sessions", json={}).json()
        with client.websocket_connect(issued["ws_url"], headers={"Origin": "http://localhost:5173"}) as ws:
            events = []
            while not events or events[-1]["type"] not in ("portal.error", "portal.audio.done"):
                events.append(ws.receive_json())
            assert events[-1]["type"] == "portal.audio.done", events
            assert events[-1]["payload"]["response_id"] == "answer"
            spoken = next(e for e in events if e["type"] == "portal.speech_text.done")
            assert "".join(e["payload"]["text"] for e in events if e["type"] == "portal.speech_text.delta") == spoken["payload"]["text"]
            assert not any(e["payload"].get("response_id") == "empty" for e in events)


@pytest.mark.parametrize("output_kind", ["speech_text.done", "audio.delta", "session.ended", "disconnect"])
@pytest.mark.parametrize("qa_mode", ["legacy", "dual_tools"])
@pytest.mark.parametrize("end_from_portal", [False, True])
def test_voice_failure_preserves_queued_final_transcript_before_cleanup(tmp_path, output_kind, qa_mode, end_from_portal):
    app = create_app(Settings(_env_file=None, auto_create_schema=True,
        qa_execution_mode=qa_mode, qa_answer_policy="knowledge_required",
        database_url=f"sqlite+aiosqlite:///{tmp_path}/rejection.db"))
    sending_transcript = asyncio.Event()
    release_close = asyncio.Event()
    stream_finished = asyncio.Event()
    close_started = []

    async def delayed_stream(ws, sid, ticket):
        original_send = ws.send_json

        async def send(data):
            if data["type"] == "portal.transcript.done" and not sending_transcript.is_set():
                sending_transcript.set()
                await asyncio.Event().wait()  # Cancelled when the unbridged output arrives.
            if data["type"] == "portal.error":
                assert not close_started  # Error delivery must precede provider/DB cleanup.
            await original_send(data)

        ws.send_json = send
        try:
            await original_stream(ws, sid, ticket)
        finally:
            stream_finished.set()

    class Scripted(MockVoiceAdapter):
        async def connect(self, summary):
            await self.queue.put(VoiceEvent("input.state", {"state": "speaking", "item_id": "input-1"}))
            await self.queue.put(VoiceEvent("transcript.done", {
                "item_id": "input-1", "text": "o, please introduce MONKEY MOONCAKE products.",
            }))

        async def events(self):
            yield await self.queue.get()
            yield await self.queue.get()
            await sending_transcript.wait()
            if output_kind == "disconnect":
                return
            if output_kind == "session.ended":
                yield VoiceEvent(output_kind)
            else:
                yield VoiceEvent(output_kind, {"response_id": "unbridged", **(
                    {"text": "An unsupported product answer."} if output_kind.startswith("speech_text")
                    else {"audio": "AAAA"})})
            await asyncio.Event().wait()

        async def close(self):
            close_started.append(True)
            if end_from_portal:
                await release_close.wait()
            return True

    async def wait_for_cleanup():
        await asyncio.wait_for(stream_finished.wait(), 2)

    with TestClient(app) as client:
        original_stream = app.state.voice.stream
        app.state.voice.stream = delayed_stream
        app.state.voice.provider_factory = Scripted
        cid = client.post("/api/v1/conversations", json={}).json()["id"]
        issued = client.post(f"/api/v1/conversations/{cid}/voice-sessions", json={}).json()
        with client.websocket_connect(issued["ws_url"], headers={"Origin": "http://localhost:5173"}) as ws:
            events = []
            while not events or events[-1]["type"] != "portal.error":
                events.append(ws.receive_json())
            assert [e["type"] for e in events] == [
                "portal.session.ready", "portal.input.state", "portal.transcript.done", "portal.error"]
            assert events[-2]["payload"]["text"] == "o, please introduce MONKEY MOONCAKE products."
            assert events[-1]["payload"]["code"] == (
                "VOICE_UNAVAILABLE" if output_kind in ("session.ended", "disconnect") else "VOICE_TOOL_REQUIRED")
            assert events[-1]["payload"]["retryable"] is True
            assert [e["server_seq"] for e in events] == sorted(e["server_seq"] for e in events)
            if end_from_portal:
                # VoiceClient.stop sends this DELETE as soon as it receives
                # portal.error, while the upstream close can still be pending.
                assert client.delete(f"/api/v1/conversations/{cid}/voice-sessions/{issued['voice_session_id']}").status_code == 200
                client.portal.call(release_close.set)
            client.portal.call(wait_for_cleanup)
            data = client.get(f"/api/v1/conversations/{cid}/messages").json()
            assert any(r["kind"] == "voice_session_end" for r in data["records"])
            with pytest.raises(WebSocketDisconnect):
                ws.receive_json()
        data = client.get(f"/api/v1/conversations/{cid}/messages").json()
        assert data["items"] == []
        assert any(r["kind"] == "user_transcript" for r in data["records"])


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
@pytest.mark.parametrize("qa_mode", ["legacy", "dual_tools"])
def test_fast_tool_result_during_ack_preserves_exactly_one_answer_permission(
    tmp_path, original_is_ack, stopped_original, qa_mode,
):
    app = create_app(Settings(
        _env_file=None, auto_create_schema=True,
        qa_execution_mode=qa_mode, qa_answer_policy="knowledge_required",
        database_url=f"sqlite+aiosqlite:///{tmp_path}/fast-result.db",
    ))
    ack = QA_ACK if qa_mode == "dual_tools" else BRIDGE_ACK

    class Scripted(MockVoiceAdapter):
        async def connect(self, summary):
            await self.queue.put(VoiceEvent("input.state", {"state": "speaking", "item_id": "input"}))
            await self.queue.put(VoiceEvent("transcript.done", {
                "item_id": "input", "text": "Find the integration sample",
            }))
            await self.queue.put(VoiceEvent("tool", {
                "response_id": "tool-response", "call_id": "call",
                "name": "lookup_knowledge" if qa_mode == "dual_tools" else "consult_service_agent",
                "arguments": json.dumps({"user_request": "Find the integration sample", **(
                    {"product_model": None, "software_version": None} if qa_mode == "dual_tools" else {})}),
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
                "text": ack if original_is_ack else "The verified answer.",
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
        assert ack not in spoken


@pytest.mark.parametrize("qa_mode", ["legacy", "dual_tools"])
def test_repeated_tool_ack_returns_the_answer_permission(tmp_path, qa_mode):
    app = create_app(Settings(
        _env_file=None, auto_create_schema=True,
        qa_execution_mode=qa_mode, qa_answer_policy="knowledge_required",
        database_url=f"sqlite+aiosqlite:///{tmp_path}/repeated-tool-ack.db",
    ))
    ack = QA_ACK if qa_mode == "dual_tools" else BRIDGE_ACK

    class Scripted(MockVoiceAdapter):
        async def connect(self, summary):
            await self.queue.put(VoiceEvent("input.state", {"state": "speaking", "item_id": "input"}))
            await self.queue.put(VoiceEvent("transcript.done", {
                "item_id": "input", "text": "Find the integration sample",
            }))
            await self.queue.put(VoiceEvent("tool", {
                "response_id": "tool-response", "call_id": "call",
                "name": "lookup_knowledge" if qa_mode == "dual_tools" else "consult_service_agent",
                "arguments": json.dumps({"user_request": "Find the integration sample", **(
                    {"product_model": None, "software_version": None} if qa_mode == "dual_tools" else {})}),
            }))

        async def submit_tool_result(self, call_id, text):
            speech = json.loads(text)["speech_text"]
            # VoiceChat coalesced a repeated model function call, replayed the
            # cached result, and emitted the fixed ACK under a new response ID.
            await self.queue.put(VoiceEvent("speech_text.done", {
                "response_id": "repeated-ack", "text": ack,
            }))
            await self.queue.put(VoiceEvent("audio.done", {"response_id": "repeated-ack"}))
            await self.queue.put(VoiceEvent("speech_text.done", {
                "response_id": "answer", "text": speech,
            }))
            await self.queue.put(VoiceEvent("audio.done", {"response_id": "answer"}))
            # The answer consumes the restored grant. No further response is
            # authorized by the same tool result.
            await self.queue.put(VoiceEvent("speech_text.delta", {
                "response_id": "unrelated", "text": "An unbridged answer.",
            }))

    with TestClient(app) as client:
        app.state.voice.provider_factory = Scripted
        cid = client.post("/api/v1/conversations", json={}).json()["id"]
        issued = client.post(f"/api/v1/conversations/{cid}/voice-sessions", json={}).json()
        with client.websocket_connect(
            issued["ws_url"], headers={"Origin": "http://localhost:5173"}
        ) as ws:
            events = []
            for _ in range(16):
                event = ws.receive_json()
                events.append(event)
                if event["type"] == "portal.error":
                    assert event["payload"]["code"] == "VOICE_TOOL_REQUIRED"
                    break
            else:
                pytest.fail("Unrelated output must be rejected after the tool answer")
        records = client.get(f"/api/v1/conversations/{cid}/messages").json()["records"]
        spoken = [record["payload"]["text"] for record in records
                  if record["kind"] == "voicechat_transcript"]
        assert ack not in spoken
        assert len(spoken) == 1 and "synthetic integration result" in spoken[0].lower()



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
