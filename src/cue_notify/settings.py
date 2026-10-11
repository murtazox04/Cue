"""Runtime configuration.

Settings are resolved from, in order of precedence:

1. environment variables prefixed with ``CUE_`` (nested keys use ``__``,
   e.g. ``CUE_DATABASE__URL`` or ``CUE_CHANNELS__SMS__AUTH_TOKEN``);
2. a ``.env`` file in the working directory;
3. a TOML file — ``cue.toml`` by default, or the path in ``CUE_CONFIG_FILE``.

Structured configuration (channels, quiet hours) is most readable in TOML; secrets are
best supplied through the environment.
"""

from __future__ import annotations

import os
from datetime import time
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal, Self
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    AnyHttpUrl,
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)

from cue_notify.core.policy import QuietHours

CONFIG_FILE_ENV = "CUE_CONFIG_FILE"
DEFAULT_CONFIG_FILE = "cue.toml"


class DatabaseSettings(BaseModel):
    url: str = Field(
        default="sqlite+aiosqlite:///./cue.db",
        description="SQLAlchemy async URL. Use postgresql+asyncpg://… in production.",
    )
    pool_size: int | None = Field(
        default=None,
        ge=1,
        description=(
            "Connections kept open per process. Default: the worker's concurrency plus 4, so "
            "busy workers never churn connections."
        ),
    )
    max_overflow: int = Field(default=10, ge=0)
    echo: bool = False

    @field_validator("url")
    @classmethod
    def _async_driver(cls, value: str) -> str:
        """Accept the plain ``postgres://`` URLs hosting platforms hand out."""
        for prefix in ("postgres://", "postgresql://"):
            if value.startswith(prefix):
                return "postgresql+asyncpg://" + value.removeprefix(prefix)
        return value

    @property
    def is_sqlite(self) -> bool:
        return self.url.startswith("sqlite")


class ApiSettings(BaseModel):
    cors_origins: list[str] = Field(default_factory=list)
    docs: bool = Field(default=True, description="Serve interactive docs at /docs.")
    root_path: str = Field(default="", description="ASGI root path when behind a proxy.")
    public_url: str | None = Field(
        default=None,
        description=(
            "Public base URL of this instance (e.g. https://notify.example.com). Enables "
            "preference-centre and one-click unsubscribe links in messages."
        ),
        examples=["https://notify.example.com"],
    )
    secret_key: SecretStr | None = Field(
        default=None,
        description=(
            "Signs preference tokens, so the database alone never holds a working link. "
            "At least 32 characters; required with public_url. Changing it invalidates every "
            "preference and unsubscribe link issued with the old value."
        ),
    )
    product_name: str | None = Field(
        default=None, max_length=100, description="Shown on the hosted preference page."
    )
    max_body_bytes: int = Field(
        default=1_048_576, ge=65_536, description="Larger request bodies are refused (413)."
    )
    max_broadcast_body_bytes: int = Field(
        default=64 * 1_048_576,
        ge=65_536,
        description="Limit for POST /v1/broadcasts, whose audience can list a million ids.",
    )

    @field_validator("public_url")
    @classmethod
    def _valid_public_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        url = AnyHttpUrl(value)
        if url.query or url.fragment:
            raise ValueError("public_url must not have a query or fragment")
        return str(url).rstrip("/")

    @model_validator(mode="after")
    def require_secret_with_public_url(self) -> Self:
        if self.secret_key is not None and len(self.secret_key.get_secret_value()) < 32:
            raise ValueError("api.secret_key must be at least 32 characters")
        if self.public_url is not None and self.secret_key is None:
            raise ValueError(
                "api.secret_key is required with api.public_url; generate one with "
                "`python -c 'import secrets; print(secrets.token_urlsafe(32))'`"
            )
        return self


