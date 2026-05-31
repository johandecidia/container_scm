# Analytics services — computation and aggregation logic.

import datetime
from decimal import Decimal

from django.utils import timezone

from apps.scm.containers.choices import ContainerStatus
from apps.scm.containers.models import Container
from apps.scm.shipments.models import Shipment
from apps.teams.models import Team

from .models import AnalyticsSnapshot

# Shipment statuses considered "active" (booked, in transit, or arrived but not yet delivered)
_ACTIVE_STATUSES = {Shipment.Status.BOOKED, Shipment.Status.IN_TRANSIT, Shipment.Status.ARRIVED}
_COMPLETED_STATUSES = {Shipment.Status.DELIVERED}


# ---------------------------------------------------------------------------
# KPI functions
# ---------------------------------------------------------------------------


def get_total_shipments(team: Team) -> int:
    return Shipment.objects.filter(team=team).count()


def get_active_shipments(team: Team) -> int:
    return Shipment.objects.filter(team=team, status__in=_ACTIVE_STATUSES).count()


def get_completed_shipments(team: Team) -> int:
    return Shipment.objects.filter(team=team, status__in=_COMPLETED_STATUSES).count()


def get_containers_in_transit(team: Team) -> int:
    return Container.objects.filter(team=team, status=ContainerStatus.IN_TRANSIT).count()


def get_containers_delivered(team: Team) -> int:
    """Count distinct containers that appear on at least one delivered shipment."""
    return (
        Container.objects.filter(
            team=team,
            shipment_containers__shipment__status=Shipment.Status.DELIVERED,
        )
        .distinct()
        .count()
    )


def get_average_transit_days(team: Team) -> Decimal | None:
    """Return mean transit duration in days for delivered shipments with both dates set."""
    shipments = Shipment.objects.filter(
        team=team,
        status=Shipment.Status.DELIVERED,
        actual_departure_at__isnull=False,
        actual_arrival_at__isnull=False,
    ).only("actual_departure_at", "actual_arrival_at")

    total_days = 0.0
    count = 0
    for s in shipments:
        delta = s.actual_arrival_at - s.actual_departure_at
        total_days += delta.total_seconds() / 86400
        count += 1

    if count == 0:
        return None
    return Decimal(str(round(total_days / count, 2)))


# ---------------------------------------------------------------------------
# Snapshot generation
# ---------------------------------------------------------------------------


def create_or_update_snapshot(team: Team, date: datetime.date | None = None) -> AnalyticsSnapshot:
    """Compute KPIs and persist (or refresh) the snapshot for *team* on *date*."""
    if date is None:
        date = timezone.localdate()

    defaults = {
        "total_shipments": get_total_shipments(team),
        "active_shipments": get_active_shipments(team),
        "completed_shipments": get_completed_shipments(team),
        "containers_in_transit": get_containers_in_transit(team),
        "containers_delivered": get_containers_delivered(team),
        "avg_transit_days": get_average_transit_days(team),
    }

    snapshot, _ = AnalyticsSnapshot.objects.update_or_create(
        team=team,
        date=date,
        defaults=defaults,
    )
    return snapshot
