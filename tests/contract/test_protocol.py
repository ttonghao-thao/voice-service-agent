import json

import pytest
from app.config import Settings
from app.contracts import BridgeArguments, DomainError
from app.voice.provider import normalize, session_update
from pydantic import ValidationError


def test_native_configuration_is_exact_and_unicode():
    config = session_update("张先生咨询过产品型号 A-中文")
    assert "中文" in json.dumps(config, ensure_ascii=False)
    assert set(config["session"]) == {"audio", "instructions", "tools"}
    assert config["session"]["audio"]["input"]["format"]["rate"] == 24000
    assert len(config["session"]["tools"]) == 1
    assert config["session"]["tools"][0]["name"] == "consult_service_agent"
    assert "response.cancel" not in json.dumps(config)


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


@pytest.mark.parametrize(
    "override",
    [
        {"app_env": "production"},
        {"external_tracing_enabled": True},
        {"auth_mode": "oidc"},
        {"voice_session_max_seconds": 0},
    ],
)
def test_unsafe_configuration_fails_closed(override):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **override)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("oidc_authorization_url", "http://id.example.invalid/authorize"),
        ("oidc_token_url", "http://id.example.invalid/token"),
        ("voicechat_health_url", "http://voice.example.invalid/health"),
    ],
)
def test_production_rejects_non_tls_service_endpoints(field, value):
    production = {
        "app_env": "production",
        "auth_mode": "oidc",
        "public_origin": "https://portal.example.invalid",
        "database_url": "postgresql+asyncpg://service:secret@postgres/service",
        "redis_url": "redis://:secret@redis:6379/0",
        "oidc_issuer": "https://id.example.invalid/",
        "oidc_audience": "portal",
        "oidc_jwks_url": "https://id.example.invalid/jwks.json",
        "tenant_id": "tenant",
        "agent_provider": "openai",
        "agent_model": "configured-model",
        "openai_api_key": "configured-key",
        "rag_mode": "real",
        "rag_base_url": "https://rag.example.invalid",
        "rag_api_key": "configured-key",
        "weather_mode": "real",
        "weather_base_url": "https://weather.example.invalid",
        "weather_api_key": "configured-key",
        "voice_provider": "nvidia",
        "auth_cookie_secret": "x" * 32,
        field: value,
    }
    with pytest.raises(ValidationError, match="TLS"):
        Settings(_env_file=None, **production)


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
