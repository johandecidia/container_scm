"""The arrivals queue: what is expected to arrive, when, where and in what condition.

Grouped by day, because a date is what the domain actually holds. Shipments and
standalone containers are both first class — a container that belongs to no shipment
is not wrapped in an invented one to make the list uniform.

Destination can be asked two ways, and they are not the same question:

``destination``
    The reported text — "Gothenburg", whatever was on the booking. An exact string
    match, offered because it is what most shipments have.

``destination_location``
    A canonical :class:`~apps.scm.containers.models.ContainerLocation`. This is the
    one that can answer "what is expected at Oceanterminalen" without hoping that
    every carrier spells Göteborg the same way, and without a depot inheriting the
    arrivals of its neighbours. A location's own subtree is included, because a
    shipment bound for a terminal is arriving at the port that contains it.

A shipment with no canonical destination is simply absent from a canonical filter.
It is not guessed into one by comparing its text against location names — that is
the false positive the canonical layer exists to prevent.

**Arrived is not expected.** Something whose containers have all been accepted into
their canonical destination has stopped being an arrival to plan for, and drops out
of the queue. That is decided from :class:`ContainerMovement` evidence — a gate-in
or a receipt at the destination or somewhere inside it — and specifically *not*
from ``current_location == destination``, which answers a different question. A box
gated in last Tuesday and trucked onward since has arrived, and comparing current
location would put it back on the list; a box a carrier merely reports near
Gothenburg has not, and comparing current location could take it off.

The full arrival lifecycle — receiving, discrepancies, closing out — is LOC-3. This
is only the point at which an expectation is satisfied.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, time, timedelta
from typing import TYPE_CHECKING, cast

from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.teams.models import Team

from ..read_models import Health, ObjectKind, VisibilityObject
from ..selectors import ARRIVING_SOON_DAYS, filter_by_eta_window, list_visibility_objects, matches_search
from .choices import text_choices

if TYPE_CHECKING:
    from django_stubs_ext import StrOrPromise

# The arrivals windows, in the order the quick filters offer them.
ARRIVAL_WINDOWS: tuple[tuple[str, StrOrPromise], ...] = (
    ("today", _("Today")),
    ("7", _("7 days")),
    ("30", _("30 days")),
    ("overdue", _("Overdue")),
)

DEFAULT_ARRIVAL_WINDOW = str(ARRIVING_SOON_DAYS)


@dataclass
class ArrivalQueueFilters:
    """The arrivals queue's filter state, parsed once from the query string."""

    window: str = DEFAULT_ARRIVAL_WINDOW
    destination: str = ""
    destination_location: str = ""
    carrier: str = ""
    health: str = ""
    kind: str = ""
    search: str = ""

    @property
    def has_narrowing_filters(self) -> bool:
        """True when something other than the date range is narrowing the list.

        Kept apart from the window because the two lead to different answers when
        nothing matches. An empty week is "no arrivals in this period, try a wider
        range"; an empty week with a carrier chosen is "nothing matches these
        filters". Telling somebody to widen the date range when the problem is the
        carrier they picked sends them the wrong way.
        """
        return bool(
            self.destination or self.destination_location or self.carrier or self.health or self.kind or self.search
        )

    @property
    def destination_location_id(self) -> int | None:
        """The chosen canonical destination as an id, or None.

        A hand-edited value that is not a number narrows nothing rather than
        erroring: the filter arrives from a query string, and a broken link should
        show the unfiltered queue, not a 500.
        """
        try:
            return int(self.destination_location)
        except TypeError, ValueError:
            return None

    @property
    def is_active(self) -> bool:
        """True when anything at all is narrowing the list, the window included.

        Drives the Clear button. The window is always set, so it only counts here
        when it is not the default — otherwise the page would offer to clear a
        filter nobody applied.
        """
        return self.has_narrowing_filters or self.window != DEFAULT_ARRIVAL_WINDOW

    @property
    def window_label(self) -> str:
        return str(dict(ARRIVAL_WINDOWS).get(self.window, self.window))


