# Import services — all business logic and write operations.
from django.utils import timezone

from apps.teams.models import Team
from apps.users.models import CustomUser

from .mappings import map_import_rows
from .models import ImportJob, ImportRow
from .parsers import create_import_rows, parse_file
from .schemas import validate_row_data
from .validators import validate_all_rows


def create_import_job(team: Team, created_by: CustomUser, file, import_type: str) -> ImportJob:
    """Create a new ImportJob from an uploaded file."""
    original_filename = getattr(file, "name", "") or ""
    return ImportJob.objects.create(
        team=team,
        created_by=created_by,
        file=file,
        original_filename=original_filename,
        import_type=import_type,
        status=ImportJob.Status.UPLOADED,
    )


def parse_import_job(job: ImportJob) -> ImportJob:
    """Parse the uploaded file, create rows, apply mapping, run Pydantic validation."""
    job.status = ImportJob.Status.PARSING
    job.started_at = timezone.now()
    job.save(update_fields=["status", "started_at", "updated_at"])
    try:
        raw_rows = parse_file(job)
        create_import_rows(job, raw_rows)
        map_import_rows(job)
        _pydantic_validate_rows(job)
        job.status = ImportJob.Status.PARSED
        job.save(update_fields=["status", "updated_at"])
    except Exception as exc:
        job.status = ImportJob.Status.FAILED
        job.metadata["parse_error"] = str(exc)
        job.save(update_fields=["status", "metadata", "updated_at"])
        raise
    return job


def _pydantic_validate_rows(job: ImportJob) -> None:
    """Run Pydantic schema validation on all rows and store validated_data."""
    rows = list(job.rows.all())
    for row in rows:
        validated_data, errors = validate_row_data(job.import_type, row.mapped_data)
        row.validated_data = validated_data
        if errors:
            row.errors = errors
            row.status = ImportRow.Status.INVALID
        else:
            row.errors = []
    ImportRow.objects.bulk_update(rows, ["validated_data", "errors", "status"])


def validate_import_job(job: ImportJob) -> ImportJob:
    """Run DB / business-rule validation on all rows."""
    job.status = ImportJob.Status.VALIDATING
    job.save(update_fields=["status", "updated_at"])
    try:
        validate_all_rows(job, job.team)
        job.status = ImportJob.Status.VALIDATED
        job.save(update_fields=["status", "updated_at"])
    except Exception as exc:
        job.status = ImportJob.Status.FAILED
        job.metadata["validate_error"] = str(exc)
        job.save(update_fields=["status", "metadata", "updated_at"])
        raise
    return job


def confirm_import_job(job: ImportJob, *, update_existing: bool = False) -> ImportJob:
    """Confirm and run the actual import."""
    from .importers import run_import

    run_import(job, update_existing=update_existing)
    return job
