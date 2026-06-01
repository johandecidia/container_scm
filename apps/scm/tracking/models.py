# Tracking models — schema only; no business logic.
from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.teams.models import BaseTeamModel
from apps.utils.models import BaseModel


class TrackingProvider(BaseModel):
    """Represents an external tracking source (carrier API, scraping, webhook, manual)."""

    class ProviderType(models.TextChoices):
        API = "api", _("API")
        SCRAPING = "scraping", _("Scraping")
        WEBHOOK = "webhook", _("Webhook")
        MANUAL = "manual", _("Manual")

    code = models.CharField(_("code"), max_length=50, unique=True)
    name = models.CharField(_("name"), max_length=200)
    provider_type = models.CharField(
        _("provider type"), max_length=20, choices=ProviderType.choices, default=ProviderType.API
    )
    base_url = models.URLField(_("base URL"), blank=True)
    is_active = models.BooleanField(_("is active"), default=True)
    notes = models.TextField(_("notes"), blank=True)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


class TrackingSubscription(BaseTeamModel):
    """Represents an active or historical tracking watch for a shipment or container."""

    class Status(models.TextChoices):
        ACTIVE = "active", _("Active")
        PAUSED = "paused", _("Paused")
        SYNCING = "syncing", _("Syncing")
        COMPLETED = "completed", _("Completed")
        FAILED = "failed", _("Failed")
        CANCELLED = "cancelled", _("Cancelled")

    class ReferenceType(models.TextChoices):
        CONTAINER_NUMBER = "container_number", _("Container Number")
        BOOKING_NUMBER = "booking_number", _("Booking Number")
        BILL_OF_LADING = "bill_of_lading", _("Bill of Lading")
        SHIPMENT_REFERENCE = "shipment_reference", _("Shipment Reference")
        MANUAL = "manual", _("Manual")

    shipment = models.ForeignKey(
        "scm_shipments.Shipment",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="tracking_subscriptions",
        verbose_name=_("shipment"),
    )
    container = models.ForeignKey(
        "scm_containers.Container",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="tracking_subscriptions",
        verbose_name=_("container"),
    )
    provider = models.ForeignKey(
        TrackingProvider,
        on_delete=models.PROTECT,
        related_name="subscriptions",
        verbose_name=_("provider"),
    )
    tracking_reference = models.CharField(_("tracking reference"), max_length=200)
    reference_type = models.CharField(
        _("reference type"), max_length=30, choices=ReferenceType.choices, default=ReferenceType.CONTAINER_NUMBER
    )
    status = models.CharField(_("status"), max_length=20, choices=Status.choices, default=Status.ACTIVE)

    # Sync state
    last_synced_at = models.DateTimeField(_("last synced at"), null=True, blank=True)
    next_sync_at = models.DateTimeField(_("next sync at"), null=True, blank=True)

    # Error tracking
    last_error_message = models.TextField(_("last error message"), blank=True)
    last_error_at = models.DateTimeField(_("last error at"), null=True, blank=True)
    consecutive_failures = models.PositiveIntegerField(_("consecutive failures"), default=0)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["team", "status"]),
            models.Index(fields=["team", "provider"]),
            models.Index(fields=["team", "shipment"]),
            models.Index(fields=["team", "container"]),
            models.Index(fields=["next_sync_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.tracking_reference} ({self.get_reference_type_display()})"


class TrackingEvent(BaseTeamModel):
    """Stores normalised tracking events from any provider."""

    class EventType(models.TextChoices):
        BOOKING_CREATED = "booking_created", _("Booking Created")
        EMPTY_RELEASED = "empty_released", _("Empty Released")
        GATE_IN = "gate_in", _("Gate In")
        LOADED_ON_VESSEL = "loaded_on_vessel", _("Loaded on Vessel")
        VESSEL_DEPARTED = "vessel_departed", _("Vessel Departed")
        TRANSSHIPMENT_ARRIVED = "transshipment_arrived", _("Transshipment Arrived")
        TRANSSHIPMENT_DEPARTED = "transshipment_departed", _("Transshipment Departed")
        VESSEL_ARRIVED = "vessel_arrived", _("Vessel Arrived")
        DISCHARGED = "discharged", _("Discharged")
        GATE_OUT = "gate_out", _("Gate Out")
        DELIVERED = "delivered", _("Delivered")
        CUSTOMS_HOLD = "customs_hold", _("Customs Hold")
        DELAY = "delay", _("Delay")
        ETA_UPDATED = "eta_updated", _("ETA Updated")
        UNKNOWN = "unknown", _("Unknown")

    subscription = models.ForeignKey(
        TrackingSubscription,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="events",
        verbose_name=_("subscription"),
    )
    shipment = models.ForeignKey(
        "scm_shipments.Shipment",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="tracking_events",
        verbose_name=_("shipment"),
    )
    container = models.ForeignKey(
        "scm_containers.Container",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="tracking_events",
        verbose_name=_("container"),
    )
    provider = models.ForeignKey(
        TrackingProvider,
        on_delete=models.PROTECT,
        related_name="events",
        verbose_name=_("provider"),
    )
    event_type = models.CharField(_("event type"), max_length=40, choices=EventType.choices, default=EventType.UNKNOWN)
    event_code = models.CharField(_("event code"), max_length=100, blank=True)
    status = models.CharField(_("status"), max_length=200, blank=True)
    description = models.TextField(_("description"), blank=True)

    # Location
    location_name = models.CharField(_("location name"), max_length=200, blank=True)
    location_unlocode = models.CharField(_("UN/LOCODE"), max_length=10, blank=True)

    # Timing
    event_datetime = models.DateTimeField(_("event datetime"), null=True, blank=True)
    event_timezone = models.CharField(_("event timezone"), max_length=50, blank=True)

    # Deduplication
    source_event_id = models.CharField(_("source event ID"), max_length=200, blank=True)
    confidence = models.PositiveSmallIntegerField(_("confidence"), default=100)

    raw_data = models.JSONField(_("raw data"), default=dict, blank=True)

    class Meta:
        ordering = ["-event_datetime", "-created_at"]
        indexes = [
            models.Index(fields=["team", "shipment"]),
            models.Index(fields=["team", "container"]),
            models.Index(fields=["team", "provider"]),
            models.Index(fields=["event_datetime"]),
            models.Index(fields=["source_event_id"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["team", "provider", "source_event_id"],
                condition=models.Q(source_event_id__gt=""),
                name="unique_tracking_event_source_id",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.get_event_type_display()} — {self.event_datetime}"


class TrackingRawPayload(BaseTeamModel):
    """Saves original data from external systems for debugging and future re-parsing."""

    class PayloadType(models.TextChoices):
        API_RESPONSE = "api_response", _("API Response")
        WEBHOOK = "webhook", _("Webhook")
        SCRAPE_RESULT = "scrape_result", _("Scrape Result")
        MANUAL_IMPORT = "manual_import", _("Manual Import")
        ERROR_RESPONSE = "error_response", _("Error Response")

    provider = models.ForeignKey(
        TrackingProvider,
        on_delete=models.PROTECT,
        related_name="raw_payloads",
        verbose_name=_("provider"),
    )
    subscription = models.ForeignKey(
        TrackingSubscription,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="raw_payloads",
        verbose_name=_("subscription"),
    )
    payload_type = models.CharField(
        _("payload type"), max_length=20, choices=PayloadType.choices, default=PayloadType.API_RESPONSE
    )
    payload_json = models.JSONField(_("payload JSON"), default=dict)
    payload_hash = models.CharField(_("payload hash"), max_length=64, blank=True)
    received_at = models.DateTimeField(_("received at"), null=True, blank=True)
    parsed_successfully = models.BooleanField(_("parsed successfully"), default=False)
    error_message = models.TextField(_("error message"), blank=True)

    class Meta:
        ordering = ["-received_at", "-created_at"]
        indexes = [
            models.Index(fields=["team", "provider"]),
            models.Index(fields=["team", "subscription"]),
            models.Index(fields=["received_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.get_payload_type_display()} from {self.provider} at {self.received_at}"


class TrackingSyncRun(BaseTeamModel):
    """Logs each sync attempt for a tracking subscription."""

    class Status(models.TextChoices):
        STARTED = "started", _("Started")
        SUCCESS = "success", _("Success")
        PARTIAL_SUCCESS = "partial_success", _("Partial Success")
        FAILED = "failed", _("Failed")
        SKIPPED = "skipped", _("Skipped")

    subscription = models.ForeignKey(
        TrackingSubscription,
        on_delete=models.CASCADE,
        related_name="sync_runs",
        verbose_name=_("subscription"),
    )
    provider = models.ForeignKey(
        TrackingProvider,
        on_delete=models.PROTECT,
        related_name="sync_runs",
        verbose_name=_("provider"),
    )
    status = models.CharField(_("status"), max_length=20, choices=Status.choices, default=Status.STARTED)
    started_at = models.DateTimeField(_("started at"), null=True, blank=True)
    finished_at = models.DateTimeField(_("finished at"), null=True, blank=True)
    events_created = models.PositiveIntegerField(_("events created"), default=0)
    events_updated = models.PositiveIntegerField(_("events updated"), default=0)
    raw_payloads_created = models.PositiveIntegerField(_("raw payloads created"), default=0)
    error_message = models.TextField(_("error message"), blank=True)
    metadata = models.JSONField(_("metadata"), default=dict, blank=True)

    class Meta:
        ordering = ["-started_at", "-created_at"]
        indexes = [
            models.Index(fields=["team", "subscription"]),
            models.Index(fields=["team", "provider"]),
            models.Index(fields=["started_at"]),
        ]

    def __str__(self) -> str:
        return f"SyncRun({self.subscription}, {self.get_status_display()}, {self.started_at})"
