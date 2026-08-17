"""Everything the container detail view needs, gathered once.

Three ideas shape this module:

**Derive, don't duplicate.** Carrier, tracking status, vessel, voyage and ETA are
not stored on Container. They are read through the container's shipment and its
tracking events, so there is one source of truth for each and nothing to keep in
sync. Container keeps what is genuinely its own: ISO identity, equipment type,
business status and condition.

**A container without a shipment is still tracked.** Status and ETA used to come
only from the shipment, so a container tracked on its own showed neither, however
much the carrier had told us. Both now fall back to the container's own events —
derived here, not stored, so there is still one source of truth for each.

**Three different statuses, kept apart.** The container's business status
(available, reserved, sold), the shipment's transport status (in transit, arrived)
and the tracking subscription's status (tracking, no data, error) answer different
questions and are presented separately.

**Position quality is part of the position.** The last known place carries how it
was obtained, so a terminal coordinate is never shown as a live GPS fix.

**Several tracking sources, one journey.** A container can have more than one
verified source — carriers that covered different legs, plus our own physical
record — so freshness spans all of them and the journey is assembled from all of
them by :func:`~apps.scm.tracking.journey.get_container_journey`. ``active_subscription``
still names the one watch the panel's status line speaks for; it is not a claim
that it is the only one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from apps.teams.models import Team

from .models import Container

if TYPE_CHECKING:
    from apps.scm.tracking.gaps import TrackingGap
    from apps.scm.tracking.journey import ContainerJourney, DerivedCurrentLocation, JourneyPoint, JourneySource
    from apps.scm.tracking.models import TrackingEvent
    from apps.scm.tracking.positions import ContainerPosition

# Enough history to be useful on one screen without loading a whole voyage.
_TIMELINE_LIMIT = 50
_MOVEMENT_LIMIT = 20
_SYNC_RUN_LIMIT = 5


@dataclass
class ContainerWorkspace:
    """Read model for the container detail view.

    Also built in bulk by :func:`get_container_workspaces` for the visibility
    overview, which needs the same tracking derivations for many containers at
    once. A bulk-built workspace has ``tracking_only`` set: its tracking fields are
    complete, and its movements, purchase orders and supplier deliveries are empty
    because they were never loaded, not because there are none.
    """

    container: Container

    # Relationships
    shipment_containers: list = field(default_factory=list)
    tracking_subscriptions: list = field(default_factory=list)
    movements: list = field(default_factory=list)
    purchase_order_lines: list = field(default_factory=list)
    supplier_delivery_lines: list = field(default_factory=list)

    # Tracking
    latest_tracking_event: TrackingEvent | None = None
    latest_actual_event: TrackingEvent | None = None
    latest_meaningful_actual_event: TrackingEvent | None = None
    tracking_eta_event: TrackingEvent | None = None
    carriage_event: TrackingEvent | None = None
    position: ContainerPosition | None = None
    timeline: list = field(default_factory=list)
    recent_sync_runs: list = field(default_factory=list)

    # The container's journey across every source that has reported it. Only the
    # full workspace builds it — a bulk-built one leaves it None rather than
    # issuing a query per container, so a caller reading it there gets nothing
    # rather than a partial answer.
    journey: ContainerJourney | None = None

    # Every internal event type the carrier has actually *observed* for this
    # container. Forecasts are excluded on purpose: an estimated arrival must never
    # let anything conclude the box has arrived.
    observed_event_types: set = field(default_factory=set)

    # True when only the tracking sections were loaded — see the class docstring.
    tracking_only: bool = False

    @property
    def active_shipment(self):
        """The most recent shipment this container is on, if any."""
        link = self.shipment_containers[0] if self.shipment_containers else None
        return link.shipment if link else None

    @property
    def active_subscription(self):
        """The subscription actually being polled, else the most recent one."""
        from apps.scm.tracking.models import TrackingSubscription

        for subscription in self.tracking_subscriptions:
            if subscription.status == TrackingSubscription.Status.ACTIVE:
                return subscription
        return self.tracking_subscriptions[0] if self.tracking_subscriptions else None

    @property
    def carrier_name(self) -> str:
        """The carrier, taken from tracking if known and from the shipment otherwise."""
        return self.tracking_carrier_name or self.shipment_carrier

    @property
    def is_tracked(self) -> bool:
        """True when a carrier has been verified as this container's tracking source.

        A subscription is only ever created from carrier data, so its existence — not
        the shipment's carrier field, and not which carrier we happen to ask — is what
        makes tracking real.
        """
        return self.active_subscription is not None

    @property
    def tracking_carrier_name(self) -> str:
        """The carrier that actually tracks this container, or "" when none does.

        One name, for the places that can only show one — the list column, the
        carrier filter. Where several sources have reported this box,
        :attr:`tracking_sources` is the honest answer and this is the primary of them.
        """
        subscription = self.active_subscription
        if subscription is not None and subscription.provider_id:
            return subscription.provider.name
        return ""

    @property
    def tracking_sources(self) -> list[JourneySource]:
        """Every source that has reported this container, carriers first.

        Empty for a bulk-built workspace, which does not load the journey.
        """
        return self.journey.sources if self.journey is not None else []

    @property
    def tracking_source_names(self) -> list[str]:
        return [source.name for source in self.tracking_sources]

    @property
    def has_multiple_tracking_sources(self) -> bool:
        return len(self.tracking_sources) > 1

    @property
    def physical_observation(self) -> JourneyPoint | None:
        """Our own physical record of where the box is, or None when we have none."""
        return self.journey.physical_observation if self.journey is not None else None

    @property
    def derived_current_location(self) -> DerivedCurrentLocation | None:
        """Where the container is now, from whichever source knows most recently.

        Not the same question as :attr:`position`, which is the last place a *carrier*
        reported. When we have physically seen the box more recently than the
        carrier's last observation, that is where it is — and the carrier's event
        stays on the timeline, because it did happen.
        """
        return self.journey.current_location if self.journey is not None else None

    @property
    def current_position(self) -> ContainerPosition | None:
        """The derived current location as a position, falling back to the carrier's.

        Lets the existing position component render either without knowing which it
        was given.
        """
        current = self.derived_current_location
        return current.position if current is not None else self.position

    @property
    def tracking_gap(self) -> TrackingGap | None:
        """The segment of the journey nothing accounts for, or None.

        Derived on read, so it disappears as soon as an event explains the segment.
        """
        from apps.scm.tracking.gaps import detect_tracking_gap

        return detect_tracking_gap(self.journey) if self.journey is not None else None

    @property
    def has_tracking_gap(self) -> bool:
        return self.tracking_gap is not None

    @property
    def shipment_carrier(self) -> str:
        """The carrier named on the shipment: who we would ask, not who answers.

        Kept apart from :attr:`tracking_carrier_name` on purpose — a shipment booked
        with Maersk that Maersk has not published events for yet is a normal state,
        and showing it as tracked would be a lie.
        """
        shipment = self.active_shipment
        return shipment.carrier if shipment else ""

    @property
    def transport_status(self) -> str:
        """The shipment's transport status — not the container's business status."""
        shipment = self.active_shipment
        return shipment.get_status_display() if shipment else ""

    @property
    def tracking_current_status(self) -> str:
        """What the carrier last observed happening to this container.

        Kept separate from the shipment's transport status and from the container's
        own business status: this one answers "where is the box in its journey",
        which is a question only the carrier's events can answer.
        """
        event = self.latest_meaningful_actual_event
        return event.get_event_type_display() if event else ""

    @property
    def current_status(self) -> str:
        """Where the box is in its journey, in one line.

        Tracking leads: the carrier's last observed event is the most specific and
        most current answer there is. The shipment's transport status stands in when
        nothing has been tracked — it is derived from the same events plus whatever
        was entered by hand, so it is a summary, not a contradiction.
        """
        return self.tracking_current_status or self.transport_status

    @property
    def last_refreshed_at(self):
        """When any of this container's carriers was last asked.

        Across every source, not just the primary one: with two watches on one box,
        reporting only one of them would say "last checked 3 days ago" about a
        container that was checked a minute ago.
        """
        times = [
            subscription.last_synced_at
            for subscription in self.tracking_subscriptions
            if subscription.last_synced_at is not None
        ]
        return max(times) if times else None

    @property
    def next_check_at(self):
        """When the scheduler will next ask a carrier about this container, or None.

        The soonest across the live watches. Only live ones: a completed or paused
        subscription may still carry an old ``next_sync_at`` that nothing will act
        on, and showing it would promise an update that is never coming.
        """
        from apps.scm.tracking.models import TrackingSubscription

        times = [
            subscription.next_sync_at
            for subscription in self.tracking_subscriptions
            if subscription.next_sync_at is not None and subscription.status == TrackingSubscription.Status.ACTIVE
        ]
        return min(times) if times else None

    @property
    def is_tracking_active(self) -> bool:
        """True when a live watch exists — the panel's green dot."""
        from apps.scm.tracking.models import TrackingSubscription

        subscription = self.active_subscription
        return subscription is not None and subscription.status == TrackingSubscription.Status.ACTIVE

    @property
    def has_tracking_error(self) -> bool:
        from apps.scm.tracking.models import TrackingSubscription

        subscription = self.active_subscription
        if subscription is None:
            return False
        return (
            subscription.status == TrackingSubscription.Status.FAILED
            or subscription.tracking_status == TrackingSubscription.TrackingStatus.ERROR
        )

    @property
    def tracking_status(self) -> str:
        """What the carrier is telling us, e.g. tracking or no data yet."""
        subscription = self.active_subscription
        return subscription.get_tracking_status_display() if subscription else ""

    @property
    def vessel_name(self) -> str:
        event = self.carriage_event
        return event.vessel_name if event else ""

    @property
    def voyage_number(self) -> str:
        event = self.carriage_event
        return event.voyage_number if event else ""

    @property
    def tracking_eta_at(self):
        """The carrier's arrival forecast for this container, to the hour.

        Full precision on purpose: a slip from 06:00 to 22:00 is a working day lost,
        and rounding it to a date would hide it.
        """
        event = self.tracking_eta_event
        return event.event_datetime if event else None

    @property
    def tracking_eta(self):
        """The carrier's arrival forecast for this container, as a date."""
        from django.utils import timezone

        eta_at = self.tracking_eta_at
        if eta_at is None:
            return None
        return timezone.localtime(eta_at).date() if timezone.is_aware(eta_at) else eta_at.date()

    @property
    def current_eta(self):
        """When the container is expected to arrive.

        The shipment's ETA when there is one — it is the planned date the business
        works to, and tracking already feeds it through ``apply_tracking_to_shipment``.
        A container tracked on its own has no shipment to carry that, so its own
        forecast events answer instead of showing nothing.
        """
        shipment = self.active_shipment
        if shipment is not None and shipment.eta:
            return shipment.eta
        return self.tracking_eta

    @property
    def eta_source(self) -> str:
        """Which of the two ETAs is being shown: "shipment", "tracking" or ""."""
        shipment = self.active_shipment
        if shipment is not None and shipment.eta:
            return "shipment"
        return "tracking" if self.tracking_eta else ""

    @property
    def original_eta(self):
        shipment = self.active_shipment
        return shipment.original_eta if shipment else None

    @property
    def eta_delay_days(self) -> int | None:
        """How much later the arrival is now expected than first forecast."""
        if self.current_eta and self.original_eta:
            return (self.current_eta - self.original_eta).days
        return None

    @property
    def journey_timeline(self) -> list[JourneyPoint]:
        """The journey as the panel shows it: newest first, capped like ``timeline``.

        One screen's worth. The cap is a display limit, not a claim about how much
        happened — the journey itself is complete.
        """
        if self.journey is None:
            return []
        return self.journey.newest_first[:_TIMELINE_LIMIT]

    @property
    def last_sync_run(self):
        return self.recent_sync_runs[0] if self.recent_sync_runs else None

    @property
    def sync_problem(self) -> str:
        """A plain-language description of why tracking is not working, if it is not.

        Returns an empty string when there is nothing wrong, so the UI can simply
        test for truthiness. "No data yet" is not a problem and is reported through
        tracking_status instead.
        """
        subscription = self.active_subscription
        if subscription is None:
            return ""

        from apps.scm.tracking.models import TrackingSubscription

        if subscription.tracking_status == TrackingSubscription.TrackingStatus.NOT_CONFIGURED:
            return str(_not_configured_message(subscription))
        if subscription.status == TrackingSubscription.Status.FAILED:
            return subscription.last_error_message or "The last tracking sync failed."
        return ""

    @property
    def purchase_orders(self) -> list:
        """Distinct purchase orders behind this container's delivery lines."""
        seen: dict[int, object] = {}
        for line in self.purchase_order_lines:
            order = line.purchase_order
            seen.setdefault(order.pk, order)
        return list(seen.values())

    @property
    def suppliers(self) -> list[str]:
        names = {order.supplier_name for order in self.purchase_orders if getattr(order, "supplier_name", "")}
        return sorted(names)


