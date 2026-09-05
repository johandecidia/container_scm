"""Everything the location workspace needs, gathered once.

A location answers three questions: what is physically here, what is expected here,
and what has moved in or out. This module answers the first and the third, and is
deliberately explicit about why it cannot yet answer the second.

**Inventory is ``Container.current_location`` and nothing else.** There is no stored
count, no dwell counter and no expected-arrival column. What is here is what points
here, counted on read, so moving a box changes the answer immediately.

**Movements are the physical record.** ``ContainerMovement`` rows into and out of
this location are what "activity" means here. They are kept apart from carrier
journey events on purpose: a gate move we recorded and a discharge a carrier
reported are different kinds of claim, made by different parties.

**Expected arrivals are canonical or they are nothing.** They come from
``Shipment.destination_location`` — the canonical location LOC-1 introduced — and
from no text comparison at all. A shipment routed to "Gothenburg" as free text does
not appear here: it appears once somebody has said which place in Gothenburg it is
going to. That is the whole point. Matching on names would let a depot named
"Oceanterminalen" claim every shipment bound for a different terminal in the same
city, and the resulting number would look precise while being wrong.

A location's own subtree counts, because containment is a relation MCR recorded
rather than inferred: a shipment bound for Oceanterminalen *is* arriving at the
Göteborg port that contains it, so the port's tab includes it and the terminal's
tab does not include the port's other traffic.

:class:`ExpectedArrivals` keeps its ``is_available`` field. It is now True for every
location, but a location whose team has nothing routed to it canonically still has
to say "nothing is expected here" rather than "we cannot tell you" — and those
remain different sentences.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

from django.db.models import Count, DateTimeField, F, OuterRef, Subquery
from django.db.models.functions import Coalesce
from django.utils import timezone

from apps.teams.models import Team

from .choices import ContainerStatus
from .models import Container, ContainerLocation, ContainerMovement, LocationAlias

# One screen of recent physical movement. A display cap, not a claim about how much
# has happened here.
_MOVEMENT_LIMIT = 25
_OVERVIEW_MOVEMENT_LIMIT = 5

# How far ahead a location's Expected tab looks, as an arrivals-queue window. Wider
# than the operational queue's week because yard planning is a monthly question.
EXPECTED_ARRIVALS_WINDOW = "30"


@dataclass(frozen=True)
class StatusCount:
    """How many containers at this location are in one business status."""

    status: str
    label: str
    count: int


@dataclass(frozen=True)
class ExpectedArrivals:
    """What is on its way here, from canonical destinations only.

    ``objects`` are :class:`~apps.scm.visibility.read_models.VisibilityObject` rows —
    the same read model the fleet-wide Arrivals queue renders, so a shipment
    described one way there is described the same way here.

    ``reason`` is kept for the case where the answer is unavailable rather than
    empty. It is unused today and must stay that way unless something genuinely
    cannot be answered: filling it in to explain an empty list would turn "nothing
    is coming" into "we do not know", which is a different and worse claim.
    """

    is_available: bool = True
    reason: str = ""
    objects: list = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.objects)

    @property
    def container_count(self) -> int:
        return sum(obj.container_count for obj in self.objects)


@dataclass
class LocationWorkspace:
    """Read model for the location detail view.

    Built once by :func:`get_location_workspace`. The inventory queryset is left
    unevaluated so the view can filter and paginate it; everything else is resolved.
    """

    location: ContainerLocation
    inventory: object = None
    container_count: int = 0
    status_counts: list[StatusCount] = field(default_factory=list)
    recent_movements: list = field(default_factory=list)
    expected: ExpectedArrivals = field(default_factory=ExpectedArrivals)
    aliases: list = field(default_factory=list)

    # -- identity -----------------------------------------------------------

    @property
    def place(self) -> str:
        """City and country in one line, empty when neither is recorded."""
        return ", ".join(part for part in (self.location.city, self.location.country) if part)

    @property
    def type_label(self) -> str:
        return self.location.get_location_type_display()

    @property
    def is_active(self) -> bool:
        return self.location.is_active

    @property
    def parent(self) -> ContainerLocation | None:
        return self.location.parent_location

    @property
    def unlocode(self) -> str:
        return self.location.unlocode

    @property
    def has_coordinates(self) -> bool:
        return self.location.latitude is not None and self.location.longitude is not None

    # -- external identity --------------------------------------------------

    @property
    def alias_count(self) -> int:
        return len(self.aliases)

    @property
    def has_aliases(self) -> bool:
        return bool(self.aliases)

    # -- inventory ----------------------------------------------------------

    @property
    def is_empty(self) -> bool:
        return self.container_count == 0

    @property
    def occupied_status_counts(self) -> list[StatusCount]:
        """Only the statuses actually present. An absent status is not a zero row."""
        return [row for row in self.status_counts if row.count]

    # -- movement -----------------------------------------------------------

    @property
    def has_movement_history(self) -> bool:
        return bool(self.recent_movements)

    @property
    def moved_in_last_week(self) -> int:
        """Arrivals recorded here in the last seven days, from the loaded movements.

        Counted off the capped recent list rather than with another query: it is a
        sense of how busy the place is, and the cap is stated in the template.
        """
        cutoff = timezone.now() - timedelta(days=7)
        return sum(
            1
            for movement in self.recent_movements
            if movement.to_location_id == self.location.pk and movement.occurred_at >= cutoff
        )


def get_location_workspace(team: Team, location: ContainerLocation) -> LocationWorkspace:
    """Gather everything the location workspace renders, team-scoped throughout.

    Four queries plus whatever the view does with the inventory queryset: the count,
    the status breakdown, the recent movements, and the inventory itself. Nothing
    scales with the number of containers at the location.
    """
    inventory = get_location_inventory(team=team, location=location)

    counts = {
        row["status"]: row["total"]
        for row in Container.objects.filter(team=team, current_location=location)
        .values("status")
        .annotate(total=Count("pk"))
    }
    status_counts = [
        StatusCount(status=value, label=str(label), count=counts.get(value, 0))
        for value, label in ContainerStatus.choices
    ]

    return LocationWorkspace(
        location=location,
        inventory=inventory,
        container_count=sum(counts.values()),
        status_counts=status_counts,
        recent_movements=get_location_movements(team=team, location=location),
        expected=get_expected_arrivals(team=team, location=location),
        aliases=list(LocationAlias.objects.filter(team=team, location=location).order_by("source", "external_name")),
    )


def get_location_inventory(team: Team, location: ContainerLocation, *, sort: str | None = None, **filters):
    """The containers physically at this location, most recently arrived first.

    Reuses the container list's own filtering so the columns, the search behaviour
    and the tracking annotations are the ones the rest of the product already has —
    there is no second idea here of what filtering a container list means. Passing
    ``sort`` hands the ordering back to that shared list; without one, the default
    here is arrival order, which is the question a depot asks.

    ``at_location_since`` is when the box got here. It prefers the most recent
    recorded movement into this location and falls back to the container's own
    ``last_location_update``, because a container whose location was set at creation
    has a movement but no update stamp, and one set by an importer may have the
    stamp and no movement. Both are real records of the same fact; neither is
    invented. A container with neither renders no date at all rather than a guess.
    """
    from .selectors import filter_containers

    arrival = (
        ContainerMovement.objects.filter(
            team=team,
            container=OuterRef("pk"),
            to_location=location,
        )
        .order_by("-occurred_at", "-created_at")
        .values("occurred_at")[:1]
    )
    queryset = filter_containers(team=team, location_id=str(location.pk), sort=sort, **filters).annotate(
        at_location_since=Coalesce(Subquery(arrival, output_field=DateTimeField()), "last_location_update"),
    )
    if sort:
        return queryset
    # NULLS LAST: a container with no recorded arrival time is not the most recent
    # thing to arrive, and sorting it to the top would say that it was.
    return queryset.order_by(F("at_location_since").desc(nulls_last=True), "-created_at")


def get_location_movements(team: Team, location: ContainerLocation, limit: int = _MOVEMENT_LIMIT) -> list:
    """Physical movements into and out of this location, newest first.

    Both directions, because a location's history is what came and what went. The
    row itself says which: a movement whose ``to_location`` is this one arrived, and
    one whose ``from_location`` is this one left.
    """
    from django.db.models import Q

    return list(
        ContainerMovement.objects.filter(team=team)
        .filter(Q(to_location=location) | Q(from_location=location))
        .select_related("container", "container__equipment_type", "from_location", "to_location")
        .order_by("-occurred_at", "-created_at")[:limit]
    )


def get_location_overview_movements(workspace: LocationWorkspace) -> list:
    """The handful of movements the Overview tab shows, off the already-loaded list."""
    return workspace.recent_movements[:_OVERVIEW_MOVEMENT_LIMIT]


def get_expected_arrivals(team: Team, location: ContainerLocation) -> ExpectedArrivals:
    """What is canonically routed to this location or somewhere inside it.

    Composed from the fleet-wide arrivals queue rather than from a query of its own.
    That queue already decides what "arriving" means — which shipment statuses
    count, which standalone containers stand on their own, how the ETA is chosen —
    and a second implementation here would eventually disagree with the Arrivals
    page about whether a given box is coming.

    The window is wider than the queue's own default: a depot planning its yard
    cares about the month ahead, where the operational queue is about the week. The
    template states the range it is showing.

    Subtree expansion is the queue's own, not repeated here — choosing Göteborg on
    the Arrivals page and opening Göteborg's Expected tab must include exactly the
    same shipments.

    The import is deferred because the arrivals queue reads the container workspace,
    which lives in this app — at module scope the two would import each other.
    """
    from apps.scm.visibility.work_queues import ArrivalQueueFilters, get_arrival_queue

    queue = get_arrival_queue(
        team,
        ArrivalQueueFilters(window=EXPECTED_ARRIVALS_WINDOW, destination_location=str(location.pk)),
    )
    return ExpectedArrivals(is_available=True, objects=queue.objects)
