from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.teams.models import BaseTeamModel


class Integration(BaseTeamModel):
    """An external system integration configured for a team."""

    class Status(models.TextChoices):
        ACTIVE = "active", _("Active")
        INACTIVE = "inactive", _("Inactive")
        ERROR = "error", _("Error")
        PENDING = "pending", _("Pending")

    class ProviderFamily(models.TextChoices):
        CARRIER = "carrier", _("Carrier")
        BUSINESS_SYSTEM = "business_system", _("Business System")
        FINANCE = "finance", _("Finance")
        OTHER = "other", _("Other")

    class ApiStyle(models.TextChoices):
        DCSA = "dcsa", _("DCSA")
        PROPRIETARY = "proprietary", _("Proprietary")
        EDI = "edi", _("EDI")
        AGGREGATOR = "aggregator", _("Aggregator")
        PORTAL = "portal", _("Portal")
        UNKNOWN = "unknown", _("Unknown")

    name = models.CharField(_("name"), max_length=100)
    provider_code = models.CharField(_("provider code"), max_length=100)
    provider_family = models.CharField(
        _("provider family"),
        max_length=20,
        choices=ProviderFamily.choices,
        default=ProviderFamily.CARRIER,
    )
    api_style = models.CharField(
        _("API style"),
        max_length=20,
        choices=ApiStyle.choices,
        default=ApiStyle.UNKNOWN,
    )
    status = models.CharField(_("status"), max_length=20, choices=Status.choices, default=Status.INACTIVE)
    config = models.JSONField(_("config"), default=dict, blank=True)

    # Health tracking
    last_tested_at = models.DateTimeField(_("last tested at"), null=True, blank=True)
    last_success_at = models.DateTimeField(_("last success at"), null=True, blank=True)
    last_error_at = models.DateTimeField(_("last error at"), null=True, blank=True)
    last_error_message = models.TextField(_("last error message"), blank=True)
    is_active = models.BooleanField(_("is active"), default=True)

    class Meta:
        ordering = ["name"]
        unique_together = [["team", "provider_code"]]

    def __str__(self):
        return f"{self.name} ({self.provider_code})"


class IntegrationCredential(BaseTeamModel):
    """Stores (encrypted) credentials for an integration.

    Never store raw secrets in plaintext — always go through the credential service.
    """

    class AuthType(models.TextChoices):
        API_KEY = "api_key", _("API Key")
        OAUTH2 = "oauth2", _("OAuth 2.0")
        BASIC = "basic", _("Basic Auth")
        BEARER = "bearer", _("Bearer Token")
        CERTIFICATE = "certificate", _("Certificate")
        CUSTOM = "custom", _("Custom")

    integration = models.OneToOneField(
        Integration,
        on_delete=models.CASCADE,
        related_name="credential",
        verbose_name=_("integration"),
    )
    auth_type = models.CharField(_("auth type"), max_length=20, choices=AuthType.choices)
    # Encrypted credential data — use the credential service to read/write.
    encrypted_data = models.TextField(_("encrypted data"), blank=True)
    expires_at = models.DateTimeField(_("expires at"), null=True, blank=True)
    last_refreshed_at = models.DateTimeField(_("last refreshed at"), null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Credential for {self.integration} ({self.auth_type})"


class IntegrationRequestLog(BaseTeamModel):
    """Logs outbound API requests made on behalf of an integration.

    Never store tokens, secrets, or full auth headers here.
    """

    integration = models.ForeignKey(
        Integration,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="request_logs",
        verbose_name=_("integration"),
    )
    provider_code = models.CharField(_("provider code"), max_length=100)
    method = models.CharField(_("HTTP method"), max_length=10, default="GET")
    endpoint = models.CharField(_("endpoint"), max_length=500)
    status_code = models.PositiveSmallIntegerField(_("status code"), null=True, blank=True)
    duration_ms = models.PositiveIntegerField(_("duration (ms)"), null=True, blank=True)
    request_id = models.CharField(_("request ID"), max_length=200, blank=True)
    success = models.BooleanField(_("success"), default=False)
    error_message = models.TextField(_("error message"), blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["team", "provider_code"]),
            models.Index(fields=["team", "integration"]),
            models.Index(fields=["created_at"]),
        ]

    def __str__(self):
        return f"{self.method} {self.endpoint} → {self.status_code}"


