import logging

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task(bind=True, max_retries=3, default_retry_delay=30)
def async_parse_import_job(self, job_id: int) -> None:
    """Parse an import job asynchronously (optional background alternative to inline parsing).

    Safe to retry: parse_import_job marks the job as PARSING before touching rows,
    so a retry after a crash will reprocess from a clean state.
    """
    from .models import ImportJob
    from .services import parse_import_job

    try:
        job = ImportJob.objects.get(pk=job_id)
    except ImportJob.DoesNotExist:
        logger.warning("async_parse_import_job: job %s not found — skipping.", job_id)
        return

    logger.info("async_parse_import_job: starting parse for job %s (type=%s).", job.pk, job.import_type)
    try:
        parse_import_job(job)
    except Exception as exc:
        logger.exception("async_parse_import_job: job %s failed: %s", job.pk, exc)
        raise self.retry(exc=exc) from exc
    logger.info("async_parse_import_job: finished parse for job %s.", job.pk)


@shared_task(bind=True, max_retries=3, default_retry_delay=30)
def async_validate_import_job(self, job_id: int) -> None:
    """Validate an import job asynchronously.

    Safe to retry: validation is read-only until the final status update.
    """
    from .models import ImportJob
    from .services import validate_import_job

    try:
        job = ImportJob.objects.get(pk=job_id)
    except ImportJob.DoesNotExist:
        logger.warning("async_validate_import_job: job %s not found — skipping.", job_id)
        return

    logger.info("async_validate_import_job: starting validation for job %s.", job.pk)
    try:
        validate_import_job(job)
    except Exception as exc:
        logger.exception("async_validate_import_job: job %s failed: %s", job.pk, exc)
        raise self.retry(exc=exc) from exc
    logger.info("async_validate_import_job: finished validation for job %s.", job.pk)
