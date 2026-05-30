# Shipment selectors — all read/query operations.
# Every function that returns team-owned data must accept `team` as first argument.
from django.db.models import Q, QuerySet

from apps.teams.models import Team

from .models import Shipment, ShipmentContainer, ShipmentEvent

_SORT_MAP = {
    "newest": "-created_at",
    "oldest": "created_at",
    "eta": "eta",
    "etd": "etd",
    "status": "status",
}


def get_team_shipments(team: Team) -> QuerySet[Shipment]:
    return Shipment.for_team.filter(team=team).select_related("created_by")


def get_team_shipment(team: Team, shipment_id: int) -> Shipment:
    return Shipment.for_team.select_related("created_by").get(team=team, pk=shipment_id)


def filter_shipments(
    team: Team,
    status: str | None = None,
    search: str | None = None,
    sort: str | None = None,
) -> QuerySet[Shipment]:
    """Return shipments for a team with optional status filter, search, and sorting."""
    qs = get_team_shipments(team)

    if status:
        qs = qs.filter(status=status)
    if search:
        qs = qs.filter(
            Q(shipment_number__icontains=search)
            | Q(reference__icontains=search)
            | Q(customer_name__icontains=search)
            | Q(carrier__icontains=search)
            | Q(origin_port__icontains=search)
            | Q(destination_port__icontains=search)
        )

    order_by = _SORT_MAP.get(sort or "newest", "-created_at")
    return qs.order_by(order_by)


def get_shipment_containers(team: Team, shipment: Shipment) -> QuerySet[ShipmentContainer]:
    """Return all containers linked to a shipment, scoped to the team."""
    return (
        ShipmentContainer.objects.filter(shipment=shipment, shipment__team=team)
        .select_related("container", "container__equipment_type")
        .order_by("sequence", "created_at")
    )


def get_shipment_events(team: Team, shipment: Shipment) -> QuerySet[ShipmentEvent]:
    """Return timeline events for a shipment, scoped to the team."""
    return (
        ShipmentEvent.objects.filter(shipment=shipment, shipment__team=team)
        .select_related("created_by")
        .order_by("-occurred_at", "-created_at")
    )
