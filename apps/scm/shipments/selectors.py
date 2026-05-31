# Shipment selectors — all read/query operations.
# Every function that returns team-owned data must accept `team` as first argument.
from dataclasses import dataclass, field

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
    return Shipment.objects.filter(team=team).select_related("created_by")


def get_team_shipment(team: Team, shipment_id: int) -> Shipment:
    return Shipment.objects.select_related("created_by").get(team=team, pk=shipment_id)


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


@dataclass
class ShipmentWorkspace:
    shipment: Shipment
    containers: list = field(default_factory=list)
    events: list = field(default_factory=list)
    tracking_subscriptions: list = field(default_factory=list)
    latest_tracking_event: object = None  # TrackingEvent | None


def get_shipment_workspace(team: Team, shipment: Shipment) -> ShipmentWorkspace:
    """Gather all workspace data for a shipment detail view."""
    from apps.scm.tracking.models import TrackingEvent, TrackingSubscription

    containers = list(get_shipment_containers(team=team, shipment=shipment))
    events = list(get_shipment_events(team=team, shipment=shipment))
    tracking_subscriptions = list(
        TrackingSubscription.objects.filter(team=team, shipment=shipment)
        .select_related("provider")
        .order_by("-created_at")
    )
    latest_event = (
        TrackingEvent.objects.filter(team=team, shipment=shipment)
        .select_related("provider")
        .order_by("-event_datetime", "-created_at")
        .first()
    )
    return ShipmentWorkspace(
        shipment=shipment,
        containers=containers,
        events=events,
        tracking_subscriptions=tracking_subscriptions,
        latest_tracking_event=latest_event,
    )
