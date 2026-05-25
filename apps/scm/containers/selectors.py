# Container selectors — all read/query operations.
from django.db.models import QuerySet

from apps.teams.models import Team

from .models import Container


def get_team_containers(team: Team) -> QuerySet[Container]:
    return Container.for_team.filter(team=team).order_by("-created_at")


def get_container_by_number(team: Team, container_number: str) -> Container:
    return Container.for_team.get(team=team, container_number=container_number)
