"""Tests for src/wa_voicenote/config.py — Settings model and loaders."""

from __future__ import annotations

import ast
import contextlib
from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from tests.conftest import REQUIRED_ENV

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _fresh_settings() -> object:
    """Import Settings fresh; clear cache for isolation."""
    from wa_voicenote.config import Settings, get_settings

    get_settings.cache_clear()
    return Settings()


# -----------------------------------------------------------------------------
# Required vars: presence + types
# -----------------------------------------------------------------------------


def test_all_required_vars_load(valid_settings_env: None) -> None:  # noqa: ARG001
    from wa_voicenote.config import Settings

    s = Settings()
    assert s.twilio_account_sid == REQUIRED_ENV["TWILIO_ACCOUNT_SID"]
    assert isinstance(s.twilio_auth_token, SecretStr)
    assert s.twilio_auth_token.get_secret_value() == REQUIRED_ENV["TWILIO_AUTH_TOKEN"]
    assert s.twilio_from == REQUIRED_ENV["TWILIO_FROM"]
    assert s.twilio_allowlist == [REQUIRED_ENV["TWILIO_ALLOWLIST"]]
    assert s.azure_openai_endpoint == REQUIRED_ENV["AZURE_OPENAI_ENDPOINT"]
    assert s.azure_openai_deployment == REQUIRED_ENV["AZURE_OPENAI_DEPLOYMENT"]
    assert s.azure_storage_account == REQUIRED_ENV["AZURE_STORAGE_ACCOUNT"]
    assert s.azure_storage_table == REQUIRED_ENV["AZURE_STORAGE_TABLE"]
    assert s.azure_storage_container == REQUIRED_ENV["AZURE_STORAGE_CONTAINER"]
    assert s.llm_system_prompt == REQUIRED_ENV["LLM_SYSTEM_PROMPT"]


@pytest.mark.parametrize("missing_key", list(REQUIRED_ENV.keys()))
def test_missing_required_raises(
    clean_env: None,  # noqa: ARG001
    monkeypatch: pytest.MonkeyPatch,
    missing_key: str,
) -> None:
    from wa_voicenote.config import Settings

    for key, value in REQUIRED_ENV.items():
        if key == missing_key:
            continue
        monkeypatch.setenv(key, value)
    with pytest.raises(ValidationError):
        Settings()


# -----------------------------------------------------------------------------
# Type coercion: list-from-CSV
# -----------------------------------------------------------------------------


