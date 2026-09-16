import re
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from pydantic import SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / ".env", extra="ignore")
    auto_create_schema: bool = False
    app_env: Literal["development", "integration", "production"] = "development"
    auth_mode: Literal["dev", "oidc"] = "dev"
    public_origin: str = "http://localhost:5173"
    database_url: str = "sqlite+aiosqlite:///./voice-service.db"
    redis_url: str | None = None
    dev_user_id: str = "dev-operator"
    dev_tenant_id: str = "dev-tenant"
    dev_admin: bool = False
    oidc_issuer: str = ""
    oidc_audience: str = ""
    oidc_jwks_url: str = ""
    oidc_authorization_url: str = ""
    oidc_token_url: str = ""
    oidc_client_secret: SecretStr = SecretStr("")
    auth_cookie_secret: SecretStr = SecretStr("")
    tenant_id: str = ""
    knowledge_base_ids: str = "kb_support"
    rag_source_hosts: str = ""
    agent_provider: Literal["mock", "openai", "compatible"] = "mock"
    agent_model: str = ""
    agent_base_url: str | None = None
    openai_api_key: SecretStr = SecretStr("")
    agent_deadline_ms: int = 12000
    max_agent_runs: int = 16
    rag_mode: Literal["mock", "real"] = "mock"
    rag_base_url: str = ""
    rag_api_key: SecretStr = SecretStr("")
    weather_mode: Literal["mock", "real"] = "mock"
    weather_provider: Literal["http_contract"] = "http_contract"
    weather_base_url: str = ""
    weather_api_key: SecretStr = SecretStr("")
    voice_provider: Literal["disabled", "mock", "nvidia"] = "mock"
    voicechat_ws_url: str = ""
    voicechat_health_url: str = ""
    voicechat_api_key: SecretStr = SecretStr("")
    voicechat_api_version: str = ""
    voicechat_image_digest: str = ""
    voicechat_capability_mode: Literal["unverified", "basic", "enhanced"] = "unverified"
    voicechat_integration_verified: bool = False
    cuekb_api_revision: str = ""
    voice_session_max_seconds: int = 105
    max_voice_sessions: int = 8
    default_locale: str = "zh-CN"
    required_voice_languages: str = "zh-CN"
    retention_days: int = 30
    request_limit_per_minute: int = 60
    external_tracing_enabled: bool = False

    @field_validator("agent_base_url", "redis_url", mode="before")
    @classmethod
    def optional_urls(cls, value):
        return value or None

    @model_validator(mode="after")
    def check(self):
        if self.agent_deadline_ms < 100 or self.max_voice_sessions < 1 or self.max_agent_runs < 1:
            raise ValueError("Invalid deadline or capacity")
        if not 10 <= self.voice_session_max_seconds <= 600 or self.retention_days < 1:
            raise ValueError("Invalid session timeout or retention")
        if self.external_tracing_enabled:
            raise ValueError("External tracing requires a reviewed redaction exporter; currently disabled")
        if self.auth_mode == "oidc" and not all(
            (self.oidc_issuer, self.oidc_audience, self.oidc_jwks_url, self.tenant_id)
        ):
            raise ValueError("OIDC issuer, audience, JWKS and server tenant are required")
        if self.app_env == "production":
            if self.auto_create_schema:
                raise ValueError("Production schema changes must use migrations")
            if self.auth_mode != "oidc" or any(
                x == "mock"
                for x in (self.agent_provider, self.rag_mode, self.weather_mode, self.voice_provider)
            ):
                raise ValueError("Production forbids dev identity and mock providers")
            if not self.database_url.startswith("postgresql+asyncpg:") or not self.redis_url:
                raise ValueError("Production requires PostgreSQL and Redis")
            if not self.agent_model or not self.openai_api_key.get_secret_value():
                raise ValueError("Production requires a configured text model")
            if not all(
                (
                    self.rag_base_url,
                    self.rag_api_key.get_secret_value(),
                    self.weather_base_url,
                    self.weather_api_key.get_secret_value(),
                )
            ):
                raise ValueError("Production requires real tool providers")
            if (
                not self.public_origin.startswith("https://")
                or len(self.auth_cookie_secret.get_secret_value()) < 32
            ):
                raise ValueError("Production requires HTTPS and a strong cookie signing secret")
            for url in (
                self.oidc_issuer,
                self.oidc_jwks_url,
                self.oidc_authorization_url,
                self.oidc_token_url,
                self.rag_base_url,
                self.weather_base_url,
                self.agent_base_url,
                self.voicechat_health_url,
            ):
                if url and urlparse(url).scheme != "https":
                    raise ValueError("Production HTTP integrations require TLS")
            if self.voicechat_ws_url and not self.voicechat_ws_url.startswith("wss://"):
                raise ValueError("Production VoiceChat requires WSS")
        if self.voicechat_integration_verified:
            if (
                not self.voicechat_api_version
                or self.voicechat_capability_mode == "unverified"
                or not re.fullmatch(r"sha256:[0-9a-f]{64}", self.voicechat_image_digest)
            ):
                raise ValueError(
                    "Verified VoiceChat requires an API version, image digest and tested capability mode"
                )
        if self.app_env == "production" and self.voice_provider == "nvidia" and not (
            self.voicechat_ws_url and self.voicechat_integration_verified
        ):
            raise ValueError("Production NVIDIA voice requires a verified pinned integration")
        return self

    @property
    def mock(self) -> bool:
        return "mock" in (self.agent_provider, self.rag_mode, self.weather_mode, self.voice_provider)
