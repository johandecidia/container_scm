# Container selectors — all read/query operations.
from collections.abc import Iterable
from typing import cast

from django.db.models import Count, OuterRef, Q, QuerySet, Subquery

from apps.teams.models import Team

from .choices import LocationAliasSource, LocationResolutionStatus
from .location_workspace import (
    LocationWorkspace,
    get_location_inventory,
    get_location_movements,
    get_location_overview_movements,
    get_location_workspace,
)
from .models import Container, ContainerLocation, EquipmentType, LocationAlias
from .movements import get_container_movements, get_current_state_movement, get_state_movements
from .utils import container_number_query
from .workspace import ContainerWorkspace, get_container_workspace

# How many levels of location containment `get_location_subtree_ids` will walk. A
# port inside a port inside a port is already past anything the domain describes;
# the bound stops a cycle written straight to the database from looping.
_MAX_SUBTREE_DEPTH = 10

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
    qs = ContainerLocation.objects.filter(team=team).select_related("parent_location")
    if active_only:
        qs = qs.filter(is_active=True)
    return qs.order_by("name")


def get_team_locations_with_counts(team: Team) -> QuerySet[ContainerLocation]:
    """Return locations annotated with the current number of containers at each."""
    return (
        ContainerLocation.objects.filter(team=team)
        .select_related("parent_location")
        .annotate(container_count=Count("containers"), alias_count=Count("aliases", distinct=True))
        .order_by("name")
    )


def get_location_aliases(team: Team, location: ContainerLocation) -> QuerySet[LocationAlias]:
    """Return the external names recorded for one location."""
    return LocationAlias.objects.filter(team=team, location=location).order_by("source", "external_name")


def get_alias_source_suggestions(team: Team) -> list[str]:
    """Alias sources worth offering: the providers in use, plus the reserved two.

    Suggestions, not a closed list. The alias source field stays free text because a
    provider is a database row and a fixed enum would go stale the moment one was
    onboarded — but somebody recording an alias should not have to guess whether the
    code is ``cma-cgm`` or ``cma_cgm``, so the codes actually in use are offered.
    """
    from apps.scm.tracking.models import TrackingSubscription

    in_use = set(TrackingSubscription.objects.filter(team=team).values_list("provider__code", flat=True).distinct())
    return sorted(code for code in in_use | set(LocationAliasSource.RESERVED) if code)


def get_location_subtree_ids(team: Team, location: ContainerLocation) -> list[int]:
    """Return *location*'s id together with every location beneath it.

    What "expected at Göteborg" has to mean: a shipment bound for Oceanterminalen is
    arriving at the port that contains it, and a port whose terminals were invisible
    to it would under-report its own arrivals. This is not inference — the
    containment is a relation MCR recorded itself.

    Walked level by level rather than with a recursive CTE. The hierarchy the domain
    describes is a port with terminals in it, so this is two or three cheap queries
    and stays readable; ``_MAX_SUBTREE_DEPTH`` bounds it against corrupt data.
    """
    ids = [location.pk]
    frontier = [location.pk]
    for _level in range(_MAX_SUBTREE_DEPTH):
        children = list(
            ContainerLocation.objects.filter(team=team, parent_location_id__in=frontier)
            .exclude(pk__in=ids)
            .values_list("pk", flat=True)
        )
        if not children:
            break
        ids.extend(children)
        frontier = children
    return ids


def get_unresolved_external_locations(team: Team, limit: int = 25) -> list[dict]:
    """External places that carrier evidence names but no canonical location claims.

    The operational bridge between the two layers: each row is a place a provider
    keeps reporting that MCR has not decided about yet, and the fix for every one of
    them is to record an alias. Grouped by provider and reported name so a carrier
    that has said "GOTHENBURG" four hundred times is one row to deal with, not four
    hundred.

    ``AMBIGUOUS`` rows sit alongside unresolved ones because they need the same
    action for a different reason — the evidence matched several canonical locations
    and an alias is what breaks the tie.
    """
    from apps.scm.tracking.models import TrackingEvent

    # `.values(...).annotate(...)` yields dicts, which the model-typed stubs for
    # `values()` do not express. Cast rather than restructure the query.
    rows = cast(
        "Iterable[dict]",
        TrackingEvent.objects.filter(
            team=team,
            location_resolution_status__in=(
                LocationResolutionStatus.UNRESOLVED,
                LocationResolutionStatus.AMBIGUOUS,
            ),
        )
        .exclude(location_name="")
        .values("provider__code", "provider__name", "location_name", "location_unlocode", "location_resolution_status")
        .annotate(event_count=Count("pk"))
        .order_by("-event_count", "location_name")[:limit],
    )
    return [
        {
            "provider_code": row["provider__code"],
            "provider_name": row["provider__name"],
            "location_name": row["location_name"],
            "unlocode": row["location_unlocode"],
            "status": row["location_resolution_status"],
            "is_ambiguous": row["location_resolution_status"] == LocationResolutionStatus.AMBIGUOUS,
            "event_count": row["event_count"],
        }
        for row in rows
    ]


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
        # The filter arrives as a query-string value; the FK is an integer.
        qs = qs.filter(current_location_id=int(location_id))
    if missing_location:
        qs = qs.filter(current_location__isnull=True)
    if search:
        # A container's ISO number is composed on read from four columns, so
        # `icontains` over them can never match a number typed whole — the string
        # "MCUU2009300" exists in no column. `container_number_query` decomposes it
        # the same way the model composes it; it is the helper global search uses,
        # imported rather than restated so the two cannot disagree about what a
        # container number is.
        matches = (
            Q(owner_code__icontains=search)
            | Q(serial_number__icontains=search)
            | Q(current_location__name__icontains=search)
            | Q(location_text__icontains=search)
            | Q(manufacturer__icontains=search)
        )
        number_query = container_number_query(search)
        if number_query is not None:
            matches |= number_query.filters
        qs = qs.filter(matches)

    order_by = _SORT_MAP.get(sort or "newest", "-created_at")
    return qs.order_by(order_by)


# The container and location detail read models live in workspace.py and
# location_workspace.py, and the physical-state reads in movements.py; re-exported
# here so callers keep importing selectors for reads.
__all__ = [
    "ContainerWorkspace",
    "LocationWorkspace",
    "filter_containers",
    "get_active_equipment_types",
    "get_container_by_id",
    "get_container_movements",
    "get_container_workspace",
    "get_current_state_movement",
    "get_default_equipment_type",
    "get_state_movements",
    "get_alias_source_suggestions",
    "get_equipment_types",
    "get_location_aliases",
    "get_location_inventory",
    "get_location_movements",
    "get_location_overview_movements",
    "get_location_subtree_ids",
    "get_location_workspace",
    "get_team_containers",
    "get_team_locations",
    "get_team_locations_with_counts",
    "get_unresolved_external_locations",
]
