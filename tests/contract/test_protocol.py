import base64
import json

import pytest
from app.config import Settings
from app.contracts import (
    BridgeArguments,
    DomainError,
    portal_client_event_adapter,
    portal_server_event_adapter,
)
from app.voice.provider import (
    NvidiaVoiceChatAdapter,
    VoiceEvent,
    VoiceTrace,
    event_metadata,
    normalize,
    session_update,
)
from pydantic import ValidationError


def test_native_configuration_is_exact_and_ascii():
    config = session_update("Confirmed model AX\n张先生咨询过产品型号 A-中文")
    assert "中文" not in json.dumps(config, ensure_ascii=False)
    assert "Confirmed model AX" in config["session"]["instructions"]
    assert json.dumps(config, ensure_ascii=False).isascii()
    assert set(config["session"]) == {"audio", "instructions", "tools"}
    assert config["session"]["audio"]["input"]["format"]["rate"] == 24000
    assert len(config["session"]["tools"]) == 1
    assert config["session"]["tools"][0]["name"] == "consult_service_agent"
    assert "response.cancel" not in json.dumps(config)


async def test_native_tool_result_rejects_non_ascii_before_send():
    sent = []

    class Socket:
        async def send(self, payload):
            sent.append(payload)

    adapter = NvidiaVoiceChatAdapter(Settings(_env_file=None))
    adapter.ws = Socket()
    with pytest.raises(DomainError) as error:
        await adapter.submit_tool_result("call-1", '{"speech_text":"caf\\u00e9"}')
    assert error.value.code == "VOICE_PROTOCOL_ERROR"
    assert sent == []
    await adapter.submit_tool_result("call-1", '{"speech_text":"Verified answer"}')
    assert json.loads(sent[0])["item"]["call_id"] == "call-1"


def test_new_conversations_are_english_only():
    from app.contracts import AnswerBundle, ConversationInput

    assert ConversationInput().locale == "en-US"
    assert AnswerBundle(status="answered", display_text="ok", speech_text="ok").speech_language == "en-US"
    with pytest.raises(ValidationError):
        ConversationInput(locale="zh-CN")
    with pytest.raises(ValidationError):
        Settings(_env_file=None, required_voice_languages="zh-CN")


@pytest.mark.parametrize(
    "args",
    [
        {"user_request": ""},
        {"user_request": "x" * 2001},
        {"user_request": "天气", "tenant_id": "evil"},
        {"user_request": "天气", "url": "http://evil"},
    ],
)
def test_bridge_rejects_untrusted_scope(args):
    with pytest.raises(ValidationError):
        BridgeArguments.model_validate(args)


def test_tool_before_transcript_preserves_native_ids():
    e = normalize(
        {
            "type": "response.function_call_arguments.done",
            "response_id": "r1",
            "call_id": "c1",
            "name": "consult_service_agent",
            "arguments": '{"user_request":"杭州明天天气"}',
        }
    )
    assert e.kind == "tool" and e.payload["call_id"] == "c1"
    assert BridgeArguments.model_validate_json(e.payload["arguments"]).user_request == "杭州明天天气"


def test_voice_event_diagnostics_exclude_content_and_bound_identifiers():
    metadata = event_metadata(VoiceEvent("tool", {
        "response_id": "r" * 200,
        "item_id": "input-1",
        "call_id": "call-1",
        "name": "consult_service_agent",
        "arguments": '{"user_request":"private question"}',
        "text": "private answer",
        "audio": "private audio",
        "token": "private token",
    }))
    assert metadata == {
        "kind": "tool", "response_id": "r" * 128,
        "item_id": "input-1", "call_id": "call-1", "name": "consult_service_agent",
        "text_chars": 14, "has_text": True, "audio_base64_chars": 13,
    }
    assert "private" not in json.dumps(metadata)


def test_voice_trace_compresses_adjacent_audio_but_preserves_control_boundaries():
    trace = VoiceTrace()
    for index in range(500):
        trace.append(VoiceEvent("audio.delta", {"response_id": "r", "item_id": "i", "audio": "AAA="}), index)
    trace.append(VoiceEvent("tool", {"call_id": "c", "arguments": "secret"}), 500)
    trace.append(VoiceEvent("audio.delta", {"response_id": "r", "item_id": "i", "audio": "AAA="}), 501)
    snapshot = trace.snapshot()
    assert len(snapshot["events"]) == 3
    assert snapshot["events"][0]["count"] == 500
    assert snapshot["events"][0]["last_seq"] == 500
    assert snapshot["events"][0]["audio_base64_chars"] == 2000
    assert snapshot["events"][1]["kind"] == "tool"
    assert not snapshot["truncated"]
    assert "secret" not in json.dumps(snapshot)


