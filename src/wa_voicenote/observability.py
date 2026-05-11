"""Observability: structlog JSON logs + OpenTelemetry traces + App Insights.

Wires up three layers, per PLAN.md §11.1:

  1. structlog with a JSON renderer that writes to stdout via the stdlib
     ``logging`` module. Container Apps captures stdout natively; queryable in
     App Insights via KQL.
  2. OpenTelemetry tracing for FastAPI, httpx (AOAI + Twilio clients) and the
     Azure SDK (Storage / Tables / Blob).
  3. Azure Monitor OpenTelemetry distro, which ships both traces and logs to
     Application Insights ``appi-wa-voicenote``.

When ``APPLICATIONINSIGHTS_CONNECTION_STRING`` is unset the distro is skipped
gracefully: structlog continues to print JSON to stdout, the FastAPI app is not
instrumented, and no network telemetry is shipped. Tests rely on this.

Configure once at app startup by calling ``configure_observability(settings,
app)``. The function is idempotent.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, cast

import structlog

if TYPE_CHECKING:
    from fastapi import FastAPI

    from wa_voicenote.config import Settings


class _State:
    """Module-level state holder.

    Using a class attribute (rather than a bare module-level ``_configured``
    flag) lets us mutate state without ``global`` — Ruff's ``PLW0603`` would
    otherwise flag every assignment.
    """

    configured: bool = False


def configure_observability(settings: Settings, app: FastAPI | None = None) -> None:
    """Configure structlog, OpenTelemetry and (optionally) App Insights.

    Idempotent: calling twice is a no-op after the first call. This lets the
    FastAPI app's lifespan hook and a worker entrypoint both call it without
    worrying about double-installation.

    Args:
        settings: validated ``Settings`` instance. The relevant fields are
            ``log_level``, ``otel_service_name``, ``env_name`` and
            ``applicationinsights_connection_string`` (the optional one).
        app: FastAPI application to instrument with OpenTelemetry. When
            ``None`` the FastAPI instrumentor is skipped (useful for tests
            and for worker-only entrypoints that do not run an HTTP server).
    """
    if _State.configured:
        return

    # 1. stdlib logging baseline at the configured level. structlog will
    #    render the full JSON line into ``%(message)s`` so the stdlib formatter
    #    must not add anything around it.
    log_level_int: int = getattr(logging, settings.log_level)
    logging.basicConfig(
        level=log_level_int,
        format="%(message)s",
        handlers=[logging.StreamHandler()],
        force=True,
    )

    # 1a. Silence verbose third-party loggers. The Azure Monitor / OpenTelemetry
    #     exporters default to DEBUG-level chatter that floods Container Apps
    #     stdout with QuickPulse request/response headers and instrumentation
    #     handshakes. Pinning them at WARNING leaves real warnings visible while
    #     keeping the console focused on app events. azure-core's HTTP logger is
    #     the worst offender; the rest are quieter but follow the same pattern.
    for noisy in (
        "azure",
        "azure.core.pipeline.policies.http_logging_policy",
        "azure.monitor.opentelemetry.exporter",
        "azure.monitor.opentelemetry.exporter.export",
        "opentelemetry",
        "opentelemetry.exporter.otlp",
        "opentelemetry.sdk",
        "urllib3",
    ):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    # 2. structlog: JSON renderer + processor chain. The wrapper is filtered at
    #    the configured level so DEBUG events are dropped cheaply in INFO mode.
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(log_level_int),
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    # 3. Azure Monitor (App Insights). Only configure when the connection
    #    string is set — local dev and unit tests must not ship telemetry.
    conn_str_secret = settings.applicationinsights_connection_string
    if conn_str_secret is not None:
        # Imports are deferred so that environments without the distro
        # installed (or test environments running without telemetry) avoid the
        # import cost. They are part of project dependencies, so this is just
        # a startup optimisation, not a soft-dependency pattern.
        from azure.monitor.opentelemetry import configure_azure_monitor
        from opentelemetry.sdk.resources import Resource

        resource = Resource.create(
            {
                "service.name": settings.otel_service_name,
                "service.instance.id": settings.env_name,
                "deployment.environment": settings.env_name,
            }
        )

        # SecretStr access is intentionally scoped to this branch so the raw
        # value never escapes outside the telemetry-enabled path.
        configure_azure_monitor(
            connection_string=conn_str_secret.get_secret_value(),
            logger_name=settings.otel_service_name,
            resource=resource,
        )

        # FastAPI auto-instrumentation. ``azure-monitor-opentelemetry`` already
        # bundles the FastAPI instrumentor, but the distro's auto-pickup runs
        # only when FastAPI is imported before ``configure_azure_monitor``.
        # Calling ``instrument_app`` explicitly is the documented manual
        # pattern and is safe to run after the distro has been configured.
        if app is not None:
            from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

            FastAPIInstrumentor.instrument_app(app)

        # httpx is NOT bundled by the distro (verified via PyPI/MS Learn docs,
        # 2026-05-10), so the AOAI and Twilio clients would otherwise be
        # invisible. Instrument it manually here.
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

        HTTPXClientInstrumentor().instrument()

    _State.configured = True


def reset_for_tests() -> None:
    """Reset module-level state so tests can re-configure cleanly.

    Tests only. Does not undo OpenTelemetry providers or stdlib handlers —
    that requires uninstrumenting each library, which is expensive and not
    needed since unit tests mock ``configure_azure_monitor``.
    """
    _State.configured = False


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Return a structlog bound logger.

    Convenience re-export so other modules import a single name from
    ``wa_voicenote.observability`` instead of touching ``structlog`` directly.
    """
    # structlog.get_logger is typed as returning Any; cast for strict mypy.
    return cast("structlog.stdlib.BoundLogger", structlog.get_logger(name))
