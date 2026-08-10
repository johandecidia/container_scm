"""SCM health check view.

Endpoint: /health/scm/

Checks SCM-critical components without hitting external APIs.
Returns a structured JSON response with per-component status.
"""

import logging

from django.http import JsonResponse
from django.utils import timezone

logger = logging.getLogger(__name__)

# How old a "last success" timestamp can be before we warn (in hours)
_IMPORT_WARNING_HOURS = 48
_TRACKING_WARNING_HOURS = 4
_DISCOVERY_WARNING_HOURS = 24


def _check_database() -> str:
    """Verify SCM models are queryable."""
    try:
        from apps.scm.shipments.models import Shipment

        Shipment.objects.exists()
        return "ok"
    except Exception:  # noqa: BLE001
        logger.exception("health_scm: database check failed")
        return "error"


def _check_last_import() -> str:
    """Return ok/warning based on when the last completed import ran."""
    try:
        from apps.scm.imports.models import ImportJob

        last = ImportJob.objects.filter(status=ImportJob.Status.COMPLETED).order_by("-completed_at").first()
        if last is None:
            # No imports yet — not an error, just a warning for new tenants
            return "warning"
        if last.completed_at is None:
            return "warning"
        age_hours = (timezone.now() - last.completed_at).total_seconds() / 3600
        if age_hours > _IMPORT_WARNING_HOURS:
            return "warning"
        return "ok"
    except Exception:  # noqa: BLE001
        logger.exception("health_scm: import check failed")
        return "error"


def _check_last_tracking_sync() -> str:
    """Return ok/warning based on when the last successful tracking sync ran."""
    try:
        from apps.scm.tracking.models import TrackingSyncRun

        last = TrackingSyncRun.objects.filter(status=TrackingSyncRun.Status.SUCCESS).order_by("-finished_at").first()
        if last is None:
            return "warning"
        if last.finished_at is None:
            return "warning"
        age_hours = (timezone.now() - last.finished_at).total_seconds() / 3600
        if age_hours > _TRACKING_WARNING_HOURS:
            return "warning"
        return "ok"
    except Exception:  # noqa: BLE001
        logger.exception("health_scm: tracking sync check failed")
        return "error"


def _check_last_discovery() -> str:
    """Return ok/warning based on when the last container discovery check ran."""
    try:
        from apps.scm.containers.models import PlannedContainer

        last = PlannedContainer.objects.filter(last_checked_at__isnull=False).order_by("-last_checked_at").first()
        if last is None:
            return "warning"
        age_hours = (timezone.now() - last.last_checked_at).total_seconds() / 3600  # type: ignore[operator]
        if age_hours > _DISCOVERY_WARNING_HOURS:
            return "warning"
        return "ok"
    except Exception:  # noqa: BLE001
        logger.exception("health_scm: discovery check failed")
        return "error"


def health_scm(request):
    """SCM aggregate health check.

    Returns 200 if all checks are ok or warning.
    Returns 503 if any check is error.
    Does NOT call external carrier APIs.
    """
    checks = {
        "database": _check_database(),
        "imports": _check_last_import(),
        "tracking": _check_last_tracking_sync(),
        "discovery": _check_last_discovery(),
    }

    has_error = any(v == "error" for v in checks.values())
    overall = "error" if has_error else ("warning" if any(v == "warning" for v in checks.values()) else "ok")

    return JsonResponse(
        {"status": overall, "checks": checks},
        status=503 if has_error else 200,
    )