class WorkerSettings(BaseModel):
    embedded: bool = Field(
        default=False,
        description="Run the job worker inside the API process (handy for small installs).",
    )
    concurrency: int = Field(default=16, ge=1, le=1024)
    poll_interval: float = Field(default=1.0, gt=0, description="Idle polling interval (s).")
    lease_seconds: int = Field(
        default=300, ge=10, description="How long a claimed job is invisible to other workers."
    )
    max_attempts: int = Field(default=10, ge=1)
    retry_base_seconds: float = Field(default=5.0, gt=0)
    retry_max_seconds: float = Field(default=3600.0, gt=0)
    shutdown_timeout: float = Field(default=30.0, ge=0)


class PolicySettings(BaseModel):
    default_locale: str = "en"
    default_timezone: str = "UTC"
    quiet_hours: QuietHours | None = Field(
        default_factory=lambda: QuietHours(start=time(22, 0), end=time(8, 0)),
        description="Default quiet hours; recipients may override. null disables.",
    )

    @field_validator("default_timezone")
    @classmethod
    def _valid_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown time zone {value!r}") from exc
        return value


class AISettings(BaseModel):
    """AI copy personalisation. Credentials come from each provider's standard env vars
    (``OPENAI_API_KEY``, ``ANTHROPIC_API_KEY``, ``GOOGLE_API_KEY``…)."""

    enabled: bool = False
    model: str | None = Field(
        default=None,
        description="Pydantic AI model id: '<provider>:<model>'.",
        examples=["openai:gpt-5-mini", "anthropic:claude-haiku-4-5", "ollama:llama3.2"],
    )
    fallback_models: list[str] = Field(
        default_factory=list, description="Tried in order if the primary model fails."
    )
    instructions: str | None = Field(
        default=None, description="Brand voice guidance prepended to every request."
    )
    temperature: float = Field(default=0.7, ge=0, le=2)
    max_output_tokens: int = Field(default=512, ge=16)
    timeout: float = Field(default=20.0, gt=0)
    daily_token_budget: int | None = Field(
        default=None, ge=1, description="Stop personalising after this many tokens per UTC day."
    )

    @model_validator(mode="after")
    def _model_required_when_enabled(self) -> Self:
        if self.enabled and not self.model:
            raise ValueError("ai.model is required when ai.enabled is true")
        return self


class ScreeningSettings(BaseModel):
    """Screen messages from agents with a decision model before they are delivered."""

    enabled: bool = False
    provider: Literal["typesafe"] = "typesafe"
    api_key: SecretStr | None = Field(
        default=None, description="Defaults to the TYPESAFE_API_KEY environment variable."
    )
    base_url: str = "https://api.typesafe.ai"
    model: str = "jev-latest"
    timeout: float = Field(default=5.0, gt=0)
    hold_above: float = Field(
        default=0.5,
        gt=0,
        le=1,
        description="Hold a message for approval when any risk is at least this likely.",
    )
    min_confidence: float = Field(
        default=0.6,
        ge=0,
        le=1,
        description="Lower a message's importance only when the urgency judgement is this sure.",
    )
    on_error: Literal["hold", "send"] = Field(
        default="hold",
        description="What to do with a message when the screening backend fails.",
    )

    @model_validator(mode="after")
    def _key_from_environment(self) -> Self:
        if self.enabled and self.api_key is None and os.environ.get("TYPESAFE_API_KEY"):
            self.api_key = SecretStr(os.environ["TYPESAFE_API_KEY"])
        if self.enabled and self.api_key is None:
            raise ValueError("screening.api_key (or TYPESAFE_API_KEY) is required when enabled")
        return self


class ComplianceSettings(BaseModel):
    """Legal safety nets for marketing text messages. See the compliance guide."""

    us_texting_rules: bool = Field(
        default=False,
        description=(
            "Hold marketing texts to US recipients to legal hours (federal and state) and "
            "the 3-per-day state caps."
        ),
    )
    sms_channels: list[str] = Field(
        default_factory=lambda: ["sms"],
        description="Channels that send text messages.",
    )
    unknown_state: Literal["strict", "federal"] = Field(
        default="strict",
        description="Rules for US recipients without a region: the strictest state, or federal.",
    )


