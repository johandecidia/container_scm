# Shipment selectors — all read/query operations.
from django.db.models import QuerySet

from apps.teams.models import Team

from .models import Shipment


def get_team_shipments(team: Team) -> QuerySet[Shipment]:
    return Shipment.for_team.filter(team=team).order_by("-created_at")


def get_active_shipments(team: Team) -> QuerySet[Shipment]:
    active_statuses = [Shipment.Status.BOOKED, Shipment.Status.IN_TRANSIT]
    return Shipment.for_team.filter(team=team, status__in=active_statuses)
