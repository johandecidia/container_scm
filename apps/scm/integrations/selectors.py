# Integration selectors — all read/query operations.
from django.db.models import QuerySet

from apps.teams.models import Team

from .models import Integration, IntegrationRequestLog, IntegrationSyncRun, IntegrationWebhookEvent


def get_team_integrations(team: Team) -> QuerySet[Integration]:
    return Integration.objects.filter(team=team).order_by("name")


def get_active_team_integrations(team: Team) -> QuerySet[Integration]:
    return Integration.objects.filter(team=team, status=Integration.Status.ACTIVE, is_active=True)


def get_team_integration_by_provider(team: Team, provider_code: str) -> Integration:
    """Return the integration for the given team and provider_code.

    Raises Integration.DoesNotExist if not found.
    """
    return Integration.objects.get(team=team, provider_code=provider_code)


def get_recent_request_logs(team: Team, limit: int = 50) -> QuerySet[IntegrationRequestLog]:
    return IntegrationRequestLog.objects.filter(team=team).order_by("-created_at")[:limit]


def get_unprocessed_webhook_events(team: Team) -> QuerySet[IntegrationWebhookEvent]:
    return IntegrationWebhookEvent.objects.filter(team=team, status=IntegrationWebhookEvent.Status.RECEIVED).order_by(
        "created_at"
    )


def get_team_business_central_integrations(team: Team) -> QuerySet[Integration]:
    """BC business-system integrations for a team (for the monitoring UI)."""
    return Integration.objects.filter(
        team=team,
        provider_family=Integration.ProviderFamily.BUSINESS_SYSTEM,
        provider_code="business_central",
    ).order_by("name")


def get_latest_sync_run(integration: Integration) -> IntegrationSyncRun | None:
    return (
        IntegrationSyncRun.objects.filter(
            integration=integration,
            resource_type=IntegrationSyncRun.ResourceType.PURCHASE_ORDERS,
        )
        .order_by("-started_at")
        .first()
    )


def is_sync_in_progress(integration: Integration) -> bool:
    return IntegrationSyncRun.objects.filter(
        integration=integration,
        resource_type=IntegrationSyncRun.ResourceType.PURCHASE_ORDERS,
        status=IntegrationSyncRun.Status.RUNNING,
    ).exists()


def get_integration_monitoring_context(integration: Integration) -> dict:
    """Sanitised monitoring summary for one integration (never exposes credentials)."""
    config = integration.config or {}
    latest = get_latest_sync_run(integration)
    last_completed = (
        IntegrationSyncRun.objects.filter(
            integration=integration,
            resource_type=IntegrationSyncRun.ResourceType.PURCHASE_ORDERS,
            status=IntegrationSyncRun.Status.COMPLETED,
        )
        .order_by("-finished_at")
        .first()
    )
    last_failed = (
        IntegrationSyncRun.objects.filter(
            integration=integration,
            resource_type=IntegrationSyncRun.ResourceType.PURCHASE_ORDERS,
            status=IntegrationSyncRun.Status.FAILED,
        )
        .order_by("-finished_at")
        .first()
    )
    return {
        "integration": integration,
        "environment": config.get("environment", ""),
        "company_id": config.get("company_id", ""),
        "latest_run": latest,
        "last_completed_run": last_completed,
        "last_failed_run": last_failed,
        "current_watermark": last_completed.watermark_to if last_completed else None,
        "in_progress": bool(latest and latest.status == IntegrationSyncRun.Status.RUNNING),
    }
