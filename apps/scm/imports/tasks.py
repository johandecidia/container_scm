from celery import shared_task


@shared_task
def async_parse_import_job(job_id: int) -> None:
    """Parse an import job asynchronously (optional background alternative to inline parsing)."""
    from .models import ImportJob
    from .services import parse_import_job

    job = ImportJob.objects.get(pk=job_id)
    parse_import_job(job)


@shared_task
def async_validate_import_job(job_id: int) -> None:
    """Validate an import job asynchronously."""
    from .models import ImportJob
    from .services import validate_import_job

    job = ImportJob.objects.get(pk=job_id)
    validate_import_job(job)
