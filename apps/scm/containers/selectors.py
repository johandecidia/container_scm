# Container selectors — all read/query operations.
from django.db.models import Count, OuterRef, Q, QuerySet, Subquery

from apps.teams.models import Team

from .choices import LocationAliasSource
from .location_hierarchy import descendant_ids
from .location_workspace import (
    LocationHierarchy,
    LocationWorkspace,
    get_location_hierarchy,
    get_location_inventory,
    get_location_movements,
    get_location_overview_movements,
    get_location_workspace,
)
from .models import Container, ContainerCondition, ContainerLocation, EquipmentType, LocationAlias
from .movements import get_container_movements, get_current_state_movement, get_state_movements
from .utils import container_number_query
from .workspace import ContainerWorkspace, get_container_workspace

_SORT_MAP = {
    "newest": "-created_at",
    "oldest": "created_at",
    "container_id": "owner_code",
    "status": "status",
    # Sorting by condition means the team's own order, not the alphabet: the point of
    # `sort_order` is that "New" before "As is" is a decision somebody made.
    "condition": "condition__sort_order",
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


def get_team_conditions(team: Team) -> QuerySet[ContainerCondition]:
    """Every condition this team has, retired ones included. For the Settings page."""
    return ContainerCondition.objects.filter(team=team)


def get_conditions_with_usage(team: Team) -> QuerySet[ContainerCondition]:
    """The team's conditions, each with how many containers carry it.

    One query for the whole table. The count is what tells an operator whether
    retiring a row will change what anybody sees, and it is also why the Settings
    page offers no delete: a condition in use is protected by the FK.
    """
    return get_team_conditions(team).annotate(container_count=Count("containers"))


def get_condition_options(team: Team, current=None) -> QuerySet[ContainerCondition]:
    """The conditions a form may offer: this team's active ones, plus ``current``.

    ``current`` — a ``ContainerCondition`` or its pk — is included even when it is
    retired, because it is what the container being edited is already graded as.
    Retiring a condition is meant to stop it being *chosen*, not to rewrite the boxes
    already carrying it, and a form that dropped the stored value would propose
    clearing it every time somebody opened an unrelated field.
    """
    matches = Q(is_active=True)
    if current is not None:
        matches |= Q(pk=getattr(current, "pk", current))
    return ContainerCondition.objects.filter(team=team).filter(matches)


def get_default_condition(team: Team) -> ContainerCondition | None:
    """The condition to fall back on when an intake did not choose one.

    The team's first active condition in its own order — the same shape as
    :func:`get_default_equipment_type`, and for the same reason: a bulk intake needs
    *a* value and has no way to know which. None means the team has configured none,
    and the container is created without a condition rather than with a guess.
    """
    return get_condition_options(team).order_by("sort_order", "name").first()


def get_team_containers(team: Team) -> QuerySet[Container]:
    return (
        Container.objects.filter(team=team)
        .select_related("equipment_type", "current_location", "condition")
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

    Kept as the name its callers already use — the arrivals queue and the location
    workspace's Expected tab — and now a call onto
    :func:`apps.scm.containers.location_hierarchy.descendant_ids`, which is where
    every traversal of the hierarchy lives. Two walks would eventually let a port
    contain a terminal on one page and not on another.
    """
    return descendant_ids(team, location)


# Unmatched external place names used to be aggregated here, for a panel on the
# Locations list. LOC-5 moved that to
# :func:`apps.scm.visibility.location_quality.get_location_data_quality`, which
# groups the same evidence, counts the containers behind it and can act on it. There
# is deliberately no copy left here: two aggregations of the same rows would
# eventually give the Locations list and the data-quality queue different answers
# about how much work there is.


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
        # Filtered by code, not by pk: the value arrives in the query string, and a
        # code keeps saved filters and shared links meaning the same thing after the
        # row behind it is renamed. The queryset is already team-scoped, so the code
        # can only match this team's condition.
        qs = qs.filter(condition__code=condition)
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
    "LocationHierarchy",
    "LocationWorkspace",
    "filter_containers",
    "get_active_equipment_types",
    "get_container_by_id",
    "get_container_movements",
    "get_container_workspace",
    "get_condition_options",
    "get_conditions_with_usage",
    "get_current_state_movement",
    "get_default_condition",
    "get_default_equipment_type",
    "get_state_movements",
    "get_alias_source_suggestions",
    "get_equipment_types",
    "get_team_conditions",
    "get_location_aliases",
    "get_location_hierarchy",
    "get_location_inventory",
    "get_location_movements",
    "get_location_overview_movements",
    "get_location_subtree_ids",
    "get_location_workspace",
    "get_team_containers",
    "get_team_locations",
    "get_team_locations_with_counts",
]