class IntegrationSyncRun(BaseTeamModel):
    """Records a single end-to-end sync run for an integration resource.

    A sync run represents one logical synchronisation (e.g. a purchase-order
    poll), not a single HTTP call — the individual HTTP calls are captured in
    IntegrationRequestLog. The successful run's ``watermark_to`` becomes the
    starting point for the next incremental run.

    Sync markers: ``watermark_from``/``watermark_to`` hold timestamp markers
    (source ``lastModifiedDateTime``). Integer markers (e.g. a ledger
    ``entryNo``) are kept in ``metadata["cursor_from"]``/``metadata["cursor_to"]``.
    Both are only set as the "to" marker on a fully completed run.
    """

    class Status(models.TextChoices):
        PENDING = "pending", _("Pending")
        RUNNING = "running", _("Running")
        COMPLETED = "completed", _("Completed")
        PARTIALLY_COMPLETED = "partially_completed", _("Partially completed")
        FAILED = "failed", _("Failed")

    class TriggerType(models.TextChoices):
        MANUAL = "manual", _("Manual")
        SCHEDULED = "scheduled", _("Scheduled")
        RETRY = "retry", _("Retry")

    class ResourceType(models.TextChoices):
        PURCHASE_ORDERS = "purchase_orders", _("Purchase orders")
        SALES_ORDERS = "sales_orders", _("Sales orders")
        PURCHASE_RECEIPTS = "purchase_receipts", _("Purchase receipts")
        SALES_SHIPMENTS = "sales_shipments", _("Sales shipments")
        ITEM_LEDGER_ENTRIES = "item_ledger_entries", _("Item ledger entries")

    integration = models.ForeignKey(
        Integration,
        on_delete=models.CASCADE,
        related_name="sync_runs",
        verbose_name=_("integration"),
    )
    resource_type = models.CharField(
        _("resource type"),
        max_length=40,
        choices=ResourceType.choices,
        default=ResourceType.PURCHASE_ORDERS,
    )
    status = models.CharField(_("status"), max_length=20, choices=Status.choices, default=Status.PENDING)
    trigger_type = models.CharField(
        _("trigger type"),
        max_length=20,
        choices=TriggerType.choices,
        default=TriggerType.SCHEDULED,
    )

    started_at = models.DateTimeField(_("started at"), null=True, blank=True)
    finished_at = models.DateTimeField(_("finished at"), null=True, blank=True)
    correlation_id = models.CharField(_("correlation ID"), max_length=64, blank=True)

    # Incremental sync watermarks (source lastModifiedDateTime bounds).
    watermark_from = models.DateTimeField(_("watermark from"), null=True, blank=True)
    watermark_to = models.DateTimeField(_("watermark to"), null=True, blank=True)

    records_fetched = models.PositiveIntegerField(_("records fetched"), default=0)
    records_created = models.PositiveIntegerField(_("records created"), default=0)
    records_updated = models.PositiveIntegerField(_("records updated"), default=0)
    records_unchanged = models.PositiveIntegerField(_("records unchanged"), default=0)
    records_failed = models.PositiveIntegerField(_("records failed"), default=0)

    error_summary = models.TextField(_("error summary"), blank=True)
    metadata = models.JSONField(_("metadata"), default=dict, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["team", "integration"]),
            models.Index(fields=["integration", "resource_type"]),
            models.Index(fields=["status"]),
            models.Index(fields=["started_at"]),
        ]

    def __str__(self):
        return f"SyncRun({self.integration_id}, {self.resource_type}, {self.status})"