def test_voice_trace_enforces_count_and_serialized_byte_limits():
    for limits in ({"max_events": 2}, {"max_bytes": 350}):
        trace = VoiceTrace(**limits)
        for index in range(100):
            trace.append(VoiceEvent("transcript.done", {"item_id": "i" * 10000, "text": "private"}), index)
        snapshot = trace.snapshot()
        assert snapshot["truncated"] and snapshot["dropped_events"] > 0
        assert len(trace.events) <= trace.max_events and trace.bytes <= trace.max_bytes
        assert trace.bytes == sum(len(json.dumps(e, ensure_ascii=True).encode()) for e in trace.events)
        assert len(json.dumps(snapshot["events"], ensure_ascii=True).encode()) <= trace.max_bytes
        assert all(len(e["item_id"]) == 128 for e in trace.events)
        assert "private" not in json.dumps(snapshot)


async def test_adapter_diagnostics_count_unknown_types_before_normalization_and_only_sent_audio():
    class Socket:
        async def send(self, raw):
            if self.failed:
                raise RuntimeError("controlled failure")

        def __aiter__(self):
            return self.events()

        async def events(self):
            for index in range(50):
                yield json.dumps({"type": f"unknown-{index}", "secret": "do not record"})
            yield json.dumps({"type": "session.end"})

    adapter = NvidiaVoiceChatAdapter(Settings(_env_file=None))
    adapter.ws = Socket()
    adapter.ws.failed = False
    await adapter.send_audio(base64.b64encode(bytes(3840)).decode())
    adapter.ws.failed = True
    with pytest.raises(RuntimeError):
        await adapter.send_audio("AAA=")
    events = [event async for event in adapter.events()]
    assert [event.kind for event in events] == ["session.ended"]
    diagnostic = adapter.diagnostics()
    assert diagnostic["sent_audio_chunks"] == 1 and diagnostic["sent_audio_samples_24k"] == 1920
    assert len(diagnostic["raw_event_counts"]) <= 33
    assert sum(diagnostic["raw_event_counts"].values()) == 51
    assert "secret" not in json.dumps(diagnostic) and diagnostic["provider_session_id"] is None


@pytest.mark.parametrize("created_session,expected_id", [(None, None), ({"id": "client-567a5110"}, "client-567a5110")])
async def test_provider_session_identifier_is_optional_diagnostic_metadata(monkeypatch, created_session, expected_id):
    from app.voice import provider

    class Socket:
        def __init__(self):
            self.index = 0

        async def recv(self):
            self.index += 1
            if self.index == 1:
                return json.dumps({"type": "session.created", "session": created_session})
            return json.dumps({"type": "session.updated", "session": session_update()["session"]})

        async def send(self, raw):
            assert json.loads(raw)["type"] == "session.update"

    async def connect(*args, **kwargs):
        return Socket()

    monkeypatch.setattr(provider, "connect", connect)
    adapter = NvidiaVoiceChatAdapter(Settings(_env_file=None))
    await adapter.connect("")
    assert adapter.diagnostics()["provider_session_id"] == expected_id


def test_input_state_preserves_native_item_identity():
    started = normalize(
        {"type": "input_audio_buffer.speech_started", "item_id": "input-1"}
    )
    stopped = normalize(
        {"type": "input_audio_buffer.speech_stopped", "item_id": "input-1"}
    )
    assert started.kind == "input.state"
    assert started.payload == {"state": "speaking", "item_id": "input-1"}
    assert stopped.payload == {"state": "quiet", "item_id": "input-1"}
    with pytest.raises(DomainError):
        normalize({"type": "input_audio_buffer.speech_started"})


def test_missing_response_identity_is_protocol_error():
    with pytest.raises(DomainError):
        normalize({"type": "response.output_audio.delta", "delta": "AAAA"})


def test_portal_event_contracts_are_discriminated_and_strict():
    server = portal_server_event_adapter.validate_python(
        {
            "type": "portal.session.ready",
            "event_id": "event-1",
            "conversation_id": "conversation-1",
            "epoch": 2,
            "server_seq": 1,
            "timestamp": "2026-09-16T00:00:00Z",
            "payload": {"sample_rate": 24000, "format": "pcm16", "chunk_ms": 80, "is_mock": False},
        }
    )
    assert server.type == "portal.session.ready"
    with pytest.raises(ValidationError):
        portal_server_event_adapter.validate_python(
            {
                "type": "portal.unknown",
                "event_id": "event-1",
                "conversation_id": "conversation-1",
                "epoch": 2,
                "server_seq": 1,
                "timestamp": "2026-09-16T00:00:00Z",
                "payload": {},
            }
        )
    with pytest.raises(ValidationError):
        portal_client_event_adapter.validate_python(
            {
                "type": "portal.interrupt",
                "epoch": 2,
                "payload": {},
                "tenant_id": "untrusted",
            }
        )


def deployment_settings(**overrides):
    values = {
        "auth_mode": "validation",
        "public_origin": "https://portal.example.invalid:8087",
        "database_url": "postgresql+asyncpg://service:secret@postgres/service",
        "redis_url": "redis://:secret@redis:6379/0",
        "agent_provider": "openai",
        "agent_model": "configured-model",
        "openai_api_key": "configured-key",
        "enabled_tools": "search_knowledge",
        "cuekb_mode": "real",
        "cuekb_base_url": "https://cuekb.example.invalid",
        "cuekb_api_key": "configured-key",
        "voice_provider": "nvidia",
        "voicechat_ws_url": "wss://voice.example.invalid/ws",
        "voicechat_api_key": "configured-key",
    }
    return Settings(_env_file=None, **{**values, **overrides})


