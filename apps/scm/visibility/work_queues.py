"""The operational work queues: what needs attention, and what is arriving.

Both queues are compositions over :func:`~apps.scm.visibility.selectors.list_visibility_objects`.
Nothing here decides that something is wrong, that a date has moved, or that a box
is late: the exception engine and the delay engine remain the only things that
decide those, and this module turns their findings into rows, bands, groups and a
sort order.

**Why there is no severity.** The domain has no SLA, no cargo value and no impact
model, so ranking a customs hold "high" against port congestion "medium" would be
a judgment the platform has never made and cannot defend. What the code *can*
distinguish is where a finding came from, and that turns out to be the useful
split:

* ``EXCEPTION`` — a carrier reported an event: a hold, a rollover, congestion.
* ``DELAY`` — a date moved, or an arrival is missing. The delay engine's verdict.
* ``TRACKING`` — no carrier event for days. Derived from the *absence* of data,
  which is a different kind of claim from the two above and belongs below them.

That ordering is the existing attention queue's rule — exceptions before delays —
with the tracking gap separated out from the exceptions it was previously mixed
into. See :attr:`~apps.scm.visibility.selectors.VisibilityOverview.needs_attention`.

**No workflow state.** There is no acknowledge, assign, resolve, snooze or owner,
because none of those exist in the database. A queue is a view of current supply
chain state, so an item leaves it when the state changes and not before.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, time, timedelta

from django.db.models import TextChoices
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.teams.models import Team

from .read_models import Health, ObjectKind, VisibilityObject
from .selectors import ARRIVING_SOON_DAYS, filter_by_eta_window, list_visibility_objects, matches_search

# The delay engine's finding, given an issue type so it can sit in one list beside
# the exception engine's. It is not an exception code and the engine never emits it.
DELAY_ISSUE = "delay"


class IssueBand(TextChoices):
    """How much of a claim an issue is, by where the finding came from.

    Not a severity. See the module docstring: this is the one ranking the domain
    can actually justify.
    """

    EXCEPTION = "exception", _("Exception")
    DELAY = "delay", _("Delay")
    TRACKING = "tracking", _("Tracking")


# Every issue type a queue can show, with the words to show it in. Keys are the
# exception engine's own codes plus DELAY_ISSUE — this table maps them, it does not
# add to them, and a code the engine stops emitting simply stops appearing.
ISSUE_LABELS: dict[str, str] = {
    "customs_hold": _("Customs hold"),
    "rolled": _("Rolled"),
    "port_congestion": _("Port congestion"),
    DELAY_ISSUE: _("Delayed"),
    "missing_event": _("Tracking stale"),
}

_BAND_BY_ISSUE: dict[str, str] = {
    "customs_hold": IssueBand.EXCEPTION,
    "rolled": IssueBand.EXCEPTION,
    "port_congestion": IssueBand.EXCEPTION,
    DELAY_ISSUE: IssueBand.DELAY,
    "missing_event": IssueBand.TRACKING,
}

_BAND_ORDER: dict[str, int] = {
    IssueBand.EXCEPTION: 0,
    IssueBand.DELAY: 1,
    IssueBand.TRACKING: 2,
}

# The arrivals windows, in the order the quick filters offer them.
ARRIVAL_WINDOWS: tuple[tuple[str, str], ...] = (
    ("today", _("Today")),
    ("7", _("7 days")),
    ("30", _("30 days")),
    ("overdue", _("Overdue")),
)

DEFAULT_ARRIVAL_WINDOW = str(ARRIVING_SOON_DAYS)

# Far enough ahead that an item with no ETA sorts last without needing a branch.
_NO_ETA = date.max


@dataclass(frozen=True)
class QueueIssue:
    """One thing wrong with one object, in words, with where it came from."""

    issue_type: str
    detail: str

    @property
    def label(self) -> str:
        return str(ISSUE_LABELS.get(self.issue_type, self.issue_type.replace("_", " ").title()))

    @property
    def band(self) -> str:
        # An exception code this table has not been told about is still an
        # exception: the carrier reported something. Better to show it in the top
        # band than to hide a finding because the label table is out of date.
        return _BAND_BY_ISSUE.get(self.issue_type, IssueBand.EXCEPTION)

    @property
    def band_label(self) -> str:
        return str(IssueBand(self.band).label)


@dataclass
class QueueItem:
    """One row of a work queue: an object, and why it is in the queue.

    An object appears once however many things are wrong with it. ``primary_issue``
    is the one the row leads with — the highest band — and the rest stay visible as
    ``other_issues`` rather than being dropped, because a box that is both held and
    stale is one thing to work with two facts about it.
    """

    object: VisibilityObject
    issues: list[QueueIssue] = field(default_factory=list)

    @property
    def key(self) -> str:
        return self.object.key

    @property
    def primary_issue(self) -> QueueIssue | None:
        return self.issues[0] if self.issues else None

    @property
    def other_issues(self) -> list[QueueIssue]:
        return self.issues[1:]

    @property
    def issue_types(self) -> list[str]:
        return [issue.issue_type for issue in self.issues]

    @property
    def band(self) -> str:
        primary = self.primary_issue
        return primary.band if primary else IssueBand.TRACKING

    @property
    def sort_key(self) -> tuple:
        """Band first, then the soonest arrival, then the label.

        Soonest ETA second because that is what makes a queue actionable: a held box
        arriving on Thursday has to be worked before one arriving in three weeks.
        Items with no ETA sort last within their band rather than first.
        """
        return (
            _BAND_ORDER.get(self.band, len(_BAND_ORDER)),
            self.object.current_eta or _NO_ETA,
            self.object.label,
        )


@dataclass
class ExceptionQueueFilters:
    """The exceptions queue's filter state, parsed once from the query string."""

    issue: str = ""
    carrier: str = ""
    kind: str = ""
    eta_window: str = ""
    search: str = ""

    @property
    def is_active(self) -> bool:
        return bool(self.issue or self.carrier or self.kind or self.eta_window or self.search)