@dataclass
class ArrivalGroup:
    """One day's arrivals.

    Grouped by date and not by hour: the ETA the domain holds is a date, and only a
    carrier forecast carries a time. Items that have one show it on their own row.
    """

    day: date
    items: list[VisibilityObject] = field(default_factory=list)

    @property
    def is_today(self) -> bool:
        return self.day == timezone.localdate()

    @property
    def is_tomorrow(self) -> bool:
        return self.day == timezone.localdate() + timedelta(days=1)

    @property
    def is_overdue(self) -> bool:
        return self.day < timezone.localdate()

    @property
    def label(self) -> str:
        if self.is_today:
            return str(_("Today"))
        if self.is_tomorrow:
            return str(_("Tomorrow"))
        return ""

    @property
    def container_count(self) -> int:
        return sum(obj.container_count for obj in self.items)


@dataclass
class ArrivalQueue:
    """Everything the arrivals page renders."""

    groups: list[ArrivalGroup] = field(default_factory=list)
    filters: ArrivalQueueFilters = field(default_factory=ArrivalQueueFilters)
    carrier_choices: list[str] = field(default_factory=list)
    destination_choices: list[str] = field(default_factory=list)
    destination_location_choices: list = field(default_factory=list)

    @property
    def objects(self) -> list[VisibilityObject]:
        return [obj for group in self.groups for obj in group.items]

    @property
    def total(self) -> int:
        return len(self.objects)

    @property
    def container_count(self) -> int:
        return sum(group.container_count for group in self.groups)

    @property
    def health_choices(self):
        return Health.choices

    @property
    def kind_choices(self):
        return ObjectKind.choices

    @property
    def windows(self):
        return ARRIVAL_WINDOWS


def parse_arrival_queue_filters(params) -> ArrivalQueueFilters:
    """Read the arrivals filter state, defaulting to the next seven days.

    An unrecognised window falls back to the default rather than showing everything:
    a hand-edited URL should not silently turn a planning view into a full list.
    """
    window = (params.get("window") or "").strip()
    if window not in dict(ARRIVAL_WINDOWS):
        window = DEFAULT_ARRIVAL_WINDOW
    return ArrivalQueueFilters(
        window=window,
        destination=(params.get("destination") or "").strip(),
        destination_location=(params.get("destination_location") or "").strip(),
        carrier=(params.get("carrier") or "").strip(),
        health=(params.get("health") or "").strip(),
        kind=(params.get("kind") or "").strip(),
        search=(params.get("search") or "").strip(),
    )


def get_arrival_queue(team: Team, filters: ArrivalQueueFilters | None = None) -> ArrivalQueue:
    """Return what is expected to arrive for *team*, grouped by day."""
    filters = filters or ArrivalQueueFilters()
    objects = list_visibility_objects(team)
    in_window = drop_arrived(team, filter_by_eta_window(objects, filters.window))

    return ArrivalQueue(
        groups=_group_by_day(_filter_arrivals(in_window, filters, team=team)),
        filters=filters,
        # Offered from everything in the window rather than from the filtered
        # result, so choosing a carrier does not remove the other carriers from the
        # dropdown that was just used to choose it.
        carrier_choices=text_choices({obj.carrier_name for obj in in_window if obj.carrier_name}, filters.carrier),
        destination_choices=text_choices(
            {obj.destination for obj in in_window if obj.destination}, filters.destination
        ),
        destination_location_choices=_destination_location_choices(team, in_window, filters),
    )


def _filter_arrivals(
    objects: list[VisibilityObject], filters: ArrivalQueueFilters, *, team: Team
) -> list[VisibilityObject]:
    result = objects
    if filters.destination:
        result = [obj for obj in result if obj.destination == filters.destination]
    if (location_id := filters.destination_location_id) is not None:
        wanted = _destination_subtree_ids(team, location_id)
        result = [obj for obj in result if obj.destination_location_id in wanted]
    if filters.carrier:
        result = [obj for obj in result if obj.carrier_name == filters.carrier]
    if filters.health:
        result = [obj for obj in result if obj.health == filters.health]
    if filters.kind:
        result = [obj for obj in result if obj.kind == filters.kind]
    if filters.search:
        needle = filters.search.lower()
        result = [obj for obj in result if matches_search(obj, needle)]
    return result


