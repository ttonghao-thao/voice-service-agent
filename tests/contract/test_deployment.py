import json
import os
import subprocess
from pathlib import Path
from urllib.parse import urlparse

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]


def test_cloud_compose_is_a_production_only_container_topology():
    compose = yaml.safe_load((ROOT / "deploy/compose.production.yaml").read_text())
    services = compose["services"]

    assert set(services) == {"postgres", "redis", "migrate", "api", "web"}
    assert "APP_ENV" not in services["api"]["environment"]
    assert services["api"]["environment"]["AUTO_CREATE_SCHEMA"] == "false"
    assert services["api"]["environment"]["AUTH_MODE"] == "validation"
    assert services["api"]["environment"]["CUEKB_MODE"] == "real"
    assert services["api"]["environment"]["ENABLED_TOOLS"] == "search_knowledge"
    assert services["api"]["environment"]["VOICE_PROVIDER"] == "nvidia"
    assert services["api"]["env_file"] == "${DEPLOY_ENV_FILE:-../.env}"
    assert services["api"]["image"] == services["migrate"]["image"]
    assert (
        services["api"]["image"] == "voice-service-agent-api:${IMAGE_TAG:?Set the prebuilt IMAGE_TAG in .env}"
    )
    assert (
        services["web"]["image"] == "voice-service-agent-web:${IMAGE_TAG:?Set the prebuilt IMAGE_TAG in .env}"
    )
    for name in ("migrate", "api", "web"):
        assert "build" not in services[name]
        assert services[name]["pull_policy"] == "never"
    assert services["migrate"]["command"][-2:] == ["upgrade", "head"]
    assert services["api"]["depends_on"]["migrate"]["condition"] == "service_completed_successfully"
    assert "ports" not in services["postgres"] and "ports" not in services["redis"]
    assert services["web"]["ports"] == ["0.0.0.0:8087:8087"]
    assert "ports" not in services["api"]
    assert services["web"]["volumes"] == [
        "${WEB_TLS_CERT_FILE:?Set the TLS certificate path in .env}:/etc/nginx/tls/tls.crt:ro",
        "${WEB_TLS_KEY_FILE:?Set the TLS private key path in .env}:/etc/nginx/tls/tls.key:ro",
    ]
    assert "https://127.0.0.1:8087/health/live" in services["web"]["healthcheck"]["test"][-1]
    images = json.loads((ROOT / "deploy/images.lock.json").read_text())
    assert services["postgres"]["image"] == images["postgres"]
    assert services["redis"]["image"] == images["redis"]
    assert images["postgres"].startswith("postgres:17.6-alpine@sha256:")
    assert images["redis"].startswith("redis:7-alpine@sha256:")
    assert not (ROOT / "deploy/compose.yaml").exists()
    assert set(services["web"]["networks"]) == {"app", "edge"}
    assert services["postgres"]["networks"] == ["data"]
    assert set(services["api"]["networks"]) == {"data", "app", "egress"}
    assert compose["networks"]["data"]["internal"] is True
    assert compose["networks"]["app"]["internal"] is True


