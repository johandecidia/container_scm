# Integration selectors — all read/query operations.
from django.db.models import QuerySet

from apps.teams.models import Team

from .models import Integration, IntegrationRequestLog, IntegrationWebhookEvent


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
