from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.teams.models import BaseTeamModel
from apps.users.models import CustomUser


class ImportJob(BaseTeamModel):
    """A data import job submitted by a team member."""

    class Status(models.TextChoices):
        PENDING = "pending", _("Pending")
        PROCESSING = "processing", _("Processing")
        COMPLETED = "completed", _("Completed")
        FAILED = "failed", _("Failed")

    file = models.FileField(_("file"), upload_to="imports/%Y/%m/")
    status = models.CharField(_("status"), max_length=20, choices=Status.choices, default=Status.PENDING)
    submitted_by = models.ForeignKey(
        CustomUser,
        on_delete=models.SET_NULL,
        null=True,
        related_name="scm_import_jobs",
        verbose_name=_("submitted by"),
    )
    error_message = models.TextField(_("error message"), blank=True)
    rows_processed = models.PositiveIntegerField(_("rows processed"), default=0)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"ImportJob #{self.pk} ({self.status})"
