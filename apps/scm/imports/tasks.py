from celery import shared_task


@shared_task
def process_import_job(job_id: int) -> None:
    """Background task to process a data import job."""
    from .models import ImportJob

    job = ImportJob.objects.get(pk=job_id)
    job.status = ImportJob.Status.PROCESSING
    job.save(update_fields=["status", "updated_at"])

    try:
        # TODO: implement import processing logic
        job.status = ImportJob.Status.COMPLETED
        job.save(update_fields=["status", "updated_at"])
    except Exception as exc:
        job.status = ImportJob.Status.FAILED
        job.error_message = str(exc)
        job.save(update_fields=["status", "error_message", "updated_at"])
        raise
