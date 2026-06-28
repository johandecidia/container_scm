"""Purchase order models for procurement visibility.

Business Central is master for Purchase Orders. This app reads and displays logistic status only.
"""

from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.teams.models import BaseTeamModel


class PurchaseOrderStatus(models.TextChoices):
    OPEN = "open", _("Open")
    RELEASED = "released", _("Released")
    PARTIALLY_RECEIVED = "partially_received", _("Partially Received")
    FULLY_RECEIVED = "fully_received", _("Fully Received")
    CLOSED = "closed", _("Closed")


class PurchaseOrder(BaseTeamModel):
    """Purchase order synced from Business Central. Read-only in SCM."""

    external_id = models.CharField(_("External ID"), max_length=255)
    po_number = models.CharField(_("PO Number"), max_length=100)
    supplier_no = models.CharField(_("Supplier No"), max_length=100)
    supplier_name = models.CharField(_("Supplier Name"), max_length=255)
    status = models.CharField(
        _("Status"),
        max_length=30,
        choices=PurchaseOrderStatus.choices,
        default=PurchaseOrderStatus.OPEN,
    )
    order_date = models.DateField(_("Order Date"), null=True, blank=True)
    expected_receipt_date = models.DateField(_("Expected Receipt Date"), null=True, blank=True)
    currency = models.CharField(_("Currency"), max_length=10, default="EUR")

    class Meta:
        verbose_name = _("Purchase Order")
        verbose_name_plural = _("Purchase Orders")
        unique_together = [("team", "external_id")]
        ordering = ["-order_date", "po_number"]
        indexes = [
            models.Index(fields=["team", "status"]),
            models.Index(fields=["team", "-order_date"]),
            models.Index(fields=["team", "supplier_no"]),
        ]

    def __str__(self) -> str:
        return f"{self.po_number} — {self.supplier_name}"


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
