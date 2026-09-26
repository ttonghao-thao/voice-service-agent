import json
import os
import subprocess
from pathlib import Path
from urllib.parse import urlparse

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
        "AGENT_MODEL",
        "OPENAI_API_KEY",
        "CUEKB_BASE_URL",
        "CUEKB_API_KEY",
        "VOICECHAT_WS_URL",
        "VOICECHAT_API_KEY",
    }
    assert "APP_ENV" not in values
    assert "AUTH_MODE" not in values
    assert values["AGENT_PROVIDER"] == "openai"
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
        "OPENAI_API_KEY",
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
        "AGENT_PROVIDER": "openai",
        "AGENT_MODEL": "model",
        "OPENAI_API_KEY": "key",
        "PUBLIC_ORIGIN": "https://voice.test:8087",
        "CUEKB_BASE_URL": "https://cuekb.test",
        "CUEKB_API_KEY": "cuekb-key",
        "VOICECHAT_WS_URL": "wss://voicechat.test/ws",
        "VOICECHAT_API_KEY": "voice-key",
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
    assert "AGENT_BASE_URL" in rejected_model.stderr
    values["AGENT_PROVIDER"] = "openai"
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
