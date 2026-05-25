# Integration selectors — all read/query operations.
from django.db.models import QuerySet

from apps.teams.models import Team

from .models import Integration


def get_team_integrations(team: Team) -> QuerySet[Integration]:
    return Integration.for_team.filter(team=team).order_by("name")


def get_active_integrations(team: Team) -> QuerySet[Integration]:
    return Integration.for_team.filter(team=team, status=Integration.Status.ACTIVE)
