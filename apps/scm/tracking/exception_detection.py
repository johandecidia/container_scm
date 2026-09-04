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


@dataclass(frozen=True)
class ExceptionIssue:
    """One exception, with the reason it was raised.

    ``exception_type`` is the code the rest of the platform matches on;
    ``detail`` is this engine's own sentence about why, e.g. "Customs hold at
    Rotterdam". They belong together: a caller that wants to show a reason beside
    a type must not have to guess which detail goes with which code.
    """

    exception_type: str
    detail: str


@dataclass
class ExceptionReport:
    """What this engine found, in three views of the same findings.

    ``issues`` pairs each type with its reason and is the primary answer.
    ``exception_types`` and ``details`` are the flat lists callers have always
    read; they are kept because de-duplication differs between them — a shipment
    reports one ``customs_hold`` type across four boxes but keeps all four reasons,
    so neither flat list can be recovered from the other.
    """

    has_exception: bool
    exception_types: list[str] = field(default_factory=list)
    details: list[str] = field(default_factory=list)
    issues: list[ExceptionIssue] = field(default_factory=list)


# Carrier event codes / descriptions that typically indicate a rollover.
_ROLLOVER_KEYWORDS = ("roll", "rolled", "rolling", "omit", "omitted", "blanksail", "blank sail")

# Carrier codes that indicate port congestion.
_CONGESTION_KEYWORDS = ("congestion", "congested", "port delay", "terminal delay")


def check_container_exceptions(team: Team, container) -> ExceptionReport:
    """Return an ExceptionReport for a container based on its tracking events."""
    events = TrackingEvent.objects.filter(team=team, container=container).order_by("-event_datetime", "-created_at")
    return evaluate_container_exceptions(events)


def evaluate_container_exceptions(events) -> ExceptionReport:
    """Return an ExceptionReport from an already-loaded set of a container's events.

    Split out from :func:`check_container_exceptions` so a caller that has already
    loaded events for many containers — the visibility overview — can reuse this
    engine instead of growing a second one. ``events`` must be newest first.
    """
    events = list(events)

    issues: list[ExceptionIssue] = []
    found: set[str] = set()

    def record(exception_type: str, detail: str) -> None:
        if exception_type in found:
            return
        found.add(exception_type)
        issues.append(ExceptionIssue(exception_type=exception_type, detail=detail))

    for event in events:
        text = f"{event.event_code} {event.description} {event.status}".lower()

        if event.event_type == TrackingEvent.EventType.CUSTOMS_HOLD:
            record("customs_hold", f"Customs hold at {event.location_name or 'unknown'}")

        if any(kw in text for kw in _ROLLOVER_KEYWORDS):
            record("rolled", f"Possible rollover: {event.description or event.event_code}")

        if any(kw in text for kw in _CONGESTION_KEYWORDS):
            record("port_congestion", f"Port congestion: {event.description or event.location_name}")

    # Check for stale tracking (no event in 5 days for active containers)
    latest_event = events[0] if events else None
    if latest_event and latest_event.event_datetime:
        age = timezone.now() - latest_event.event_datetime
        if age > timedelta(days=5):
            record("missing_event", f"No tracking update for {age.days} days")

    return _report_from_issues(issues)


def check_shipment_exceptions(team: Team, shipment) -> ExceptionReport:
    """Return a combined ExceptionReport across all containers in a shipment."""
    from apps.scm.shipments.models import ShipmentContainer

    container_ids = ShipmentContainer.objects.filter(shipment=shipment, shipment__team=team).values_list(
        "container_id", flat=True
    )

    from apps.scm.containers.models import Container

    return merge_exception_reports(
        check_container_exceptions(team=team, container=container)
        for container in Container.objects.filter(pk__in=container_ids)
    )


def merge_exception_reports(reports) -> ExceptionReport:
    """Combine several containers' reports into one for the thing that carries them.

    Types and issues are de-duplicated — one customs hold across four boxes is one
    exception for the shipment, and the first box's reason is the one kept. ``details``
    stays complete, because four boxes held at four different ports have four reasons
    worth reading.

    Lives here rather than in each caller so a shipment's exceptions mean the same
    thing whether the shipment view, the visibility overview or a work queue asked.
    """
    issues: list[ExceptionIssue] = []
    found: set[str] = set()
    details: list[str] = []
    for report in reports:
        if report is None:
            continue
        details.extend(report.details)
        for issue in report.issues:
            if issue.exception_type in found:
                continue
            found.add(issue.exception_type)
            issues.append(issue)
    return _report_from_issues(issues, details=details)


def _report_from_issues(issues: list[ExceptionIssue], *, details: list[str] | None = None) -> ExceptionReport:
    """Build a report from paired issues, deriving the flat lists callers read."""
    return ExceptionReport(
        has_exception=bool(issues),
        exception_types=[issue.exception_type for issue in issues],
        details=[issue.detail for issue in issues] if details is None else details,
        issues=issues,
    )
