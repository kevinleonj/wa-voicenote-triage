"""Application settings loaded from environment variables.

Per PLAN §10.2: every configurable value (messages, prompts, timeouts, names)
lives here so business modules never inline literals or magic numbers.

The env-file path defaults to ``.env`` but can be overridden at process start
by setting ``ENV_FILE`` (e.g. docker-compose mounts secrets at
``/run/secrets/wa-voicenote.env`` and points ``ENV_FILE`` there). Per
pydantic-settings v2 semantics, env vars always take priority over the
``.env`` file.
"""

from __future__ import annotations

import functools
import os
from typing import Annotated, Any

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# Named constants for non-secret defaults. Kept here (not inline) so PLR2004
# never fires inside the Settings class body.
_DEFAULT_API_VERSION = "2025-04-01-preview"
_DEFAULT_LANGUAGES = ("ES", "EN", "DE")
_DEFAULT_CONTEXT_TIMEOUT_S = 120
_DEFAULT_LANGUAGE_POLICY = "match_inbound"
_DEFAULT_IDEMPOTENCY_RING_SIZE = 100
_DEFAULT_HTTP_TIMEOUT_S = 45
_DEFAULT_LOG_LEVEL = "INFO"
_DEFAULT_OTEL_SERVICE_NAME = "wa-voicenote-triage"
_DEFAULT_ENV_NAME = "local"

_VALID_LOG_LEVELS: frozenset[str] = frozenset({"DEBUG", "INFO", "WARNING", "ERROR"})

_DEFAULT_MSG_ACK_RECEIVED = "Voice note received. Reply with extra context, or send 'no' to skip."
_DEFAULT_MSG_REPLACED_AUDIO = "Replaced previous voice note. Send context or 'no'."
_DEFAULT_MSG_IDLE_TEXT_HINT = "Send me a voice note to start."
_DEFAULT_MSG_TRANSCODE_ERROR = "Could not process that voice note. Re-record and resend."
_DEFAULT_MSG_LLM_ERROR = "Processing failed. Re-record and resend."
_DEFAULT_MSG_TIMEOUT_DROPPED = ""
_DEFAULT_LABEL_TRANSCRIPT = "Transcript ({language}):\n"
_DEFAULT_LABEL_SUMMARY = "Summary:\n"
_DEFAULT_LABEL_SUGGESTED_REPLY = "Suggested reply:\n"


def _split_csv(raw: Any) -> Any:
    """Split a comma-separated string into a stripped, non-empty list.

    Pass-through for already-list values (so programmatic construction works).
    """
    if isinstance(raw, str):
        return [item.strip() for item in raw.split(",") if item.strip()]
    return raw