class ApprovalSettings(BaseModel):
    """Where Cue reports decisions on approval requests (``POST /v1/approvals``)."""

    callback_url: AnyHttpUrl | None = Field(
        default=None,
        description="Receives a signed POST when a request is approved, rejected or expires.",
    )
    callback_secret: SecretStr | None = Field(
        default=None, description="Signs callbacks (Cue-Signature header). Required with a URL."
    )
    timeout: float = Field(default=10.0, gt=0)
    max_pending_per_agent: int = Field(
        default=10,
        ge=1,
        description="Open requests one agent may have at a time, so it cannot flood reviewers.",
    )

    @model_validator(mode="after")
    def _secret_with_url(self) -> Self:
        if self.callback_url is not None and self.callback_secret is None:
            raise ValueError("approvals.callback_secret is required with approvals.callback_url")
        return self


class ChannelSpec(BaseModel):
    """Configuration of one delivery channel.

    ``provider`` selects the implementation (``fcm``, ``twilio``, ``smtp``, ``telegram``,
    ``webhook``, ``console`` or any installed plugin); every other key is validated by
    that provider's own config model.
    """

    model_config = ConfigDict(extra="allow")

    provider: str
    enabled: bool = True

    @property
    def options(self) -> dict[str, Any]:
        return dict(self.model_extra or {})

    def __repr_args__(self) -> list[tuple[str | None, Any]]:  # pyright: ignore[reportIncompatibleMethodOverride]
        # Options hold credentials (auth tokens, passwords); show only their names.
        return [
            ("provider", self.provider),
            ("enabled", self.enabled),
            ("options", sorted(self.options)),
        ]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CUE_",
        env_nested_delimiter="__",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        validate_default=True,
    )

    environment: Literal["development", "test", "production"] = "development"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_format: Literal["console", "json"] = "console"

    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    api: ApiSettings = Field(default_factory=ApiSettings)
    worker: WorkerSettings = Field(default_factory=WorkerSettings)
    policy: PolicySettings = Field(default_factory=PolicySettings)
    ai: AISettings = Field(default_factory=AISettings)
    screening: ScreeningSettings = Field(default_factory=ScreeningSettings)
    approvals: ApprovalSettings = Field(default_factory=ApprovalSettings)
    compliance: ComplianceSettings = Field(default_factory=ComplianceSettings)
    channels: dict[str, ChannelSpec] = Field(
        default_factory=dict,
        description="Delivery channels keyed by the name used in rules and addresses.",
    )
    metrics: bool = Field(default=False, description="Expose Prometheus metrics at /metrics.")

    @model_validator(mode="before")
    @classmethod
    def _platform_database_url(cls, data: Any) -> Any:
        """Use ``DATABASE_URL`` (set by Heroku, Railway, Render…) when no URL is configured."""
        url = os.environ.get("DATABASE_URL")
        if not url or not isinstance(data, dict):
            return data
        database: Any = data.get("database")  # pyright: ignore[reportUnknownMemberType]
        if isinstance(database, DatabaseSettings) or (
            isinstance(database, dict) and "url" in database
        ):
            return data
        return {**data, "database": {**(database or {}), "url": url}}  # pyright: ignore[reportUnknownArgumentType]

    @field_validator("channels")
    @classmethod
    def _valid_channel_names(cls, value: dict[str, ChannelSpec]) -> dict[str, ChannelSpec]:
        for name in value:
            if not name.replace("_", "").replace("-", "").isalnum() or not name.islower():
                raise ValueError(
                    f"channel name {name!r} must be lowercase letters, digits, '-' or '_'"
                )
        return value

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        explicit = os.environ.get(CONFIG_FILE_ENV)
        config_file = Path(explicit or DEFAULT_CONFIG_FILE)
        if explicit and not config_file.is_file():
            raise FileNotFoundError(f"{CONFIG_FILE_ENV} points to a missing file: {config_file}")
        sources: list[PydanticBaseSettingsSource] = [init_settings, env_settings, dotenv_settings]
        if config_file.is_file():
            sources.append(TomlConfigSettingsSource(settings_cls, toml_file=config_file))
        sources.append(file_secret_settings)
        return tuple(sources)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
