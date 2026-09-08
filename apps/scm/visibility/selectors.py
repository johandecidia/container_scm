"""Reads for the supply chain visibility layer.

Everything here composes existing read models — the container workspace, the
shipment selectors, the tracking position and timeline helpers, the delay and
exception engines — into the objects the overview and the detail maps draw.

Two rules shape the queries:

**A fixed number of them.** The overview can cover every tracked box a team has,
so nothing is allowed to run per object. The container workspaces arrive from one
bulk builder, and the two remaining per-object questions — "does a DELAY event
exist" and "what do this container's events say about exceptions" — are answered
in one query each for the whole page.

**Current state, not history.** The overview says where things are now. Full event
history is loaded only by the shipment and container journey maps, which are about
one object at a time.

**One selection, read twice.** The Control Tower's board and its map are two
renderings of the same filtered list — see :class:`VisibilityView` and
:func:`apply_view`. Neither fetches its own, which is what stops a filter change
narrowing the list while the map keeps drawing the whole fleet.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import cast

from django.db.models import Q, TextChoices
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.scm.containers.models import Container
from apps.scm.containers.workspace import get_container_workspaces
from apps.scm.shipments.models import Shipment, ShipmentContainer
from apps.scm.tracking.delay_detection import evaluate_shipment_delay
from apps.scm.tracking.exception_detection import (
    ExceptionReport,
    evaluate_container_exceptions,
    merge_exception_reports,
)
from apps.scm.tracking.models import ETAHistory, TrackingEvent, TrackingSubscription
from apps.teams.models import Team

from .arrival_lifecycle import ArrivalQuestion, get_arrival_progress
from .read_models import Health, JourneyState, ObjectKind, VisibilityObject

# Shipments worth watching: everything that has left draft and has not finished.
ACTIVE_SHIPMENT_STATUSES = (
    Shipment.Status.BOOKED,
    Shipment.Status.IN_TRANSIT,
    Shipment.Status.ARRIVED,
    Shipment.Status.PARTIALLY_RECEIVED,
    Shipment.Status.EXCEPTION,
)

# "Arriving soon" window, in days, for the overview statistic and filter.
ARRIVING_SOON_DAYS = 7

# Only the fields the exception engine reads, so scanning every event of every
# tracked container on the overview stays cheap.
_EXCEPTION_FIELDS = (
    "container_id",
    "event_type",
    "event_code",
    "description",
    "status",
    "location_name",
    "event_datetime",
)


class VisibilityView(TextChoices):
    """Which operational question the Control Tower is answering right now.

    Three views over one read model, not three pages. The board, the map and the
    counts are all built from whichever of these is selected, which is what stops a
    filter narrowing the list while the map keeps drawing the fleet.

    ``TRACKING`` is the default and leads on purpose. "What are we watching, and what
    is coming next" is the question an operator opens this page with; exceptions and
    delays are what they turn to when something is wrong, and a board that opened on
    them described the platform as a list of problems.
    """

    TRACKING = "tracking", _("Tracking")
    EXCEPTIONS = "exceptions", _("Exceptions")
    DELAYED = "delayed", _("Delayed")


# What ``view`` means when it is not set at all: every object, narrowed by nothing.
#
# Not reachable from the UI, and not the default a request gets — see
# :func:`parse_visibility_filters`, which supplies TRACKING for a URL that names no
# view. It is the honest default for the *filter object*, so that
# ``VisibilityFilters()`` still means "no filters applied" for the callers that
# compose these reads rather than serving them.
VIEW_ALL = ""


@dataclass
class VisibilityFilters:
    """The overview's filter state, parsed once from the query string."""

    # The operational view. One of :class:`VisibilityView`, or :data:`VIEW_ALL`.
    view: str = VIEW_ALL
    status: str = ""
    carrier: str = ""
    eta_window: str = ""
    search: str = ""

    @property
    def is_active(self) -> bool:
        """True when the board is showing something narrower than its default.

        The Tracking view does not count. It is where the page starts, so treating it
        as an active filter would offer a Clear button that clears nothing and label
        an empty fleet as an over-narrow search.
        """
        return bool(self.status or self.carrier or self.eta_window or self.search or self.is_narrowed_view)

    @property
    def is_narrowed_view(self) -> bool:
        return self.view not in (VIEW_ALL, VisibilityView.TRACKING)

    @property
    def selected_view(self) -> str:
        """The view a control should render as chosen — TRACKING where none was."""
        return self.view or VisibilityView.TRACKING


