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
    """Represents an active or historical tracking watch for a shipment or container.

    Two statuses are tracked separately and must not be conflated:

    ``status``
        The lifecycle of our watch — active, paused, completed, failed.

    ``tracking_status``
        What the carrier is telling us. In particular, NO_DATA means the call
        worked and the carrier does not know this reference yet, while
        NOT_CONFIGURED means we never got to ask.
    """

    class Status(models.TextChoices):
        ACTIVE = "active", _("Active")
        PAUSED = "paused", _("Paused")
        SYNCING = "syncing", _("Syncing")
        COMPLETED = "completed", _("Completed")
        FAILED = "failed", _("Failed")
        CANCELLED = "cancelled", _("Cancelled")

    class TrackingStatus(models.TextChoices):
        PENDING = "pending", _("Pending first sync")
        NO_DATA = "no_data", _("No data at carrier yet")
        TRACKING = "tracking", _("Tracking")
        NOT_CONFIGURED = "not_configured", _("Carrier not configured")
        ERROR = "error", _("Error")

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
    tracking_status = models.CharField(
        _("tracking status"),
        max_length=20,
        choices=TrackingStatus.choices,
        default=TrackingStatus.PENDING,
    )

    # Sync state
    last_synced_at = models.DateTimeField(_("last synced at"), null=True, blank=True)
    next_sync_at = models.DateTimeField(_("next sync at"), null=True, blank=True)
    last_event_at = models.DateTimeField(
        _("last event received at"),
        null=True,
        blank=True,
        help_text=_("When this subscription last produced at least one carrier event."),
    )
    sync_interval_minutes = models.PositiveIntegerField(
        _("sync interval (minutes)"),
        null=True,
        blank=True,
        help_text=_("Overrides the state-based polling interval for this subscription."),
    )

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
            models.Index(fields=["team", "tracking_status"]),
        ]

    def __str__(self) -> str:
        return f"{self.tracking_reference} ({self.get_reference_type_display()})"


