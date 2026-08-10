from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.teams.models import BaseTeamModel

from .choices import (
    ColorSystem,
    ContainerCategory,
    ContainerCondition,
    ContainerStatus,
    EquipmentCategory,
    LocationSource,
    LocationType,
    MovementType,
)
from .utils import validate_container_id


class PlannedContainerStatus(models.TextChoices):
    PLANNED = "planned", _("Planned")
    DETECTED = "detected", _("Detected")
    IN_TRANSIT = "in_transit", _("In Transit")
    ARRIVED = "arrived", _("Arrived")
    CANCELLED = "cancelled", _("Cancelled")
    EXPIRED = "expired", _("Expired")


class PlannedContainerResult(models.TextChoices):
    """The outcome of the most recent discovery attempt.

    NOT_FOUND is a valid answer — the carrier does not know the number yet — and is
    kept distinct from SKIPPED (never asked) and ERROR (asked and failed).
    """

    PENDING = "pending", _("Not checked yet")
    NOT_FOUND = "not_found", _("Not known at carrier yet")
    DETECTED = "detected", _("Detected")
    SKIPPED = "skipped", _("Skipped — carrier not available")
    ERROR = "error", _("Error")


def equipment_type_image_path(instance, filename: str) -> str:
    ext = filename.rsplit(".", 1)[-1]
    return f"equipment_types/{instance.iso_code}.{ext}"


class ContainerLocation(BaseTeamModel):
    """A named location where containers can be positioned along the supply chain."""

    name = models.CharField(_("name"), max_length=200)
    location_type = models.CharField(
        _("location type"),
        max_length=30,
        choices=LocationType.choices,
        default=LocationType.UNKNOWN,
    )
    country = models.CharField(_("country"), max_length=100, blank=True)
    city = models.CharField(_("city"), max_length=100, blank=True)
    address = models.TextField(_("address"), blank=True)
    external_reference = models.CharField(_("external reference"), max_length=100, blank=True)
    owner_name = models.CharField(_("owner name"), max_length=200, blank=True)
    notes = models.TextField(_("notes"), blank=True)
    is_active = models.BooleanField(_("active"), default=True)

    class Meta:
        ordering = ["name"]
        indexes = [
            models.Index(fields=["team", "location_type"]),
            models.Index(fields=["team", "is_active"]),
        ]
        verbose_name = _("Container Location")
        verbose_name_plural = _("Container Locations")

    def __str__(self) -> str:
        parts = [self.name]
        if self.city:
            parts.append(self.city)
        if self.country:
            parts.append(self.country)
        return ", ".join(parts)


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
    current_location = models.ForeignKey(
        ContainerLocation,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="containers",
        verbose_name=_("current location"),
    )
    last_location_update = models.DateTimeField(_("last location update"), null=True, blank=True)
    location_source = models.CharField(
        _("location source"),
        max_length=30,
        choices=LocationSource.choices,
        blank=True,
    )
    location_text = models.CharField(_("location (text)"), max_length=200, blank=True)
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
            models.Index(fields=["team", "last_location_update"]),
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
    next_check_at = models.DateTimeField(_("next check at"), null=True, blank=True)
    attempts = models.PositiveIntegerField(_("discovery attempts"), default=0)
    max_attempts = models.PositiveIntegerField(
        _("max attempts"),
        null=True,
        blank=True,
        help_text=_("Give up after this many attempts. Falls back to the team/global default."),
    )
    expires_at = models.DateTimeField(
        _("expires at"),
        null=True,
        blank=True,
        help_text=_("Stop looking for this container number after this time."),
    )
    last_result = models.CharField(
        _("last result"),
        max_length=20,
        choices=PlannedContainerResult.choices,
        default=PlannedContainerResult.PENDING,
    )
    last_error_message = models.TextField(_("last error message"), blank=True)
    notes = models.TextField(_("notes"), blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["team", "status"]),
            models.Index(fields=["team", "container_number"]),
            models.Index(fields=["last_checked_at"]),
            models.Index(fields=["status", "next_check_at"]),
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


class ContainerMovement(BaseTeamModel):
    """Records a container's movement between locations, forming a position history."""

    container = models.ForeignKey(
        Container,
        on_delete=models.CASCADE,
        related_name="movements",
        verbose_name=_("container"),
    )
    from_location = models.ForeignKey(
        ContainerLocation,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="departures",
        verbose_name=_("from location"),
    )
    to_location = models.ForeignKey(
        ContainerLocation,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="arrivals",
        verbose_name=_("to location"),
    )
    movement_type = models.CharField(
        _("movement type"),
        max_length=30,
        choices=MovementType.choices,
        default=MovementType.UNKNOWN,
    )
    occurred_at = models.DateTimeField(_("occurred at"))
    source = models.CharField(
        _("source"),
        max_length=30,
        choices=LocationSource.choices,
        default=LocationSource.MANUAL,
    )
    related_shipment = models.ForeignKey(
        "scm_shipments.Shipment",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="container_movements",
        verbose_name=_("related shipment"),
    )
    related_supplier_delivery = models.ForeignKey(
        "scm_supplier_deliveries.SupplierDelivery",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="container_movements",
        verbose_name=_("related supplier delivery"),
    )
    related_tracking_event = models.ForeignKey(
        "scm_tracking.TrackingEvent",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="container_movements",
        verbose_name=_("related tracking event"),
    )
    notes = models.TextField(_("notes"), blank=True)

    class Meta:
        ordering = ["-occurred_at", "-created_at"]
        indexes = [
            models.Index(fields=["team", "container"]),
            models.Index(fields=["team", "occurred_at"]),
        ]
        verbose_name = _("Container Movement")
        verbose_name_plural = _("Container Movements")

    def __str__(self) -> str:
        return f"{self.container} → {self.to_location} ({self.occurred_at:%Y-%m-%d})"
