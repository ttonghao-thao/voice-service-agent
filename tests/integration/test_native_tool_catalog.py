import asyncio
import base64
import copy
import json
import logging

import httpx
import pytest
from app.agent_runtime.context import RunContext
from app.agent_runtime.tool_catalog import NativeToolCatalog, NativeToolEntry
from app.config import Settings
from app.contracts import DomainError, Principal
from app.main import create_app
from app.voice.provider import MockVoiceAdapter, NvidiaVoiceChatAdapter, VoiceEvent, session_update
from fastapi.testclient import TestClient
from websockets.asyncio.server import serve

pytestmark = pytest.mark.usefixtures("legacy_native_catalog")


def configured(tmp_path, **extra):
    return Settings(_env_file=None, auto_create_schema=True, qa_execution_mode="dual_tools",
        dev_admin=True, database_url=f"sqlite+aiosqlite:///{tmp_path}/catalog.db", **extra)


def three_tools(items):
    result = copy.deepcopy(items)
    third = copy.deepcopy(result[0])
    third.update(name="lookup_product_manual", description="Look up a documented product manual procedure.",
                 ack_messages=["I will check the product manual."], examples=[])
    result.append(third)
    return result


def test_admin_configures_three_tools_and_snapshot_preserves_trusted_schemas(tmp_path):
    app = create_app(configured(tmp_path))
    with TestClient(app) as client:
        original = client.get("/api/v1/admin/voice-tools").json()
        items = three_tools(original["items"])
        response = client.put("/api/v1/admin/voice-tools", json={"expected_revision": 0, "items": items})
        assert response.status_code == 200, response.text
        assert response.json()["revision"] == 1
        caps = client.get("/api/v1/capabilities").json()
        assert caps["native_tools"] == [item["name"] for item in items]
        assert caps["native_tool_revision"] == 1
        cid = client.post("/api/v1/conversations", json={}).json()["id"]
        issued = client.post(f"/api/v1/conversations/{cid}/voice-sessions", json={}).json()
        session = app.state.voice.sessions[issued["voice_session_id"]]
        assert len(session.registered_tools) == 3
        assert session.tool_catalog["lookup_product_manual"].executor == app.state.coordinator.runtime.direct.run
        assert session.tool_catalog["lookup_product_manual"].kind == "direct"
        definition = session.tool_definitions[-1]
        assert definition["ack_messages"] == ["I will check the product manual."]
        assert definition["parameters"]["additionalProperties"] is False
        assert "use null" in definition["parameters"]["properties"]["product_model"]["description"]
        # Names in the prompt come only from registered definitions, not fixed routing rules.
        update = session_update("", [definition], "general_qa")
        assert "reason_over_knowledge" not in update["session"]["instructions"]
        assert update["session"]["tools"][0]["ack_messages"] == definition["ack_messages"]


@pytest.mark.parametrize("change", ["unknown", "duplicate", "example", "schema", "non_ascii", "empty_ack"])
def test_invalid_candidate_does_not_replace_live_configuration(tmp_path, change):
    with TestClient(create_app(configured(tmp_path))) as client:
        items = client.get("/api/v1/admin/voice-tools").json()["items"]
        if change == "unknown":
            items[0]["implementation"] = "uninstalled_executor"
        elif change == "duplicate":
            items.append(copy.deepcopy(items[0]))
        elif change == "example":
            items[0]["examples"] = [{"user_request": "Incomplete arguments"}]
        elif change == "schema":
            items[0]["parameters"] = {"type": "object"}
        elif change == "non_ascii":
            items[0]["description"] = "中文"
        else:
            items[0]["ack_messages"] = ["   "]
        response = client.put("/api/v1/admin/voice-tools", json={"expected_revision": 0, "items": items})
        assert response.status_code == 422
        assert client.get("/api/v1/admin/voice-tools").json()["revision"] == 0
        assert len(client.get("/api/v1/capabilities").json()["native_tools"]) == 2


