# Analytics selectors — all read/query operations.

import datetime

from django.db.models import QuerySet
from django.utils import timezone

from apps.teams.models import Team

from .models import AnalyticsSnapshot


def get_snapshots_for_team(team: Team) -> QuerySet[AnalyticsSnapshot]:
    """Return all snapshots for *team*, newest first."""
    return AnalyticsSnapshot.objects.filter(team=team).order_by("-date")


def get_latest_snapshot(team: Team) -> AnalyticsSnapshot | None:
    """Return the most recent snapshot for *team*, or None if none exist."""
    return AnalyticsSnapshot.objects.filter(team=team).order_by("-date").first()


def get_snapshot_for_date(team: Team, date: datetime.date) -> AnalyticsSnapshot | None:
    """Return the snapshot for *team* on *date*, or None."""
    return AnalyticsSnapshot.objects.filter(team=team, date=date).first()


def get_live_dashboard_stats(team: Team) -> dict:
    """Return live-computed KPI counts directly from the operational models.

    These reflect the current state of the database, unlike AnalyticsSnapshot
    which only captures a daily point-in-time view.
    """
    from apps.scm.containers.models import Container
    from apps.scm.imports.models import ImportJob
    from apps.scm.shipments.models import Shipment
    from apps.scm.tracking.models import TrackingSubscription

    today = timezone.now().date()
    active_statuses = [Shipment.Status.BOOKED, Shipment.Status.IN_TRANSIT]

    return {
        "active_shipments": Shipment.objects.filter(team=team, status__in=active_statuses).count(),
        "delayed_shipments": Shipment.objects.filter(team=team, status__in=active_statuses, eta__lt=today).count(),
        "containers_in_transit": Container.objects.filter(team=team, status="IN_TRANSIT").count(),
        "containers_available": Container.objects.filter(team=team, status="AVAILABLE").count(),
        "tracking_issues": TrackingSubscription.objects.filter(team=team, status="FAILED").count(),
        "recent_imports": ImportJob.objects.filter(team=team).order_by("-created_at")[:5],
    }
