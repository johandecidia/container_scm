# Integration services — all business logic, write operations, and external API calls.
import logging
from datetime import datetime

from django.utils import timezone

from apps.teams.models import Team

from .models import Integration, IntegrationRequestLog, IntegrationSyncRun

logger = logging.getLogger(__name__)


# ── Sync run watermark ──────────────────────────────────────────────────────


def get_last_successful_watermark(integration: Integration, resource_type: str):
    """Return the watermark_to of the most recent fully completed sync run.

    Only ``COMPLETED`` runs advance the watermark — a failed or partially
    completed run must not move it forward, so those are excluded here. Returns
    None when there is no completed run yet (first sync).
    """
    run = (
        IntegrationSyncRun.objects.filter(
            integration=integration,
            resource_type=resource_type,
            status=IntegrationSyncRun.Status.COMPLETED,
            watermark_to__isnull=False,
        )
        .order_by("-watermark_to")
        .first()
    )
    return run.watermark_to if run else None


# ── Integration lifecycle ─────────────────────────────────────────────────────


def create_integration(
    team: Team,
    name: str,
    provider_code: str,
    provider_family: str = Integration.ProviderFamily.CARRIER,
    api_style: str = Integration.ApiStyle.UNKNOWN,
    config: dict | None = None,
) -> Integration:
    return Integration.objects.create(
        team=team,
        name=name,
        provider_code=provider_code,
        provider_family=provider_family,
        api_style=api_style,
        config=config or {},
    )


def activate_integration(integration: Integration) -> Integration:
    integration.status = Integration.Status.ACTIVE
    integration.is_active = True
    integration.save(update_fields=["status", "is_active", "updated_at"])
    return integration


def deactivate_integration(integration: Integration) -> Integration:
    integration.status = Integration.Status.INACTIVE
    integration.is_active = False
    integration.save(update_fields=["status", "is_active", "updated_at"])
    return integration


# ── Request logging ───────────────────────────────────────────────────────────


def log_integration_request(
    team: Team,
    provider_code: str,
    method: str,
    endpoint: str,
    *,
    integration: Integration | None = None,
    status_code: int | None = None,
    duration_ms: int | None = None,
    request_id: str = "",
    success: bool = False,
    error_message: str = "",
) -> IntegrationRequestLog:
    """Create an IntegrationRequestLog entry.

    Never include tokens, secrets, or auth headers in error_message or endpoint.
    """
    return IntegrationRequestLog.objects.create(
        team=team,
        integration=integration,
        provider_code=provider_code,
        method=method,
        endpoint=endpoint,
        status_code=status_code,
        duration_ms=duration_ms,
        request_id=request_id,
        success=success,
        error_message=error_message,
    )


def mark_integration_success(integration: Integration) -> None:
    """Record a successful API interaction on the Integration model."""
    integration.status = Integration.Status.ACTIVE
    integration.last_success_at = timezone.now()
    integration.last_error_message = ""
    integration.save(update_fields=["status", "last_success_at", "last_error_message", "updated_at"])


def mark_integration_error(integration: Integration, error_message: str) -> None:
    """Record a failed API interaction on the Integration model."""
    integration.status = Integration.Status.ERROR
    integration.last_error_at = timezone.now()
    integration.last_error_message = error_message
    integration.save(update_fields=["status", "last_error_at", "last_error_message", "updated_at"])


# ── Connection test ───────────────────────────────────────────────────────────


def test_integration_connection(integration: Integration) -> dict:
    """Test connectivity and credentials for a carrier integration.

    1. Looks up the carrier in the registry.
    2. Instantiates the client.
    3. Calls test_connection().
    4. Updates integration status and logs the result.
    Returns {"success": bool, "message": str}.
    """
    from .carriers.registry import UnknownCarrierError, get_carrier_client_class

    integration.last_tested_at = timezone.now()
    integration.save(update_fields=["last_tested_at", "updated_at"])

    started_at: datetime = timezone.now()
    try:
        client_class = get_carrier_client_class(integration.provider_code)
        client = client_class()
        result = client.test_connection()
        duration_ms = int((timezone.now() - started_at).total_seconds() * 1000)

        mark_integration_success(integration)
        log_integration_request(
            team=integration.team,
            provider_code=integration.provider_code,
            method="GET",
            endpoint="test_connection",
            integration=integration,
            duration_ms=duration_ms,
            success=True,
        )
        logger.info("Integration %s connection test succeeded.", integration.pk)
        return result if isinstance(result, dict) else {"success": True, "message": "OK"}

    except UnknownCarrierError as exc:
        error_message = str(exc)
        mark_integration_error(integration, error_message)
        logger.warning("Integration %s unknown carrier: %s", integration.pk, error_message)
        return {"success": False, "message": error_message}

    except Exception as exc:  # noqa: BLE001
        error_message = str(exc)
        duration_ms = int((timezone.now() - started_at).total_seconds() * 1000)
        mark_integration_error(integration, error_message)
        log_integration_request(
            team=integration.team,
            provider_code=integration.provider_code,
            method="GET",
            endpoint="test_connection",
            integration=integration,
            duration_ms=duration_ms,
            success=False,
            error_message=error_message,
        )
        logger.warning("Integration %s connection test failed: %s", integration.pk, error_message)
        return {"success": False, "message": error_message}