def _not_configured_message(subscription) -> str:
    carrier = subscription.provider.name if subscription.provider_id else "This carrier"
    return f"{carrier} is not configured for live tracking yet, so no data is being fetched."


def get_container_workspace(team: Team, container: Container) -> ContainerWorkspace:
    """Gather all workspace data for a container detail view, team-scoped throughout."""
    from apps.scm.shipments.models import ShipmentContainer
    from apps.scm.supplier_deliveries.models import SupplierDeliveryLine
    from apps.scm.tracking.journey import get_container_journey
    from apps.scm.tracking.models import TrackingEvent, TrackingSubscription, TrackingSyncRun
    from apps.scm.tracking.positions import get_latest_container_position
    from apps.scm.tracking.selectors import get_container_tracking_eta_event, get_latest_meaningful_actual_event

    from .models import ContainerMovement

    shipment_containers = list(
        ShipmentContainer.objects.filter(container=container, shipment__team=team)
        .select_related("shipment")
        .order_by("-created_at")
    )
    tracking_subscriptions = list(
        TrackingSubscription.objects.filter(team=team, container=container)
        .select_related("provider", "shipment")
        .order_by("-created_at")
    )

    events = TrackingEvent.objects.filter(team=team, container=container).select_related("provider")
    # Every provider's events, oldest first, for the journey. Loaded once and handed
    # over so the journey costs one query rather than repeating this page's reads.
    journey_events = list(events.order_by("event_datetime", "created_at"))
    latest_event = events.order_by("-event_datetime", "-created_at").first()
    latest_actual_event = (
        events.filter(event_time_type=TrackingEvent.EventTimeType.ACTUAL)
        .order_by("-event_datetime", "-created_at")
        .first()
    )
    timeline = list(events.order_by("-event_datetime", "-created_at")[:_TIMELINE_LIMIT])
    observed_event_types = set(
        events.filter(event_time_type=TrackingEvent.EventTimeType.ACTUAL)
        .exclude(event_type=TrackingEvent.EventType.UNKNOWN)
        .values_list("event_type", flat=True)
    )

    movements = list(
        ContainerMovement.objects.filter(team=team, container=container)
        .select_related("from_location", "to_location")
        .order_by("-occurred_at")[:_MOVEMENT_LIMIT]
    )

    delivery_lines = list(
        SupplierDeliveryLine.objects.filter(team=team, container=container)
        .select_related("delivery", "purchase_order_line", "purchase_order_line__purchase_order")
        .order_by("-created_at")
    )
    purchase_order_lines = [
        line.purchase_order_line for line in delivery_lines if line.purchase_order_line_id is not None
    ]

    subscription_ids = [subscription.pk for subscription in tracking_subscriptions]
    recent_sync_runs = (
        list(
            TrackingSyncRun.objects.filter(team=team, subscription_id__in=subscription_ids)
            .select_related("provider")
            .order_by("-started_at", "-created_at")[:_SYNC_RUN_LIMIT]
        )
        if subscription_ids
        else []
    )

    return ContainerWorkspace(
        container=container,
        shipment_containers=shipment_containers,
        tracking_subscriptions=tracking_subscriptions,
        movements=movements,
        purchase_order_lines=purchase_order_lines,
        supplier_delivery_lines=delivery_lines,
        latest_tracking_event=latest_event,
        latest_actual_event=latest_actual_event,
        latest_meaningful_actual_event=get_latest_meaningful_actual_event(team, container),
        tracking_eta_event=get_container_tracking_eta_event(team, container),
        carriage_event=_first_event_with_a_vessel(timeline),
        position=get_latest_container_position(team, container),
        timeline=timeline,
        recent_sync_runs=recent_sync_runs,
        observed_event_types=observed_event_types,
        journey=get_container_journey(
            team,
            container,
            events=journey_events,
            # Cancelled watches are not sources: someone stopped them on purpose.
            subscriptions=[
                subscription
                for subscription in reversed(tracking_subscriptions)
                if subscription.status != TrackingSubscription.Status.CANCELLED
            ],
        ),
    )