class TrackingEvent(BaseTeamModel):
    """Stores normalised tracking events from any provider.

    ``event_type`` is our internal, provider-independent classification;
    ``carrier_event_type`` / ``event_code`` / ``carrier_description`` keep what the
    carrier actually said, so a mapping gap never destroys information.

    Whether the time is observed or forecast is carried by the single
    ``event_time_type`` field. There is deliberately no pair of is_actual /
    is_estimated booleans, which could contradict each other; ``is_actual`` is a
    derived property.
    """

    class EventTimeType(models.TextChoices):
        """Whether the event time is observed or forecast (DCSA event classifier)."""

        ACTUAL = "actual", _("Actual")
        ESTIMATED = "estimated", _("Estimated")
        PLANNED = "planned", _("Planned")
        REQUESTED = "requested", _("Requested")
        UNKNOWN = "unknown", _("Unknown")

    class TransportMode(models.TextChoices):
        VESSEL = "vessel", _("Vessel")
        RAIL = "rail", _("Rail")
        TRUCK = "truck", _("Truck")
        BARGE = "barge", _("Barge")
        AIR = "air", _("Air")
        OTHER = "other", _("Other")

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
    raw_payload = models.ForeignKey(
        "scm_tracking.TrackingRawPayload",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="events",
        verbose_name=_("raw payload"),
        help_text=_("The stored carrier response this event was parsed from."),
    )

    # Classification — internal first, then what the carrier actually sent.
    event_type = models.CharField(_("event type"), max_length=40, choices=EventType.choices, default=EventType.UNKNOWN)
    carrier_event_type = models.CharField(_("carrier event type"), max_length=60, blank=True)
    event_code = models.CharField(_("event code"), max_length=100, blank=True)
    event_time_type = models.CharField(
        _("event time type"),
        max_length=20,
        choices=EventTimeType.choices,
        default=EventTimeType.UNKNOWN,
    )
    status = models.CharField(_("status"), max_length=200, blank=True)
    description = models.TextField(_("description"), blank=True)
    carrier_description = models.TextField(_("carrier description"), blank=True)

    # Location
    location_name = models.CharField(_("location name"), max_length=200, blank=True)
    location_unlocode = models.CharField(_("UN/LOCODE"), max_length=10, blank=True)
    location_latitude = models.DecimalField(_("latitude"), max_digits=9, decimal_places=6, null=True, blank=True)
    location_longitude = models.DecimalField(_("longitude"), max_digits=9, decimal_places=6, null=True, blank=True)

    # Transport
    vessel_name = models.CharField(_("vessel name"), max_length=200, blank=True)
    vessel_imo = models.CharField(_("vessel IMO"), max_length=20, blank=True)
    voyage_number = models.CharField(_("voyage number"), max_length=50, blank=True)
    transport_mode = models.CharField(_("transport mode"), max_length=20, choices=TransportMode.choices, blank=True)
    equipment_reference = models.CharField(_("equipment reference"), max_length=20, blank=True)

    # Timing
    event_datetime = models.DateTimeField(_("event datetime"), null=True, blank=True)
    event_timezone = models.CharField(_("event timezone"), max_length=50, blank=True)
    received_at = models.DateTimeField(_("received at"), null=True, blank=True)

    # Deduplication
    source_event_id = models.CharField(_("source event ID"), max_length=200, blank=True)
    event_fingerprint = models.CharField(
        _("event fingerprint"),
        max_length=64,
        blank=True,
        help_text=_("Stable hash used to recognise the same carrier event across syncs."),
    )
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
            models.Index(fields=["team", "container", "event_time_type"]),
        ]
        constraints = [
            # The fingerprint is the single deduplication key: it is derived from the
            # carrier event ID when there is one, and from the event's identifying
            # fields when there is not.
            models.UniqueConstraint(
                fields=["team", "provider", "event_fingerprint"],
                condition=models.Q(event_fingerprint__gt=""),
                name="unique_tracking_event_fingerprint",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.get_event_type_display()} — {self.event_datetime}"

    @property
    def is_actual(self) -> bool:
        """True when the carrier reported this as an observed event."""
        return self.event_time_type == self.EventTimeType.ACTUAL

    @property
    def is_estimated(self) -> bool:
        """True when the event time is a forecast, not an observation."""
        return self.event_time_type in (self.EventTimeType.ESTIMATED, self.EventTimeType.PLANNED)


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
    payload_bytes = models.PositiveIntegerField(
        _("payload size (bytes)"),
        null=True,
        blank=True,
        help_text=_("Size of the original payload, retained after the body is archived away."),
    )
    received_at = models.DateTimeField(_("received at"), null=True, blank=True)
    parsed_successfully = models.BooleanField(_("parsed successfully"), default=False)
    error_message = models.TextField(_("error message"), blank=True)
    archived_at = models.DateTimeField(
        _("archived at"),
        null=True,
        blank=True,
        help_text=_("When the payload body was dropped by retention. Hash and metadata are kept."),
    )

    class Meta:
        ordering = ["-received_at", "-created_at"]
        indexes = [
            models.Index(fields=["team", "provider"]),
            models.Index(fields=["team", "subscription"]),
            models.Index(fields=["received_at"]),
            models.Index(fields=["archived_at", "received_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.get_payload_type_display()} from {self.provider} at {self.received_at}"

    @property
    def is_archived(self) -> bool:
        """True when the body has been dropped by retention but the record remains."""
        return self.archived_at is not None


class ETAHistory(BaseTeamModel):
    """Records every ETA change for a shipment to allow drift analysis.

    Append-only — never update existing records.
    """

    shipment = models.ForeignKey(
        "scm_shipments.Shipment",
        on_delete=models.CASCADE,
        related_name="eta_history",
        verbose_name=_("shipment"),
    )
    container = models.ForeignKey(
        "scm_containers.Container",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="eta_history",
        verbose_name=_("container"),
    )
    previous_eta = models.DateField(_("previous ETA"), null=True, blank=True)
    new_eta = models.DateField(_("new ETA"), null=True, blank=True)
    changed_at = models.DateTimeField(_("changed at"))
    source = models.CharField(_("source"), max_length=100, blank=True)
    raw_payload = models.JSONField(_("raw payload"), default=dict, blank=True)

    class Meta:
        ordering = ["-changed_at"]
        indexes = [
            models.Index(fields=["team", "shipment"]),
            models.Index(fields=["team", "container"]),
            models.Index(fields=["changed_at"]),
        ]
        verbose_name = _("ETA History")
        verbose_name_plural = _("ETA History")

    def __str__(self) -> str:
        return f"ETA change for {self.shipment}: {self.previous_eta} → {self.new_eta} at {self.changed_at}"


class TrackingSyncRun(BaseTeamModel):
    """Logs each sync attempt for a tracking subscription.

    SKIPPED means nothing was attempted (adapter not implemented, integration not
    configured, or another run already in progress) — it is neither a success nor
    a failure, and must never be presented as "synced, no events".
    """

    class Status(models.TextChoices):
        STARTED = "started", _("Started")
        SUCCESS = "success", _("Success")
        PARTIAL_SUCCESS = "partial_success", _("Partial Success")
        FAILED = "failed", _("Failed")
        SKIPPED = "skipped", _("Skipped")

    class ErrorType(models.TextChoices):
        """Why a run did not succeed, so failures can be told apart at a glance."""

        NONE = "", _("None")
        NOT_IMPLEMENTED = "not_implemented", _("Adapter not implemented")
        NOT_CONFIGURED = "not_configured", _("Integration not configured")
        ALREADY_RUNNING = "already_running", _("Sync already running")
        UNSUPPORTED_REFERENCE = "unsupported_reference", _("Unsupported reference")
        AUTHENTICATION = "authentication", _("Authentication failed")
        RATE_LIMIT = "rate_limit", _("Rate limited")
        TIMEOUT = "timeout", _("Timeout or network error")
        SERVER_ERROR = "server_error", _("Carrier server error")
        INVALID_RESPONSE = "invalid_response", _("Invalid or unparseable response")
        PARSE_ERROR = "parse_error", _("Parser error")
        UNEXPECTED = "unexpected", _("Unexpected error")

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
    error_type = models.CharField(
        _("error type"), max_length=30, choices=ErrorType.choices, blank=True, default=ErrorType.NONE
    )
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
