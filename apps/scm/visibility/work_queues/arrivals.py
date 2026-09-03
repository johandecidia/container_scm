"""The arrivals queue: what is expected to arrive, when, where and in what condition.

Grouped by day, because a date is what the domain actually holds. Shipments and
standalone containers are both first class — a container that belongs to no shipment
is not wrapped in an invented one to make the list uniform.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, time, timedelta
from typing import TYPE_CHECKING

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
        return bool(self.destination or self.carrier or self.health or self.kind or self.search)

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
        carrier=(params.get("carrier") or "").strip(),
        health=(params.get("health") or "").strip(),
        kind=(params.get("kind") or "").strip(),
        search=(params.get("search") or "").strip(),
    )


def get_arrival_queue(team: Team, filters: ArrivalQueueFilters | None = None) -> ArrivalQueue:
    """Return what is expected to arrive for *team*, grouped by day."""
    filters = filters or ArrivalQueueFilters()
    objects = list_visibility_objects(team)
    in_window = filter_by_eta_window(objects, filters.window)

    return ArrivalQueue(
        groups=_group_by_day(_filter_arrivals(in_window, filters)),
        filters=filters,
        # Offered from everything in the window rather than from the filtered
        # result, so choosing a carrier does not remove the other carriers from the
        # dropdown that was just used to choose it.
        carrier_choices=text_choices({obj.carrier_name for obj in in_window if obj.carrier_name}, filters.carrier),
        destination_choices=text_choices(
            {obj.destination for obj in in_window if obj.destination}, filters.destination
        ),
    )


def _filter_arrivals(objects: list[VisibilityObject], filters: ArrivalQueueFilters) -> list[VisibilityObject]:
    result = objects
    if filters.destination:
        result = [obj for obj in result if obj.destination == filters.destination]
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
