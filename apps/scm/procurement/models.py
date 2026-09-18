"""Purchase order models for procurement visibility.

Business Central is master for BC-sourced Purchase Orders; SCM reads and displays
logistic status for those. Manually entered orders are SCM's own — see
``PurchaseOrderSource``.

The two container links at the bottom of this module are what let SCM answer the
only two procurement questions a physical box raises. See their docstrings.
"""

from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.scm.containers.models import Container
from apps.teams.models import BaseTeamModel


class PurchaseOrderStatus(models.TextChoices):
    """Business Central document status.

    This mirrors the source system's document status and is written ONLY by the
    Business Central mapper/sync. It is not the SCM logistics status — see
    ``PurchaseOrderLogisticsStatus`` and
    ``apps.scm.procurement.selectors.get_purchase_order_logistics_status``.
    """

    OPEN = "open", _("Open")
    RELEASED = "released", _("Released")
    PARTIALLY_RECEIVED = "partially_received", _("Partially Received")
    FULLY_RECEIVED = "fully_received", _("Fully Received")
    CLOSED = "closed", _("Closed")


class PurchaseOrderLogisticsStatus(models.TextChoices):
    """SCM-owned logistics status, COMPUTED from fulfillment quantities.

    This is calculated by Container SCM (not stored, and never written by the
    Business Central mapper) from PO line quantities and supplier deliveries.
    See ``apps.scm.procurement.selectors.get_purchase_order_logistics_status``.
    """

    NOT_STARTED = "not_started", _("Not started")
    PARTIALLY_SHIPPED = "partially_shipped", _("Partially shipped")
    FULLY_SHIPPED = "fully_shipped", _("Fully shipped")
    ARRIVED = "arrived", _("Arrived")
    PARTIALLY_RECEIVED = "partially_received", _("Partially received")
    COMPLETED = "completed", _("Completed")
    EXCEPTION = "exception", _("Exception")


class PurchaseOrderSource(models.TextChoices):
    """Where a purchase order originated. Determines read-only enforcement."""

    BUSINESS_CENTRAL = "business_central", _("Business Central")
    DOCUMENT_IMPORT = "document_import", _("Document import")
    MANUAL = "manual", _("Manual")


class PurchaseOrder(BaseTeamModel):
    """Purchase order. Business Central is master for BC-sourced orders (read-only in SCM)."""

    external_id = models.CharField(_("External ID"), max_length=255)
    po_number = models.CharField(_("PO Number"), max_length=100)
    supplier_no = models.CharField(_("Supplier No"), max_length=100)
    supplier_name = models.CharField(_("Supplier Name"), max_length=255)
    # Business Central *document* status — written ONLY by the BC mapper/sync.
    status = models.CharField(
        _("Status"),
        max_length=30,
        choices=PurchaseOrderStatus.choices,
        default=PurchaseOrderStatus.OPEN,
    )
    order_date = models.DateField(_("Order Date"), null=True, blank=True)
    expected_receipt_date = models.DateField(_("Expected Receipt Date"), null=True, blank=True)
    currency = models.CharField(_("Currency"), max_length=10, default="EUR")

    # ── Source metadata (owned by the sync/import layer) ──────────────────
    source_system = models.CharField(
        _("Source system"),
        max_length=30,
        choices=PurchaseOrderSource.choices,
        default=PurchaseOrderSource.BUSINESS_CENTRAL,
    )
    source_company_id = models.CharField(_("Source company ID"), max_length=255, blank=True)
    source_last_modified_at = models.DateTimeField(_("Source last modified at"), null=True, blank=True)
    last_synced_at = models.DateTimeField(_("Last synced at"), null=True, blank=True)
    raw_payload = models.JSONField(_("Raw payload"), default=dict, blank=True)
    sync_hash = models.CharField(_("Sync hash"), max_length=64, blank=True)
    source_active = models.BooleanField(_("Source active"), default=True)
    source_deleted_at = models.DateTimeField(_("Source deleted at"), null=True, blank=True)

    class Meta:
        verbose_name = _("Purchase Order")
        verbose_name_plural = _("Purchase Orders")
        unique_together = [("team", "external_id")]
        ordering = ["-order_date", "po_number"]
        indexes = [
            models.Index(fields=["team", "status"]),
            models.Index(fields=["team", "-order_date"]),
            models.Index(fields=["team", "supplier_no"]),
            models.Index(fields=["team", "source_system"]),
            models.Index(fields=["team", "source_active"]),
        ]

    def __str__(self) -> str:
        return f"{self.po_number} — {self.supplier_name}"

    @property
    def is_business_central(self) -> bool:
        """True when this PO is owned by Business Central (read-only in SCM)."""
        return self.source_system == PurchaseOrderSource.BUSINESS_CENTRAL