class IntegrationWebhookEvent(BaseTeamModel):
    """Stores raw inbound webhook payloads from carrier or business system integrations."""

    class Status(models.TextChoices):
        RECEIVED = "received", _("Received")
        PROCESSING = "processing", _("Processing")
        PROCESSED = "processed", _("Processed")
        FAILED = "failed", _("Failed")
        IGNORED = "ignored", _("Ignored")

    integration = models.ForeignKey(
        Integration,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="webhook_events",
        verbose_name=_("integration"),
    )
    provider_code = models.CharField(_("provider code"), max_length=100)
    event_type = models.CharField(_("event type"), max_length=200, blank=True)
    external_event_id = models.CharField(_("external event ID"), max_length=200, blank=True)
    headers = models.JSONField(_("headers"), default=dict, blank=True)
    payload = models.JSONField(_("payload"), default=dict)
    processed_at = models.DateTimeField(_("processed at"), null=True, blank=True)
    status = models.CharField(_("status"), max_length=20, choices=Status.choices, default=Status.RECEIVED)
    error_message = models.TextField(_("error message"), blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["team", "provider_code"]),
            models.Index(fields=["status"]),
            models.Index(fields=["created_at"]),
        ]

    def __str__(self):
        return f"WebhookEvent({self.provider_code}, {self.status}, {self.created_at})"


class BusinessCentralItemLedgerEntry(BaseTeamModel):
    """A local copy of one Business Central item ledger entry (Analytics API ``itemLedgerEntries``).

    Ledger entries are posted history: BC never edits their identity fields
    (entry no, item, serial, type, document, posting date, quantity, source) —
    corrections are new entries. Fields BC *does* update later (``open``,
    ``remainingQuantity``, ``invoicedQuantity``, cost/sales amounts) are kept only
    in ``raw_payload`` as of the last sync, not as columns, so nothing reads a
    stale snapshot as current.

    One row per BC source and ``entryNo``: an integration is bound to a single
    tenant/environment/company (source binding), so ``(integration, company_id,
    entry_no)`` is unique. The sync never touches ``container`` / ``link_status``;
    only the separate matching pass sets them, and it never changes a Container.
    """

    class SerialStatus(models.TextChoices):
        VALID_ISO = "valid_iso", _("Valid ISO 6346 number")
        INVALID_ISO = "invalid_iso", _("Invalid ISO 6346 number")
        MISSING_SERIAL = "missing_serial", _("No serial number")

    class ItemClassification(models.TextChoices):
        # Whether the *item* is a container is a separate question from whether
        # the serial is a valid ISO number; until a reliable rule exists, unclassified.
        UNCLASSIFIED = "unclassified", _("Unclassified")
        CONTAINER = "container", _("Container")
        NOT_CONTAINER = "not_container", _("Not a container")

    class LinkStatus(models.TextChoices):
        # Set by ``container_links.match_item_ledger_entries`` and recomputed on every
        # pass, so a status is the current state of the entry, never a duplicate.
        PENDING = "pending", _("Not checked yet")
        LINKED = "linked", _("Linked to a container")
        INVALID_SERIAL = "invalid_serial", _("Invalid ISO 6346 serial")
        MISSING_SERIAL = "missing_serial", _("No serial number")
        UNCLASSIFIED_ITEM = "unclassified_item", _("Item not classified")
        NON_CONTAINER_ITEM = "non_container_item", _("Item is not a container")
        VARIANT_UNRESOLVED = "variant_unresolved", _("Item variant unknown")
        MISSING_EQUIPMENT_TYPE = "missing_equipment_type", _("No equipment type mapped")
        EQUIPMENT_TYPE_CONFLICT = "equipment_type_conflict", _("Equipment type differs from the container's")
        CONTAINER_NOT_FOUND = "container_not_found", _("Container not in Pegasus")

    integration = models.ForeignKey(
        Integration,
        on_delete=models.CASCADE,
        related_name="item_ledger_entries",
        verbose_name=_("integration"),
    )
    company_id = models.CharField(_("BC company ID"), max_length=64)
    entry_no = models.BigIntegerField(_("entry no."))
    item_no = models.CharField(_("item no."), max_length=50, blank=True)
    serial_no_raw = models.CharField(_("serial no. (as in BC)"), max_length=100, blank=True)
    serial_no = models.CharField(_("serial no. (normalised)"), max_length=100, blank=True)
    serial_status = models.CharField(_("serial status"), max_length=20, choices=SerialStatus.choices)
    item_classification = models.CharField(
        _("item classification"),
        max_length=20,
        choices=ItemClassification.choices,
        default=ItemClassification.UNCLASSIFIED,
    )
    lot_no = models.CharField(_("lot no."), max_length=100, blank=True)
    entry_type = models.CharField(_("entry type"), max_length=50, blank=True)
    document_type = models.CharField(_("document type"), max_length=50, blank=True)
    document_no = models.CharField(_("document no."), max_length=50, blank=True)
    posting_date = models.DateField(_("posting date"), null=True, blank=True)
    quantity = models.DecimalField(_("quantity"), max_digits=20, decimal_places=5, null=True, blank=True)
    location_code = models.CharField(_("location code"), max_length=50, blank=True)
    source_type = models.CharField(_("source type"), max_length=50, blank=True)
    source_no = models.CharField(_("source no."), max_length=50, blank=True)
    raw_payload = models.JSONField(_("raw payload"), default=dict, blank=True)
    sync_hash = models.CharField(_("sync hash"), max_length=64)
    synced_at = models.DateTimeField(_("synced at"))

    # Link to the physical container (identity = team + ISO 6346 number, never the
    # item no.). Commercial history is kept if the container is deleted.
    container = models.ForeignKey(
        "scm_containers.Container",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="bc_item_ledger_entries",
        verbose_name=_("container"),
    )
    link_status = models.CharField(
        _("link status"), max_length=30, choices=LinkStatus.choices, default=LinkStatus.PENDING
    )
    link_updated_at = models.DateTimeField(_("link updated at"), null=True, blank=True)

    class Meta:
        ordering = ["integration", "entry_no"]
        constraints = [
            models.UniqueConstraint(
                fields=["integration", "company_id", "entry_no"], name="uniq_bc_item_ledger_entry_per_source"
            ),
        ]
        indexes = [
            models.Index(fields=["team", "serial_no"]),
            models.Index(fields=["team", "item_no"]),
            models.Index(fields=["team", "serial_status"]),
            models.Index(fields=["integration", "link_status"]),
        ]

    def __str__(self):
        return f"ILE {self.entry_no} {self.item_no} {self.serial_no or '—'}"