@dataclass
class ExceptionQueue:
    """Everything the exceptions page renders."""

    items: list[QueueItem] = field(default_factory=list)
    filters: ExceptionQueueFilters = field(default_factory=ExceptionQueueFilters)
    carrier_choices: list[str] = field(default_factory=list)
    issue_choices: list[tuple[str, str]] = field(default_factory=list)
    # How many items need attention before any filter — the number the page header
    # and the Control Tower are talking about.
    total: int = 0

    @property
    def kind_choices(self):
        return ObjectKind.choices


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


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def parse_exception_queue_filters(params) -> ExceptionQueueFilters:
    return ExceptionQueueFilters(
        issue=(params.get("issue") or "").strip(),
        carrier=(params.get("carrier") or "").strip(),
        kind=(params.get("kind") or "").strip(),
        eta_window=(params.get("eta") or "").strip(),
        search=(params.get("search") or "").strip(),
    )


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


# ---------------------------------------------------------------------------
# The queues
# ---------------------------------------------------------------------------


def get_exception_queue(team: Team, filters: ExceptionQueueFilters | None = None) -> ExceptionQueue:
    """Return what currently needs operational attention for *team*.

    One pass over the team's visibility objects, so the query count is the overview's
    and does not grow with the number of items in the queue.
    """
    filters = filters or ExceptionQueueFilters()
    objects = list_visibility_objects(team)
    items = [item for item in (_queue_item(obj) for obj in objects) if item.issues]
    items.sort(key=lambda item: item.sort_key)

    return ExceptionQueue(
        items=_filter_exception_items(items, filters),
        filters=filters,
        carrier_choices=sorted({item.object.carrier_name for item in items if item.object.carrier_name}),
        issue_choices=_issue_choices(items),
        total=len(items),
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
        carrier_choices=sorted({obj.carrier_name for obj in in_window if obj.carrier_name}),
        destination_choices=sorted({obj.destination for obj in in_window if obj.destination}),
    )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _queue_item(obj: VisibilityObject) -> QueueItem:
    """Turn one object's exception and delay findings into a queue row.

    Both engines are read, neither is re-implemented. The delay is only listed when
    the delay engine says so, and its wording is the engine's own reason.
    """
    issues = [QueueIssue(issue_type=issue.exception_type, detail=issue.detail) for issue in obj.exception_issues]
    if obj.is_delayed:
        issues.append(QueueIssue(issue_type=DELAY_ISSUE, detail=_delay_detail(obj)))
    issues.sort(key=lambda issue: _BAND_ORDER.get(issue.band, len(_BAND_ORDER)))
    return QueueItem(object=obj, issues=issues)


def _delay_detail(obj: VisibilityObject) -> str:
    """The delay engine's verdict, with the drift it measured where there is one."""
    reason = obj.delay_reason
    if obj.delay_days > 0 and reason:
        return f"{reason} · +{obj.delay_days}d"
    return reason


def _issue_choices(items: list[QueueItem]) -> list[tuple[str, str]]:
    """The issue types actually present, in ISSUE_LABELS order.

    Only what is in the queue: a filter offering "Rolled" when nothing has rolled
    invites a click that can only return an empty list.
    """
    present = {issue_type for item in items for issue_type in item.issue_types}
    return [(issue_type, str(label)) for issue_type, label in ISSUE_LABELS.items() if issue_type in present]


def _filter_exception_items(items: list[QueueItem], filters: ExceptionQueueFilters) -> list[QueueItem]:
    result = items
    if filters.issue:
        result = [item for item in result if filters.issue in item.issue_types]
    if filters.carrier:
        result = [item for item in result if item.object.carrier_name == filters.carrier]
    if filters.kind:
        result = [item for item in result if item.object.kind == filters.kind]
    if filters.eta_window:
        # Reuses the Control Tower's window rule, applied to the rows' objects.
        in_window = {obj.key for obj in filter_by_eta_window([item.object for item in result], filters.eta_window)}
        result = [item for item in result if item.key in in_window]
    if filters.search:
        needle = filters.search.lower()
        result = [item for item in result if matches_search(item.object, needle)]
    return result


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