class PurchaseOrderLine(BaseTeamModel):
    """A single line on a Purchase Order."""

    purchase_order = models.ForeignKey(
        PurchaseOrder,
        on_delete=models.CASCADE,
        related_name="lines",
        verbose_name=_("Purchase Order"),
    )
    external_id = models.CharField(_("External ID"), max_length=255)
    line_no = models.CharField(_("Line No"), max_length=20)
    item_no = models.CharField(_("Item No"), max_length=100)
    description = models.CharField(_("Description"), max_length=255, blank=True)
    ordered_qty = models.DecimalField(_("Ordered Qty"), max_digits=12, decimal_places=3, default=0)
    shipped_qty = models.DecimalField(_("Shipped Qty"), max_digits=12, decimal_places=3, default=0)
    received_qty = models.DecimalField(_("Received Qty"), max_digits=12, decimal_places=3, default=0)
    unit_price = models.DecimalField(_("Unit Price"), max_digits=14, decimal_places=4, null=True, blank=True)
    expected_receipt_date = models.DateField(_("Expected Receipt Date"), null=True, blank=True)

    # ── Source metadata (source_system is inherited from the parent PO) ───
    source_last_modified_at = models.DateTimeField(_("Source last modified at"), null=True, blank=True)
    last_synced_at = models.DateTimeField(_("Last synced at"), null=True, blank=True)
    raw_payload = models.JSONField(_("Raw payload"), default=dict, blank=True)
    sync_hash = models.CharField(_("Sync hash"), max_length=64, blank=True)
    source_active = models.BooleanField(_("Source active"), default=True)
    source_deleted_at = models.DateTimeField(_("Source deleted at"), null=True, blank=True)

    @property
    def line_amount(self):
        if self.unit_price is not None:
            return self.unit_price * self.ordered_qty
        return None

    class Meta:
        verbose_name = _("Purchase Order Line")
        verbose_name_plural = _("Purchase Order Lines")
        unique_together = [("purchase_order", "external_id")]
        ordering = ["line_no"]

    def __str__(self) -> str:
        return f"{self.purchase_order.po_number} / {self.line_no} — {self.item_no}"


def _validate_container_link(link) -> None:
    """Both ends of a procurement↔container link must belong to the link's own team.

    Declared on the models rather than only in the link services, because ``save``
    calls ``full_clean`` and so this holds for every writer — a service, a form, the
    admin, a shell session, the Business Central import that will use the same
    services later. Nothing in either foreign key prevents pointing at another
    tenant's row.
    """
    if link.team_id is None:
        return
    errors = {}
    if link.purchase_order_line_id is not None and link.purchase_order_line.team_id != link.team_id:
        errors["purchase_order_line"] = _("That purchase order line belongs to another team.")
    if link.container_id is not None and link.container.team_id != link.team_id:
        errors["container"] = _("That container belongs to another team.")
    if errors:
        raise ValidationError(errors)


