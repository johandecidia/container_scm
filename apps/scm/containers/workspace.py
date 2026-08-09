"""Everything the container detail view needs, gathered once.

Three ideas shape this module:

**Derive, don't duplicate.** Carrier, tracking status, vessel, voyage and ETA are
not stored on Container. They are read through the container's shipment and its
tracking events, so there is one source of truth for each and nothing to keep in
sync. Container keeps what is genuinely its own: ISO identity, equipment type,
business status and condition.

**Three different statuses, kept apart.** The container's business status
(available, reserved, sold), the shipment's transport status (in transit, arrived)
and the tracking subscription's status (tracking, no data, error) answer different
questions and are presented separately.

**Position quality is part of the position.** The last known place carries how it
was obtained, so a terminal coordinate is never shown as a live GPS fix.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from apps.teams.models import Team

from .models import Container

if TYPE_CHECKING:
    from apps.scm.tracking.positions import ContainerPosition

# Enough history to be useful on one screen without loading a whole voyage.
_TIMELINE_LIMIT = 50
_MOVEMENT_LIMIT = 20
_SYNC_RUN_LIMIT = 5


@dataclass
class ContainerWorkspace:
    """Read model for the container detail view."""

    container: Container

    # Relationships
    shipment_containers: list = field(default_factory=list)
    tracking_subscriptions: list = field(default_factory=list)
    movements: list = field(default_factory=list)
    purchase_order_lines: list = field(default_factory=list)
    supplier_delivery_lines: list = field(default_factory=list)

    # Tracking
    latest_tracking_event: object = None
    latest_actual_event: object = None
    position: ContainerPosition | None = None
    timeline: list = field(default_factory=list)
    recent_sync_runs: list = field(default_factory=list)

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
        """The carrier that actually tracks this container, or "" when none does."""
        subscription = self.active_subscription
        if subscription is not None and subscription.provider_id:
            return subscription.provider.name
        return ""

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
    def current_status(self) -> str:
        """Where the box is in its journey, in one line.

        The shipment's transport status when there is a shipment, otherwise the last
        thing the carrier actually reported. Nothing new is stored for this.
        """
        if self.transport_status:
            return self.transport_status
        event = self.latest_actual_event or self.latest_tracking_event
        return event.get_event_type_display() if event else ""

    @property
    def last_refreshed_at(self):
        """When the carrier was last asked about this container."""
        subscription = self.active_subscription
        return subscription.last_synced_at if subscription else None

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
        event = self.latest_actual_event or self.latest_tracking_event
        return event.vessel_name if event else ""

    @property
    def voyage_number(self) -> str:
        event = self.latest_actual_event or self.latest_tracking_event
        return event.voyage_number if event else ""

    @property
    def current_eta(self):
        shipment = self.active_shipment
        return shipment.eta if shipment else None

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
    from apps.scm.tracking.models import TrackingEvent, TrackingSubscription, TrackingSyncRun
    from apps.scm.tracking.positions import get_latest_container_position

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
    latest_event = events.order_by("-event_datetime", "-created_at").first()
    latest_actual_event = (
        events.filter(event_time_type=TrackingEvent.EventTimeType.ACTUAL)
        .order_by("-event_datetime", "-created_at")
        .first()
    )
    timeline = list(events.order_by("-event_datetime", "-created_at")[:_TIMELINE_LIMIT])

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
        position=get_latest_container_position(team, container),
        timeline=timeline,
        recent_sync_runs=recent_sync_runs,
    )
