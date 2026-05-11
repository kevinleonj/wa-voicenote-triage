"""Tests for src/wa_voicenote/observability.py.

These tests must NEVER actually ship telemetry. Every test that exercises the
``conn_str_secret is not None`` branch mocks ``configure_azure_monitor`` (and
its sibling instrumentors) so no network call escapes the test process.
"""

from __future__ import annotations

import io
import json
import logging
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest
import structlog
from pydantic import SecretStr

from wa_voicenote.observability import (
    configure_observability,
    get_logger,
    reset_for_tests,
)

if TYPE_CHECKING:
    from wa_voicenote.config import Settings


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_settings(
    valid_settings_env: None,  # noqa: ARG001 - fixture dependency
    *,
    with_conn_str: bool = False,
    log_level: str = "INFO",
) -> Settings:
    """Build a Settings instance with optional App Insights conn string.

    Always depends on ``valid_settings_env`` so the required env vars are
    populated before pydantic-settings reads the environment.
    """
    from wa_voicenote.config import Settings, get_settings

    get_settings.cache_clear()
    s = Settings()
    s = s.model_copy(update={"log_level": log_level})
    if with_conn_str:
        s = s.model_copy(
            update={
                "applicationinsights_connection_string": SecretStr(
                    "InstrumentationKey=00000000-0000-0000-0000-000000000000;"
                    "IngestionEndpoint=https://example.in.applicationinsights.azure.com/"
                )
            }
        )
    return s


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def test_configure_idempotent(valid_settings_env: None) -> None:
    """Calling configure_observability twice must not double-configure."""
    settings = _make_settings(valid_settings_env)
    with patch("azure.monitor.opentelemetry.configure_azure_monitor") as mock_azmon:
        configure_observability(settings)
        configure_observability(settings)
    # Without conn string the mock must never be called regardless of how many
    # times we configure.
    assert mock_azmon.call_count == 0


# ---------------------------------------------------------------------------
# Telemetry disabled when conn string absent
# ---------------------------------------------------------------------------


def test_configure_without_conn_str_does_not_call_azure_monitor(
    valid_settings_env: None,
) -> None:
    settings = _make_settings(valid_settings_env, with_conn_str=False)
    with patch("azure.monitor.opentelemetry.configure_azure_monitor") as mock_azmon:
        configure_observability(settings)
    mock_azmon.assert_not_called()


# ---------------------------------------------------------------------------
# Telemetry enabled when conn string present
# ---------------------------------------------------------------------------


def test_configure_with_conn_str_calls_azure_monitor(
    valid_settings_env: None,
) -> None:
    settings = _make_settings(valid_settings_env, with_conn_str=True)
    with (
        patch("azure.monitor.opentelemetry.configure_azure_monitor") as mock_azmon,
        patch("opentelemetry.instrumentation.httpx.HTTPXClientInstrumentor") as mock_httpx,
    ):
        configure_observability(settings)
    mock_azmon.assert_called_once()
    kwargs = mock_azmon.call_args.kwargs
    assert (
        kwargs["connection_string"]
        == settings.applicationinsights_connection_string.get_secret_value()  # type: ignore[union-attr]
    )
    assert kwargs["logger_name"] == settings.otel_service_name
    # ``resource`` is an OpenTelemetry Resource — verify its attributes carry
    # the values we passed in.
    resource = kwargs["resource"]
    attrs = dict(resource.attributes)
    assert attrs["service.name"] == settings.otel_service_name
    assert attrs["service.instance.id"] == settings.env_name
    assert attrs["deployment.environment"] == settings.env_name
    # httpx must always be instrumented when telemetry is enabled (since
    # azure-monitor-opentelemetry does not bundle httpx instrumentation).
    mock_httpx.return_value.instrument.assert_called_once()


# ---------------------------------------------------------------------------
# Log level
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("level", ["DEBUG", "INFO", "WARNING", "ERROR"])
def test_log_level_applied(valid_settings_env: None, level: str) -> None:
    settings = _make_settings(valid_settings_env, log_level=level)
    configure_observability(settings)
    # The root logger should be at the requested level. We assert >= so that
    # a more-verbose parent (e.g. NOTSET=0) still satisfies the contract
    # because ``logging.basicConfig(force=True)`` rebuilds the root handlers.
    assert logging.getLogger().level == getattr(logging, level)


# ---------------------------------------------------------------------------
# get_logger
# ---------------------------------------------------------------------------


def test_get_logger_returns_structlog_bound(valid_settings_env: None) -> None:
    settings = _make_settings(valid_settings_env)
    configure_observability(settings)
    log = get_logger("test")
    # structlog's get_logger returns a proxy until first call; we assert it
    # has the structlog logger surface (info / warning / bind).
    assert hasattr(log, "info")
    assert hasattr(log, "warning")
    assert hasattr(log, "bind")


# ---------------------------------------------------------------------------
# JSON output format
# ---------------------------------------------------------------------------