def test_cloud_environment_template_cannot_enable_development_fallbacks():
    values = {}
    for line in (ROOT / ".env.example").read_text().splitlines():
        if line and not line.startswith("#"):
            key, value = line.split("=", 1)
            values[key] = value

    assert set(values) == {
        "IMAGE_TAG",
        "PUBLIC_ORIGIN",
        "WEB_TLS_CERT_FILE",
        "WEB_TLS_KEY_FILE",
        "POSTGRES_PASSWORD",
        "REDIS_PASSWORD",
        "KNOWLEDGE_BASE_IDS",
        "AGENT_PROVIDER",
        "AGENT_DEADLINE_MS",
        "CUEKB_BASE_URL",
        "CUEKB_API_KEY",
        "VOICECHAT_WS_URL",
        "VOICECHAT_API_KEY",
        "QA_EXECUTION_MODE",
        "QA_ANSWER_POLICY",
        "QA_TOOLSET_VERSION",
        "QA_EXTERNAL_FALLBACK_ENABLED",
        "QA_MAX_RETRIEVAL_CALLS",
        "QA_PROVIDER_ANSWER_TIMEOUT_MS",
    }
    assert "APP_ENV" not in values
    assert "AUTH_MODE" not in values
    assert values["AGENT_PROVIDER"] == "disabled"
    assert values["AGENT_DEADLINE_MS"] == "60000"
    assert values["QA_TOOLSET_VERSION"] == "cuekb-native-v1"
    assert values["QA_EXTERNAL_FALLBACK_ENABLED"] == "false"
    assert values["QA_MAX_RETRIEVAL_CALLS"] == "1"
    assert not {"AGENT_MODEL", "OPENAI_API_KEY", "AGENT_BASE_URL"} & values.keys()
    assert values["QA_EXECUTION_MODE"] == "dual_tools"
    assert values["QA_ANSWER_POLICY"] == "general_qa"
    assert "CUEKB_MODE" not in values
    assert "ENABLED_TOOLS" not in values
    assert not any(key.startswith("WEATHER_") for key in values)
    assert "VOICE_PROVIDER" not in values
    assert values["VOICECHAT_WS_URL"].startswith("REPLACE_")
    origin = urlparse(values["PUBLIC_ORIGIN"])
    assert origin.scheme == "https" and origin.port == 8087 and origin.path == ""
    assert values["WEB_TLS_CERT_FILE"].startswith("/")
    assert values["WEB_TLS_KEY_FILE"].startswith("/")
    assert values["IMAGE_TAG"]
    assert urlparse(values["CUEKB_BASE_URL"]).scheme in {"http", "https"}
    for key in (
        "POSTGRES_PASSWORD",
        "REDIS_PASSWORD",
        "CUEKB_API_KEY",
        "VOICECHAT_API_KEY",
    ):
        assert values[key].startswith("REPLACE_")
    assert "CUEKB_TOP_K" not in values and "MAX_VOICE_SESSIONS" not in values
    assert not (ROOT / ".env.production.example").exists()


def test_cloud_deployment_uses_prebuilt_images():
    script = (ROOT / "scripts/deploy-cloud.sh").read_text()
    assert "compose build" not in script
    assert 'docker image inspect "$image"' in script
    assert script.count("compose up -d --no-build") == 3
    assert "IMAGE_TAG" in script
    assert "LOCAL_USERS_JSON" not in script and "API_BIND_ADDRESS" not in script
    assert "APP_ENV" not in script
    dockerfile = (ROOT / "deploy/Dockerfile.api").read_text()
    assert "COPY scripts/configure_native_cuekb.py scripts/configure_native_cuekb.py" in dockerfile
    assert "COPY scripts/validate_native_cuekb.py scripts/validate_native_cuekb.py" in dockerfile
    assert "COPY scripts/probe_voicechat.py scripts/probe_voicechat.py" in dockerfile
    assert "COPY artifacts/native-cuekb/cases.json validation/native-cuekb/cases.json" in dockerfile
    assert "COPY artifacts/native-cuekb/*.wav validation/native-cuekb/" in dockerfile
    dockerignore = (ROOT / ".dockerignore").read_text()
    assert "!artifacts/native-cuekb/cases.json" in dockerignore
    assert "!artifacts/native-cuekb/*.wav" in dockerignore
    voicechat_dockerfile = (ROOT / "deploy/voicechat/Dockerfile").read_text()
    assert "COPY --from=agent_repo scripts/inspect_voicechat_artifacts.py" in voicechat_dockerfile
    assert "COPY --from=agent_repo artifacts/native-cuekb/*.wav" in voicechat_dockerfile
    voicechat_readme = (ROOT / "deploy/voicechat/README.md").read_text()
    assert "--build-context agent_repo=/path/to/voice-service-agent" in voicechat_readme
    voicechat_runbook = (ROOT / "docs/voicechat/docker-run.md").read_text()
    assert "voicechat_validation_bundle=passed" in voicechat_runbook
    assert '"$VOICECHAT_RELEASE_IMAGE"' in voicechat_runbook
    assert "configure_native_cuekb.py --apply" in script
    nginx = (ROOT / "deploy/nginx.conf").read_text()
    assert "listen 8087 ssl;" in nginx
    assert "ssl_certificate /etc/nginx/tls/tls.crt;" in nginx
    assert "/api/v1/auth/login" not in nginx
    assert "login_limit" not in nginx
    assert "proxy_set_header X-Forwarded-For $remote_addr;" in nginx