async def test_configuration_is_shared_across_instances_and_execution_keeps_permissions(tmp_path):
    app = create_app(configured(tmp_path))
    async with app.router.lifespan_context(app):
        runtime = app.state.coordinator.runtime
        catalog = runtime.catalog
        other = NativeToolCatalog(runtime.dispatcher, app.state.registry)
        _, items = await catalog.configuration()
        entries = [NativeToolEntry.model_validate(item) for item in three_tools([value.model_dump() for value in items])]
        assert await catalog.update(entries, 0, "operator") == 1
        assert len((await other.snapshot()).tools) == 3
        with pytest.raises(DomainError) as stale:
            await other.update(items, 0, "other-operator")
        assert stale.value.code == "STALE_TOOL_CONFIGURATION"
        principal = Principal(user_id="no-business-permission")
        snapshot = await other.snapshot()
        decision = runtime.dispatcher.resolve("lookup_product_manual",
            '{"user_request":"Read AX manual","product_model":null,"software_version":null}',
            tuple(snapshot.tools), catalog=snapshot.tools)
        # The public catalog is not filtered by this principal, but invocation is.
        result = await runtime.run_selected(decision, "Read AX manual", RunContext(principal, "cid", "tid", 0), [])
        assert result.reason_code == "FORBIDDEN"


def test_dynamic_refresh_closes_old_session_and_next_session_registers_latest_tools(tmp_path):
    app = create_app(configured(tmp_path))
    connected = []

    class RecordingAdapter(MockVoiceAdapter):
        async def connect(self, summary):
            connected.append(self.configuration)

    with TestClient(app) as client:
        app.state.voice.provider_factory = RecordingAdapter
        cid = client.post("/api/v1/conversations", json={}).json()["id"]
        issued = client.post(f"/api/v1/conversations/{cid}/voice-sessions", json={}).json()
        with client.websocket_connect(issued["ws_url"], headers={"Origin": "http://localhost:5173"}) as socket:
            assert socket.receive_json()["type"] == "portal.session.ready"
            items = three_tools(client.get("/api/v1/admin/voice-tools").json()["items"])
            assert client.put("/api/v1/admin/voice-tools", json={"expected_revision": 0, "items": items}).status_code == 200
            error = socket.receive_json()
            assert error["type"] == "portal.error"
            assert error["payload"]["code"] == "VOICE_TOOL_CONFIGURATION_CHANGED"
        issued = client.post(f"/api/v1/conversations/{cid}/voice-sessions", json={}).json()
        with client.websocket_connect(issued["ws_url"], headers={"Origin": "http://localhost:5173"}) as socket:
            assert socket.receive_json()["type"] == "portal.session.ready"
        assert [len(update["session"]["tools"]) for update in connected] == [2, 3]


def test_disable_and_file_reload_publish_new_revisions(tmp_path):
    with TestClient(create_app(configured(tmp_path))) as client:
        items = client.get("/api/v1/admin/voice-tools").json()["items"]
        items[0]["enabled"] = False
        assert client.put("/api/v1/admin/voice-tools", json={"expected_revision": 0, "items": items}).status_code == 200
        assert client.get("/api/v1/capabilities").json()["native_tools"] == ["reason_over_knowledge"]
        result = client.post("/api/v1/admin/voice-tools/reload", json={"expected_revision": 1})
        assert result.status_code == 200 and result.json()["revision"] == 2
        assert len(client.get("/api/v1/capabilities").json()["native_tools"]) == 2