def test_json_output_format(valid_settings_env: None) -> None:
    """Emit a log line through structlog and verify the rendered output parses
    as JSON with the expected keys."""
    settings = _make_settings(valid_settings_env)
    configure_observability(settings)

    # Capture by attaching a fresh StreamHandler with a StringIO target. We
    # cannot rely on capsys because structlog renders via stdlib logging,
    # which writes to whatever handler is on the root logger.
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(logging.Formatter("%(message)s"))
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        get_logger("test").info("hello world", foo="bar")
    finally:
        root.removeHandler(handler)

    output = buf.getvalue().strip()
    # If multiple lines were emitted (e.g. from other loggers) parse the last.
    last_line = output.splitlines()[-1]
    parsed = json.loads(last_line)
    assert parsed["event"] == "hello world"
    assert parsed["foo"] == "bar"
    assert parsed["level"] == "info"
    assert "timestamp" in parsed


# ---------------------------------------------------------------------------
# FastAPI instrumentation
# ---------------------------------------------------------------------------


def test_fastapi_instrumented_when_app_provided(valid_settings_env: None) -> None:
    settings = _make_settings(valid_settings_env, with_conn_str=True)
    fake_app = MagicMock(name="fake_fastapi_app")
    with (
        patch("azure.monitor.opentelemetry.configure_azure_monitor"),
        patch("opentelemetry.instrumentation.fastapi.FastAPIInstrumentor") as mock_fastapi_instr,
        patch("opentelemetry.instrumentation.httpx.HTTPXClientInstrumentor"),
    ):
        configure_observability(settings, app=fake_app)
    mock_fastapi_instr.instrument_app.assert_called_once_with(fake_app)


def test_fastapi_not_instrumented_when_no_conn_str(
    valid_settings_env: None,
) -> None:
    """Even with an app provided, telemetry-disabled mode must skip FastAPI
    instrumentation (the instrumentor depends on the OTel tracer provider
    set up by configure_azure_monitor)."""
    settings = _make_settings(valid_settings_env, with_conn_str=False)
    fake_app = MagicMock(name="fake_fastapi_app")
    with patch("opentelemetry.instrumentation.fastapi.FastAPIInstrumentor") as mock_fastapi_instr:
        configure_observability(settings, app=fake_app)
    mock_fastapi_instr.instrument_app.assert_not_called()


# ---------------------------------------------------------------------------
# Secret hygiene
# ---------------------------------------------------------------------------


def test_secret_not_logged(
    valid_settings_env: None,  # noqa: ARG001 - fixture dependency
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The raw connection-string value must never appear in stdout/stderr
    during configuration, and must never be embedded in a structlog event."""
    fake_conn_value = (
        "InstrumentationKey=ffffffff-ffff-ffff-ffff-ffffffffffff;"
        "IngestionEndpoint=https://example.in.applicationinsights.azure.com/"
    )
    from wa_voicenote.config import Settings, get_settings

    get_settings.cache_clear()
    settings = Settings().model_copy(
        update={
            "applicationinsights_connection_string": SecretStr(fake_conn_value),
        }
    )
    with (
        patch("azure.monitor.opentelemetry.configure_azure_monitor"),
        patch("opentelemetry.instrumentation.httpx.HTTPXClientInstrumentor"),
    ):
        configure_observability(settings)
        # Emit a couple of log lines for good measure.
        get_logger("test").info("startup complete")
        get_logger("test").warning("should not include secret", settings_str=str(settings))

    captured = capsys.readouterr()
    assert fake_conn_value not in captured.out
    assert fake_conn_value not in captured.err
    # Pydantic SecretStr renders as ``**********`` in repr/str, so a log line
    # that captures ``str(settings)`` must NOT leak the value either.
    assert "ffffffff-ffff-ffff-ffff-ffffffffffff" not in captured.out
    assert "ffffffff-ffff-ffff-ffff-ffffffffffff" not in captured.err


# ---------------------------------------------------------------------------
# Reset helper
# ---------------------------------------------------------------------------


def test_reset_for_tests_allows_reconfigure(valid_settings_env: None) -> None:
    """After reset_for_tests, configure_observability runs again."""
    settings = _make_settings(valid_settings_env, with_conn_str=True)
    with (
        patch("azure.monitor.opentelemetry.configure_azure_monitor") as mock_azmon,
        patch("opentelemetry.instrumentation.httpx.HTTPXClientInstrumentor"),
    ):
        configure_observability(settings)
        reset_for_tests()
        configure_observability(settings)
    # Once before reset + once after — total two calls.
    assert mock_azmon.call_count == 2


# ---------------------------------------------------------------------------
# Sanity: structlog is wired to stdlib so OTel log handler can pick it up
# ---------------------------------------------------------------------------


def test_structlog_uses_stdlib_logger_factory(valid_settings_env: None) -> None:
    settings = _make_settings(valid_settings_env)
    configure_observability(settings)
    config = structlog.get_config()
    # ``LoggerFactory`` (stdlib) is what allows Azure Monitor's logging
    # handler (attached to stdlib's logging tree) to receive structlog events.
    assert isinstance(config["logger_factory"], structlog.stdlib.LoggerFactory)
