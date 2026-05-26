# Container selectors — all read/query operations.
from django.db.models import QuerySet

from apps.teams.models import Team

from .models import Container


def get_team_containers(team: Team) -> QuerySet[Container]:
    return Container.for_team.filter(team=team).order_by("-created_at")


def get_containers_for_team(team: Team, query_params=None) -> QuerySet[Container]:
    """Return containers for a team, optionally filtered by q, status, and size."""
    qs = get_team_containers(team)
    if query_params:
        if q := query_params.get("q"):
            qs = qs.filter(container_number__icontains=q)
        if status := query_params.get("status"):
            qs = qs.filter(status=status)
        if size := query_params.get("size"):
            qs = qs.filter(size=size)
    return qs


def get_container_by_number(team: Team, container_number: str) -> Container:
    return Container.for_team.get(team=team, container_number=container_number)
