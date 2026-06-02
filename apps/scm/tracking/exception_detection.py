"""Exception detection for shipments and containers.

Identifies operational exceptions such as:
- Customs hold
- Vessel rollover (container rolled to a later vessel)
- Port congestion
- Missing tracking events (no update within expected window)
- Unknown carrier exception codes
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import timedelta

from django.utils import timezone

from apps.teams.models import Team

from .models import TrackingEvent

logger = logging.getLogger(__name__)


@dataclass
class ExceptionReport:
    has_exception: bool
    exception_types: list[str] = field(default_factory=list)
    details: list[str] = field(default_factory=list)


# Carrier event codes / descriptions that typically indicate a rollover.
_ROLLOVER_KEYWORDS = ("roll", "rolled", "rolling", "omit", "omitted", "blanksail", "blank sail")

# Carrier codes that indicate port congestion.
_CONGESTION_KEYWORDS = ("congestion", "congested", "port delay", "terminal delay")


def check_container_exceptions(team: Team, container) -> ExceptionReport:
    """Return an ExceptionReport for a container based on its tracking events."""
    events = TrackingEvent.objects.filter(team=team, container=container).order_by("-event_datetime", "-created_at")

    exception_types: list[str] = []
    details: list[str] = []

    for event in events:
        text = f"{event.event_code} {event.description} {event.status}".lower()

        if event.event_type == TrackingEvent.EventType.CUSTOMS_HOLD and "customs_hold" not in exception_types:
            exception_types.append("customs_hold")
            details.append(f"Customs hold at {event.location_name or 'unknown'}")

        if any(kw in text for kw in _ROLLOVER_KEYWORDS) and "rolled" not in exception_types:
            exception_types.append("rolled")
            details.append(f"Possible rollover: {event.description or event.event_code}")

        if any(kw in text for kw in _CONGESTION_KEYWORDS) and "port_congestion" not in exception_types:
            exception_types.append("port_congestion")
            details.append(f"Port congestion: {event.description or event.location_name}")

    # Check for stale tracking (no event in 5 days for active containers)
    latest_event = events.first()
    if latest_event and latest_event.event_datetime:
        age = timezone.now() - latest_event.event_datetime
        if age > timedelta(days=5):
            exception_types.append("missing_event")
            details.append(f"No tracking update for {age.days} days")

    return ExceptionReport(
        has_exception=bool(exception_types),
        exception_types=exception_types,
        details=details,
    )


def check_shipment_exceptions(team: Team, shipment) -> ExceptionReport:
    """Return a combined ExceptionReport across all containers in a shipment."""
    from apps.scm.shipments.models import ShipmentContainer

    container_ids = ShipmentContainer.objects.filter(shipment=shipment, shipment__team=team).values_list(
        "container_id", flat=True
    )

    all_types: list[str] = []
    all_details: list[str] = []

    from apps.scm.containers.models import Container

    for container in Container.objects.filter(pk__in=container_ids):
        report = check_container_exceptions(team=team, container=container)
        for exc_type in report.exception_types:
            if exc_type not in all_types:
                all_types.append(exc_type)
        all_details.extend(report.details)

    return ExceptionReport(
        has_exception=bool(all_types),
        exception_types=all_types,
        details=all_details,
    )
