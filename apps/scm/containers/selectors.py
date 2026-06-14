# Container selectors — all read/query operations.
from dataclasses import dataclass, field

from django.db.models import Count, Q, QuerySet

from apps.teams.models import Team

from .models import Container, ContainerLocation, EquipmentType

_SORT_MAP = {
    "newest": "-created_at",
    "oldest": "created_at",
    "container_id": "owner_code",
    "status": "status",
    "condition": "condition",
    "equipment_type": "equipment_type__iso_code",
    "location": "current_location__name",
}


def get_equipment_types() -> QuerySet[EquipmentType]:
    return EquipmentType.objects.all()


def get_active_equipment_types() -> QuerySet[EquipmentType]:
    return EquipmentType.objects.filter(is_active=True)


def get_team_containers(team: Team) -> QuerySet[Container]:
    return Container.objects.filter(team=team).select_related("equipment_type", "current_location")


def get_container_by_id(team: Team, container_id: int) -> Container:
    return Container.objects.select_related("equipment_type", "current_location").get(team=team, pk=container_id)


def get_team_locations(team: Team, active_only: bool = True) -> QuerySet[ContainerLocation]:
    """Return container locations for a team."""
    qs = ContainerLocation.objects.filter(team=team)
    if active_only:
        qs = qs.filter(is_active=True)
    return qs.order_by("name")


def get_team_locations_with_counts(team: Team) -> QuerySet[ContainerLocation]:
    """Return locations annotated with the current number of containers at each."""
    return ContainerLocation.objects.filter(team=team).annotate(container_count=Count("containers")).order_by("name")


def filter_containers(
    team: Team,
    status: str | None = None,
    condition: str | None = None,
    equipment_type: str | None = None,
    location_type: str | None = None,
    location_id: str | None = None,
    missing_location: bool = False,
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
    if location_type:
        qs = qs.filter(current_location__location_type=location_type)
    if location_id:
        qs = qs.filter(current_location_id=location_id)
    if missing_location:
        qs = qs.filter(current_location__isnull=True)
    if search:
        qs = qs.filter(
            Q(owner_code__icontains=search)
            | Q(serial_number__icontains=search)
            | Q(current_location__name__icontains=search)
            | Q(location_text__icontains=search)
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
    movements: list = field(default_factory=list)


def get_container_workspace(team: Team, container: Container) -> ContainerWorkspace:
    """Gather all workspace data for a container detail view."""
    from apps.scm.containers.models import ContainerMovement
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
    movements = list(
        ContainerMovement.objects.filter(team=team, container=container)
        .select_related("from_location", "to_location")
        .order_by("-occurred_at")[:20]
    )
    return ContainerWorkspace(
        container=container,
        shipment_containers=shipment_containers,
        tracking_subscriptions=tracking_subscriptions,
        latest_tracking_event=latest_event,
        movements=movements,
    )