def _first_event_with_a_vessel(events):
    """Return the most recent event that names a vessel, or None.

    Not simply the latest event: the last thing to happen to a box is often a truck
    movement, which names no vessel. Falling back to the most recent one that does
    keeps the voyage on screen instead of blanking it. ``events`` must be newest
    first.
    """
    for event in events:
        if event.vessel_name:
            return event
    return None


# ---------------------------------------------------------------------------
# Bulk construction
#
# The visibility overview needs the tracking derivations above for every tracked
# container at once, and calling get_container_workspace in a loop would issue
# eight queries per container. The builder below answers the same questions with a
# fixed number of queries, whatever the number of containers, and reuses the same
# derivation code — position classification, ETA rules, status rules — so there is
# one implementation of each rather than a faster, subtly different second one.
# ---------------------------------------------------------------------------


def get_container_workspaces(team: Team, containers) -> dict[int, ContainerWorkspace]:
    """Return tracking-only workspaces for many containers, keyed by container id.

    Every returned workspace has ``tracking_only=True``: movements, purchase orders
    and supplier deliveries are deliberately not loaded. Reading them from one of
    these workspaces would report "none" for data that was simply never fetched.
    """
    from apps.scm.shipments.models import ShipmentContainer
    from apps.scm.tracking.models import TrackingEvent, TrackingSubscription
    from apps.scm.tracking.positions import HAS_A_PLACE, position_from_event
    from apps.scm.tracking.selectors import ARRIVAL_FORECAST_EVENT_TYPES

    containers = list(containers)
    container_ids = [container.pk for container in containers]
    if not container_ids:
        return {}

    links = _group_by_container(
        ShipmentContainer.objects.filter(container_id__in=container_ids, shipment__team=team)
        .select_related("shipment")
        .order_by("-created_at")
    )
    subscriptions = _group_by_container(
        TrackingSubscription.objects.filter(team=team, container_id__in=container_ids)
        .select_related("provider", "shipment")
        .order_by("-created_at")
    )

    events = TrackingEvent.objects.filter(team=team, container_id__in=container_ids).select_related("provider")
    dated = events.exclude(event_datetime__isnull=True)
    actual = dated.filter(event_time_type=TrackingEvent.EventTimeType.ACTUAL)

    classified_actual = actual.exclude(event_type=TrackingEvent.EventType.UNKNOWN)

    latest = _latest_per_container(dated)
    latest_actual = _latest_per_container(actual)
    # The same two preferences get_latest_container_position applies: observed over
    # forecast, and located over placeless.
    latest_located_actual = _latest_per_container(actual.filter(HAS_A_PLACE))
    latest_meaningful = _latest_per_container(classified_actual)
    latest_carriage = _latest_per_container(dated.exclude(vessel_name=""))
    forecasts = _latest_per_container(
        dated.filter(
            event_time_type__in=[TrackingEvent.EventTimeType.ESTIMATED, TrackingEvent.EventTimeType.PLANNED],
            event_type__in=ARRIVAL_FORECAST_EVENT_TYPES,
        )
    )
    # A forecast that the carrier has already answered with an actual arrival is no
    # longer an ETA — the same rule get_container_tracking_eta_event applies.
    arrived_ids = set(
        actual.filter(
            event_type__in=(TrackingEvent.EventType.VESSEL_ARRIVED, TrackingEvent.EventType.DISCHARGED)
        ).values_list("container_id", flat=True)
    )

    observed: dict[int, set[str]] = {}
    for container_id, event_type in classified_actual.values_list("container_id", "event_type").distinct():
        observed.setdefault(container_id, set()).add(event_type)

    workspaces = {}
    for container in containers:
        anchor = latest_located_actual.get(container.pk) or latest_actual.get(container.pk) or latest.get(container.pk)
        workspaces[container.pk] = ContainerWorkspace(
            container=container,
            shipment_containers=links.get(container.pk, []),
            tracking_subscriptions=subscriptions.get(container.pk, []),
            latest_tracking_event=latest.get(container.pk),
            latest_actual_event=latest_actual.get(container.pk),
            latest_meaningful_actual_event=latest_meaningful.get(container.pk),
            tracking_eta_event=None if container.pk in arrived_ids else forecasts.get(container.pk),
            carriage_event=latest_carriage.get(container.pk),
            position=position_from_event(anchor) if anchor is not None else None,
            observed_event_types=observed.get(container.pk, set()),
            tracking_only=True,
        )
    return workspaces


def _group_by_container(queryset) -> dict[int, list]:
    """Bucket an already-ordered queryset of container-linked rows by container id."""
    grouped: dict[int, list] = {}
    for row in queryset:
        grouped.setdefault(row.container_id, []).append(row)
    return grouped


def _latest_per_container(queryset) -> dict[int, TrackingEvent]:
    """Return the newest row per container, in one query.

    ``DISTINCT ON`` keeps this to a single round trip however many containers are
    involved; the ``created_at`` tiebreak makes the choice deterministic when a
    carrier reports two events at the same instant.
    """
    rows = queryset.order_by("container_id", "-event_datetime", "-created_at").distinct("container_id")
    return {row.container_id: row for row in rows}
