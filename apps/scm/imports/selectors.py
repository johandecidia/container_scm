# Import selectors — all read/query operations.
from django.db.models import QuerySet
from django.shortcuts import get_object_or_404

from apps.teams.models import Team

from .models import ImportError, ImportJob, ImportRow


def get_team_import_jobs(team: Team) -> QuerySet[ImportJob]:
    return ImportJob.objects.filter(team=team).select_related("created_by").order_by("-created_at")


def get_import_job(team: Team, pk: int) -> ImportJob:
    return get_object_or_404(ImportJob, team=team, pk=pk)


def get_import_rows(job: ImportJob) -> QuerySet[ImportRow]:
    return job.rows.all()


def get_valid_rows(job: ImportJob) -> QuerySet[ImportRow]:
    return job.rows.filter(status=ImportRow.Status.VALID)


def get_invalid_rows(job: ImportJob) -> QuerySet[ImportRow]:
    return job.rows.filter(status=ImportRow.Status.INVALID)


def get_import_errors(job: ImportJob) -> QuerySet[ImportError]:
    return (
        ImportError.objects.filter(import_job=job)
        .select_related("import_row")
        .order_by("severity", "import_row__row_number")
    )


# Statuses a job can only reach after its file has been read.
_POST_PARSE_STATUSES = (
    ImportJob.Status.PARSED,
    ImportJob.Status.VALIDATING,
    ImportJob.Status.VALIDATED,
    ImportJob.Status.IMPORTING,
    ImportJob.Status.COMPLETED,
)


def get_job_summary(job: ImportJob) -> dict:
    """Return a summary dict suitable for preview / stats display."""
    return {
        "total_rows": job.total_rows,
        "valid_rows": job.valid_rows,
        "invalid_rows": job.invalid_rows,
        "processed_rows": job.processed_rows,
        "status": job.status,
        "import_type": job.import_type,
        # A parsed job with no rows means the file was read but nothing usable
        # came out of it — the UI must not present that as a success.
        "has_no_rows": job.total_rows == 0 and job.status in _POST_PARSE_STATUSES,
    }


def get_pending_import_jobs() -> QuerySet[ImportJob]:
    """Cross-team query for background workers — do NOT use in user-facing views."""
    return ImportJob.objects.filter(status=ImportJob.Status.UPLOADED)