class BusinessCentralItemRule(BaseTeamModel):
    """How one Business Central item (or item-number prefix) is classified, per integration.

    Resolution for an item ledger entry is deterministic (``item_rules.resolve_item``):
    exact item + variant, then exact item, then the longest matching prefix, else
    unclassified. An exact ``NOT_CONTAINER`` rule therefore beats a ``CON`` prefix.
    ``equipment_type`` is the Pegasus type a ``CONTAINER`` item maps to; without it
    no container can be created from the item. Variants apply to exact rules only.
    """

    class MatchType(models.TextChoices):
        EXACT = "exact", _("Exact item no.")
        PREFIX = "prefix", _("Item no. prefix")

    integration = models.ForeignKey(
        Integration,
        on_delete=models.CASCADE,
        related_name="item_rules",
        verbose_name=_("integration"),
    )
    match_type = models.CharField(_("match"), max_length=10, choices=MatchType.choices, default=MatchType.EXACT)
    item_no = models.CharField(_("item no."), max_length=50)
    variant_code = models.CharField(_("variant code"), max_length=20, blank=True)
    classification = models.CharField(
        _("classification"),
        max_length=20,
        choices=[
            (BusinessCentralItemLedgerEntry.ItemClassification.CONTAINER.value, _("Container")),
            (BusinessCentralItemLedgerEntry.ItemClassification.NOT_CONTAINER.value, _("Not a container")),
        ],
    )
    equipment_type = models.ForeignKey(
        "scm_containers.EquipmentType",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("equipment type"),
    )

    class Meta:
        ordering = ["match_type", "item_no", "variant_code"]
        constraints = [
            models.UniqueConstraint(
                fields=["integration", "match_type", "item_no", "variant_code"], name="uniq_bc_item_rule"
            ),
        ]

    def __str__(self):
        target = f"{self.item_no}*" if self.match_type == self.MatchType.PREFIX else self.item_no
        if self.variant_code:
            target = f"{target}/{self.variant_code}"
        return f"{target} → {self.classification}"
