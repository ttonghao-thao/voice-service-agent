import os
import subprocess
from pathlib import Path
from urllib.parse import urlparse

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
COMPOSE_DEFAULTS = {
    "AGENT_PROVIDER": "disabled",
    "AGENT_DEADLINE_MS": "60000",
    "QA_EXECUTION_MODE": "dual_tools",
    "QA_ANSWER_POLICY": "general_qa",
    "QA_EXTERNAL_FALLBACK_ENABLED": "false",
    "QA_MAX_RETRIEVAL_CALLS": "1",
    "QA_PROVIDER_ANSWER_TIMEOUT_MS": "30000",
    "KNOWLEDGE_BASE_IDS": "",
    "WEATHER_PROVIDER": "qweather",
    "WEATHER_MODE": "real",
    "ENABLED_TOOLS": "search_knowledge,weather,web_search",
    "DELEGATION_ROUTER_PROVIDER": "openai_compatible",
}


def test_cloud_compose_is_a_production_only_container_topology():
    compose = yaml.safe_load((ROOT / "deploy/compose.production.yaml").read_text())
    services = compose["services"]

    assert set(services) == {"postgres", "redis", "livekit", "migrate", "api", "web"}
    assert "APP_ENV" not in services["api"]["environment"]
    assert services["api"]["environment"]["AUTO_CREATE_SCHEMA"] == "false"
    assert services["api"]["environment"]["AUTH_MODE"] == "validation"
    assert services["api"]["environment"]["CUEKB_MODE"] == "real"
    for name, value in COMPOSE_DEFAULTS.items():
        assert services["api"]["environment"][name] == f"${{{name}:-{value}}}"
    assert services["api"]["environment"]["VOICE_PROVIDER"] == "${VOICE_PROVIDER:-sophon_live}"
    assert services["api"]["env_file"] == "${DEPLOY_ENV_FILE:-../.env}"
    assert "SSL_CERT_FILE" not in services["api"]["environment"]
    assert "/app/ca-bundle.pem" not in str(services["api"].get("volumes", []))
    api_ca = yaml.safe_load((ROOT / "deploy/compose.api-ca.yaml").read_text())["services"]
    assert set(api_ca) == {"api"}
    assert api_ca["api"]["environment"] == {"SSL_CERT_FILE": "/app/ca-bundle.pem"}
    assert api_ca["api"]["volumes"] == [
        {
            "type": "bind",
            "source": "${API_CA_BUNDLE_FILE:?Set the API CA bundle path in .env}",
            "target": "/app/ca-bundle.pem",
            "read_only": True,
            "bind": {"create_host_path": False},
        }
    ]
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
    assert services["web"]["ports"] == ["0.0.0.0:8087:8087", "0.0.0.0:7880:7880"]
    assert "ports" not in services["api"]
    assert services["web"]["volumes"] == [
        "${WEB_TLS_CERT_FILE:?Set the TLS certificate path in .env}:/etc/nginx/tls/tls.crt:ro",
        "${WEB_TLS_KEY_FILE:?Set the TLS private key path in .env}:/etc/nginx/tls/tls.key:ro",
    ]
    assert "https://127.0.0.1:8087/health/live" in services["web"]["healthcheck"]["test"][-1]
    assert services["postgres"]["image"] == "postgres:17.6-alpine"
    assert services["redis"]["image"] == "redis:7-alpine"
    assert services["livekit"]["image"] == "livekit/livekit-server:v1.13.11"
    livekit = services["livekit"]
    config = yaml.safe_load(livekit["environment"]["LIVEKIT_CONFIG"])
    assert config["redis"]["address"] == "redis:6379"
    assert config["redis"]["db"] == 1
    assert config["redis"]["password"].startswith("${REDIS_PASSWORD:?")
    assert services["api"]["environment"]["REDIS_URL"].endswith("@redis:6379/0")
    assert config["rtc"]["node_ip"] == "${LIVEKIT_NODE_IP:-}"
    assert config["rtc"]["use_external_ip"] is False
    assert config["rtc"]["interfaces"]["includes"] == '${LIVEKIT_RTC_INTERFACE:+["${LIVEKIT_RTC_INTERFACE}"]}'
    assert livekit["environment"]["LIVEKIT_RTC_INTERFACE"] == "${LIVEKIT_RTC_INTERFACE:-}"
    assert config["rtc"]["udp_port"] == 7882 and config["rtc"]["tcp_port"] == 7881
    assert livekit["ports"] == ["0.0.0.0:7881:7881/tcp", "0.0.0.0:7882:7882/udp"]
    assert livekit["depends_on"]["redis"]["condition"] == "service_healthy"
    assert services["api"]["depends_on"]["livekit"]["condition"] == "service_healthy"
    assert services["api"]["depends_on"]["livekit"]["required"] is False
    assert livekit["profiles"] == ["sophon"]
    assert services["web"]["environment"]["VOICE_PROVIDER"] == "${VOICE_PROVIDER:-sophon_live}"
    assert set(livekit["networks"]) == {"data", "app", "edge"}
    assert "--dev" not in str(livekit)
    assert not (ROOT / "deploy/compose.yaml").exists()
    assert set(services["web"]["networks"]) == {"app", "edge"}
    assert services["postgres"]["networks"] == ["data"]
    assert set(services["api"]["networks"]) == {"data", "app", "egress"}
    assert compose["networks"]["data"]["internal"] is True
    assert compose["networks"]["app"]["internal"] is True