def test_six_tools_survive_api_restart_without_a_two_or_five_tool_cap(tmp_path):
    settings = configured(tmp_path)
    with TestClient(create_app(settings)) as client:
        items = client.get("/api/v1/admin/voice-tools").json()["items"]
        for index in range(4):
            entry = copy.deepcopy(items[0])
            entry.update(name=f"lookup_manual_{index}", examples=[])
            items.append(entry)
        assert client.put("/api/v1/admin/voice-tools", json={"expected_revision": 0, "items": items}).status_code == 200
    with TestClient(create_app(settings)) as restarted:
        assert restarted.get("/api/v1/capabilities").json()["native_tools"] == [entry["name"] for entry in items]
        cid = restarted.post("/api/v1/conversations", json={}).json()["id"]
        issued = restarted.post(f"/api/v1/conversations/{cid}/voice-sessions", json={})
        assert issued.status_code == 201
        assert restarted.get("/api/v1/admin/voice-tools").json()["revision"] == 1


def test_zero_tools_is_valid_for_general_mode_but_not_strict_knowledge_mode(tmp_path):
    app = create_app(configured(tmp_path))
    with TestClient(app) as client:
        assert client.put("/api/v1/admin/voice-tools", json={"expected_revision": 0, "items": []}).status_code == 200
        assert client.get("/api/v1/capabilities").json()["native_tools"] == []
        cid = client.post("/api/v1/conversations", json={}).json()["id"]
        assert client.post(f"/api/v1/conversations/{cid}/voice-sessions", json={}).status_code == 201
        app.state.settings.qa_answer_policy = "knowledge_required"
        strict = client.post("/api/v1/conversations", json={}).json()["id"]
        assert client.post(f"/api/v1/conversations/{strict}/voice-sessions", json={}).status_code == 503


def test_third_configured_tool_executes_and_returns_original_call_id(tmp_path):
    app = create_app(configured(tmp_path))
    returned = []

    class Scripted(MockVoiceAdapter):
        async def connect(self, summary):
            await self.queue.put(VoiceEvent("input.state", {"state": "speaking", "item_id": "source"}))
            await self.queue.put(VoiceEvent("transcript.done", {"item_id": "source", "text": "Find the integration sample"}))
            await self.queue.put(VoiceEvent("tool", {"call_id": "original-third-call", "response_id": "tool-response",
                "name": "lookup_product_manual", "arguments": json.dumps({"user_request": "Find the integration sample",
                    "product_model": None, "software_version": None})}))

        async def submit_tool_result(self, call_id, text):
            result = json.loads(text)
            returned.append((call_id, result))
            await self.queue.put(VoiceEvent("speech_text.done", {"response_id": "answer", "text": result["speech_text"]}))
            await self.queue.put(VoiceEvent("audio.done", {"response_id": "answer"}))

    with TestClient(app) as client:
        app.state.voice.provider_factory = Scripted
        items = three_tools(client.get("/api/v1/admin/voice-tools").json()["items"])
        items[-1]["implementation"] = "reason_over_knowledge"
        assert client.put("/api/v1/admin/voice-tools", json={"expected_revision": 0, "items": items}).status_code == 200
        cid = client.post("/api/v1/conversations", json={}).json()["id"]
        issued = client.post(f"/api/v1/conversations/{cid}/voice-sessions", json={}).json()
        with client.websocket_connect(issued["ws_url"], headers={"Origin": "http://localhost:5173"}) as socket:
            assert socket.receive_json()["type"] == "portal.session.ready"
            while True:
                event = socket.receive_json()
                assert event["type"] != "portal.error", event
                if event["type"] == "portal.audio.done":
                    break
        assert len(returned) == 1
        assert returned[0][0] == "original-third-call"
        assert returned[0][1]["status"] == "answered"
        turns = client.get(f"/api/v1/conversations/{cid}/messages").json()["items"]
        assert len(turns) == 1 and turns[0]["selected_tool"] == "lookup_product_manual"


