"""Project-wide pytest fixtures."""

from __future__ import annotations

import os

import pytest

REQUIRED_ENV: dict[str, str] = {
    "TWILIO_ACCOUNT_SID": "AC" + "0" * 32,
    "TWILIO_AUTH_TOKEN": "test_token",
    "TWILIO_FROM": "whatsapp:+14155238886",
    "TWILIO_ALLOWLIST": "whatsapp:+34611779374",
    "AZURE_OPENAI_ENDPOINT": "https://test.openai.azure.com/",
    "AZURE_OPENAI_DEPLOYMENT": "gpt-audio-15",
    "AZURE_STORAGE_ACCOUNT": "stwavoicenote",
    "AZURE_STORAGE_TABLE": "convstate",
    "AZURE_STORAGE_CONTAINER": "audio-staging",
    "LLM_SYSTEM_PROMPT": "test prompt",
}

_WIPE_PREFIXES: tuple[str, ...] = (
    "TWILIO_",
    "AZURE_",
    "MSG_",
    "LABEL_",
    "CONTEXT_",
    "LANGUAGE_",
    "EXPECTED_",
    "IDEMPOTENCY_",
    "HTTP_",
    "LOG_",
    "OTEL_",
    "ENV_NAME",
    "LLM_",
    "APPLICATIONINSIGHTS_",
    "DIAG_",
    "ENV_FILE",
)


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Wipe all WA-related env vars; tests opt back in via valid_settings_env."""
    for key in list(os.environ.keys()):
        for prefix in _WIPE_PREFIXES:
            if key.startswith(prefix):
                monkeypatch.delenv(key, raising=False)
                break
    # Point ENV_FILE at a path that does not exist so the .env loader is a no-op
    # regardless of what the developer has on disk in CI or locally.
    monkeypatch.setenv("ENV_FILE", "/nonexistent/.env.does.not.exist")
    _clear_settings_cache()


@pytest.fixture
def valid_settings_env(
    clean_env: None,  # noqa: ARG001 - fixture dependency
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Set all required env vars to dummy valid values."""
    for key, value in REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)
    _clear_settings_cache()


def _clear_settings_cache() -> None:
    try:
        from wa_voicenote.config import get_settings
    except ImportError:
        return
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _reset_observability() -> object:
    """Reset observability module-level state between tests.

    The ``configure_observability`` function is idempotent, so without this
    reset the second test in a process would silently skip configuration.
    Imported lazily so test modules that don't touch observability still
    work in isolation.
    """
    yield
    try:
        from wa_voicenote.observability import reset_for_tests
    except ImportError:
        return
    reset_for_tests()
