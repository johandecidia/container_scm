# Container selectors — all read/query operations.
from dataclasses import dataclass, field

from django.db.models import Q, QuerySet

from apps.teams.models import Team

from .models import Container, EquipmentType

_SORT_MAP = {
    "newest": "-created_at",
    "oldest": "created_at",
    "container_id": "owner_code",
    "status": "status",
    "condition": "condition",
    "equipment_type": "equipment_type__iso_code",
}


def get_equipment_types() -> QuerySet[EquipmentType]:
    return EquipmentType.objects.all()


def get_active_equipment_types() -> QuerySet[EquipmentType]:
    return EquipmentType.objects.filter(is_active=True)


def get_team_containers(team: Team) -> QuerySet[Container]:
    return Container.objects.filter(team=team).select_related("equipment_type")


def get_container_by_id(team: Team, container_id: int) -> Container:
    return Container.objects.select_related("equipment_type").get(team=team, pk=container_id)


def filter_containers(
    team: Team,
    status: str | None = None,
    condition: str | None = None,
    equipment_type: str | None = None,
    search: str | None = None,
    sort: str | None = None,
) -> QuerySet[Container]:
    """Return containers for a team with optional filters and sorting."""
    qs = get_team_containers(team)

    if status:
        qs = qs.filter(status=status)
    if condition:
        qs = qs.filter(condition=condition)
    if equipment_type:
        qs = qs.filter(equipment_type_id=equipment_type)
    if search:
        qs = qs.filter(
            Q(owner_code__icontains=search)
            | Q(serial_number__icontains=search)
            | Q(current_location__icontains=search)
            | Q(manufacturer__icontains=search)
        )

    order_by = _SORT_MAP.get(sort or "newest", "-created_at")
    return qs.order_by(order_by)


@dataclass
class ContainerWorkspace:
    container: Container
    shipment_containers: list = field(default_factory=list)
    tracking_subscriptions: list = field(default_factory=list)
    latest_tracking_event: object = None  # TrackingEvent | None


def get_container_workspace(team: Team, container: Container) -> ContainerWorkspace:
    """Gather all workspace data for a container detail view."""
    from apps.scm.shipments.models import ShipmentContainer
    from apps.scm.tracking.models import TrackingEvent, TrackingSubscription

    shipment_containers = list(
        ShipmentContainer.objects.filter(container=container, shipment__team=team)
        .select_related("shipment")
        .order_by("-created_at")
    )
    tracking_subscriptions = list(
        TrackingSubscription.objects.filter(team=team, container=container)
        .select_related("provider")
        .order_by("-created_at")
    )
    latest_event = (
        TrackingEvent.objects.filter(team=team, container=container)
        .select_related("provider")
        .order_by("-event_datetime", "-created_at")
        .first()
    )
    return ContainerWorkspace(
        container=container,
        shipment_containers=shipment_containers,
        tracking_subscriptions=tracking_subscriptions,
        latest_tracking_event=latest_event,
    )