def drop_arrived(team: Team, objects: list[VisibilityObject]) -> list[VisibilityObject]:
    """Remove what has physically arrived at its canonical destination.

    An object stays when *any* of its containers has not yet been accepted into the
    destination: a shipment of twenty boxes with nineteen gated in is still an
    arrival somebody is waiting on, and dropping it at the first gate-in would hide
    the one that matters. Only when every container has arrived does the whole thing
    stop being expected.

    An object with no canonical destination is untouched. There is nothing to have
    arrived *at*, and inferring one from the reported text is the guess this layer
    refuses to make.

    Two queries plus the subtree walks, whatever the size of the queue: the arrival
    lookup is done once for every container and every destination together.
    """
    from apps.scm.containers.movements import arrivals_by_location

    routed = [obj for obj in objects if obj.destination_location_id is not None]
    if not routed:
        return objects

    # A destination's own subtree counts, for the same reason it does when filtering:
    # a box gated into Oceanterminalen has arrived at the Göteborg port it sits in.
    # Non-NULL: `routed` is exactly the objects that have one.
    destination_ids = {cast(int, obj.destination_location_id) for obj in routed}
    subtrees = {location_id: _destination_subtree_ids(team, location_id) for location_id in destination_ids}
    container_ids = [container.pk for obj in routed for container in obj.containers]
    all_location_ids = set().union(*subtrees.values()) if subtrees else set()

    arrived_by_location = arrivals_by_location(team, container_ids, all_location_ids)

    def has_arrived(obj: VisibilityObject) -> bool:
        wanted = subtrees.get(cast(int, obj.destination_location_id)) or set()
        containers = obj.containers
        if not containers:
            # Nothing to have arrived. A shipment with no containers linked yet is
            # still an expectation.
            return False
        return all(arrived_by_location.get(container.pk, set()) & wanted for container in containers)

    return [obj for obj in objects if obj.destination_location_id is None or not has_arrived(obj)]


def _destination_subtree_ids(team: Team, location_id: int) -> set[int]:
    """The chosen location together with everything inside it, as ids.

    Team-scoped by the lookup itself, so a location id belonging to another tenant
    matches nothing rather than that tenant's subtree.
    """
    from apps.scm.containers.models import ContainerLocation
    from apps.scm.containers.selectors import get_location_subtree_ids

    location = ContainerLocation.objects.filter(team=team, pk=location_id).first()
    if location is None:
        return set()
    return set(get_location_subtree_ids(team=team, location=location))


def _destination_location_choices(team: Team, in_window: list[VisibilityObject], filters: ArrivalQueueFilters) -> list:
    """The canonical destinations the dropdown offers.

    Only locations something in the window is actually routed to, plus whatever is
    currently selected — a filtered link can outlive the shipment that justified it,
    and a page reading "Any destination" while showing an empty filtered list would
    leave nothing for the empty state to point at.
    """
    from apps.scm.containers.models import ContainerLocation

    ids = {obj.destination_location_id for obj in in_window if obj.destination_location_id}
    if (selected := filters.destination_location_id) is not None:
        ids.add(selected)
    if not ids:
        return []
    return list(
        ContainerLocation.objects.filter(team=team, pk__in=ids).select_related("parent_location").order_by("name")
    )


def _group_by_day(objects: list[VisibilityObject]) -> list[ArrivalGroup]:
    """Bucket objects by ETA date, earliest day first.

    Within a day, the ones the carrier gave a time for come first and in time order,
    then the rest by label. Nothing is given a time it does not have.
    """
    by_day: dict[date, list[VisibilityObject]] = {}
    for obj in objects:
        if obj.current_eta is None:
            continue
        by_day.setdefault(obj.current_eta, []).append(obj)

    return [ArrivalGroup(day=day, items=sorted(by_day[day], key=_within_day_key)) for day in sorted(by_day)]


def _within_day_key(obj: VisibilityObject) -> tuple:
    at = obj.current_eta_at
    return (at is None, timezone.localtime(at).time() if at else time.min, obj.label)
