"""Deriving a shipment's transport state from normalised carrier events.

The rule that shapes this module: **only actual events move a shipment forward.**
An estimated arrival is a forecast, and a forecast must never make a shipment look
arrived or delivered — that is how a container gets marked received while it is
still at sea. Estimated events feed the ETA; actual events feed the status.

The derivation is deterministic: given the same set of events it always produces
the same milestones, so it can be re-run safely after a backfill or a re-parse.

Order matters, most advanced first: a container that has been delivered was also
discharged and loaded, and the shipment's status should reflect the furthest point
reached, not the most recent event received (carriers backfill out of order).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from django.utils import timezone

from apps.scm.tracking.models import TrackingEvent

from .models import Shipment, ShipmentEvent

logger = logging.getLogger(__name__)

_EventType = TrackingEvent.EventType

# Event types that prove departure and arrival, in the order we trust them.
_DEPARTURE_EVENTS = (_EventType.VESSEL_DEPARTED, _EventType.LOADED_ON_VESSEL)
_ARRIVAL_EVENTS = (_EventType.VESSEL_ARRIVED, _EventType.DISCHARGED)
_DELIVERY_EVENTS = (_EventType.DELIVERED,)


@dataclass
class TransportSnapshot:
    """What the carrier's actual events say has happened so far."""

    actual_departure_at: datetime | None = None
    actual_arrival_at: datetime | None = None
    delivered_at: datetime | None = None
    latest_event: TrackingEvent | None = None
    latest_actual_event: TrackingEvent | None = None
    latest_estimated_arrival: TrackingEvent | None = None

    @property
    def has_departed(self) -> bool:
        return self.actual_departure_at is not None

    @property
    def has_arrived(self) -> bool:
        return self.actual_arrival_at is not None


def get_transport_snapshot(shipment: Shipment) -> TransportSnapshot:
    """Summarise a shipment's tracking events into transport milestones.

    Only events belonging to the shipment's own team are considered; the shipment
    itself is the team scope.
    """
    events = list(
        TrackingEvent.objects.filter(team_id=shipment.team_id, shipment=shipment)
        .exclude(event_datetime__isnull=True)
        .order_by("event_datetime")
    )
    if not events:
        return TransportSnapshot()

    actual = [event for event in events if event.is_actual]

    def _first_time(event_types) -> object | None:
        for event in actual:
            if event.event_type in event_types:
                return event.event_datetime
        return None

    estimated_arrivals = [
        event
        for event in events
        if event.is_estimated and event.event_type in (_EventType.VESSEL_ARRIVED, _EventType.ETA_UPDATED)
    ]

    return TransportSnapshot(
        actual_departure_at=_first_time(_DEPARTURE_EVENTS),
        actual_arrival_at=_first_time(_ARRIVAL_EVENTS),
        delivered_at=_first_time(_DELIVERY_EVENTS),
        latest_event=events[-1],
        latest_actual_event=actual[-1] if actual else None,
        latest_estimated_arrival=estimated_arrivals[-1] if estimated_arrivals else None,
    )


def apply_tracking_to_shipment(shipment: Shipment, *, container=None) -> Shipment:
    """Bring a shipment's milestones, status and ETA in line with its tracking events.

    Called after carrier events are persisted. It:

    1. sets actual_departure_at / actual_arrival_at from **actual** events only;
    2. recalculates the status through the existing status rules;
    3. updates the ETA from the latest **estimated** arrival, through the single ETA
       service so the change lands in ETAHistory;
    4. records one internal ShipmentEvent summarising a status transition — not one
       per carrier event. Carrier events live in TrackingEvent; duplicating them as
       ShipmentEvents would make the timeline say everything twice.
    """
    from .services import recalculate_and_save_shipment_status

    snapshot = get_transport_snapshot(shipment)
    previous_status = shipment.status

    changed_fields = []
    if snapshot.actual_departure_at and shipment.actual_departure_at != snapshot.actual_departure_at:
        shipment.actual_departure_at = snapshot.actual_departure_at
        changed_fields.append("actual_departure_at")
    if snapshot.actual_arrival_at and shipment.actual_arrival_at != snapshot.actual_arrival_at:
        shipment.actual_arrival_at = snapshot.actual_arrival_at
        changed_fields.append("actual_arrival_at")

    if snapshot.latest_event is not None:
        shipment.last_tracking_sync_at = timezone.now()
        changed_fields.append("last_tracking_sync_at")
        status_text = snapshot.latest_actual_event.get_event_type_display() if snapshot.latest_actual_event else ""
        if status_text and shipment.tracking_status != status_text:
            shipment.tracking_status = status_text
            changed_fields.append("tracking_status")

    if changed_fields:
        shipment.save(update_fields=[*dict.fromkeys(changed_fields), "updated_at"])

    _update_eta_from_tracking(shipment, snapshot, container=container)
    recalculate_and_save_shipment_status(shipment)

    if shipment.status != previous_status:
        _record_status_transition(shipment, previous_status, snapshot)

    return shipment


def _update_eta_from_tracking(shipment: Shipment, snapshot: TransportSnapshot, *, container=None) -> None:
    """Push the carrier's latest estimated arrival into the shipment's ETA."""
    from .services import update_shipment_eta

    estimate = snapshot.latest_estimated_arrival
    if estimate is None or estimate.event_datetime is None:
        return
    if snapshot.actual_arrival_at is not None:
        # It has already arrived; a stale forecast must not overwrite reality.
        return

    new_eta_at = estimate.event_datetime
    new_eta = timezone.localtime(new_eta_at).date() if timezone.is_aware(new_eta_at) else new_eta_at.date()
    previous_eta_at = _previous_precise_eta(shipment)

    if shipment.eta == new_eta and previous_eta_at == new_eta_at:
        return

    update_shipment_eta(
        shipment,
        new_eta,
        source=estimate.provider.code if estimate.provider_id else "carrier",
        confidence="high" if estimate.event_time_type == TrackingEvent.EventTimeType.ESTIMATED else "medium",
        eta_at=new_eta_at,
        previous_eta_at=previous_eta_at,
        location_name=estimate.location_name,
        location_unlocode=estimate.location_unlocode,
        tracking_event=estimate,
        container=container or estimate.container,
    )


def _previous_precise_eta(shipment: Shipment):
    """Return the hour-precision ETA from the most recent history row, if any."""
    from apps.scm.tracking.models import ETAHistory

    latest = (
        ETAHistory.objects.filter(team_id=shipment.team_id, shipment=shipment)
        .exclude(new_eta_at__isnull=True)
        .order_by("-changed_at")
        .first()
    )
    return latest.new_eta_at if latest else None


def _record_status_transition(shipment: Shipment, previous_status: str, snapshot: TransportSnapshot) -> None:
    """Record one internal event summarising the transition.

    Deliberately one event per transition, not one per carrier event: the carrier's
    own events are already stored as TrackingEvents and shown on the merged timeline.
    """
    from .services import create_shipment_event

    trigger = snapshot.latest_actual_event
    create_shipment_event(
        shipment=shipment,
        event_type=ShipmentEvent.EventType.TRACKING_UPDATED,
        description=f"Status changed from {previous_status} to {shipment.status} based on carrier tracking.",
        metadata={
            "previous_status": previous_status,
            "new_status": shipment.status,
            "trigger_event_type": trigger.event_type if trigger else "",
            "trigger_event_id": trigger.pk if trigger else None,
        },
    )
    logger.info(
        "Shipment %s status %s → %s from tracking.",
        shipment.pk,
        previous_status,
        shipment.status,
    )
