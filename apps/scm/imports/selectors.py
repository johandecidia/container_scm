# Import selectors — all read/query operations.
from django.db.models import QuerySet

from apps.teams.models import Team

from .models import ImportJob


def get_team_import_jobs(team: Team) -> QuerySet[ImportJob]:
    return ImportJob.for_team.filter(team=team).order_by("-created_at")


def get_pending_import_jobs() -> QuerySet[ImportJob]:
    return ImportJob.objects.filter(status=ImportJob.Status.PENDING)
