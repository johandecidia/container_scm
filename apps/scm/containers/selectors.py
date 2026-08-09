# Container selectors — all read/query operations.
from django.db.models import Count, OuterRef, Q, QuerySet, Subquery

from apps.teams.models import Team

from .models import Container, ContainerLocation, EquipmentType
from .workspace import ContainerWorkspace, get_container_workspace

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


def get_default_equipment_type() -> EquipmentType | None:
    """Return the equipment type to fall back on when nobody has chosen one.

    Quick container registration and carrier auto-link both need *an* equipment
    type because Container requires one; neither knows which. Active types win, and
    ISO code order makes the answer stable rather than whatever the DB returns
    first. None means no equipment types are configured at all.
    """
    return EquipmentType.objects.order_by("-is_active", "iso_code").first()


def get_team_containers(team: Team) -> QuerySet[Container]:
    return (
        Container.objects.filter(team=team)
        .select_related("equipment_type", "current_location")
        .annotate(**_tracking_annotations())
    )


def _tracking_annotations() -> dict:
    """Carrier and tracking state for a list of containers, in two subqueries.

    Annotated rather than followed per row: the list renders 25 containers and must
    not issue a query each for their subscriptions. Cancelled watches are ignored so
    a container someone stopped tracking reads as untracked, not as a stale carrier.
    """
    from apps.scm.tracking.models import TrackingSubscription

    latest = (
        TrackingSubscription.objects.filter(team=OuterRef("team"), container=OuterRef("pk"))
        .exclude(status=TrackingSubscription.Status.CANCELLED)
        .order_by("-created_at")
    )
    return {
        "tracking_carrier_name": Subquery(latest.values("provider__name")[:1]),
        "tracking_watch_status": Subquery(latest.values("status")[:1]),
        "tracking_carrier_status": Subquery(latest.values("tracking_status")[:1]),
    }


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


# The container detail read model lives in workspace.py; re-exported here so callers
# keep importing selectors for reads.
__all__ = [
    "ContainerWorkspace",
    "filter_containers",
    "get_active_equipment_types",
    "get_container_by_id",
    "get_container_workspace",
    "get_default_equipment_type",
    "get_equipment_types",
    "get_team_containers",
    "get_team_locations",
    "get_team_locations_with_counts",
]
