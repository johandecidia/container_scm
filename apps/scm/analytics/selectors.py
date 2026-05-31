# Analytics selectors — all read/query operations.

import datetime

from django.db.models import QuerySet

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