@dataclass
class VisibilityOverview:
    """Everything the overview page renders.

    Two lists, and the difference between them is the whole of how the view filter
    works. ``objects`` is what the board and the map show — the selected view, with
    every other filter applied. ``universe`` is everything before any of that, and it
    is what the view counts are taken from: a Tracking chip reading the length of the
    list it is already showing would say the same number whichever view was selected,
    and an Exceptions count of nothing while three exceptions sit one click away is
    worse than no count at all.
    """

    objects: list[VisibilityObject] = field(default_factory=list)
    filters: VisibilityFilters = field(default_factory=VisibilityFilters)
    carrier_choices: list[str] = field(default_factory=list)

    # Every object the team has, before the view and the filters narrowed anything.
    universe: list[VisibilityObject] = field(default_factory=list)

    @property
    def active_shipments(self) -> int:
        return sum(1 for obj in self.objects if obj.kind == ObjectKind.SHIPMENT)

    # -- the three views -------------------------------------------------------
    #
    # Each reads one existing verdict and adds no definition of its own: the tracking
    # domain decides what being watched means, the exception engine what an exception
    # is, and the delay engine what a delay is. All three are taken from `universe`,
    # so the numbers describe the views a click would switch to rather than the one
    # already on screen.

    @property
    def tracking_objects(self) -> list[VisibilityObject]:
        return [obj for obj in self.universe if obj.is_actively_tracked]

    @property
    def tracking_container_count(self) -> int:
        """Distinct containers under a live watch.

        Containers rather than watches, and distinct rather than summed: a box
        tracked through an aggregator that also carries an older direct watch is one
        container being tracked, and counting subscriptions would report two. It is
        also why this is not a sum over the objects — a container can be reached
        through more than one of them.
        """
        return len({container.pk for obj in self.tracking_objects for container in obj.containers})

    @property
    def delayed_total(self) -> int:
        return sum(1 for obj in self.universe if obj.is_delayed)

    @property
    def exception_total(self) -> int:
        return sum(1 for obj in self.universe if obj.has_exception)

    @property
    def view(self) -> str:
        return self.filters.selected_view

    @property
    def view_label(self) -> str:
        return str(VisibilityView(self.view).label)

    @property
    def view_choices(self) -> list[tuple[str, str, int]]:
        """The three view buttons: value, label, and how much is in each.

        Built here rather than in the template so the order and the counts are the
        read model's answer, and Tracking leads wherever this is rendered.
        """
        counts = {
            VisibilityView.TRACKING: len(self.tracking_objects),
            VisibilityView.EXCEPTIONS: self.exception_total,
            VisibilityView.DELAYED: self.delayed_total,
        }
        return [(value, str(VisibilityView(value).label), counts[value]) for value in VisibilityView.values]

    @property
    def arriving_soon(self) -> list[VisibilityObject]:
        """What is still coming in the next week, soonest first.

        Since LOC-3 this is what the arrival lifecycle says is outstanding, not
        simply what has an ETA in the window. A shipment whose boxes were all gated
        in on Monday is not something to plan for on Wednesday, and counting it here
        made the card overstate the work — the Control Tower and the arrivals queue
        now answer the question the same way.
        """
        cutoff = timezone.localdate() + timedelta(days=ARRIVING_SOON_DAYS)
        today = timezone.localdate()
        upcoming = [
            obj
            for obj in self.objects
            if obj.current_eta and today <= obj.current_eta <= cutoff and not obj.has_arrived
        ]
        return sorted(upcoming, key=lambda obj: obj.current_eta)

    @property
    def awaiting_receipt(self) -> list[VisibilityObject]:
        """Physically here, and not yet taken into anybody's records.

        The operational gap LOC-3 makes visible: arrival and receipt are different
        events, and the space between them is where a box sits in a yard that
        nothing has accounted for. Reported as a fact with no threshold attached —
        there is no SLA in the domain to say when the wait becomes a problem.
        """
        return [obj for obj in self.objects if obj.is_awaiting_receipt]

    @property
    def overdue_arrivals(self) -> list[VisibilityObject]:
        """ETA passed, and nothing has physically arrived at the destination."""
        return [obj for obj in self.objects if obj.is_arrival_overdue]

    @property
    def delayed(self) -> list[VisibilityObject]:
        return [obj for obj in self.objects if obj.is_delayed]

    @property
    def exceptions(self) -> list[VisibilityObject]:
        return [obj for obj in self.objects if obj.has_exception]

    @property
    def needs_attention(self) -> list[VisibilityObject]:
        """The attention panel's list: exceptions first, then delays.

        Built from ``objects``, so it describes the view currently on screen. Kept
        as one list beside the two separate views on purpose: a view answers "show me
        the delayed ones", and this answers "what is wrong here", which is a question
        about the selection rather than a way of narrowing it.

        A composition of the two lists above rather than a new idea of what is
        wrong — the exception engine and the delay engine remain the only things
        that decide that. Order is severity: an exception is a thing that has
        happened, a delay is a date that moved.

        An object that is both appears once, under its exception, because a
        customs hold that has pushed the ETA is one problem to work, not two.
        """
        exceptions = self.exceptions
        already_listed = {obj.key for obj in exceptions}
        return exceptions + [obj for obj in self.delayed if obj.key not in already_listed]

    @property
    def status_choices(self):
        return JourneyState.choices

    @property
    def health_choices(self):
        return Health.choices


