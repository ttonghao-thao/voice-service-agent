import re
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse
from uuid import UUID

from pydantic import SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / ".env", extra="ignore")
    auto_create_schema: bool = False
    auth_mode: Literal["fixture", "validation"] = "fixture"
    public_origin: str = "http://localhost:5173"
    database_url: str = "sqlite+aiosqlite:///./voice-service.db"
    redis_url: str | None = None
    dev_user_id: str = "dev-operator"
    dev_tenant_id: str = "dev-tenant"
    dev_admin: bool = False
    tenant_id: str = ""
    knowledge_base_ids: str = "00000000-0000-4000-8000-000000000001"
    agent_provider: Literal["mock", "openai", "compatible"] = "mock"
    agent_model: str = ""
    agent_base_url: str | None = None
    openai_api_key: SecretStr = SecretStr("")
    agent_deadline_ms: int = 12000
    max_agent_runs: int = 16
    cuekb_mode: Literal["mock", "real"] = "mock"
    cuekb_base_url: str = ""
    cuekb_api_key: SecretStr = SecretStr("")
    cuekb_search_mode: Literal["auto", "exact", "hybrid", "related"] = "auto"
    cuekb_top_k: int = 5
    enabled_tools: str = "search_knowledge"
    weather_mode: Literal["mock", "real"] = "mock"
    weather_provider: Literal["http_contract"] = "http_contract"
    weather_base_url: str = ""
    weather_api_key: SecretStr = SecretStr("")
    voice_provider: Literal["disabled", "mock", "nvidia"] = "mock"
    voicechat_ws_url: str = ""
    voicechat_api_key: SecretStr = SecretStr("")
    voice_session_max_seconds: int = 105
    max_voice_sessions: int = 8
    default_locale: Literal["en-US"] = "en-US"
    required_voice_languages: Literal["en-US"] = "en-US"
    retention_days: int = 30
    request_limit_per_minute: int = 60
    external_tracing_enabled: bool = False

    @field_validator("agent_base_url", "redis_url", mode="before")
    @classmethod
    def optional_urls(cls, value):
        return value or None

    @field_validator("enabled_tools")
    @classmethod
    def normalized_enabled_tools(cls, value):
        names = [name.strip() for name in value.split(",") if name.strip()]
        if (
            not names
            or len(names) != len(set(names))
            or any(not re.fullmatch(r"[a-z][a-z0-9_]{0,79}", name) for name in names)
        ):
            raise ValueError("ENABLED_TOOLS must contain unique tool names")
        return ",".join(names)

    @model_validator(mode="after")
    def check(self):
        if self.agent_deadline_ms < 100 or self.max_voice_sessions < 1 or self.max_agent_runs < 1:
            raise ValueError("Invalid deadline or capacity")
        if not 1 <= self.cuekb_top_k <= 20:
            raise ValueError("CueKB top_k must be between 1 and 20")
        try:
            knowledge_ids = [value.strip() for value in self.knowledge_base_ids.split(",") if value.strip()]
            if not knowledge_ids or len(set(map(UUID, knowledge_ids))) != len(knowledge_ids):
                raise ValueError
        except ValueError as exc:
            raise ValueError("KNOWLEDGE_BASE_IDS must contain unique UUID values") from exc
        if not 10 <= self.voice_session_max_seconds <= 600 or self.retention_days < 1:
            raise ValueError("Invalid session timeout or retention")
        if self.external_tracing_enabled:
            raise ValueError("External tracing requires a reviewed redaction exporter; currently disabled")
        return self

    def validate_deployment(self) -> None:
        """Fail closed for the single deployed configuration; injected test settings bypass this gate."""
        if self.auto_create_schema or self.auth_mode != "validation" or self.mock:
            raise ValueError(
                "Deployment forbids automatic schema creation, fixture identity and mock providers"
            )
        if not self.database_url.startswith("postgresql+asyncpg:") or not self.redis_url:
            raise ValueError("Deployment requires PostgreSQL and Redis")
        if not self.agent_model or not self.openai_api_key.get_secret_value():
            raise ValueError("Deployment requires a configured text model")
        if self.agent_provider == "compatible" and not self.agent_base_url:
            raise ValueError("Compatible text model requires AGENT_BASE_URL")
        if "search_knowledge" in self.enabled_tool_names and (
            self.cuekb_mode != "real" or not all((self.cuekb_base_url, self.cuekb_api_key.get_secret_value()))
        ):
            raise ValueError("Deployment requires real CueKB when search_knowledge is enabled")
        if "weather" in self.enabled_tool_names and (
            self.weather_mode != "real"
            or not all((self.weather_base_url, self.weather_api_key.get_secret_value()))
        ):
            raise ValueError("Deployment requires real weather when weather is enabled")
        origin = urlparse(self.public_origin)
        if (
            origin.scheme != "https"
            or not origin.hostname
            or origin.username
            or origin.password
            or origin.path
            or origin.query
            or origin.fragment
        ):
            raise ValueError("PUBLIC_ORIGIN must be an HTTPS origin without a path")
        if not self.tenant_id:
            raise ValueError("Deployment requires a tenant")
        if self.voice_provider != "nvidia":
            raise ValueError("Deployment requires NVIDIA VoiceChat for end-to-end validation")
        urls = [self.agent_base_url]
        if "search_knowledge" in self.enabled_tool_names:
            urls.append(self.cuekb_base_url)
        if "weather" in self.enabled_tool_names:
            urls.append(self.weather_base_url)
        if any(url and urlparse(url).scheme != "https" for url in urls):
            raise ValueError("Deployment HTTP integrations require TLS")
        if self.voicechat_ws_url and not self.voicechat_ws_url.startswith("wss://"):
            raise ValueError("Deployment VoiceChat requires WSS")
        if self.voice_provider == "nvidia" and not (
            self.voicechat_ws_url and self.voicechat_api_key.get_secret_value()
        ):
            raise ValueError("NVIDIA voice requires VOICECHAT_WS_URL and VOICECHAT_API_KEY")

    @property
    def mock(self) -> bool:
        enabled_modes = [self.agent_provider]
        if self.voice_provider != "disabled":
            enabled_modes.append(self.voice_provider)
        if "search_knowledge" in self.enabled_tool_names:
            enabled_modes.append(self.cuekb_mode)
        if "weather" in self.enabled_tool_names:
            enabled_modes.append(self.weather_mode)
        return "mock" in enabled_modes

    @property
    def enabled_tool_names(self) -> frozenset[str]:
        return frozenset(self.enabled_tools.split(","))
