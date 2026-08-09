# Shipment selectors — all read/query operations.
# Every function that returns team-owned data must accept `team` as first argument.
from dataclasses import dataclass, field
from datetime import datetime

from django.db.models import Q, QuerySet

from apps.teams.models import Team

from .models import Shipment, ShipmentContainer, ShipmentEvent


@dataclass
class ShipmentTimelineItem:
    """Unified timeline entry combining ShipmentEvents and TrackingEvents."""

    occurred_at: datetime | None
    title: str
    description: str
    source: str  # "shipment" or "tracking"
    event_type: str
    location: str = ""
    created_by: object = None  # user or None


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


def get_merged_shipment_timeline(team: Team, shipment: Shipment) -> list[ShipmentTimelineItem]:
    """Return a merged, date-sorted timeline of shipment and tracking events.

    Combines ShipmentEvents and TrackingEvents into a single list of
    ShipmentTimelineItems, sorted newest first.
    """
    from apps.scm.tracking.models import TrackingEvent

    items: list[ShipmentTimelineItem] = []

    # ShipmentEvent entries
    for event in get_shipment_events(team=team, shipment=shipment):
        items.append(
            ShipmentTimelineItem(
                occurred_at=event.occurred_at,
                title=event.get_event_type_display(),
                description=event.description,
                source="shipment",
                event_type=event.event_type,
                created_by=event.created_by,
            )
        )

    # TrackingEvent entries
    tracking_qs = (
        TrackingEvent.objects.filter(team=team, shipment=shipment)
        .select_related("provider")
        .order_by("-event_datetime", "-created_at")
    )
    for event in tracking_qs:
        location = event.location_name
        if event.location_unlocode:
            location = f"{location} ({event.location_unlocode})" if location else event.location_unlocode
        items.append(
            ShipmentTimelineItem(
                occurred_at=event.event_datetime,
                title=event.display_title,
                description=event.description or event.status or event.carrier_reference,
                source="tracking",
                event_type=event.event_type,
                location=location,
            )
        )

    # Sort: newest first; None datetimes go to the end
    items.sort(key=lambda i: (i.occurred_at is None, -(i.occurred_at.timestamp() if i.occurred_at else 0)))
    return items


@dataclass
class ShipmentWorkspace:
    shipment: Shipment
    containers: list = field(default_factory=list)
    events: list = field(default_factory=list)
    tracking_subscriptions: list = field(default_factory=list)
    latest_tracking_event: object = None  # TrackingEvent | None


def get_shipment_purchase_orders(team: Team, shipment: Shipment):
    """Return purchase orders linked to this shipment via container → supplier delivery lines."""
    from apps.scm.procurement.models import PurchaseOrder

    container_ids = ShipmentContainer.objects.filter(shipment=shipment, shipment__team=team).values_list(
        "container_id", flat=True
    )
    return (
        PurchaseOrder.objects.filter(
            team=team,
            supplier_deliveries__lines__container_id__in=container_ids,
        )
        .distinct()
        .order_by("-order_date", "po_number")
    )


def get_shipment_supplier_deliveries(team: Team, shipment: Shipment):
    """Return supplier deliveries linked to this shipment via container."""
    from apps.scm.supplier_deliveries.models import SupplierDelivery

    container_ids = ShipmentContainer.objects.filter(shipment=shipment, shipment__team=team).values_list(
        "container_id", flat=True
    )
    return (
        SupplierDelivery.objects.filter(
            team=team,
            lines__container_id__in=container_ids,
        )
        .select_related("purchase_order")
        .distinct()
        .order_by("-created_at")
    )


def get_shipment_detail_context(team: Team, shipment_id: int) -> dict:
    """Return all context data needed for the shipment detail view.

    Gathers: shipment, containers, tracking events, purchase orders,
    supplier deliveries, timeline events — all team-scoped.
    """
    from apps.scm.tracking.models import TrackingEvent, TrackingSubscription

    shipment = get_team_shipment(team=team, shipment_id=shipment_id)
    containers = list(get_shipment_containers(team=team, shipment=shipment))
    timeline_events = get_merged_shipment_timeline(team=team, shipment=shipment)
    tracking_subscriptions = list(
        TrackingSubscription.objects.filter(team=team, shipment=shipment)
        .select_related("provider")
        .order_by("-created_at")
    )
    latest_tracking_event = (
        TrackingEvent.objects.filter(team=team, shipment=shipment)
        .select_related("provider")
        .order_by("-event_datetime", "-created_at")
        .first()
    )
    purchase_orders = list(get_shipment_purchase_orders(team=team, shipment=shipment))
    supplier_deliveries = list(get_shipment_supplier_deliveries(team=team, shipment=shipment))

    return {
        "shipment": shipment,
        "containers": containers,
        "tracking_subscriptions": tracking_subscriptions,
        "latest_tracking_event": latest_tracking_event,
        "purchase_orders": purchase_orders,
        "supplier_deliveries": supplier_deliveries,
        "timeline_events": timeline_events,
    }


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
