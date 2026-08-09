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
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

from django.db.models import Q
from django.utils import timezone

from apps.scm.containers.models import Container
from apps.scm.containers.workspace import get_container_workspaces
from apps.scm.shipments.models import Shipment, ShipmentContainer
from apps.scm.tracking.delay_detection import evaluate_shipment_delay
from apps.scm.tracking.exception_detection import ExceptionReport, evaluate_container_exceptions
from apps.scm.tracking.models import ETAHistory, TrackingEvent, TrackingSubscription
from apps.teams.models import Team

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


@dataclass
class VisibilityFilters:
    """The overview's filter state, parsed once from the query string."""

    status: str = ""
    carrier: str = ""
    eta_window: str = ""
    delayed_only: bool = False
    exceptions_only: bool = False
    search: str = ""

    @property
    def is_active(self) -> bool:
        return bool(
            self.status or self.carrier or self.eta_window or self.delayed_only or self.exceptions_only or self.search
        )


@dataclass
class VisibilityOverview:
    """Everything the overview page renders."""

    objects: list[VisibilityObject] = field(default_factory=list)
    filters: VisibilityFilters = field(default_factory=VisibilityFilters)
    carrier_choices: list[str] = field(default_factory=list)

    @property
    def active_shipments(self) -> int:
        return sum(1 for obj in self.objects if obj.kind == ObjectKind.SHIPMENT)

    @property
    def tracked_containers(self) -> int:
        return sum(obj.container_count for obj in self.objects if obj.is_tracked)

    @property
    def arriving_soon(self) -> list[VisibilityObject]:
        cutoff = timezone.localdate() + timedelta(days=ARRIVING_SOON_DAYS)
        today = timezone.localdate()
        upcoming = [obj for obj in self.objects if obj.current_eta and today <= obj.current_eta <= cutoff]
        return sorted(upcoming, key=lambda obj: obj.current_eta)

    @property
    def delayed(self) -> list[VisibilityObject]:
        return [obj for obj in self.objects if obj.is_delayed]

    @property
    def exceptions(self) -> list[VisibilityObject]:
        return [obj for obj in self.objects if obj.has_exception]

    @property
    def status_choices(self):
        return JourneyState.choices

    @property
    def health_choices(self):
        return Health.choices


def parse_visibility_filters(params) -> VisibilityFilters:
    """Read filter state out of a request's query parameters."""
    return VisibilityFilters(
        status=(params.get("status") or "").strip(),
        carrier=(params.get("carrier") or "").strip(),
        eta_window=(params.get("eta") or "").strip(),
        delayed_only=params.get("delayed") == "1",
        exceptions_only=params.get("exceptions") == "1",
        search=(params.get("search") or "").strip(),
    )


def get_visibility_overview(team: Team, filters: VisibilityFilters | None = None) -> VisibilityOverview:
    """Return the composed overview for a team, with filters applied."""
    filters = filters or VisibilityFilters()
    objects = list_visibility_objects(team)
    return VisibilityOverview(
        objects=_apply_filters(objects, filters),
        filters=filters,
        carrier_choices=sorted({obj.carrier_name for obj in objects if obj.carrier_name}),
    )


def list_visibility_objects(team: Team) -> list[VisibilityObject]:
    """Return every shipment and standalone tracked container worth showing.

    A container on an active shipment is folded into that shipment, so twenty boxes
    on one vessel are one object rather than twenty. A container tracked without a
    shipment — or whose only shipment is finished or still a draft — stands on its
    own, because the carrier is telling us about it either way.
    """
    shipments = list(
        Shipment.objects.filter(team=team, status__in=ACTIVE_SHIPMENT_STATUSES).order_by("eta", "-created_at")
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
                exceptions=_merge_exceptions(exceptions.get(w.container.pk) for w in members),
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
    return VisibilityObject(
        kind=ObjectKind.SHIPMENT,
        shipment=shipment,
        workspaces=members,
        delay=evaluate_shipment_delay(shipment, has_delay_event=has_delay_event),
        exceptions=_merge_exceptions(_exception_reports(team, container_ids).get(cid) for cid in container_ids),
    )


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
    return VisibilityObject(
        kind=ObjectKind.CONTAINER,
        shipment=shipment,
        workspaces=[workspace],
        delay=delay,
        exceptions=_exception_reports(team, [container.pk]).get(container.pk) or ExceptionReport(has_exception=False),
    )


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
        by_container.setdefault(event.container_id, []).append(event)
    return {cid: evaluate_container_exceptions(rows) for cid, rows in by_container.items()}


def _merge_exceptions(reports) -> ExceptionReport:
    """Combine several containers' exception reports into the shipment's own.

    Types are de-duplicated — one customs hold across four boxes is one exception
    for the shipment — while details are kept in full so the reason survives.
    """
    types: list[str] = []
    details: list[str] = []
    for report in reports:
        if report is None:
            continue
        for exception_type in report.exception_types:
            if exception_type not in types:
                types.append(exception_type)
        details.extend(report.details)
    return ExceptionReport(has_exception=bool(types), exception_types=types, details=details)


def _apply_filters(objects: list[VisibilityObject], filters: VisibilityFilters) -> list[VisibilityObject]:
    """Narrow the object list.

    Applied in Python rather than SQL because every one of these values is derived
    from tracking rather than stored — filtering in the database would mean a second
    implementation of the derivations, which is exactly what this layer avoids.
    """
    result = objects
    if filters.status:
        result = [obj for obj in result if obj.journey_state == filters.status]
    if filters.carrier:
        result = [obj for obj in result if obj.carrier_name == filters.carrier]
    if filters.delayed_only:
        result = [obj for obj in result if obj.is_delayed]
    if filters.exceptions_only:
        result = [obj for obj in result if obj.has_exception]
    if filters.eta_window:
        result = _filter_by_eta_window(result, filters.eta_window)
    if filters.search:
        result = [obj for obj in result if _matches_search(obj, filters.search.lower())]
    return result


def _filter_by_eta_window(objects: list[VisibilityObject], window: str) -> list[VisibilityObject]:
    today = timezone.localdate()
    windows = {"7": 7, "14": 14, "30": 30}
    if window == "overdue":
        return [obj for obj in objects if obj.current_eta and obj.current_eta < today]
    days = windows.get(window)
    if days is None:
        return objects
    cutoff = today + timedelta(days=days)
    return [obj for obj in objects if obj.current_eta and today <= obj.current_eta <= cutoff]


def _matches_search(obj: VisibilityObject, needle: str) -> bool:
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
