import logging

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task
def async_parse_import_job(job_id: int) -> None:
    """Parse an import job asynchronously (optional background alternative to inline parsing)."""
    from .models import ImportJob
    from .services import parse_import_job

    try:
        job = ImportJob.objects.get(pk=job_id)
    except ImportJob.DoesNotExist:
        logger.warning("async_parse_import_job: job %s not found — skipping.", job_id)
        return

    logger.info("async_parse_import_job: starting parse for job %s (type=%s).", job.pk, job.import_type)
    parse_import_job(job)
    logger.info("async_parse_import_job: finished parse for job %s.", job.pk)


@shared_task
def async_validate_import_job(job_id: int) -> None:
    """Validate an import job asynchronously."""
    from .models import ImportJob
    from .services import validate_import_job

    try:
        job = ImportJob.objects.get(pk=job_id)
    except ImportJob.DoesNotExist:
        logger.warning("async_validate_import_job: job %s not found — skipping.", job_id)
        return

    logger.info("async_validate_import_job: starting validation for job %s.", job.pk)
    validate_import_job(job)
    logger.info("async_validate_import_job: finished validation for job %s.", job.pk)
