import json

import pytest
from app.config import Settings
from app.contracts import (
    BridgeArguments,
    DomainError,
    portal_client_event_adapter,
    portal_server_event_adapter,
)
from app.voice.provider import NvidiaVoiceChatAdapter, normalize, session_update
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