class Settings(BaseSettings):
    """Runtime configuration. Built once per process via ``get_settings``."""

    model_config = SettingsConfigDict(
        env_file=os.environ.get("ENV_FILE", ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Required: Twilio ----------------------------------------------------
    twilio_account_sid: str = Field(description="Twilio Account SID, must start with 'AC'.")
    twilio_auth_token: SecretStr = Field(description="Twilio auth token (SecretStr).")
    twilio_from: str = Field(description="Twilio sender, must start with 'whatsapp:+' (E.164).")
    twilio_allowlist: Annotated[list[str], NoDecode] = Field(
        description="Comma-separated allowlist of inbound numbers (each 'whatsapp:+...')."
    )

    # --- Required: Azure OpenAI / Storage -----------------------------------
    azure_openai_endpoint: str = Field(description="AOAI endpoint URL; trailing slash enforced.")
    azure_openai_deployment: str = Field(description="AOAI deployment name (e.g. 'gpt-audio-15').")
    azure_storage_account: str = Field(description="Azure Storage account name.")
    azure_storage_table: str = Field(description="Conversation-state table name.")
    azure_storage_container: str = Field(description="Audio-staging blob container name.")

    # --- Required: prompt ----------------------------------------------------
    llm_system_prompt: str = Field(description="Verbatim system prompt for gpt-audio-1.5.")

    # --- Operational knobs (defaulted) --------------------------------------
    azure_openai_api_version: str = Field(default=_DEFAULT_API_VERSION)
    expected_languages: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: list(_DEFAULT_LANGUAGES)
    )
    context_timeout_seconds: int = Field(default=_DEFAULT_CONTEXT_TIMEOUT_S)
    language_policy: str = Field(default=_DEFAULT_LANGUAGE_POLICY)
    idempotency_ring_size: int = Field(default=_DEFAULT_IDEMPOTENCY_RING_SIZE)
    http_timeout_seconds: int = Field(default=_DEFAULT_HTTP_TIMEOUT_S)
    log_level: str = Field(default=_DEFAULT_LOG_LEVEL)
    otel_service_name: str = Field(default=_DEFAULT_OTEL_SERVICE_NAME)
    env_name: str = Field(default=_DEFAULT_ENV_NAME)

    # --- Optional (None = feature disabled) ---------------------------------
    applicationinsights_connection_string: SecretStr | None = Field(default=None)
    diag_token: SecretStr | None = Field(default=None)
    azure_openai_api_key: SecretStr | None = Field(default=None)

    # --- User-facing message templates --------------------------------------
    msg_ack_received: str = Field(default=_DEFAULT_MSG_ACK_RECEIVED)
    msg_replaced_audio: str = Field(default=_DEFAULT_MSG_REPLACED_AUDIO)
    msg_idle_text_hint: str = Field(default=_DEFAULT_MSG_IDLE_TEXT_HINT)
    msg_transcode_error: str = Field(default=_DEFAULT_MSG_TRANSCODE_ERROR)
    msg_llm_error: str = Field(default=_DEFAULT_MSG_LLM_ERROR)
    msg_timeout_dropped: str = Field(default=_DEFAULT_MSG_TIMEOUT_DROPPED)
    label_transcript: str = Field(default=_DEFAULT_LABEL_TRANSCRIPT)
    label_summary: str = Field(default=_DEFAULT_LABEL_SUMMARY)
    label_suggested_reply: str = Field(default=_DEFAULT_LABEL_SUGGESTED_REPLY)

    # ----- Validators -------------------------------------------------------

    @field_validator("twilio_account_sid")
    @classmethod
    def _validate_sid(cls, value: str) -> str:
        if not value.startswith("AC"):
            raise ValueError("TWILIO_ACCOUNT_SID must start with 'AC'")
        return value

    @field_validator("twilio_from")
    @classmethod
    def _validate_from(cls, value: str) -> str:
        if not value.startswith("whatsapp:+"):
            raise ValueError("TWILIO_FROM must start with 'whatsapp:+'")
        return value

    @field_validator("twilio_allowlist", mode="before")
    @classmethod
    def _split_allowlist(cls, value: Any) -> Any:
        return _split_csv(value)

    @field_validator("twilio_allowlist")
    @classmethod
    def _validate_allowlist_entries(cls, value: list[str]) -> list[str]:
        for entry in value:
            if not entry.startswith("whatsapp:+"):
                raise ValueError(f"TWILIO_ALLOWLIST entry must start with 'whatsapp:+': {entry!r}")
        return value

    @field_validator("expected_languages", mode="before")
    @classmethod
    def _split_languages(cls, value: Any) -> Any:
        return _split_csv(value)

    @field_validator("azure_openai_endpoint")
    @classmethod
    def _ensure_trailing_slash(cls, value: str) -> str:
        return value if value.endswith("/") else value + "/"

    @field_validator("log_level", mode="before")
    @classmethod
    def _normalize_log_level(cls, value: Any) -> Any:
        if isinstance(value, str):
            return value.upper()
        return value

    @field_validator("log_level")
    @classmethod
    def _validate_log_level(cls, value: str) -> str:
        if value not in _VALID_LOG_LEVELS:
            raise ValueError(f"LOG_LEVEL must be one of {sorted(_VALID_LOG_LEVELS)}; got {value!r}")
        return value

    @field_validator(
        "context_timeout_seconds",
        "idempotency_ring_size",
        "http_timeout_seconds",
    )
    @classmethod
    def _must_be_positive(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("value must be a positive integer")
        return value


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide Settings singleton.

    Wrapped in ``lru_cache`` so the env is read and validated exactly once.
    Tests call ``get_settings.cache_clear()`` between cases.
    """
    # Required fields are populated from the environment by pydantic-settings;
    # mypy cannot see this, so silence the call-arg check at this call site.
    return Settings()  # type: ignore[call-arg]
