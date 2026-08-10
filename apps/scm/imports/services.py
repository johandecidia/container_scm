# Import services — all business logic and write operations.
from django.utils import timezone

from apps.scm.audit_log.models import SCMAuditLog
from apps.scm.audit_log.services import log_scm_action
from apps.scm.monitoring import get_scm_logger, log_import_completed, log_import_failed, log_import_started
from apps.teams.models import Team
from apps.users.models import CustomUser

from .mappings import map_import_rows
from .models import ImportError, ImportJob, ImportRow
from .parsers import create_import_rows, parse_file
from .schemas import validate_row_data
from .validators import validate_all_rows

logger = get_scm_logger(__name__)

# Job-level error codes for feedback reported by a document extraction service.
EXTRACTION_WARNING_CODE = "extraction_warning"
EXTRACTION_REVIEW_CODE = "extraction_requires_review"

# Diagnostic metadata keys cleared at the start of every parse run so a retry
# never shows the previous attempt's failure.
_STALE_ERROR_KEYS = ("parse_error", "extract_error", "validate_error")


def create_import_job(team: Team, created_by: CustomUser, file, import_type: str) -> ImportJob:
    """Create a new ImportJob from an uploaded file."""
    original_filename = getattr(file, "name", "") or ""
    job = ImportJob.objects.create(
        team=team,
        created_by=created_by,
        file=file,
        original_filename=original_filename,
        import_type=import_type,
        status=ImportJob.Status.UPLOADED,
    )
    log_scm_action(
        team=team,
        action=SCMAuditLog.Action.IMPORT_STARTED,
        object_type="ImportJob",
        object_id=str(job.pk),
        object_repr=f"Import {import_type} ({original_filename})",
        metadata={"import_type": import_type, "filename": original_filename},
        actor=created_by,
    )
    return job


def parse_import_job(job: ImportJob) -> ImportJob:
    """Parse the uploaded file, create rows, apply mapping, run Pydantic validation."""
    log_import_started(logger, job.pk, job.import_type, job.team_id)
    job.status = ImportJob.Status.PARSING
    job.started_at = timezone.now()
    for key in _STALE_ERROR_KEYS:
        job.metadata.pop(key, None)
    job.save(update_fields=["status", "started_at", "metadata", "updated_at"])
    try:
        raw_rows = parse_file(job)
        create_import_rows(job, raw_rows)
        # A file that yields no rows is a failed import, not a completed empty one.
        if not job.total_rows:
            raise ValueError("No rows could be read from the file — nothing to import.")
        map_import_rows(job)
        _pydantic_validate_rows(job)
        _record_extraction_warnings(job)
        job.status = ImportJob.Status.PARSED
        job.save(update_fields=["status", "updated_at"])
        log_import_completed(logger, job.pk, job.import_type, job.team_id, job.total_rows or 0, 0)
    except Exception as exc:
        job.status = ImportJob.Status.FAILED
        job.metadata["parse_error"] = str(exc)
        job.save(update_fields=["status", "metadata", "updated_at"])
        log_import_failed(logger, job.pk, job.import_type, job.team_id, str(exc))
        raise
    return job


def _record_extraction_warnings(job: ImportJob) -> None:
    """Persist document-extraction feedback as job-level ImportError warnings.

    The extraction service answers HTTP 200 with warnings when it recognised the
    document only partially.  Rows exist so the import may proceed, but the user
    must see what was uncertain before confirming.  Re-running a parse replaces
    the previous run's warnings rather than appending to them.
    """
    ImportError.objects.filter(
        import_job=job,
        import_row__isnull=True,
        code__in=(EXTRACTION_WARNING_CODE, EXTRACTION_REVIEW_CODE),
    ).delete()

    entries = [
        ImportError(
            import_job=job,
            code=EXTRACTION_WARNING_CODE,
            message=str(warning),
            severity=ImportError.Severity.WARNING,
        )
        for warning in job.metadata.get("extraction_warnings") or []
    ]

    if job.metadata.get("extraction_requires_review"):
        confidence = job.metadata.get("extraction_confidence")
        detail = f" (confidence {confidence})" if confidence is not None else ""
        entries.append(
            ImportError(
                import_job=job,
                code=EXTRACTION_REVIEW_CODE,
                message=(
                    f"Document extraction flagged this import for manual review{detail}. "
                    "Check every row before confirming."
                ),
                severity=ImportError.Severity.WARNING,
            )
        )

    ImportError.objects.bulk_create(entries)


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
            row.status = ImportRow.Status.VALID
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