def parse_visibility_filters(params) -> VisibilityFilters:
    """Read filter state out of a request's query parameters.

    ``view`` is explicit in the URL so a selection can be linked, bookmarked and
    read back off the address bar. A request that names no view — or names one that
    does not exist, which is what a hand-edited or stale link sends — gets Tracking,
    the same forgiveness :func:`~apps.scm.visibility.map_positions.parse_map_filters`
    applies for the same reason: a query string is not a form, and a bad one should
    show the default board rather than a 500.
    """
    return VisibilityFilters(
        view=_parse_view(params),
        status=(params.get("status") or "").strip(),
        carrier=(params.get("carrier") or "").strip(),
        eta_window=(params.get("eta") or "").strip(),
        search=(params.get("search") or "").strip(),
    )


def _parse_view(params) -> str:
    """The view a request is asking for, defaulting to Tracking.

    Links written before the views existed carried the two operational filters as
    flags of their own — ``?exceptions=1``, ``?delayed=1`` — and they are still out
    there in bookmarks and in the KPI cards' own history. They are read as the
    equivalent view rather than left to select nothing, which is what keeps an old
    link showing the list it was saved for.
    """
    requested = (params.get("view") or "").strip()
    if requested in VisibilityView.values:
        return requested
    if params.get("exceptions") == "1":
        return VisibilityView.EXCEPTIONS
    if params.get("delayed") == "1":
        return VisibilityView.DELAYED
    return VisibilityView.TRACKING


def get_visibility_overview(team: Team, filters: VisibilityFilters | None = None) -> VisibilityOverview:
    """Return the composed overview for a team, with the view and filters applied."""
    filters = filters or VisibilityFilters()
    objects = list_visibility_objects(team)
    return VisibilityOverview(
        objects=_apply_filters(objects, filters),
        filters=filters,
        carrier_choices=sorted({obj.carrier_name for obj in objects if obj.carrier_name}),
        universe=objects,
    )


