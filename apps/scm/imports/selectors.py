# Import selectors — all read/query operations.
from django.db.models import QuerySet

from apps.teams.models import Team

from .models import ImportJob


def get_team_import_jobs(team: Team) -> QuerySet[ImportJob]:
    return ImportJob.for_team.filter(team=team).order_by("-created_at")


def get_pending_import_jobs() -> QuerySet[ImportJob]:
    # Cross-team query — intended for Celery workers processing all pending jobs system-wide.
    # Do NOT use this in user-facing views; use get_team_import_jobs() instead.
    return ImportJob.objects.filter(status=ImportJob.Status.PENDING)
