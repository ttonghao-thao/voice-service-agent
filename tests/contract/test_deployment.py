from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def test_cloud_compose_is_a_production_only_container_topology():
    compose = yaml.safe_load((ROOT / "deploy/compose.production.yaml").read_text())
    services = compose["services"]

    assert set(services) == {"postgres", "redis", "migrate", "api", "web"}
    assert services["api"]["environment"]["APP_ENV"] == "production"
    assert services["api"]["environment"]["AUTO_CREATE_SCHEMA"] == "false"
    assert services["api"]["env_file"] == "${PRODUCTION_ENV_FILE:-../.env.production}"
    assert services["migrate"]["command"][-2:] == ["upgrade", "head"]
    assert services["api"]["depends_on"]["migrate"]["condition"] == "service_completed_successfully"
    assert "ports" not in services["postgres"] and "ports" not in services["redis"]
    assert services["web"]["ports"] == ["${WEB_BIND_ADDRESS:-127.0.0.1}:${WEB_PORT:-8080}:80"]
    assert set(services["web"]["networks"]) == {"app", "edge"}
    assert services["postgres"]["networks"] == ["data"]
    assert set(services["api"]["networks"]) == {"data", "app", "egress"}
    assert compose["networks"]["data"]["internal"] is True
    assert compose["networks"]["app"]["internal"] is True


def test_cloud_environment_template_cannot_enable_development_fallbacks():
    values = {}
    for line in (ROOT / ".env.production.example").read_text().splitlines():
        if line and not line.startswith("#"):
            key, value = line.split("=", 1)
            values[key] = value

    assert values["APP_ENV"] == "production"
    assert values["AUTH_MODE"] == "oidc"
    assert values["AGENT_PROVIDER"] != "mock"
    assert values["RAG_MODE"] == "real"
    assert values["WEATHER_MODE"] == "real"
    assert values["VOICE_PROVIDER"] != "mock"
    assert values["VOICECHAT_API_VERSION"]
    assert values["VOICECHAT_IMAGE_DIGEST"].startswith("sha256:")
    assert values["VOICECHAT_CAPABILITY_MODE"] == "unverified"
    assert values["VOICECHAT_INTEGRATION_VERIFIED"] == "false"
    assert values["PUBLIC_ORIGIN"].startswith("https://")
