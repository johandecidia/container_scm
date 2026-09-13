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
of the queue. Since LOC-3 that is not a rule of this module: it is
:attr:`~apps.scm.visibility.arrival_lifecycle.ArrivalProgress.has_arrived`, read off
the one arrival lifecycle interpreter, so the queue, the Control Tower, the
workspaces and the attention list cannot disagree about whether a given box is still
coming. This page decides only which lifecycle states belong on it.

**The default queue is what is outstanding.** EXPECTED and ARRIVING. Choosing a
state explicitly reaches past that — ``?state=arrived`` is how somebody finds what
has landed and still has to be received — so completed arrivals are available
without the planning view filling up with them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, time, timedelta
from typing import TYPE_CHECKING

from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.teams.models import Team

from ..arrival_lifecycle import OUTSTANDING_STATES, ArrivalState, destination_subtree_ids
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
    # A lifecycle state, when one has been chosen. Empty means the page's own
    # default — what is still outstanding — rather than "every state".
    state: str = ""
    search: str = ""

    @property
    def shows_completed_arrivals(self) -> bool:
        """True when a state has been chosen that the outstanding view excludes.

        The one filter that widens the queue rather than narrowing it. Asking for
        ARRIVED or RECEIVED is asking to see past the planning view, and the queue
        has to stop dropping them before the filter can match anything.
        """
        return self.state in {ArrivalState.ARRIVED, ArrivalState.RECEIVED}

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
            self.destination
            or self.destination_location
            or self.carrier
            or self.health
            or self.kind
            or self.state
            or self.search
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

    @property
    def state_label(self) -> str:
        """The chosen lifecycle state in words, or "" when none is chosen."""
        return str(ArrivalState(self.state).label) if self.state in ArrivalState.values else ""


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
    def state_choices(self):
        return ArrivalState.choices

    @property
    def windows(self):
        return ARRIVAL_WINDOWS

    @property
    def overdue_count(self) -> int:
        """How many of these rows should already have been here."""
        return sum(1 for obj in self.objects if obj.is_arrival_overdue)


def parse_arrival_queue_filters(params) -> ArrivalQueueFilters:
    """Read the arrivals filter state, defaulting to the next seven days.

    An unrecognised window falls back to the default rather than showing everything:
    a hand-edited URL should not silently turn a planning view into a full list.
    """
    window = (params.get("window") or "").strip()
    if window not in dict(ARRIVAL_WINDOWS):
        window = DEFAULT_ARRIVAL_WINDOW
    # An unrecognised lifecycle state narrows nothing rather than erroring, for the
    # same reason a broken window falls back: the value arrives from a query string.
    state = (params.get("state") or "").strip()
    if state not in ArrivalState.values:
        state = ""
    return ArrivalQueueFilters(
        window=window,
        destination=(params.get("destination") or "").strip(),
        destination_location=(params.get("destination_location") or "").strip(),
        carrier=(params.get("carrier") or "").strip(),
        health=(params.get("health") or "").strip(),
        kind=(params.get("kind") or "").strip(),
        state=state,
        search=(params.get("search") or "").strip(),
    )


def get_arrival_queue(team: Team, filters: ArrivalQueueFilters | None = None) -> ArrivalQueue:
    """Return what is expected to arrive for *team*, grouped by day."""
    filters = filters or ArrivalQueueFilters()
    objects = list_visibility_objects(team)
    in_window = _outstanding(filter_by_eta_window(objects, filters.window), filters)

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
        wanted = destination_subtree_ids(team, location_id)
        result = [obj for obj in result if obj.destination_location_id in wanted]
    if filters.carrier:
        result = [obj for obj in result if obj.carrier_name == filters.carrier]
    if filters.health:
        result = [obj for obj in result if obj.health == filters.health]
    if filters.kind:
        result = [obj for obj in result if obj.kind == filters.kind]
    if filters.state:
        result = [obj for obj in result if obj.arrival_state == filters.state]
    if filters.search:
        needle = filters.search.lower()
        result = [obj for obj in result if matches_search(obj, needle)]
    return result


def _outstanding(objects: list[VisibilityObject], filters: ArrivalQueueFilters) -> list[VisibilityObject]:
    """Narrow to what is still coming — unless a completed state was asked for.

    The rule itself belongs to the arrival lifecycle, which is why this reads
    ``arrival_state`` instead of comparing movements or current locations. An object
    stays while *any* of its containers is outstanding: a shipment of twenty boxes
    with nineteen gated in is still an arrival somebody is waiting on, and the
    lifecycle's roll-up already takes the least advanced container's word for it.

    An object with no canonical destination stays too. There is nothing for it to
    have arrived at, so its lifecycle can never leave EXPECTED or ARRIVING —
    inferring a destination from the booking text is the guess the canonical layer
    exists to prevent, and dropping it silently would lose a real expectation.
    """
    if filters.shows_completed_arrivals:
        return objects
    return [obj for obj in objects if obj.arrival_state in OUTSTANDING_STATES]


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