def test_type_coercion_allowlist(
    valid_settings_env: None,  # noqa: ARG001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from wa_voicenote.config import Settings

    monkeypatch.setenv("TWILIO_ALLOWLIST", "whatsapp:+1111,whatsapp:+2222")
    s = Settings()
    assert s.twilio_allowlist == ["whatsapp:+1111", "whatsapp:+2222"]


def test_type_coercion_expected_languages(
    valid_settings_env: None,  # noqa: ARG001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from wa_voicenote.config import Settings

    monkeypatch.setenv("EXPECTED_LANGUAGES", "ES,EN,DE")
    s = Settings()
    assert s.expected_languages == ["ES", "EN", "DE"]


def test_twilio_allowlist_whitespace_trimmed(
    valid_settings_env: None,  # noqa: ARG001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from wa_voicenote.config import Settings

    monkeypatch.setenv("TWILIO_ALLOWLIST", " whatsapp:+1111 , whatsapp:+2222 ")
    s = Settings()
    assert s.twilio_allowlist == ["whatsapp:+1111", "whatsapp:+2222"]


def test_twilio_allowlist_drops_empties(
    valid_settings_env: None,  # noqa: ARG001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from wa_voicenote.config import Settings

    monkeypatch.setenv("TWILIO_ALLOWLIST", "whatsapp:+1111,,whatsapp:+2222,")
    s = Settings()
    assert s.twilio_allowlist == ["whatsapp:+1111", "whatsapp:+2222"]


# -----------------------------------------------------------------------------
# Defaults
# -----------------------------------------------------------------------------


def test_api_version_default(valid_settings_env: None) -> None:  # noqa: ARG001
    from wa_voicenote.config import Settings

    s = Settings()
    assert s.azure_openai_api_version == "2025-04-01-preview"


def test_expected_languages_default(valid_settings_env: None) -> None:  # noqa: ARG001
    from wa_voicenote.config import Settings

    s = Settings()
    assert s.expected_languages == ["ES", "EN", "DE"]


def test_context_timeout_default_120(valid_settings_env: None) -> None:  # noqa: ARG001
    from wa_voicenote.config import Settings

    s = Settings()
    assert s.context_timeout_seconds == 120


def test_context_timeout_override(
    valid_settings_env: None,  # noqa: ARG001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from wa_voicenote.config import Settings

    monkeypatch.setenv("CONTEXT_TIMEOUT_SECONDS", "300")
    s = Settings()
    assert s.context_timeout_seconds == 300
    assert isinstance(s.context_timeout_seconds, int)


def test_idempotency_ring_default(valid_settings_env: None) -> None:  # noqa: ARG001
    from wa_voicenote.config import Settings

    s = Settings()
    assert s.idempotency_ring_size == 100


def test_http_timeout_default(valid_settings_env: None) -> None:  # noqa: ARG001
    from wa_voicenote.config import Settings

    s = Settings()
    assert s.http_timeout_seconds == 45


def test_language_policy_default(valid_settings_env: None) -> None:  # noqa: ARG001
    from wa_voicenote.config import Settings

    s = Settings()
    assert s.language_policy == "match_inbound"


def test_otel_service_name_default(valid_settings_env: None) -> None:  # noqa: ARG001
    from wa_voicenote.config import Settings

    s = Settings()
    assert s.otel_service_name == "wa-voicenote-triage"


def test_env_name_default(valid_settings_env: None) -> None:  # noqa: ARG001
    from wa_voicenote.config import Settings

    s = Settings()
    assert s.env_name == "local"


def test_optional_telemetry_defaults_none(valid_settings_env: None) -> None:  # noqa: ARG001
    from wa_voicenote.config import Settings

    s = Settings()
    assert s.applicationinsights_connection_string is None
    assert s.diag_token is None
    assert s.azure_openai_api_key is None


def test_default_message_templates(valid_settings_env: None) -> None:  # noqa: ARG001
    from wa_voicenote.config import Settings

    s = Settings()
    assert s.msg_ack_received == (
        "Voice note received. Reply with extra context, or send 'no' to skip."
    )
    assert s.msg_replaced_audio == "Replaced previous voice note. Send context or 'no'."
    assert s.msg_idle_text_hint == "Send me a voice note to start."
    assert s.msg_transcode_error == "Could not process that voice note. Re-record and resend."
    assert s.msg_llm_error == "Processing failed. Re-record and resend."
    assert s.msg_timeout_dropped == ""
    assert s.label_transcript == "Transcript ({language}):\n"
    assert s.label_summary == "Summary:\n"
    assert s.label_suggested_reply == "Suggested reply:\n"


# -----------------------------------------------------------------------------
# Validators
# -----------------------------------------------------------------------------


def test_log_level_validator_accepts_lowercase(
    valid_settings_env: None,  # noqa: ARG001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from wa_voicenote.config import Settings

    monkeypatch.setenv("LOG_LEVEL", "debug")
    s = Settings()
    assert s.log_level == "DEBUG"


def test_log_level_validator_rejects_invalid(
    valid_settings_env: None,  # noqa: ARG001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from wa_voicenote.config import Settings

    monkeypatch.setenv("LOG_LEVEL", "WARN")
    with pytest.raises(ValidationError):
        Settings()


def test_twilio_account_sid_must_start_AC(  # noqa: N802 - mirrors plan test name
    valid_settings_env: None,  # noqa: ARG001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from wa_voicenote.config import Settings

    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "NOT_AN_SID")
    with pytest.raises(ValidationError):
        Settings()


def test_twilio_from_must_start_whatsapp(
    valid_settings_env: None,  # noqa: ARG001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from wa_voicenote.config import Settings

    monkeypatch.setenv("TWILIO_FROM", "+14155238886")
    with pytest.raises(ValidationError):
        Settings()


def test_allowlist_entries_must_start_whatsapp(
    valid_settings_env: None,  # noqa: ARG001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from wa_voicenote.config import Settings

    monkeypatch.setenv("TWILIO_ALLOWLIST", "whatsapp:+1111,+2222")
    with pytest.raises(ValidationError):
        Settings()


def test_endpoint_trailing_slash_normalized(
    valid_settings_env: None,  # noqa: ARG001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from wa_voicenote.config import Settings

    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://x.openai.azure.com")
    s = Settings()
    assert s.azure_openai_endpoint == "https://x.openai.azure.com/"


def test_positive_timeout_rejected_when_zero(
    valid_settings_env: None,  # noqa: ARG001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from wa_voicenote.config import Settings

    monkeypatch.setenv("CONTEXT_TIMEOUT_SECONDS", "0")
    with pytest.raises(ValidationError):
        Settings()


def test_positive_ring_size_rejected_when_negative(
    valid_settings_env: None,  # noqa: ARG001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from wa_voicenote.config import Settings

    monkeypatch.setenv("IDEMPOTENCY_RING_SIZE", "-1")
    with pytest.raises(ValidationError):
        Settings()


# -----------------------------------------------------------------------------
# Secrets handling
# -----------------------------------------------------------------------------


def test_secrets_are_secretstr(valid_settings_env: None) -> None:  # noqa: ARG001
    from wa_voicenote.config import Settings

    s = Settings()
    assert isinstance(s.twilio_auth_token, SecretStr)
    # repr must not leak the value
    rendered = repr(s.twilio_auth_token)
    assert REQUIRED_ENV["TWILIO_AUTH_TOKEN"] not in rendered


def test_secrets_env_file_not_in_repo() -> None:
    secrets_path = Path.home() / ".config" / "wa-voicenote" / "secrets.env"
    # Path identity check only — the file may or may not exist on disk.
    assert PROJECT_ROOT not in secrets_path.parents
    assert secrets_path != PROJECT_ROOT


# -----------------------------------------------------------------------------
# LLM system prompt
# -----------------------------------------------------------------------------


def test_llm_system_prompt_loaded_from_env(
    valid_settings_env: None,  # noqa: ARG001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from wa_voicenote.config import Settings

    custom_prompt = "Verbatim multi-line\nprompt content\nfor the LLM."
    monkeypatch.setenv("LLM_SYSTEM_PROMPT", custom_prompt)
    s = Settings()
    assert s.llm_system_prompt == custom_prompt


# -----------------------------------------------------------------------------
# Caching
# -----------------------------------------------------------------------------


def test_get_settings_is_cached(valid_settings_env: None) -> None:  # noqa: ARG001
    from wa_voicenote.config import get_settings

    get_settings.cache_clear()
    a = get_settings()
    b = get_settings()
    assert a is b


# -----------------------------------------------------------------------------
# Static check: no hardcoded user-facing strings in handlers.py
# -----------------------------------------------------------------------------


HANDLERS_PATH = PROJECT_ROOT / "src" / "wa_voicenote" / "handlers.py"


@pytest.mark.skipif(
    not HANDLERS_PATH.exists(),
    reason="handlers.py is created in c6/c12 — this test activates then.",
)
def test_no_hardcoded_messages_in_handlers() -> None:
    """Scan handlers.py AST for string constants in module body. None expected.

    User-facing strings must come from Settings, not literals.
    """
    source = HANDLERS_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source)
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            value = node.value
            # Allow docstrings and short identifiers; flag any string that looks
            # like a sentence with a space and length > 12 (heuristic).
            if " " in value and len(value) > 12 and not value.startswith(("http", "/")):
                offenders.append(value)
    # Allow module-level docstring(s) by removing the first if it sits as Expr.
    if tree.body and isinstance(tree.body[0], ast.Expr):
        first = tree.body[0].value
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            with contextlib.suppress(ValueError):
                offenders.remove(first.value)
    assert offenders == [], f"User-facing literals in handlers.py: {offenders}"
