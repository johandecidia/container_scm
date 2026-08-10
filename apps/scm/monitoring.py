"""SCM monitoring helpers — structured logging and Sentry context.

Import the module-level logger in each SCM sub-app:

    from apps.scm.monitoring import get_scm_logger, set_sentry_scm_context
    logger = get_scm_logger(__name__)

All helpers are safe to call regardless of whether Sentry is configured.
"""

from __future__ import annotations

import logging
from typing import Any


def get_scm_logger(name: str) -> logging.Logger:
    """Return a logger under the apps.scm namespace."""
    return logging.getLogger(name)


# ---------------------------------------------------------------------------
# Sentry helpers — all no-ops when Sentry is not installed / not configured.
# ---------------------------------------------------------------------------


def set_sentry_scm_context(
    team_id: int | None = None,
    team_slug: str | None = None,
    integration_id: int | None = None,
    carrier: str | None = None,
    shipment_id: int | None = None,
    container_id: int | None = None,
) -> None:
    """Set Sentry tags / context for the current SCM operation scope.

    Safe to call unconditionally — silently skips if Sentry is not configured.
    """
    try:
        import sentry_sdk

        scope = sentry_sdk.get_current_scope()
        if team_id is not None:
            scope.set_tag("scm.team_id", str(team_id))
        if team_slug is not None:
            scope.set_tag("team", team_slug)
        if integration_id is not None:
            scope.set_tag("scm.integration_id", str(integration_id))
        if carrier is not None:
            scope.set_tag("scm.carrier", carrier)
        if shipment_id is not None:
            scope.set_tag("scm.shipment_id", str(shipment_id))
        if container_id is not None:
            scope.set_tag("scm.container_id", str(container_id))
    except Exception:  # noqa: BLE001
        pass


def add_sentry_breadcrumb(message: str, category: str = "scm", data: dict | None = None) -> None:
    """Add a Sentry breadcrumb for the current SCM operation.

    Safe to call unconditionally — silently skips if Sentry is not configured.
    """
    try:
        import sentry_sdk

        sentry_sdk.add_breadcrumb(message=message, category=category, data=data or {})
    except Exception:  # noqa: BLE001
        pass


def capture_scm_exception(exc: Exception, context: dict[str, Any] | None = None) -> None:
    """Capture an exception to Sentry with optional SCM context.

    Safe to call unconditionally — silently skips if Sentry is not configured.
    """
    try:
        import sentry_sdk

        with sentry_sdk.push_scope() as scope:
            if context:
                for key, value in context.items():
                    scope.set_extra(key, value)
            sentry_sdk.capture_exception(exc)
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# Structured log helpers — produce consistent, searchable log entries.
# ---------------------------------------------------------------------------


def log_import_started(logger: logging.Logger, job_id: int, import_type: str, team_id: int) -> None:
    logger.info(
        "scm.import.started job_id=%s import_type=%s team_id=%s",
        job_id,
        import_type,
        team_id,
    )
    add_sentry_breadcrumb(
        f"Import started: {import_type}",
        category="scm.import",
        data={"job_id": job_id, "import_type": import_type, "team_id": team_id},
    )


def log_import_completed(
    logger: logging.Logger, job_id: int, import_type: str, team_id: int, processed: int, failed: int
) -> None:
    logger.info(
        "scm.import.completed job_id=%s import_type=%s team_id=%s processed=%s failed=%s",
        job_id,
        import_type,
        team_id,
        processed,
        failed,
    )


def log_import_failed(logger: logging.Logger, job_id: int, import_type: str, team_id: int, error: str) -> None:
    logger.error(
        "scm.import.failed job_id=%s import_type=%s team_id=%s error=%r",
        job_id,
        import_type,
        team_id,
        error,
    )


def log_tracking_sync_started(logger: logging.Logger, subscription_id: int, provider: str, team_id: int) -> None:
    logger.info(
        "scm.tracking.sync.started subscription_id=%s provider=%s team_id=%s",
        subscription_id,
        provider,
        team_id,
    )


def log_tracking_sync_completed(
    logger: logging.Logger,
    subscription_id: int,
    provider: str,
    team_id: int,
    events_created: int,
    events_updated: int,
) -> None:
    logger.info(
        "scm.tracking.sync.completed subscription_id=%s provider=%s team_id=%s events_created=%s events_updated=%s",
        subscription_id,
        provider,
        team_id,
        events_created,
        events_updated,
    )


def log_tracking_sync_failed(
    logger: logging.Logger, subscription_id: int, provider: str, team_id: int, error: str
) -> None:
    logger.error(
        "scm.tracking.sync.failed subscription_id=%s provider=%s team_id=%s error=%r",
        subscription_id,
        provider,
        team_id,
        error,
    )


def log_carrier_api_failed(
    logger: logging.Logger, carrier: str, endpoint: str, error: str, team_id: int | None = None
) -> None:
    logger.error(
        "scm.carrier.api.failed carrier=%s endpoint=%r error=%r team_id=%s",
        carrier,
        endpoint,
        error,
        team_id,
    )


def log_container_discovery_failed(logger: logging.Logger, container_number: str, team_id: int, error: str) -> None:
    logger.error(
        "scm.container.discovery.failed container_number=%s team_id=%s error=%r",
        container_number,
        team_id,
        error,
    )


def log_analytics_failed(logger: logging.Logger, team_id: int, error: str) -> None:
    logger.error(
        "scm.analytics.failed team_id=%s error=%r",
        team_id,
        error,
    )


def log_shipment_status_calculation_failed(logger: logging.Logger, shipment_id: int, team_id: int, error: str) -> None:
    logger.error(
        "scm.shipment.status_calculation.failed shipment_id=%s team_id=%s error=%r",
        shipment_id,
        team_id,
        error,
    )
