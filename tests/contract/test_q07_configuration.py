import os
import runpy
import subprocess
from pathlib import Path

import pytest
from app.config import Settings

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("provider", [None, "", "  ", "none"])
def test_empty_provider_selects_direct(provider):
    kwargs = {} if provider is None else {"agent_provider": provider}
    s = Settings(_env_file=None, **kwargs)
    assert s.execution_profile().mode == "direct" and not s.execution_profile().text_available


@pytest.mark.parametrize("provider", ["openai", "compatible"])
def test_external_configuration_normalizes_and_remains_external(provider):
    s = Settings(
        _env_file=None,
        agent_provider=f" {provider} ",
        agent_model=" model ",
        openai_api_key=" key ",
        agent_base_url=" http://model.test/v1 " if provider == "compatible" else " ",
    )
    assert s.execution_profile().mode == "external" and s.agent_model == "model"
    assert s.openai_api_key.get_secret_value() == "key"


@pytest.mark.parametrize(
    "values",
    [
        {"agent_model": "model"},
        {"openai_api_key": "key"},
        {"agent_base_url": "https://model.test/v1"},
        {"agent_provider": "openai"},
        {"agent_provider": "compatible", "agent_model": "model", "openai_api_key": "key"},
    ],
)
def test_conflicting_or_incomplete_config_fails(values):
    with pytest.raises(ValueError):
        Settings(_env_file=None, **values).execution_profile()


@pytest.mark.parametrize(
    "url",
    [
        "ftp://model.test/v1",
        "http:///v1",
        "http://u:p@model.test/v1",
        "http://model.test/v1?q=x",
        "http://model.test/v1#frag",
        "http://model.test/chat/completions",
        "http://model.test:bad/v1",
    ],
)
def test_invalid_compatible_base_url_fails(url):
    with pytest.raises(ValueError):
        Settings(
            _env_file=None,
            agent_provider="compatible",
            agent_model="model",
            openai_api_key="key",
            agent_base_url=url,
        ).execution_profile()


@pytest.mark.parametrize("mode", ["direct", "external"])
def test_readiness_expected_mode_and_required_fields(mode):
    validate = runpy.run_path(ROOT / "scripts/verify_deployment.py")["validate_ready"]
    external = mode == "external"
    payload = dict(
        status="ready",
        execution_mode=mode,
        text_configured=external,
        external_llm_enabled=external,
        text_available=external,
        voice_configured=True,
        is_mock=False,
        enabled_tools=["search_knowledge"],
    )
    assert validate(payload, {"search_knowledge"}, mode)["execution_mode"] == mode
    for key in payload:
        bad = {k: v for k, v in payload.items() if k != key}
        with pytest.raises(ValueError):
            validate(bad, {"search_knowledge"}, mode)
    with pytest.raises(ValueError):
        validate(payload, {"search_knowledge"}, "external" if mode == "direct" else "direct")
    with pytest.raises(ValueError):
        validate({**payload, "is_mock": True}, {"search_knowledge"}, mode)


@pytest.mark.parametrize(
    "provider, model, key, url, accepted",
    [
        ("none", "", "", "", True),
        ("", "", "", "", True),
        (" none ", "", "", "", True),
        ("none", " leftover ", "", "", False),
        ("none", "", "key", "", False),
        ("openai", "model", "key", "", True),
        ("openai", "", "key", "", False),
        ("compatible", " model ", " key ", " http://model.test/v1 ", True),
        ("compatible", "model", "key", "http://model.test/chat/completions", False),
        ("compatible", "model", "key", "http://u:p@model.test/v1", False),
        ("openai", "model", "key", "http://model.test/v1#frag", False),
        ("mock", "", "", "", False),
    ],
)
def test_deployment_shell_matrix_without_docker(tmp_path, provider, model, key, url, accepted):
    # A local command fixture validates shell flow; no actual Docker executable is run.
    docker = tmp_path / "docker"
    docker.write_text("#!/bin/sh\nexit 0\n")
    docker.chmod(0o755)
    cert = tmp_path / "cert.pem"
    cert.write_text("fixture")
    values = dict(
        IMAGE_TAG="fixture",
        POSTGRES_PASSWORD="pass",
        REDIS_PASSWORD="pass",
        KNOWLEDGE_BASE_IDS="00000000-0000-4000-8000-000000000001",
        WEB_TLS_CERT_FILE=cert,
        WEB_TLS_KEY_FILE=cert,
        PUBLIC_ORIGIN="https://portal.test:8087",
        CUEKB_BASE_URL="http://cuekb.test",
        CUEKB_API_KEY="key",
        VOICECHAT_WS_URL="ws://voice.test/v1/realtime",
        VOICECHAT_API_KEY="key",
        AGENT_PROVIDER=provider,
        AGENT_MODEL=model,
        OPENAI_API_KEY=key,
        AGENT_BASE_URL=url,
    )
    env_file = tmp_path / ".env"
    env_file.write_text("\n".join(f"{k}={v}" for k, v in values.items()) + "\n")
    result = subprocess.run(
        ["sh", str(ROOT / "scripts/deploy-cloud.sh"), str(env_file)],
        env={**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}"},
        capture_output=True,
        text=True,
    )
    assert (result.returncode == 0) is accepted, result.stderr


def test_evaluation_reports_modes_without_pooling_latency():
    summarize = runpy.run_path(ROOT / "scripts/score_voice_evaluation.py")["summarize"]
    report = summarize(
        [
            {
                "case_id": "same",
                "execution_mode": "direct",
                "real_service": True,
                "answer_first_audio_ms": 100,
            },
            {
                "case_id": "same",
                "execution_mode": "external",
                "real_service": True,
                "answer_first_audio_ms": 900,
            },
            {
                "case_id": "fixture",
                "execution_mode": "direct",
                "real_service": False,
                "answer_first_audio_ms": 1,
            },
        ]
    )
    assert "answer_first_audio_ms" not in report
    assert report["by_execution_mode"]["direct"]["answer_first_audio_ms"]["p50"] == 100
    assert report["by_execution_mode"]["external"]["answer_first_audio_ms"]["p95"] == 900
    with pytest.raises(ValueError):
        summarize([{"case_id": "same"}, {"case_id": "same"}])