@pytest.mark.parametrize(("interface", "status"), [("", 0), ("eth2", 0), ("eth9", 1)])
def test_livekit_healthcheck_requires_configured_interface(tmp_path, interface, status):
    compose = yaml.safe_load((ROOT / "deploy/compose.production.yaml").read_text())
    check = compose["services"]["livekit"]["healthcheck"]["test"][1]
    interfaces = tmp_path / "interfaces"
    (interfaces / "eth2").mkdir(parents=True)
    (interfaces / "eth2" / "ifindex").write_text("3\n")
    wget = tmp_path / "wget"
    wget.write_text('#!/bin/sh\nprintf "HTTP readiness checked\\n"\n')
    wget.chmod(0o755)
    result = subprocess.run(
        ["sh", "-c", check.replace("$$", "$").replace("/sys/class/net", str(interfaces))],
        env={"PATH": f"{tmp_path}:{os.environ['PATH']}", "LIVEKIT_RTC_INTERFACE": interface},
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == status
    if status:
        assert f"LIVEKIT_RTC_INTERFACE '{interface}' was not found" in result.stderr
        assert result.stdout == ""
    else:
        assert result.stdout == "HTTP readiness checked\n"


def test_cloud_environment_template_cannot_enable_development_fallbacks():
    values = {}
    for line in (ROOT / ".env.example").read_text().splitlines():
        if line and not line.startswith("#"):
            key, value = line.split("=", 1)
            values[key] = value

    assert set(values) == {
        "LIVEKIT_URL", "LIVEKIT_NODE_IP", "LIVEKIT_RTC_INTERFACE", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET",
        "WEB_SEARCH_API_KEY",
        "WEATHER_BASE_URL", "WEATHER_API_KEY",
        "IMAGE_TAG",
        "PUBLIC_ORIGIN",
        "WEB_TLS_CERT_FILE",
        "WEB_TLS_KEY_FILE",
        "API_CA_BUNDLE_FILE",
        "POSTGRES_PASSWORD",
        "REDIS_PASSWORD",
        "KNOWLEDGE_BASE_IDS",
        "ENABLED_TOOLS",
        "CUEKB_BASE_URL",
        "CUEKB_API_KEY",
        "VOICE_PROVIDER",
        "SOPHON_LIVE_WS_URL",
        "SOPHON_LIVE_API_KEY",
        "SOPHON_LIVE_LOCALE",
        "DELEGATION_ROUTER_BASE_URL",
        "DELEGATION_ROUTER_MODEL",
        "DELEGATION_ROUTER_API_KEY",
    }
    assert "REPLACE_" in values["WEATHER_BASE_URL"]
    assert values["WEATHER_API_KEY"].startswith("REPLACE_")
    assert values["VOICE_PROVIDER"] == "sophon_live"
    assert values["ENABLED_TOOLS"] == COMPOSE_DEFAULTS["ENABLED_TOOLS"]
    assert values["DELEGATION_ROUTER_MODEL"] == "qwen3.7-flash"
    assert urlparse(values["LIVEKIT_URL"]).scheme == "wss"
    assert urlparse(values["LIVEKIT_URL"]).port == 7880
    assert values["LIVEKIT_NODE_IP"].startswith("REPLACE_")
    assert values["LIVEKIT_RTC_INTERFACE"] == ""
    origin = urlparse(values["PUBLIC_ORIGIN"])
    assert origin.scheme == "https" and origin.port == 8087 and origin.path == ""
    assert values["WEB_TLS_CERT_FILE"].startswith("/")
    assert values["WEB_TLS_KEY_FILE"].startswith("/")
    assert values["API_CA_BUNDLE_FILE"] == ""
    assert values["IMAGE_TAG"]
    assert urlparse(values["CUEKB_BASE_URL"]).scheme in {"http", "https"}
    for key in (
        "POSTGRES_PASSWORD",
        "REDIS_PASSWORD",
        "CUEKB_API_KEY",
        "KNOWLEDGE_BASE_IDS",
        "LIVEKIT_API_KEY",
        "LIVEKIT_API_SECRET",
        "WEB_SEARCH_API_KEY",
        "DELEGATION_ROUTER_API_KEY",
    ):
        assert values[key].startswith("REPLACE_")
    assert not (ROOT / ".env.production.example").exists()


def test_cloud_deployment_uses_prebuilt_images():
    script = (ROOT / "scripts/deploy-cloud.sh").read_text()
    assert "compose build" not in script
    assert 'docker image inspect "$image"' in script
    assert script.count("compose up -d --no-build") == 4
    assert "IMAGE_TAG" in script
    assert "LOCAL_USERS_JSON" not in script and "API_BIND_ADDRESS" not in script
    assert "APP_ENV" not in script
    dockerfile = (ROOT / "deploy/Dockerfile.api").read_text()
    assert "COPY scripts/configure_native_cuekb.py scripts/configure_native_cuekb.py" in dockerfile
    assert "COPY scripts/validate_native_cuekb.py scripts/validate_native_cuekb.py" in dockerfile
    assert "COPY scripts/run_native_cuekb_validation.py scripts/run_native_cuekb_validation.py" in dockerfile
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
    assert '"$VOICECHAT_RELEASE_IMAGE"' in voicechat_runbook
    selection_runbook = (ROOT / "docs/validation.md").read_text()
    assert "bash scripts/validate_deployment.sh auto core" in selection_runbook
    assert "bash scripts/validate_deployment.sh web-end" in selection_runbook
    assert "COPY scripts/collect_validation_summary.py scripts/collect_validation_summary.py" in dockerfile
    assert "COPY scripts/inspect_input_prefix.py scripts/inspect_input_prefix.py" in dockerfile
    assert "configure_native_cuekb.py --apply" not in script
    assert "--expect-cuekb-native" not in script
    nginx = (ROOT / "deploy/nginx.conf").read_text()
    assert "listen 8087 ssl;" in nginx
    assert "listen 7880 ssl;" in nginx
    assert "proxy_pass http://$livekit_upstream;" in nginx
    assert "set $livekit_upstream livekit:7880;" in nginx
    assert "resolver 127.0.0.11" in nginx
    assert "if ($selected_voice != sophon_live) { return 404; }" in nginx
    assert "proxy_set_header Upgrade $http_upgrade;" in nginx
    assert "access_log off;" in nginx.split("listen 7880 ssl;", 1)[1]
    assert "ssl_certificate /etc/nginx/tls/tls.crt;" in nginx
    assert "/api/v1/auth/login" not in nginx
    assert "login_limit" not in nginx
    assert "proxy_set_header X-Forwarded-For $remote_addr;" in nginx


def test_cloud_template_loads_real_settings_from_environment_strings(tmp_path, monkeypatch):
    from app.config import Settings
    from app.voice.gateway import VoiceGateway
    from app.voice.provider import NvidiaVoiceChatAdapter
    from app.voice.sophon_live import SophonLiveAdapter
    from pydantic import ValidationError

    values = COMPOSE_DEFAULTS | dict(
        line.split("=", 1) for line in (ROOT / ".env.example").read_text().splitlines()
        if line and not line.startswith("#")
    )
    values.update(
        AUTO_CREATE_SCHEMA="false", AUTH_MODE="validation", CUEKB_MODE="real",
        PUBLIC_ORIGIN="https://voice.test:8087",
        DATABASE_URL="postgresql+asyncpg://voice_service:test-postgres@postgres/voice_service",
        REDIS_URL="redis://:test-redis@redis:6379/0",
        LIVEKIT_URL="wss://voice.test:7880", LIVEKIT_NODE_IP="203.0.113.7",
        LIVEKIT_API_KEY="test-key", LIVEKIT_API_SECRET="test-secret-with-at-least-32-characters",
        CUEKB_BASE_URL="https://cuekb.test", CUEKB_API_KEY="test-cuekb-key",
        KNOWLEDGE_BASE_IDS="00000000-0000-4000-8000-000000000001",
        SOPHON_LIVE_WS_URL="wss://sophon.test/v1/live/sessions",
        WEATHER_BASE_URL="https://fixture.qweatherapi.com", WEATHER_API_KEY="test-weather-key",
        WEB_SEARCH_API_KEY="test-search-key",
        DELEGATION_ROUTER_BASE_URL="https://intent.fixture/v1",
        DELEGATION_ROUTER_API_KEY="test-intent-key",
    )
    for name in values:
        monkeypatch.delenv(name, raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text("\n".join(f"{key}={value}" for key, value in values.items()) + "\n")
    settings = Settings(_env_file=env_file)
    settings.validate_deployment()
    assert settings.enabled_tool_names == {"search_knowledge", "weather", "web_search"}
    assert settings.sophon_live_max_delegations_per_input == 1
    assert settings.voice_max_retrievals_per_input == 1
    assert settings.cuekb_max_evidence_bytes == 24000
    assert settings.cuekb_voice_token_budget == 120
    assert settings.livekit_api_secret.get_secret_value() == values["LIVEKIT_API_SECRET"]
    for invalid in ("0", "2", "1.5", "invalid"):
        with pytest.raises(ValidationError):
            Settings(_env_file=env_file, sophon_live_max_delegations_per_input=invalid)

    # Backend selection is a startup contract, without connecting to NVIDIA.
    nvidia = Settings(_env_file=env_file, voice_provider="nvidia",
        voicechat_ws_url="wss://voicechat.test/ws", voicechat_api_key="test-voice-key",
        sophon_live_ws_url="inactive", livekit_url="inactive",
        livekit_api_key="", livekit_api_secret="", delegation_router_base_url="inactive",
        delegation_router_api_key="", delegation_router_model="")
    nvidia.validate_deployment()
    assert not nvidia.livekit_enabled
    assert settings.livekit_enabled
    assert VoiceGateway(settings, None, None).provider_factory is SophonLiveAdapter
    assert VoiceGateway(nvidia, None, None).provider_factory is NvidiaVoiceChatAdapter
    for invalid in ("sophon_live,nvidia", "nvidia,sophon_live", "", "both"):
        with pytest.raises(ValidationError):
            Settings(_env_file=env_file, voice_provider=invalid)


def test_deployment_rejects_incomplete_config_before_starting_containers(tmp_path):
    docker = tmp_path / "docker"
    docker.write_text(
        '#!/bin/sh\n'
        'printf "%s | profiles=%s\\n" "$*" "$COMPOSE_PROFILES" >> "$DOCKER_CALLS"\n'
        'case "$*" in\n'
        '  *" config --quiet")\n'
        '    printf "%s,%s\\n%s\\n%s\\n%s\\n" "${AGENT_PROVIDER-unset}" "${WEATHER_MODE-unset}" \\\n'
        '      "$LIVEKIT_RTC_INTERFACE" "$API_CA_BUNDLE_FILE" "$ENABLED_TOOLS" > "$COMPOSE_INPUTS" ;;\n'
        'esac\nexit 0\n'
    )
    docker.chmod(0o755)
    calls = tmp_path / "docker-calls"
    compose_inputs = tmp_path / "compose-inputs"
    env = {
        **os.environ,
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "DOCKER_CALLS": str(calls),
        "COMPOSE_INPUTS": str(compose_inputs),
        "AGENT_PROVIDER": "mock",
        "WEATHER_MODE": "mock",
        "LIVEKIT_RTC_INTERFACE": "inherited0",
        "API_CA_BUNDLE_FILE": "/missing/inherited-ca.pem",
    }
    values = {
        "IMAGE_TAG": "test",
        "POSTGRES_PASSWORD": "postgres-pass",
        "REDIS_PASSWORD": "redis-pass",
        "KNOWLEDGE_BASE_IDS": "00000000-0000-4000-8000-000000000001",
        "VOICE_PROVIDER": "sophon_live",
        "SOPHON_LIVE_WS_URL": "wss://sophon.test/v1/live/sessions",
        "LIVEKIT_URL": "wss://voice.test:7880",
        "LIVEKIT_NODE_IP": "203.0.113.7",
        "LIVEKIT_API_KEY": "test-key",
        "LIVEKIT_API_SECRET": "test-secret-with-at-least-32-characters",
        "WEATHER_BASE_URL": "https://weather.test",
        "WEATHER_API_KEY": "weather-key",
        "WEB_SEARCH_API_KEY": "public-search-key",
        "DELEGATION_ROUTER_BASE_URL": "https://intent.test/v1",
        "DELEGATION_ROUTER_MODEL": "qwen3.7-flash",
        "DELEGATION_ROUTER_API_KEY": "test-intent-key",
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
        calls.write_text("")
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
    del values["AGENT_PROVIDER"]
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
    assert compose_inputs.read_text().splitlines() == ["unset,unset", "", "", COMPOSE_DEFAULTS["ENABLED_TOOLS"]]
    assert "compose.api-ca.yaml" not in calls.read_text()
    assert all(line.endswith(" | profiles=sophon") for line in calls.read_text().splitlines()[1:])
    commands = [line.split(" | profiles=", 1)[0] for line in calls.read_text().splitlines()]
    infrastructure = next(i for i, command in enumerate(commands) if command.endswith("postgres redis livekit"))
    migration = next(i for i, command in enumerate(commands) if command.endswith("run --rm --no-deps migrate"))
    api = next(i for i, command in enumerate(commands) if " up " in command and command.endswith("--no-deps api"))
    verification = next(i for i, command in enumerate(commands) if "--expect-autonomous-voice" in command)
    web = next(i for i, command in enumerate(commands) if command.endswith("--force-recreate web"))
    assert infrastructure < migration < api < verification < web
    assert not any("configure_native_cuekb.py" in command for command in commands)
    bundle = tmp_path / "api trust bundle.pem"
    bundle.write_text("test CA bundle")
    values["API_CA_BUNDLE_FILE"] = f'"{bundle}"'
    accepted_bundle = check()
    assert accepted_bundle.returncode == 0, accepted_bundle.stderr
    assert compose_inputs.read_text().splitlines()[2] == str(bundle)
    compose_calls = [line for line in calls.read_text().splitlines() if line.startswith("compose --env-file")]
    assert compose_calls and all("compose.api-ca.yaml" in line for line in compose_calls)
    for invalid_bundle, error in (
        ("relative.pem", "an absolute host path"),
        (str(tmp_path / "missing.pem"), "a readable file"),
        (str(tmp_path), "a readable file"),
    ):
        values["API_CA_BUNDLE_FILE"] = invalid_bundle
        rejected_bundle = check()
        assert rejected_bundle.returncode != 0
        assert "API_CA_BUNDLE_FILE" in rejected_bundle.stderr and error in rejected_bundle.stderr
        assert "compose --env-file" not in calls.read_text()
    del values["API_CA_BUNDLE_FILE"]
    values["LIVEKIT_RTC_INTERFACE"] = '"eth2"'
    accepted_interface = check()
    assert accepted_interface.returncode == 0, accepted_interface.stderr
    assert compose_inputs.read_text().splitlines()[1] == "eth2"
    del values["LIVEKIT_RTC_INTERFACE"]
    values["QA_ANSWER_POLICY"] = "knowledge_required"
    rejected_strict = check()
    assert rejected_strict.returncode != 0
    assert "QA_ANSWER_POLICY=general_qa" in rejected_strict.stderr
    del values["QA_ANSWER_POLICY"]

    for key, bad_value, message in (
        ("LIVEKIT_URL", "ws://voice.test:7880", "WSS origin"),
        ("LIVEKIT_URL", "", "real value for LIVEKIT_URL"),
        ("LIVEKIT_NODE_IP", "203.0.113.999", "host IPv4"),
        ("LIVEKIT_RTC_INTERFACE", "eth2,eth0", "one container network interface name"),
        ("LIVEKIT_RTC_INTERFACE", "..", "one container network interface name"),
        ("LIVEKIT_RTC_INTERFACE", "very-long-interface0", "one container network interface name"),
        ("LIVEKIT_API_KEY", 'key\": injected', "URL-safe"),
        ("LIVEKIT_API_SECRET", "too-short", "at least 32"),
        ("LIVEKIT_API_SECRET", "", "real value for LIVEKIT_API_SECRET"),
        ("REDIS_PASSWORD", 'bad\"password', "URL-safe"),
        ("ENABLED_TOOLS", "search_knowledge,weather", "include web_search"),
        ("CUEKB_BASE_URL", "", "real value for CUEKB_BASE_URL"),
        ("CUEKB_API_KEY", "", "real value for CUEKB_API_KEY"),
        ("KNOWLEDGE_BASE_IDS", "REPLACE_WITH_CUEKB_KB_UUID", "real value for KNOWLEDGE_BASE_IDS"),
        ("WEATHER_BASE_URL", "", "real value for WEATHER_BASE_URL"),
        ("WEATHER_API_KEY", "", "real value for WEATHER_API_KEY"),
        ("WEB_SEARCH_API_KEY", "", "real value for WEB_SEARCH_API_KEY"),
        ("DELEGATION_ROUTER_API_KEY", "", "real value for DELEGATION_ROUTER_API_KEY"),
        ("DELEGATION_ROUTER_PROVIDER", "rule", "DELEGATION_ROUTER_PROVIDER=openai_compatible"),
        ("WEATHER_MODE", "mock", "WEATHER_MODE=real"),
        ("VOICE_PROVIDER", "sophon_live,nvidia", "select exactly one backend"),
        ("VOICE_PROVIDER", "", "select exactly one backend"),
    ):
        original = values.get(key)
        values[key] = bad_value
        rejected = check()
        assert rejected.returncode != 0, (key, rejected.stdout)
        assert message in rejected.stderr, (key, rejected.stderr)
        assert not any(" up " in command or " run " in command for command in calls.read_text().splitlines())
        if original is None:
            del values[key]
        else:
            values[key] = original

    cuekb_fields = {"CUEKB_BASE_URL", "CUEKB_API_KEY", "KNOWLEDGE_BASE_IDS"}
    weather_fields = {"WEATHER_BASE_URL", "WEATHER_API_KEY"}
    full_values = values.copy()
    for tools, normalized, omitted in (
        ("weather, web_search", "weather,web_search", cuekb_fields),
        ("search_knowledge,web_search", "search_knowledge,web_search", weather_fields),
        ("web_search", "web_search", cuekb_fields | weather_fields),
    ):
        values.clear()
        values.update({key: value for key, value in full_values.items() if key not in omitted})
        values.update(ENABLED_TOOLS=tools, PUBLIC_ORIGIN="https://203.0.113.7:8087", LIVEKIT_URL="wss://203.0.113.7:7880")
        accepted = check()
        assert accepted.returncode == 0, accepted.stderr
        assert compose_inputs.read_text().splitlines()[-1] == normalized
        assert "--force-recreate web" in calls.read_text()
    values.clear()
    values.update(full_values)

    values["VOICE_PROVIDER"] = "nvidia"
    # Neither dormant Sophon fields nor a caller's inherited profile may start SFU.
    for name in list(values):
        if name.startswith(("SOPHON_", "LIVEKIT_", "DELEGATION_ROUTER_")):
            values.pop(name)
    values["LIVEKIT_RTC_INTERFACE"] = "eth2,eth0"
    env["COMPOSE_PROFILES"] = "sophon"
    accepted_nvidia = check()
    assert accepted_nvidia.returncode == 0, accepted_nvidia.stderr
    assert compose_inputs.read_text().splitlines()[1] == ""
    commands = calls.read_text().splitlines()[1:]
    assert all(command.endswith(" | profiles=") for command in commands)
    assert any("--profile sophon stop livekit" in command for command in commands)
    assert any("postgres redis | profiles=" in command for command in commands)
    assert not any(" up " in command and "livekit" in command for command in commands)
    values["ENABLED_TOOLS"] = "web_search"
    rejected_nvidia = check()
    assert rejected_nvidia.returncode != 0
    assert "NVIDIA requires ENABLED_TOOLS to include search_knowledge" in rejected_nvidia.stderr
    del values["ENABLED_TOOLS"]
    values["VOICECHAT_API_KEY"] = ""
    rejected_nvidia = check()
    assert rejected_nvidia.returncode != 0
    assert "real value for VOICECHAT_API_KEY" in rejected_nvidia.stderr


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