def list_visibility_objects(team: Team) -> list[VisibilityObject]:
    """Return every shipment and standalone tracked container worth showing.

    A container on an active shipment is folded into that shipment, so twenty boxes
    on one vessel are one object rather than twenty. A container tracked without a
    shipment — or whose only shipment is finished or still a draft — stands on its
    own, because the carrier is telling us about it either way.
    """
    shipments = list(
        Shipment.objects.filter(team=team, status__in=ACTIVE_SHIPMENT_STATUSES)
        # The canonical locations come with the shipment: the arrivals queue reads a
        # destination for every row, and following the FK per object would make the
        # page's query count grow with the number of shipments on it.
        .select_related("origin_location", "destination_location")
        .order_by("eta", "-created_at")
    )
    shipment_ids = [shipment.pk for shipment in shipments]

    links = list(
        ShipmentContainer.objects.filter(shipment_id__in=shipment_ids, shipment__team=team).order_by(
            "sequence", "created_at"
        )
    )
    grouped_container_ids: dict[int, list[int]] = {}
    for link in links:
        grouped_container_ids.setdefault(link.shipment_id, []).append(link.container_id)
    on_active_shipment = {link.container_id for link in links}

    tracked_container_ids = set(
        TrackingSubscription.objects.filter(team=team, container__isnull=False)
        .exclude(status=TrackingSubscription.Status.CANCELLED)
        .values_list("container_id", flat=True)
    )
    standalone_ids = tracked_container_ids - on_active_shipment

    container_ids = sorted(on_active_shipment | standalone_ids)
    containers = Container.objects.filter(team=team, pk__in=container_ids).select_related(
        "equipment_type", "current_location"
    )
    workspaces = get_container_workspaces(team, containers)

    exceptions = _exception_reports(team, container_ids)
    delay_event_shipment_ids = set(
        TrackingEvent.objects.filter(
            team=team, shipment_id__in=shipment_ids, event_type=TrackingEvent.EventType.DELAY
        ).values_list("shipment_id", flat=True)
    )

    objects: list[VisibilityObject] = []
    for shipment in shipments:
        members = [workspaces[cid] for cid in grouped_container_ids.get(shipment.pk, []) if cid in workspaces]
        objects.append(
            VisibilityObject(
                kind=ObjectKind.SHIPMENT,
                shipment=shipment,
                workspaces=members,
                delay=evaluate_shipment_delay(shipment, has_delay_event=shipment.pk in delay_event_shipment_ids),
                exceptions=merge_exception_reports(exceptions.get(w.container.pk) for w in members),
            )
        )

    for container_id in sorted(standalone_ids):
        workspace = workspaces.get(container_id)
        if workspace is None:
            continue
        objects.append(
            VisibilityObject(
                kind=ObjectKind.CONTAINER,
                shipment=workspace.active_shipment,
                workspaces=[workspace],
                delay=None,
                exceptions=exceptions.get(container_id) or ExceptionReport(has_exception=False),
            )
        )

    attach_arrival_progress(team, objects)
    return objects


def attach_arrival_progress(team: Team, objects: list[VisibilityObject]) -> list[VisibilityObject]:
    """Interpret every object's inbound arrival and hang the answer on it.

    Done here, once, for whatever list was just built: the arrivals queue, the
    Control Tower, the attention queue and the workspaces all read the same
    ``ArrivalProgress`` instances, so none of them can develop its own idea of what
    has arrived. One extra query for the whole page, whatever its length.

    The ETA is handed over rather than re-derived — ``current_eta`` is already the
    read model's answer about which of a shipment's date and a carrier's forecast to
    believe, anchored to *this* object's shipment.
    """
    if not objects:
        return objects
    questions = [
        ArrivalQuestion(
            containers=obj.containers,
            shipment=obj.shipment,
            eta=obj.current_eta,
            eta_at=obj.current_eta_at,
        )
        for obj in objects
    ]
    for obj, progress in zip(objects, get_arrival_progress(team, questions), strict=True):
        obj.arrival = progress
    return objects


def get_shipment_visibility(team: Team, shipment: Shipment) -> VisibilityObject:
    """Return the visibility read model for one shipment."""
    container_ids = list(
        ShipmentContainer.objects.filter(shipment=shipment, shipment__team=team)
        .order_by("sequence", "created_at")
        .values_list("container_id", flat=True)
    )
    containers = Container.objects.filter(team=team, pk__in=container_ids).select_related("equipment_type")
    workspaces = get_container_workspaces(team, containers)
    members = [workspaces[cid] for cid in container_ids if cid in workspaces]
    has_delay_event = TrackingEvent.objects.filter(
        team=team, shipment=shipment, event_type=TrackingEvent.EventType.DELAY
    ).exists()
    obj = VisibilityObject(
        kind=ObjectKind.SHIPMENT,
        shipment=shipment,
        workspaces=members,
        delay=evaluate_shipment_delay(shipment, has_delay_event=has_delay_event),
        exceptions=merge_exception_reports(_exception_reports(team, container_ids).get(cid) for cid in container_ids),
    )
    return attach_arrival_progress(team, [obj])[0]