class ContainerAcquisition(BaseTeamModel):
    """The container *is* the purchase: this box was acquired through this PO line.

    Answers "which purchase order line acquired this physical container?" — the
    case where the ordered article is equipment, so a line reading ``CONT45G1 × 20``
    becomes twenty boxes with their own ISO numbers. One line acquires many
    containers; a container has at most one procurement origin, which is what the
    one-to-one enforces. Re-linking the same pair is a no-op rather than an error;
    moving a box to another line means unlinking it first.

    Deliberately *not* :class:`~apps.scm.supplier_deliveries.models.SupplierDeliveryLine`,
    which books quantity onto a dated, referenced delivery batch. This is a fact
    about where a box came from, and it is true with no delivery in sight.

    Nothing about the purchase is copied here — no supplier, article or price. Those
    are read through ``purchase_order_line`` so there is one place they can be wrong.
    """

    purchase_order_line = models.ForeignKey(
        PurchaseOrderLine,
        on_delete=models.CASCADE,
        related_name="acquired_containers",
        verbose_name=_("Purchase Order Line"),
    )
    container = models.OneToOneField(
        Container,
        on_delete=models.CASCADE,
        related_name="acquisition",
        verbose_name=_("Container"),
    )

    class Meta:
        verbose_name = _("Acquired Container")
        verbose_name_plural = _("Acquired Containers")
        # Read from both directions, so no join is baked into the default ordering;
        # the read functions in container_links.py order for the view they serve.
        ordering = ["id"]
        indexes = [
            models.Index(fields=["team", "purchase_order_line"]),
        ]

    def __str__(self) -> str:
        return f"{self.container.container_id} ← {self.purchase_order_line.line_no}"

    def clean(self) -> None:
        super().clean()
        _validate_container_link(self)

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)


class ContainerLoad(BaseTeamModel):
    """The container *carries* the purchase: goods from this PO line are loaded in it.

    Answers "which purchased goods are transported in this physical container?" —
    the doors and spare parts case. Several PO lines can share one box, and one PO
    line can be spread over several boxes, so the pair is what is unique rather than
    either end of it.

    ``quantity`` is optional on purpose. Knowing that a line's goods are in a box is
    useful before anybody has counted how much of it went in, and a nullable column
    says "not recorded" where a zero would say "none".

    Same restraint as :class:`ContainerAcquisition`: article, supplier and price stay
    on the purchase order line.
    """

    purchase_order_line = models.ForeignKey(
        PurchaseOrderLine,
        on_delete=models.CASCADE,
        related_name="container_loads",
        verbose_name=_("Purchase Order Line"),
    )
    container = models.ForeignKey(
        Container,
        on_delete=models.CASCADE,
        related_name="loads",
        verbose_name=_("Container"),
    )
    quantity = models.DecimalField(
        _("Quantity"),
        max_digits=12,
        decimal_places=3,
        null=True,
        blank=True,
        help_text=_("How much of the line went into this container. Leave blank when it is not known."),
    )

    class Meta:
        verbose_name = _("Container Load")
        verbose_name_plural = _("Container Loads")
        ordering = ["id"]
        constraints = [
            models.UniqueConstraint(
                fields=["purchase_order_line", "container"],
                name="unique_container_load_per_po_line",
            ),
        ]
        indexes = [
            models.Index(fields=["team", "purchase_order_line"]),
            models.Index(fields=["team", "container"]),
        ]

    def __str__(self) -> str:
        return f"{self.purchase_order_line.line_no} → {self.container.container_id}"

    def clean(self) -> None:
        super().clean()
        _validate_container_link(self)
        if self.quantity is not None and self.quantity < Decimal("0"):
            raise ValidationError({"quantity": _("A loaded quantity cannot be negative.")})

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)


class PurchaseOrderEventType(models.TextChoices):
    CREATED = "CREATED", _("Created")
    PARTIALLY_SHIPPED = "PARTIALLY_SHIPPED", _("Partially Shipped")
    FULLY_SHIPPED = "FULLY_SHIPPED", _("Fully Shipped")
    LOADED = "LOADED", _("Loaded")
    ARRIVED = "ARRIVED", _("Arrived")
    RECEIVED = "RECEIVED", _("Received")


class PurchaseOrderEvent(models.Model):
    """Timeline event for a Purchase Order."""

    purchase_order = models.ForeignKey(
        PurchaseOrder,
        on_delete=models.CASCADE,
        related_name="events",
        verbose_name=_("Purchase Order"),
    )
    event_type = models.CharField(
        _("Event Type"),
        max_length=30,
        choices=PurchaseOrderEventType.choices,
    )
    timestamp = models.DateTimeField(_("Timestamp"), auto_now_add=True)
    description = models.TextField(_("Description"), blank=True)
    metadata = models.JSONField(_("Metadata"), default=dict, blank=True)

    class Meta:
        verbose_name = _("Purchase Order Event")
        verbose_name_plural = _("Purchase Order Events")
        ordering = ["timestamp"]

    def __str__(self) -> str:
        return f"{self.purchase_order.po_number} — {self.get_event_type_display()}"
