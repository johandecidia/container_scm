from django.contrib.contenttypes.models import ContentType
from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.teams.models import BaseTeamModel
from apps.users.models import CustomUser


class ImportJob(BaseTeamModel):
    """A data import job submitted by a team member."""

    class Status(models.TextChoices):
        UPLOADED = "uploaded", _("Uploaded")
        PARSING = "parsing", _("Parsing")
        PARSED = "parsed", _("Parsed")
        VALIDATING = "validating", _("Validating")
        VALIDATED = "validated", _("Validated")
        IMPORTING = "importing", _("Importing")
        COMPLETED = "completed", _("Completed")
        FAILED = "failed", _("Failed")
        CANCELLED = "cancelled", _("Cancelled")

    class ImportType(models.TextChoices):
        CONTAINERS = "containers", _("Containers")
        PURCHASE_ORDERS = "purchase_orders", _("Purchase Orders")
        SHIPMENTS = "shipments", _("Shipments")
        TRACKING_EVENTS = "tracking_events", _("Tracking Events")
        RATES = "rates", _("Rates")

    created_by = models.ForeignKey(
        CustomUser,
        on_delete=models.SET_NULL,
        null=True,
        related_name="scm_import_jobs",
        verbose_name=_("created by"),
    )
    file = models.FileField(_("file"), upload_to="imports/%Y/%m/")
    original_filename = models.CharField(_("original filename"), max_length=255)
    import_type = models.CharField(_("import type"), max_length=20, choices=ImportType.choices)
    status = models.CharField(_("status"), max_length=20, choices=Status.choices, default=Status.UPLOADED)
    total_rows = models.PositiveIntegerField(_("total rows"), default=0)
    valid_rows = models.PositiveIntegerField(_("valid rows"), default=0)
    invalid_rows = models.PositiveIntegerField(_("invalid rows"), default=0)
    processed_rows = models.PositiveIntegerField(_("processed rows"), default=0)
    started_at = models.DateTimeField(_("started at"), null=True, blank=True)
    completed_at = models.DateTimeField(_("completed at"), null=True, blank=True)
    metadata = models.JSONField(_("metadata"), default=dict, blank=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = _("Import Job")
        verbose_name_plural = _("Import Jobs")

    def __str__(self) -> str:
        return f"ImportJob #{self.pk} ({self.import_type}, {self.status})"


class ImportRow(models.Model):
    """A single row from an import file."""

    class Status(models.TextChoices):
        PENDING = "pending", _("Pending")
        VALID = "valid", _("Valid")
        INVALID = "invalid", _("Invalid")
        IMPORTED = "imported", _("Imported")
        SKIPPED = "skipped", _("Skipped")

    import_job = models.ForeignKey(
        ImportJob,
        on_delete=models.CASCADE,
        related_name="rows",
        verbose_name=_("import job"),
    )
    row_number = models.PositiveIntegerField(_("row number"))
    raw_data = models.JSONField(_("raw data"), default=dict)
    mapped_data = models.JSONField(_("mapped data"), default=dict)
    validated_data = models.JSONField(_("validated data"), default=dict)
    status = models.CharField(_("status"), max_length=20, choices=Status.choices, default=Status.PENDING)
    errors = models.JSONField(_("errors"), default=list)
    imported_object_id = models.PositiveIntegerField(_("imported object ID"), null=True, blank=True)
    imported_content_type = models.ForeignKey(
        ContentType,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        verbose_name=_("imported content type"),
    )

    class Meta:
        ordering = ["row_number"]
        verbose_name = _("Import Row")
        verbose_name_plural = _("Import Rows")
        unique_together = [["import_job", "row_number"]]

    def __str__(self) -> str:
        return f"Row {self.row_number} of ImportJob #{self.import_job_id}"


class ImportError(models.Model):
    """A validation error or warning linked to an import job or row."""

    class Severity(models.TextChoices):
        ERROR = "error", _("Error")
        WARNING = "warning", _("Warning")
        INFO = "info", _("Info")

    import_job = models.ForeignKey(
        ImportJob,
        on_delete=models.CASCADE,
        related_name="import_errors",
        verbose_name=_("import job"),
    )
    import_row = models.ForeignKey(
        ImportRow,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="import_errors",
        verbose_name=_("import row"),
    )
    code = models.CharField(_("code"), max_length=50)
    message = models.TextField(_("message"))
    field_name = models.CharField(_("field name"), max_length=100, blank=True)
    severity = models.CharField(_("severity"), max_length=10, choices=Severity.choices, default=Severity.ERROR)

    class Meta:
        ordering = ["severity", "import_row__row_number"]
        verbose_name = _("Import Error")
        verbose_name_plural = _("Import Errors")

    def __str__(self) -> str:
        return f"[{self.severity}] {self.code}: {self.message[:60]}"


class ImportTemplate(models.Model):
    """A reusable column mapping template for a given import type."""

    team = models.ForeignKey(
        "teams.Team",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="import_templates",
        verbose_name=_("team"),
    )
    name = models.CharField(_("name"), max_length=100)
    import_type = models.CharField(_("import type"), max_length=20, choices=ImportJob.ImportType.choices)
    mapping = models.JSONField(_("mapping"), default=dict)
    is_default = models.BooleanField(_("is default"), default=False)

    class Meta:
        verbose_name = _("Import Template")
        verbose_name_plural = _("Import Templates")

    def __str__(self) -> str:
        return f"{self.name} ({self.import_type})"
