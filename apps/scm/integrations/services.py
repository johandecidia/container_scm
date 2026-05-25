# Integration services — all business logic, write operations, and external API calls.
# External API clients belong in integrations/clients/.
from apps.teams.models import Team

from .models import Integration


def create_integration(team: Team, name: str, provider: str, **kwargs) -> Integration:
    return Integration.objects.create(team=team, name=name, provider=provider, **kwargs)


def activate_integration(integration: Integration) -> Integration:
    integration.status = Integration.Status.ACTIVE
    integration.save(update_fields=["status", "updated_at"])
    return integration


def deactivate_integration(integration: Integration) -> Integration:
    integration.status = Integration.Status.INACTIVE
    integration.save(update_fields=["status", "updated_at"])
    return integration