async def test_validation_operator_key_cannot_be_replaced_by_customer_call_token(tmp_path):
    app = create_app(configured(tmp_path, auth_mode="validation", tools_admin_api_key="test-only-operator-secret"))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            calls = [(await client.post("/api/v1/conversations", json={})).json() for _ in range(2)]
            for call in calls:
                assert (await client.get("/api/v1/admin/voice-tools", headers={
                    "Authorization": "Bearer " + call["access_token"]})).status_code == 401
            headers = {"Authorization": "Bearer test-only-operator-secret"}
            response = await client.get("/api/v1/admin/voice-tools", headers=headers)
            assert response.status_code == 200
            items = three_tools(response.json()["items"])
            assert (await client.put("/api/v1/admin/voice-tools", headers=headers,
                json={"expected_revision": 0, "items": items})).status_code == 200
            for call in calls:
                issued = await client.post(f"/api/v1/conversations/{call['id']}/voice-sessions",
                    headers={"Authorization": "Bearer " + call["access_token"]}, json={})
                assert issued.status_code == 201
                assert len(app.state.voice.sessions[issued.json()["voice_session_id"]].registered_tools) == 3
            # Operator administration does not grant access to private customer history.
            assert (await client.get(f"/api/v1/conversations/{calls[0]['id']}/messages", headers=headers)).status_code == 401


@pytest.mark.parametrize("mutation", ["missing_tools", "description", "parameters", "ack", "instructions", "duplicate"])
@pytest.mark.parametrize("serialized_echo", [False, True])
async def test_voicechat_registration_mismatch_fails_before_audio(mutation, serialized_echo):
    received_audio = []

    async def server(ws):
        await ws.send(json.dumps({"type": "session.created"}))
        update = json.loads(await ws.recv())["session"]
        if mutation == "missing_tools":
            update.pop("tools")
        elif mutation == "instructions":
            update["instructions"] = "A different policy"
        elif mutation == "duplicate":
            update["tools"].append(copy.deepcopy(update["tools"][0]))
        else:
            field = "ack_messages" if mutation == "ack" else mutation
            update["tools"][0][field] = [] if mutation == "ack" else {} if mutation == "parameters" else "Different scope"
        if serialized_echo and "tools" in update:
            update["tools"] = json.dumps(update["tools"])
        await ws.send(json.dumps({"type": "session.updated", "session": update}))
        async for raw in ws:
            received_audio.append(raw)

    async with serve(server, "127.0.0.1", 0) as endpoint:
        port = endpoint.sockets[0].getsockname()[1]
        adapter = NvidiaVoiceChatAdapter(Settings(_env_file=None, voicechat_ws_url=f"ws://127.0.0.1:{port}"))
        try:
            with pytest.raises(DomainError) as error:
                await adapter.connect("")
            assert error.value.code == "VOICE_PROTOCOL_ERROR"
        finally:
            await adapter.close()
    assert received_audio == []


@pytest.mark.parametrize("answer_policy", ["knowledge_required", "general_qa"])
@pytest.mark.parametrize("tool_count", [None, 0, 1, 3], ids=["legacy", "no-tools", "one-tool", "three-tools"])
async def test_prompt_output_policy_matches_gateway_authorization(app, answer_policy, tool_count):
    catalog = app.state.coordinator.runtime.catalog
    entries = three_tools([item.model_dump() for item in catalog.defaults])
    compiled = catalog.compile([NativeToolEntry.model_validate(item) for item in entries])
    definitions = catalog.dispatcher.definitions(tuple(compiled)[:tool_count], catalog=compiled) if tool_count is not None else None
    update = session_update("Confirmed model AX", definitions, answer_policy)
    session = update["session"]
    instructions = session["instructions"]
    assert "{{" not in instructions
    assert json.dumps(update, ensure_ascii=False).isascii()
    assert "Confirmed model AX" in instructions
    assert "only the configured fixed ack_messages associated with that tool call" in instructions
    assert "You may briefly acknowledge" not in instructions
    if tool_count is not None:
        assert session["tools"] == definitions  # Preserve custom names, schemas and ACKs.
        assert "consult_service_agent" not in instructions
    else:
        assert [tool["name"] for tool in session["tools"]] == ["consult_service_agent"]
    if answer_policy == "general_qa" and tool_count is not None:
        assert "answer from your own knowledge without tools" in instructions
        assert "explain the limitation or ask for clarification" in instructions
        assert "Do not speak greetings" not in instructions
        return
    assert "Do not answer facts from memory" in instructions
    assert "answer from your own knowledge without tools" not in instructions
    assert "Do not speak greetings, thanks, small talk, clarifying questions or limitations without tool authorization" in instructions
    assert "If no matching tool is available" in instructions
    assert "leave errors and clarification without tool authorization to the application's text interface" in instructions
    assert "Never call an unrelated tool to obtain permission to speak" in instructions
    assert "including a greeting followed by a product question" in instructions
    for conflicting_permission in (
        "Brief greetings and acknowledgments may be conversational",
        "If no matching tool is available, explain",
        "Ask when a required object is unclear",
        "If a tool fails or evidence is insufficient, explain",
    ):
        assert conflicting_permission not in instructions


