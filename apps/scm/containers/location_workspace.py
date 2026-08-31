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

**Expected arrivals are not implemented, and that is a finding rather than an
omission.** A shipment's destination is ``Shipment.destination_port``, a free-text
string. A ``ContainerLocation`` has a name, a city and a country. Nothing in the
schema connects the two: there is no UN/LOCODE on the location, no location foreign
key on the shipment, and no normalisation layer between them. Matching them by
comparing text would produce a number that looks precise and is not — a depot named
"Oceanterminalen" in Gothenburg would silently claim every shipment routed to
"Gothenburg", including those going to a different terminal in the same city.

:class:`ExpectedArrivals` therefore reports *why* the answer is unavailable rather
than guessing at it. See ``docs`` in the UX-4 report for the domain change that
would make it answerable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

from django.db.models import Count, DateTimeField, F, OuterRef, Subquery
from django.db.models.functions import Coalesce
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.teams.models import Team

from .choices import ContainerStatus
from .models import Container, ContainerLocation, ContainerMovement

# One screen of recent physical movement. A display cap, not a claim about how much
# has happened here.
_MOVEMENT_LIMIT = 25
_OVERVIEW_MOVEMENT_LIMIT = 5


@dataclass(frozen=True)
class StatusCount:
    """How many containers at this location are in one business status."""

    status: str
    label: str
    count: int


@dataclass(frozen=True)
class ExpectedArrivals:
    """What is on its way here — or why we cannot say.

    ``is_available`` is False today for every location, because no reliable
    relationship exists between a location and a shipment's destination. It is a
    field rather than a constant so the tab reads the same once one does, and so
    nothing downstream has to be rewritten to turn it on.
    """

    is_available: bool = False
    reason: str = ""
    objects: list = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.objects)


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
        expected=_expected_arrivals(),
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


def _expected_arrivals() -> ExpectedArrivals:
    """Why we cannot say what is expected here yet. See the module docstring."""
    return ExpectedArrivals(
        is_available=False,
        reason=str(
            _(
                "A shipment's destination is recorded as free text and a location has no "
                "canonical identifier, so nothing in the data reliably connects the two. "
                "Matching them by name would credit this location with arrivals bound for "
                "a different terminal in the same city."
            )
        ),
    )
