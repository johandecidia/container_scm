from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.teams.models import BaseTeamModel

from .choices import ColorSystem, ContainerCategory, ContainerCondition, ContainerStatus, EquipmentCategory
from .utils import validate_container_id


class PlannedContainerStatus(models.TextChoices):
    PLANNED = "planned", _("Planned")
    DETECTED = "detected", _("Detected")
    IN_TRANSIT = "in_transit", _("In Transit")
    ARRIVED = "arrived", _("Arrived")
    CANCELLED = "cancelled", _("Cancelled")


def equipment_type_image_path(instance, filename: str) -> str:
    ext = filename.rsplit(".", 1)[-1]
    return f"equipment_types/{instance.iso_code}.{ext}"


class EquipmentType(models.Model):
    """ISO 6346 equipment type, identified by a 4-character ISO code (e.g. 22G1 → 20GP)."""

    iso_code = models.CharField(_("ISO code"), max_length=4, primary_key=True)
    category = models.CharField(_("category"), max_length=10, choices=EquipmentCategory.choices)
    length_ft = models.PositiveSmallIntegerField(_("length (ft)"))
    high_cube = models.BooleanField(_("high cube"), default=False)
    description = models.CharField(_("description"), max_length=100)

    image = models.ImageField(
        _("image"),
        upload_to=equipment_type_image_path,
        null=True,
        blank=True,
    )

    std_external_length = models.PositiveIntegerField(_("std external length (mm)"), null=True, blank=True)
    std_external_width = models.PositiveIntegerField(_("std external width (mm)"), null=True, blank=True)
    std_external_height = models.PositiveIntegerField(_("std external height (mm)"), null=True, blank=True)

    std_tare_weight = models.PositiveIntegerField(_("std tare weight (kg)"), null=True, blank=True)
    std_max_payload = models.PositiveIntegerField(_("std max payload (kg)"), null=True, blank=True)

    std_cubic_capacity = models.DecimalField(
        _("std cubic capacity (m³)"),
        max_digits=6,
        decimal_places=2,
        null=True,
        blank=True,
    )

    is_active = models.BooleanField(_("active"), default=True)

    class Meta:
        ordering = ["length_ft", "category"]
        verbose_name = _("Equipment Type")
        verbose_name_plural = _("Equipment Types")

    def __str__(self) -> str:
        return f"{self.iso_code} — {self.description}"

    @property
    def image_url(self) -> str | None:
        return self.image.url if self.image else None


class Container(BaseTeamModel):
    """A physical shipping container identified by an ISO 6346 container ID."""

    owner_code = models.CharField(_("owner code"), max_length=3)
    category_id = models.CharField(
        _("category identifier"),
        max_length=1,
        choices=ContainerCategory.choices,
        default=ContainerCategory.U,
    )
    serial_number = models.CharField(_("serial number"), max_length=6)
    check_digit = models.PositiveSmallIntegerField(_("check digit"))

    equipment_type = models.ForeignKey(
        EquipmentType,
        on_delete=models.PROTECT,
        related_name="containers",
        verbose_name=_("equipment type"),
    )

    status = models.CharField(
        _("status"),
        max_length=20,
        choices=ContainerStatus.choices,
        default=ContainerStatus.AVAILABLE,
    )
    condition = models.CharField(
        _("condition"),
        max_length=10,
        choices=ContainerCondition.choices,
        default=ContainerCondition.GOOD,
    )

    color_code = models.CharField(_("color code"), max_length=50, blank=True)
    color_system = models.CharField(
        _("color system"),
        max_length=10,
        choices=ColorSystem.choices,
        default=ColorSystem.UNKNOWN,
    )

    manufacture_date = models.DateField(_("manufacture date"), null=True, blank=True)
    manufacturer = models.CharField(_("manufacturer"), max_length=100, blank=True)
    manufacturer_id = models.CharField(_("manufacturer ID"), max_length=100, blank=True)
    current_location = models.CharField(_("current location"), max_length=200, blank=True)
    notes = models.TextField(_("notes"), blank=True)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="containers_created",
        verbose_name=_("created by"),
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="containers_updated",
        verbose_name=_("updated by"),
    )

    class Meta:
        indexes = [
            models.Index(fields=["team", "owner_code", "category_id", "serial_number"]),
            models.Index(fields=["team", "status"]),
            models.Index(fields=["team", "condition"]),
            models.Index(fields=["team", "equipment_type"]),
            models.Index(fields=["team", "current_location"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["team", "owner_code", "category_id", "serial_number"],
                name="unique_container_per_team",
            )
        ]
        ordering = ["-created_at"]
        verbose_name = _("Container")
        verbose_name_plural = _("Containers")

    def __str__(self) -> str:
        return self.container_id

    @property
    def container_id(self) -> str:
        return f"{self.owner_code}{self.category_id}{self.serial_number}{self.check_digit}"

    def clean(self) -> None:
        super().clean()
        validate_container_id(
            self.owner_code,
            self.category_id,
            self.serial_number,
            self.check_digit,
        )

    def save(self, *args, **kwargs):
        self.owner_code = self.owner_code.upper()
        self.category_id = self.category_id.upper()
        self.full_clean()
        return super().save(*args, **kwargs)


class PlannedContainer(BaseTeamModel):
    """A container number that is planned/expected but may not yet exist at the carrier.

    Used in the container discovery workflow: planned numbers are polled against
    carrier APIs until they are detected, then transitioned to tracking.
    """

    container_number = models.CharField(
        _("container number"),
        max_length=11,
        help_text=_("Full ISO 6346 container number, e.g. MCUU1234561"),
    )
    status = models.CharField(
        _("status"),
        max_length=20,
        choices=PlannedContainerStatus.choices,
        default=PlannedContainerStatus.PLANNED,
    )
    carrier = models.CharField(_("carrier"), max_length=100, blank=True)
    shipment = models.ForeignKey(
        "scm_shipments.Shipment",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="planned_containers",
        verbose_name=_("shipment"),
    )
    # Linked actual Container once detected and validated
    container = models.ForeignKey(
        Container,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="planned_entries",
        verbose_name=_("container"),
    )
    detected_at = models.DateTimeField(_("detected at"), null=True, blank=True)
    last_checked_at = models.DateTimeField(_("last checked at"), null=True, blank=True)
    notes = models.TextField(_("notes"), blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["team", "status"]),
            models.Index(fields=["team", "container_number"]),
            models.Index(fields=["last_checked_at"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["team", "container_number"],
                name="unique_planned_container_per_team",
            )
        ]
        verbose_name = _("Planned Container")
        verbose_name_plural = _("Planned Containers")

    def __str__(self) -> str:
        return f"{self.container_number} ({self.get_status_display()})"
