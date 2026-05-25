# Import services — all business logic and write operations.
from apps.teams.models import Team
from apps.users.models import CustomUser

from .models import ImportJob


def create_import_job(team: Team, submitted_by: CustomUser, file) -> ImportJob:
    job = ImportJob.objects.create(team=team, submitted_by=submitted_by, file=file)
    from .tasks import process_import_job

    process_import_job.delay(job.pk)
    return job


def mark_import_failed(job: ImportJob, error_message: str) -> ImportJob:
    job.status = ImportJob.Status.FAILED
    job.error_message = error_message
    job.save(update_fields=["status", "error_message", "updated_at"])
    return job