def test_deployment_cuekb_only_does_not_require_weather_configuration():
    settings = deployment_settings(weather_mode="mock")
    settings.validate_deployment()
    assert settings.enabled_tool_names == {"search_knowledge"} and settings.mock is False


def test_compatible_model_requires_explicit_base_url():
    settings = deployment_settings(agent_provider="compatible")
    with pytest.raises(ValueError, match="AGENT_BASE_URL"):
        settings.validate_deployment()


@pytest.mark.parametrize("scheme", ["http", "https"])
def test_internal_text_model_and_cuekb_allow_http_or_https(scheme):
    settings = deployment_settings(
        agent_provider="compatible",
        agent_base_url=f"{scheme}://model.internal:8003/v1",
        cuekb_base_url=f"{scheme}://cuekb.internal:8085",
    )
    settings.validate_deployment()


@pytest.mark.parametrize("scheme", ["ws", "wss"])
def test_voicechat_allows_ws_or_wss(scheme):
    settings = deployment_settings(
        voicechat_ws_url=f"{scheme}://voicechat.internal:9000/v1/realtime"
    )
    settings.validate_deployment()


@pytest.mark.parametrize(
    "value",
    [
        "http://voicechat.internal:9000/v1/realtime",
        "wss:///missing-host",
    ],
)
def test_voicechat_rejects_non_websocket_or_missing_host(value):
    settings = deployment_settings(voicechat_ws_url=value)
    with pytest.raises(ValueError, match="WS or WSS URL with a host"):
        settings.validate_deployment()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("agent_base_url", "ftp://model.internal/v1"),
        ("cuekb_base_url", "ws://cuekb.internal:8085"),
        ("cuekb_base_url", "http:///missing-host"),
    ],
)
def test_deployment_rejects_non_http_text_or_cuekb_url(field, value):
    overrides = {
        "agent_provider": "compatible",
        "agent_base_url": "http://model.internal:8003/v1",
        field: value,
    }
    settings = deployment_settings(**overrides)
    with pytest.raises(ValueError, match="HTTP or HTTPS"):
        settings.validate_deployment()


def test_deployment_keeps_weather_on_https():
    settings = deployment_settings(
        enabled_tools="weather",
        weather_mode="real",
        weather_base_url="http://weather.internal",
        weather_api_key="configured-key",
    )
    with pytest.raises(ValueError, match="Weather integration requires HTTPS"):
        settings.validate_deployment()


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("public_origin", "http://portal.example.invalid:8087", "PUBLIC_ORIGIN"),
        ("public_origin", "https://portal.example.invalid:8087/", "PUBLIC_ORIGIN"),
        ("public_origin", "https://portal.example.invalid:8087/app", "PUBLIC_ORIGIN"),
        ("auth_mode", "fixture", "fixture identity"),
        ("voice_provider", "disabled", "NVIDIA VoiceChat"),
        ("agent_provider", "mock", "mock providers"),
        ("auto_create_schema", True, "automatic schema"),
    ],
)
def test_deployment_rejects_unsafe_configuration(field, value, message):
    settings = deployment_settings(**{field: value})
    with pytest.raises(ValueError, match=message):
        settings.validate_deployment()


def test_external_tracing_stays_disabled():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, external_tracing_enabled=True)


def test_websocket_handshake_and_oidc_logs_redact_query_credentials():
    import logging

    from app.api.logging import SensitiveQueryFilter

    record = logging.LogRecord(
        "uvicorn.error",
        logging.INFO,
        "",
        0,
        "WebSocket %s accepted",
        ("/api/v1/voice-sessions/example/stream?ticket=synthetic-secret&epoch=2",),
        None,
    )
    SensitiveQueryFilter().filter(record)
    assert "synthetic-secret" not in record.getMessage()
    assert "ticket=[REDACTED]" in record.getMessage()
    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        "",
        0,
        "/api/v1/auth/callback?code=synthetic-code&state=synthetic-state",
        (),
        None,
    )
    SensitiveQueryFilter().filter(record)
    assert "synthetic-code" not in record.getMessage() and "synthetic-state" not in record.getMessage()


def test_application_logging_is_visible_redacted_and_idempotent():
    import logging

    from app.api.logging import ApplicationLogHandler, configure_logging

    configure_logging()
    configure_logging()
    logger = logging.getLogger("app.tools.registry")
    handlers = [
        handler
        for handler in logging.getLogger("app").handlers
        if isinstance(handler, ApplicationLogHandler)
    ]

    assert logger.isEnabledFor(logging.INFO)
    assert len(handlers) == 1
    record = logging.LogRecord(
        "app.tools.registry",
        logging.INFO,
        __file__,
        1,
        "endpoint=%s",
        ("wss://voice.test/stream?ticket=secret",),
        None,
    )
    assert handlers[0].filters[0].filter(record) is True
    assert record.args[0].endswith("ticket=[REDACTED]")
