import json
import os
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def test_cloud_compose_is_a_production_only_container_topology():
    compose = yaml.safe_load((ROOT / "deploy/compose.production.yaml").read_text())
    services = compose["services"]

    assert set(services) == {"postgres", "redis", "migrate", "api", "web"}
    assert "APP_ENV" not in services["api"]["environment"]
    assert services["api"]["environment"]["AUTO_CREATE_SCHEMA"] == "false"
    assert services["api"]["env_file"] == "${DEPLOY_ENV_FILE:-../.env}"
    assert services["api"]["image"] == services["migrate"]["image"]
    assert services["api"]["image"].startswith("${API_IMAGE:?")
    assert services["web"]["image"].startswith("${WEB_IMAGE:?")
    for name in ("migrate", "api", "web"):
        assert "build" not in services[name]
        assert services[name]["pull_policy"] == "never"
    assert services["migrate"]["command"][-2:] == ["upgrade", "head"]
    assert services["api"]["depends_on"]["migrate"]["condition"] == "service_completed_successfully"
    assert "ports" not in services["postgres"] and "ports" not in services["redis"]
    assert services["web"]["ports"] == ["0.0.0.0:8087:8087"]
    assert services["api"]["ports"] == ["${API_BIND_ADDRESS:?Set the host private IPv4 address in .env}:8088:8000"]
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

    assert "APP_ENV" not in values
    assert values["AUTH_MODE"] == "local"
    assert values["AGENT_PROVIDER"] != "mock"
    assert values["CUEKB_MODE"] == "real"
    assert values["ENABLED_TOOLS"] == "search_knowledge"
    assert not any(key.startswith("WEATHER_") for key in values)
    assert values["VOICE_PROVIDER"] != "mock"
    assert not values["VOICECHAT_API_VERSION"] and not values["VOICECHAT_IMAGE_DIGEST"]
    assert values["VOICECHAT_CAPABILITY_MODE"] == "unverified"
    assert values["VOICECHAT_INTEGRATION_VERIFIED"] == "false"
    assert values["PUBLIC_ORIGIN"].startswith("REPLACE_")
    assert values["API_BIND_ADDRESS"].startswith("REPLACE_")
    assert values["WEB_TLS_CERT_FILE"].startswith("REPLACE_")
    assert values["WEB_TLS_KEY_FILE"].startswith("REPLACE_")
    assert values["LOCAL_USERS_JSON"].startswith("REPLACE_")
    assert values["CUEKB_BASE_URL"].startswith("REPLACE_")
    assert values["API_IMAGE"].startswith("REPLACE_")
    assert values["WEB_IMAGE"].startswith("REPLACE_")
    assert not (ROOT / ".env.production.example").exists()


def test_cloud_deployment_uses_prebuilt_images():
    script = (ROOT / "scripts/deploy-cloud.sh").read_text()
    assert "compose build" not in script
    assert 'docker image inspect "$image"' in script
    assert script.count("compose up -d --no-build") == 3
    assert "LOCAL_USERS_JSON" in script
    assert "APP_ENV" not in script
    nginx = (ROOT / "deploy/nginx.conf").read_text()
    assert "listen 8087 ssl;" in nginx
    assert "ssl_certificate /etc/nginx/tls/tls.crt;" in nginx
    assert "location = /api/v1/auth/login" in nginx
    assert "zone=login_limit" in nginx


def test_deployment_rejects_public_api_bind_and_missing_tls_files(tmp_path):
    docker = tmp_path / "docker"
    docker.write_text("#!/bin/sh\nexit 0\n")
    docker.chmod(0o755)
    env = {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}"}
    values = {
        "AUTH_MODE": "local",
        "API_IMAGE": "api:test",
        "WEB_IMAGE": "web:test",
        "POSTGRES_PASSWORD": "postgres-pass",
        "REDIS_PASSWORD": "redis-pass",
        "AUTH_COOKIE_SECRET": "a" * 32,
        "LOCAL_USERS_JSON": "accounts",
        "TENANT_ID": "tenant",
        "KNOWLEDGE_BASE_IDS": "00000000-0000-4000-8000-000000000001",
        "AGENT_PROVIDER": "openai",
        "AGENT_MODEL": "model",
        "OPENAI_API_KEY": "key",
        "ENABLED_TOOLS": "search_knowledge",
        "API_BIND_ADDRESS": "0.0.0.0",
        "WEB_TLS_CERT_FILE": "/missing/cert.pem",
        "WEB_TLS_KEY_FILE": "/missing/key.pem",
    }
    env_file = tmp_path / ".env"

    def check():
        env_file.write_text("\n".join(f"{key}={value}" for key, value in values.items()) + "\n")
        return subprocess.run(
            ["sh", str(ROOT / "scripts/deploy-cloud.sh"), str(env_file)],
            env=env, capture_output=True, text=True, check=False,
        )

    rejected_ip = check()
    assert rejected_ip.returncode != 0
    assert "API_BIND_ADDRESS must be a host RFC 1918 private IPv4 address" in rejected_ip.stderr
    values["API_BIND_ADDRESS"] = "192.168.1.10"
    rejected_cert = check()
    assert rejected_cert.returncode != 0
    assert "WEB_TLS_CERT_FILE must point to a readable file" in rejected_cert.stderr


def test_deployment_readiness_verifier_requires_real_matching_configuration():
    import runpy

    validate_ready = runpy.run_path(ROOT / "scripts/verify_deployment.py")["validate_ready"]

    assert validate_ready(
        {
            "status": "ready",
            "is_mock": False,
            "enabled_tools": ["search_knowledge"],
            "text_configured": True,
            "voice_configured": False,
        },
        {"search_knowledge"},
    )["enabled_tools"] == ["search_knowledge"]
