# Container selectors — all read/query operations.
from django.db.models import QuerySet

from apps.teams.models import Team

from .models import Container


def list_team_containers(team: Team) -> QuerySet[Container]:
    return Container.for_team.filter(team=team).order_by("-created_at")


def filter_team_containers(team: Team, query_params=None) -> QuerySet[Container]:
    """Return containers for a team, optionally filtered by q, status, and carrier."""
    qs = list_team_containers(team)
    if query_params:
        if q := query_params.get("q"):
            qs = qs.filter(container_number__icontains=q)
        if status := query_params.get("status"):
            qs = qs.filter(status=status)
        if carrier := query_params.get("carrier"):
            qs = qs.filter(carrier__icontains=carrier)
    return qs


def get_container_by_id(team: Team, container_id: int) -> Container:
    return Container.for_team.get(team=team, pk=container_id)