def get_container_visibility(team: Team, container: Container, workspace=None) -> VisibilityObject:
    """Return the visibility read model for one container, shipment or not.

    ``workspace`` lets the container detail view pass the full workspace it already
    built, so the page does not load the same tracking twice.
    """
    if workspace is None:
        workspace = get_container_workspaces(team, [container]).get(container.pk)
    if workspace is None:
        return VisibilityObject(kind=ObjectKind.CONTAINER, exceptions=ExceptionReport(has_exception=False))

    shipment = workspace.active_shipment
    delay = None
    if shipment is not None:
        has_delay_event = TrackingEvent.objects.filter(
            team=team, shipment=shipment, event_type=TrackingEvent.EventType.DELAY
        ).exists()
        delay = evaluate_shipment_delay(shipment, has_delay_event=has_delay_event)
    obj = VisibilityObject(
        kind=ObjectKind.CONTAINER,
        shipment=shipment,
        workspaces=[workspace],
        delay=delay,
        exceptions=_exception_reports(team, [container.pk]).get(container.pk) or ExceptionReport(has_exception=False),
    )
    return attach_arrival_progress(team, [obj])[0]


# ---------------------------------------------------------------------------
# Journey history — one object at a time
# ---------------------------------------------------------------------------


def get_shipment_journey_events(team: Team, shipment: Shipment) -> list[TrackingEvent]:
    """Return a shipment's tracking events oldest first, for its journey map.

    Events reach a shipment either directly or through one of its containers; both
    are included, because a carrier that reports at container level still describes
    this shipment's journey.
    """
    container_ids = ShipmentContainer.objects.filter(shipment=shipment, shipment__team=team).values_list(
        "container_id", flat=True
    )
    return list(
        TrackingEvent.objects.filter(team=team)
        .filter(Q(shipment=shipment) | Q(container_id__in=container_ids))
        .exclude(event_datetime__isnull=True)
        .select_related("provider", "container")
        .order_by("event_datetime", "created_at")
    )


def get_container_journey_events(team: Team, container: Container) -> list[TrackingEvent]:
    """Return a container's tracking events oldest first, for its journey map."""
    return list(
        TrackingEvent.objects.filter(team=team, container=container)
        .exclude(event_datetime__isnull=True)
        .select_related("provider")
        .order_by("event_datetime", "created_at")
    )


def get_shipment_eta_history(team: Team, shipment: Shipment):
    """Return a shipment's ETA changes, oldest first."""
    return list(
        ETAHistory.objects.filter(team=team, shipment=shipment).select_related("tracking_event").order_by("changed_at")
    )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _exception_reports(team: Team, container_ids) -> dict[int, ExceptionReport]:
    """Run the exception engine for many containers off one query."""
    container_ids = list(container_ids)
    if not container_ids:
        return {}

    by_container: dict[int, list] = {}
    events = (
        TrackingEvent.objects.filter(team=team, container_id__in=container_ids)
        .only(*_EXCEPTION_FIELDS)
        .order_by("container_id", "-event_datetime", "-created_at")
    )
    for event in events:
        # Non-NULL: the queryset only asked for events on these containers.
        by_container.setdefault(cast(int, event.container_id), []).append(event)
    return {cid: evaluate_container_exceptions(rows) for cid, rows in by_container.items()}


def _apply_filters(objects: list[VisibilityObject], filters: VisibilityFilters) -> list[VisibilityObject]:
    """Narrow the object list to the selected view, then to the other filters.

    Applied in Python rather than SQL because every one of these values is derived
    from tracking rather than stored — filtering in the database would mean a second
    implementation of the derivations, which is exactly what this layer avoids.
    """
    result = apply_view(objects, filters.view)
    if filters.status:
        result = [obj for obj in result if obj.journey_state == filters.status]
    if filters.carrier:
        result = [obj for obj in result if obj.carrier_name == filters.carrier]
    if filters.eta_window:
        result = filter_by_eta_window(result, filters.eta_window)
    if filters.search:
        result = [obj for obj in result if matches_search(obj, filters.search.lower())]
    return sort_by_arrival(result)


