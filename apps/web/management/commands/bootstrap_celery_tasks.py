from django.conf import settings
from django.core.management.base import BaseCommand
from django_celery_beat.models import PeriodicTask
from django_celery_beat.schedulers import ModelEntry


class Command(BaseCommand):
    """Write settings.SCHEDULED_TASKS into the database Beat schedule.

    Celery Beat runs the DatabaseScheduler, so a task defined only in settings is
    never executed — this command is what makes the schedule real. It is idempotent
    and safe to run on every deploy and every container start.
    """

    help = "Bootstrap Celery periodic tasks for the environment."

    def add_arguments(self, parser):
        parser.add_argument(
            "--remove-stale",
            action="store_true",
            help="Remove tasks that are not defined in this command",
        )

    def handle(self, *args, **options):
        created_task_names = []
        for task_name, entry in settings.SCHEDULED_TASKS.items():
            # Copied, not mutated: SCHEDULED_TASKS is module-level state, and popping
            # from it would leave the setting broken for anything that reads it later
            # in the same process.
            task_config = dict(entry)
            schedule_spec = task_config.pop("schedule")
            try:
                schedule, field = ModelEntry.to_model_schedule(schedule_spec)
            except ValueError:
                self.stderr.write(self.style.ERROR(f"Invalid schedule type for task {task_name}: {schedule_spec!r}"))

            task_config[field] = schedule
            task, created = PeriodicTask.objects.update_or_create(
                name=task_name,
                defaults=task_config,
            )

            if created:
                self.stdout.write(self.style.SUCCESS(f"- Created task: {task_name}"))
            else:
                self.stdout.write(self.style.SUCCESS(f"- Updated task: {task_name}"))

            created_task_names.append(task_name)

        if options["remove_stale"]:
            stale_tasks = PeriodicTask.objects.exclude(name__in=created_task_names).exclude(
                name__startswith="celery."  # Don't remove internal Celery tasks
            )

            for task in stale_tasks:
                self.stdout.write(self.style.WARNING(f"- Removing stale task: {task.name}"))
                task.delete()

        self.stdout.write(self.style.SUCCESS("Successfully bootstrapped Celery periodic tasks"))
