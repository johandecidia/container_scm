from django.conf import settings
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.teams.models import BaseTeamModel
from apps.utils.models import BaseModel


class Shipment(BaseTeamModel):
    """End-to-end shipment lifecycle owned by a team."""

    class Status(models.TextChoices):
        DRAFT = "DRAFT", _("Draft")
        BOOKED = "BOOKED", _("Booked")
        IN_TRANSIT = "IN_TRANSIT", _("In Transit")
        ARRIVED = "ARRIVED", _("Arrived")
        PARTIALLY_RECEIVED = "PARTIALLY_RECEIVED", _("Partially Received")
        DELIVERED = "DELIVERED", _("Delivered")
        CANCELLED = "CANCELLED", _("Cancelled")
        EXCEPTION = "EXCEPTION", _("Exception")

    shipment_number = models.CharField(_("shipment number"), max_length=100, blank=True)
    reference = models.CharField(_("reference"), max_length=100, blank=True)
    customer_name = models.CharField(_("customer name"), max_length=200, blank=True)

    # Carrier & booking identifiers
    # TODO: full carrier integration will live in apps/scm/integrations/
    carrier = models.CharField(_("carrier"), max_length=100, blank=True)
    carrier_booking_reference = models.CharField(_("carrier booking reference"), max_length=100, blank=True)
    bill_of_lading_number = models.CharField(_("bill of lading number"), max_length=100, blank=True)

    # Routing
    origin_port = models.CharField(_("origin port"), max_length=200, blank=True)
    destination_port = models.CharField(_("destination port"), max_length=200, blank=True)

    # Dates
    etd = models.DateField(_("estimated departure"), null=True, blank=True)
    eta = models.DateField(_("estimated arrival"), null=True, blank=True)
    original_eta = models.DateField(_("original ETA"), null=True, blank=True)
    eta_source = models.CharField(_("ETA source"), max_length=50, blank=True)
    eta_last_updated = models.DateTimeField(_("ETA last updated"), null=True, blank=True)
    eta_confidence = models.CharField(_("ETA confidence"), max_length=20, blank=True)
    actual_departure_at = models.DateTimeField(_("actual departure"), null=True, blank=True)
    actual_arrival_at = models.DateTimeField(_("actual arrival"), null=True, blank=True)

    # Status
    status = models.CharField(_("status"), max_length=20, choices=Status.choices, default=Status.DRAFT)

    # Tracking fields — real tracking sync will live in apps/scm/tracking/
    # External API clients will live in apps/scm/integrations/
    tracking_status = models.CharField(_("tracking status"), max_length=200, blank=True)
    last_tracking_sync_at = models.DateTimeField(_("last tracking sync"), null=True, blank=True)

    notes = models.TextField(_("notes"), blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_shipments",
        verbose_name=_("created by"),
    )

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["team", "status"]),
            models.Index(fields=["team", "eta"]),
            models.Index(fields=["team", "etd"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["team", "shipment_number"],
                condition=models.Q(shipment_number__gt=""),
                name="unique_team_shipment_number",
            ),
        ]

    def __str__(self) -> str:
        return self.shipment_number or self.reference or f"Shipment #{self.pk}"

    @property
    def route_label(self) -> str:
        """The route in one line, e.g. "Shanghai → Gothenburg".

        Only the ports that are recorded: a shipment with one known port reads as
        that port rather than as an arrow pointing at nothing. Empty when neither
        is known, so callers can test it for truthiness.
        """
        return " → ".join(part for part in (self.origin_port, self.destination_port) if part)


class ShipmentContainer(BaseModel):
    """Through model linking a Container to a Shipment."""

    shipment = models.ForeignKey(
        Shipment,
        on_delete=models.CASCADE,
        related_name="shipment_containers",
        verbose_name=_("shipment"),
    )
    # String ref to avoid import-time circular dependency; app label from ContainersConfig
    container = models.ForeignKey(
        "scm_containers.Container",
        on_delete=models.CASCADE,
        related_name="shipment_containers",
        verbose_name=_("container"),
    )
    sequence = models.PositiveIntegerField(_("sequence"), default=0)
    seal_number = models.CharField(_("seal number"), max_length=100, blank=True)
    gross_weight_kg = models.DecimalField(
        _("gross weight (kg)"), max_digits=10, decimal_places=2, null=True, blank=True
    )
    loaded_at = models.DateTimeField(_("loaded at"), null=True, blank=True)
    discharged_at = models.DateTimeField(_("discharged at"), null=True, blank=True)
    notes = models.TextField(_("notes"), blank=True)

    class Meta:
        ordering = ["sequence", "created_at"]
        constraints = [
            models.UniqueConstraint(fields=["shipment", "container"], name="unique_container_per_shipment"),
        ]
        indexes = [
            models.Index(fields=["shipment", "sequence"]),
        ]

    def __str__(self) -> str:
        return f"{self.shipment} / {self.container}"

    def clean(self) -> None:
        from django.core.exceptions import ValidationError

        if self.shipment_id and self.container_id and self.shipment.team_id != self.container.team_id:
            raise ValidationError(_("Shipment and container must belong to the same team."))


class ShipmentEvent(BaseModel):
    """Timeline event recording what happened to a shipment over time."""

    class EventType(models.TextChoices):
        CREATED = "CREATED", _("Created")
        STATUS_CHANGED = "STATUS_CHANGED", _("Status Changed")
        CONTAINER_ADDED = "CONTAINER_ADDED", _("Container Added")
        CONTAINER_REMOVED = "CONTAINER_REMOVED", _("Container Removed")
        ETA_UPDATED = "ETA_UPDATED", _("ETA Updated")
        DELIVERED = "DELIVERED", _("Delivered")
        CANCELLED = "CANCELLED", _("Cancelled")
        # TODO: TRACKING_UPDATED events will be created by apps/scm/tracking/ Celery tasks
        TRACKING_UPDATED = "TRACKING_UPDATED", _("Tracking Updated")
        SUPPLIER_DELIVERY_LINKED = "SUPPLIER_DELIVERY_LINKED", _("Supplier Delivery Linked")
        EXCEPTION = "EXCEPTION", _("Exception")
        NOTE_ADDED = "NOTE_ADDED", _("Note Added")

    shipment = models.ForeignKey(
        Shipment,
        on_delete=models.CASCADE,
        related_name="events",
        verbose_name=_("shipment"),
    )
    event_type = models.CharField(_("event type"), max_length=50, choices=EventType.choices)
    description = models.CharField(_("description"), max_length=500)
    occurred_at = models.DateTimeField(_("occurred at"), default=timezone.now)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_shipment_events",
        verbose_name=_("created by"),
    )
    # metadata allows future tracking payloads without schema changes
    metadata = models.JSONField(_("metadata"), default=dict, blank=True)

    class Meta:
        ordering = ["-occurred_at", "-created_at"]
        indexes = [
            models.Index(fields=["shipment", "-occurred_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.get_event_type_display()} — {self.shipment}"