def test_deployment_requires_https_and_readable_tls_files(tmp_path):
    docker = tmp_path / "docker"
    docker.write_text("#!/bin/sh\nexit 0\n")
    docker.chmod(0o755)
    env = {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}"}
    values = {
        "IMAGE_TAG": "test",
        "POSTGRES_PASSWORD": "postgres-pass",
        "REDIS_PASSWORD": "redis-pass",
        "KNOWLEDGE_BASE_IDS": "00000000-0000-4000-8000-000000000001",
        "AGENT_PROVIDER": "disabled",
        "PUBLIC_ORIGIN": "https://voice.test:8087",
        "CUEKB_BASE_URL": "https://cuekb.test",
        "CUEKB_API_KEY": "cuekb-key",
        "VOICECHAT_WS_URL": "wss://voicechat.test/ws",
        "VOICECHAT_API_KEY": "voice-key",
        "QA_EXECUTION_MODE": "dual_tools",
        "QA_ANSWER_POLICY": "general_qa",
        "WEB_TLS_CERT_FILE": "/missing/cert.pem",
        "WEB_TLS_KEY_FILE": "/missing/key.pem",
    }
    env_file = tmp_path / ".env"

    def check():
        env_file.write_text("\n".join(f"{key}={value}" for key, value in values.items()) + "\n")
        return subprocess.run(
            ["sh", str(ROOT / "scripts/deploy-cloud.sh"), str(env_file)],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

    values["AGENT_PROVIDER"] = "compatible"
    rejected_model = check()
    assert rejected_model.returncode != 0
    assert "AGENT_PROVIDER=disabled" in rejected_model.stderr
    values["AGENT_PROVIDER"] = "disabled"
    rejected_cert = check()
    assert rejected_cert.returncode != 0
    assert "WEB_TLS_CERT_FILE must point to a readable file" in rejected_cert.stderr

    cert = tmp_path / "cert.pem"
    key = tmp_path / "key.pem"
    cert.write_text("test certificate")
    key.write_text("test key")
    values["WEB_TLS_CERT_FILE"] = str(cert)
    values["WEB_TLS_KEY_FILE"] = str(key)
    values["PUBLIC_ORIGIN"] = "http://voice.test:8087"
    rejected_http = check()
    assert rejected_http.returncode != 0
    assert "HTTPS origin" in rejected_http.stderr
    values["PUBLIC_ORIGIN"] = "https://voice.test:8087"
    accepted_https = check()
    assert accepted_https.returncode == 0
    values["QA_ANSWER_POLICY"] = "knowledge_required"
    rejected_strict = check()
    assert rejected_strict.returncode != 0
    assert "QA_ANSWER_POLICY=general_qa" in rejected_strict.stderr


def test_deployment_readiness_verifier_requires_real_matching_configuration():
    import runpy

    validate_ready = runpy.run_path(ROOT / "scripts/verify_deployment.py")["validate_ready"]

    assert validate_ready(
        {
            "status": "ready",
            "is_mock": False,
            "enabled_tools": ["search_knowledge"],
            "text_configured": True,
            "voice_configured": True,
        },
        {"search_knowledge"},
    )["enabled_tools"] == ["search_knowledge"]


@pytest.mark.parametrize("change", [
    {}, {"provider": "mock"}, {"qa_execution_mode": "legacy"},
    {"answer_policy": "knowledge_required"}, {"is_mock": True}, {"voice_available": False},
])
def test_autonomous_voice_deployment_checks_actual_capabilities(change):
    import runpy
    validate = runpy.run_path(ROOT / "scripts/verify_deployment.py")["validate_voice_policy"]
    payload = {"provider": "nvidia", "qa_execution_mode": "dual_tools", "answer_policy": "general_qa",
               "is_mock": False, "voice_available": True, **change}
    if change:
        with pytest.raises(ValueError):
            validate(payload)
    else:
        assert validate(payload)["answer_policy"] == "general_qa"


def test_native_cuekb_readiness_allows_no_text_model_but_requires_single_tool():
    import runpy
    functions = runpy.run_path(ROOT / "scripts/verify_deployment.py")
    ready = {"status": "ready", "is_mock": False, "enabled_tools": ["search_knowledge"],
             "text_configured": False, "voice_configured": True}
    assert functions["validate_ready"](ready, {"search_knowledge"}, require_text=False)["text_configured"] is False
    with pytest.raises(ValueError):
        functions["validate_ready"](ready, {"search_knowledge"})
    caps = {"provider": "nvidia", "qa_execution_mode": "dual_tools", "answer_policy": "general_qa",
            "is_mock": False, "voice_available": True, "agent_provider": "disabled",
            "native_tools": ["search_knowledge_base"]}
    assert functions["validate_voice_policy"](caps, cuekb_only=True)
    for change in ({"agent_provider": "openai"}, {"native_tools": ["lookup_knowledge", "reason_over_knowledge"]}):
        with pytest.raises(ValueError):
            functions["validate_voice_policy"]({**caps, **change}, cuekb_only=True)