def apply_view(objects: list[VisibilityObject], view: str) -> list[VisibilityObject]:
    """Narrow *objects* to one operational view.

    Three reads of three existing verdicts. Nothing here decides what an exception
    or a delay is — the engines do, and the separate Exceptions queue asks them the
    same question — and nothing here decides what being tracked means either.

    Exceptions and Delayed are separate datasets rather than one attention list: an
    object that is both appears in both, because the two views are asking different
    questions about it. The combined list is still
    :attr:`VisibilityOverview.needs_attention`, which is what the attention panel and
    the Exceptions queue read.

    An unrecognised view narrows nothing rather than raising, for the same reason
    the parser forgives one.
    """
    if view == VisibilityView.TRACKING:
        return [obj for obj in objects if obj.is_actively_tracked]
    if view == VisibilityView.EXCEPTIONS:
        return [obj for obj in objects if obj.has_exception]
    if view == VisibilityView.DELAYED:
        return [obj for obj in objects if obj.is_delayed]
    return list(objects)


def sort_by_arrival(objects: list[VisibilityObject]) -> list[VisibilityObject]:
    """Order the board by what arrives soonest, then by what we heard about most recently.

    ETA ascending with nulls last. The operational question the Control Tower is
    open to answer is "what is coming next", and an object with no ETA cannot answer
    it — so it goes after everything that can, rather than sorting as though it were
    arriving in 1970.

    Freshness breaks the tie among those, newest first. It is the only ordering that
    is useful for a box nobody has forecast: the ones we have just heard from are the
    ones something is happening to. ``key`` is the final tiebreak, so two objects with
    the same ETA and the same silence come out in the same order on every request —
    a list that reshuffles between two identical loads reads as data changing.

    Sorted in Python rather than in SQL, and deliberately: ``current_eta`` is the read
    model's own answer about which of a shipment's date and a carrier's forecast to
    believe, and ordering in the database would need a second implementation of that
    rule. It costs no queries — every value is already on the loaded workspaces — so
    this is a sort over a list in memory, not an N+1.
    """
    return sorted(objects, key=_arrival_sort_key)


def _arrival_sort_key(obj: VisibilityObject) -> tuple:
    eta = obj.current_eta
    activity = obj.last_event_at or obj.last_synced_at
    # Negated so a *descending* freshness rides inside an ascending sort, and
    # infinite so an object nobody has heard from sorts last among its equals.
    freshness = -activity.timestamp() if activity is not None else float("inf")
    return (eta is None, eta or date.min, freshness, obj.key)


def filter_by_eta_window(objects: list[VisibilityObject], window: str) -> list[VisibilityObject]:
    """Narrow *objects* to those arriving within *window*.

    Public because the work queues ask the same question the Control Tower's ETA
    filter does, and two implementations of "the next seven days" would eventually
    disagree about whether today counts. An unrecognised window narrows nothing.
    """
    today = timezone.localdate()
    windows = {"today": 0, "7": 7, "14": 14, "30": 30}
    if window == "overdue":
        return [obj for obj in objects if obj.current_eta and obj.current_eta < today]
    days = windows.get(window)
    if days is None:
        return objects
    cutoff = today + timedelta(days=days)
    return [obj for obj in objects if obj.current_eta and today <= obj.current_eta <= cutoff]


def matches_search(obj: VisibilityObject, needle: str) -> bool:
    """True when *needle* — already lowercased — appears in anything naming *obj*."""
    haystack = [obj.label, obj.carrier_name, obj.vessel_name, obj.voyage_number]
    haystack.extend(container.container_id for container in obj.containers)
    if obj.shipment is not None:
        haystack.extend(
            [
                obj.shipment.shipment_number,
                obj.shipment.reference,
                obj.shipment.carrier_booking_reference,
                obj.shipment.bill_of_lading_number,
                obj.shipment.origin_port,
                obj.shipment.destination_port,
            ]
        )
    return any(needle in (value or "").lower() for value in haystack)