@pytest.mark.parametrize("native_tools", [False, True])
async def test_voicechat_accepts_documented_json_string_tool_echo(native_tools, caplog, monkeypatch):
    caplog.set_level("INFO", logger="app.voice.provider")
    monkeypatch.setattr(logging.getLogger("app"), "propagate", True)
    audio_seen = asyncio.Event()
    pcm = base64.b64encode(bytes(3840)).decode()

    async def server(ws):
        await ws.send(json.dumps({"type": "session.created"}))
        update = json.loads(await ws.recv())["session"]
        update["tools"] = json.dumps(update["tools"])
        await ws.send(json.dumps({"type": "session.updated", "session": update}))
        async for raw in ws:
            event = json.loads(raw)
            assert event["type"] == "input_audio_buffer.append" and event["audio"] == pcm
            audio_seen.set()

    async with serve(server, "127.0.0.1", 0) as endpoint:
        port = endpoint.sockets[0].getsockname()[1]
        adapter = NvidiaVoiceChatAdapter(Settings(_env_file=None, voicechat_ws_url=f"ws://127.0.0.1:{port}"))
        if native_tools:
            # Dynamic catalog names and ACK must survive parsing, not be replaced with defaults.
            adapter.configuration = session_update("", [{
                "name": "lookup_product_manual", "description": "Retrieve documented product instructions.",
                "parameters": {"type": "object", "properties": {"query": {"type": "string"}},
                    "required": ["query"], "additionalProperties": False},
                "ack_messages": ["I will check the product manual."],
            }])
        try:
            await adapter.connect("")
            assert "voice_registration_confirmed tools_count=1 tools_echo_type=str" in caplog.text
            assert "Retrieve documented product instructions" not in caplog.text
            assert "Current session answer policy" not in caplog.text
            await adapter.send_audio(pcm)
            await asyncio.wait_for(audio_seen.wait(), 1)
        finally:
            await adapter.close()


@pytest.mark.parametrize("tool_echo", ['not-json', '{}', 'null', '"[]"', '[]'])
async def test_invalid_json_string_tool_echo_still_fails_before_audio(tool_echo):
    received_audio = []

    async def server(ws):
        await ws.send(json.dumps({"type": "session.created"}))
        update = json.loads(await ws.recv())["session"]
        update["tools"] = tool_echo
        await ws.send(json.dumps({"type": "session.updated", "session": update}))
        async for raw in ws:
            received_audio.append(raw)

    async with serve(server, "127.0.0.1", 0) as endpoint:
        port = endpoint.sockets[0].getsockname()[1]
        adapter = NvidiaVoiceChatAdapter(Settings(_env_file=None, voicechat_ws_url=f"ws://127.0.0.1:{port}"))
        try:
            with pytest.raises(DomainError) as error:
                await adapter.connect("")
            assert error.value.code == "VOICE_PROTOCOL_ERROR"
        finally:
            await adapter.close()
    assert received_audio == []
