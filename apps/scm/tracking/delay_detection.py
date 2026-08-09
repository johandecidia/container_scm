"""Delay detection for shipments.

A shipment is considered delayed when:
- ETA has been pushed forward (new_eta > previous_eta in ETAHistory), or
- Arrival event is missing after the original ETA has passed, or
- Carrier event explicitly indicates a delay.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from django.utils import timezone

from apps.teams.models import Team

from .models import ETAHistory, TrackingEvent

logger = logging.getLogger(__name__)


@dataclass
class DelayReport:
    is_delayed: bool
    reason: str
    eta_drift_days: int = 0  # positive = ETA moved forward (delayed)


def check_shipment_delay(team: Team, shipment) -> DelayReport:
    """Return a DelayReport for the given shipment.

    Checks in order:
    1. ETA has drifted forward compared to original_eta.
    2. DELAY tracking event exists.
    3. Arrival event is missing after original ETA has passed.
    """
    has_delay_event = TrackingEvent.objects.filter(
        team=team,
        shipment=shipment,
        event_type=TrackingEvent.EventType.DELAY,
    ).exists()
    return evaluate_shipment_delay(shipment, has_delay_event=has_delay_event)


def evaluate_shipment_delay(shipment, *, has_delay_event: bool) -> DelayReport:
    """Return a DelayReport from a shipment plus one already-answered question.

    Split out from :func:`check_shipment_delay` so a caller checking many shipments
    — the visibility overview — can answer "does a DELAY event exist" once in bulk
    and still run this one engine, rather than growing a second one beside it.
    """
    original_eta = shipment.original_eta
    current_eta = shipment.eta
    now_date = timezone.now().date()

    # 1. ETA drift
    if original_eta and current_eta and current_eta > original_eta:
        drift = (current_eta - original_eta).days
        return DelayReport(is_delayed=True, reason="ETA moved forward", eta_drift_days=drift)

    # 2. Explicit carrier delay event
    if has_delay_event:
        return DelayReport(is_delayed=True, reason="Carrier delay event received")

    # 3. Overdue — original ETA passed, no arrival yet
    if original_eta and original_eta < now_date and shipment.actual_arrival_at is None:
        overdue_days = (now_date - original_eta).days
        return DelayReport(
            is_delayed=True,
            reason=f"No arrival recorded {overdue_days} day(s) after original ETA",
            eta_drift_days=overdue_days,
        )

    return DelayReport(is_delayed=False, reason="")


def get_delayed_shipments(team: Team, shipments):
    """Return a list of (shipment, DelayReport) tuples for all delayed shipments.

    Args:
        team: The owning team.
        shipments: Iterable of Shipment instances.

    Returns:
        List of (shipment, DelayReport) tuples where report.is_delayed is True.
    """
    return [(s, r) for s in shipments if (r := check_shipment_delay(team, s)).is_delayed]


def get_eta_drift_days(team: Team, shipment) -> int:
    """Return total ETA drift in days from first to latest ETAHistory entry.

    Positive = delayed (ETA moved later). Negative = earlier than original.
    Zero if no history.
    """
    entries = ETAHistory.objects.filter(team=team, shipment=shipment).order_by("changed_at")
    if not entries.exists():
        return 0

    first = entries.first()
    last = entries.last()

    if first.previous_eta is None or last.new_eta is None:
        return 0

    return (last.new_eta - first.previous_eta).days
