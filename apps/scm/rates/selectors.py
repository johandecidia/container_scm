# Rate selectors — all read/query operations.
from django.db.models import QuerySet

from apps.teams.models import Team

from .models import Rate


def get_team_rates(team: Team) -> QuerySet[Rate]:
    return Rate.for_team.filter(team=team).order_by("-created_at")


def get_rates_for_lane(team: Team, origin: str, destination: str) -> QuerySet[Rate]:
    return Rate.for_team.filter(team=team, origin=origin, destination=destination)
