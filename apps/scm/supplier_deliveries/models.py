"""Supplier Delivery models.

Tracks partial and full deliveries against Purchase Orders.
A PO may have multiple SupplierDeliveries (partial shipments).
Each SupplierDeliveryLine tracks qty delivered for a specific PO line.
"""

from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.scm.containers.models import Container
from apps.scm.procurement.models import PurchaseOrder, PurchaseOrderLine
from apps.teams.models import BaseTeamModel


class SupplierDeliveryStatus(models.TextChoices):
    PLANNED = "planned", _("Planned")
    BOOKED = "booked", _("Booked")
    IN_PRODUCTION = "in_production", _("In Production")
    READY = "ready", _("Ready")
    SHIPPED = "shipped", _("Shipped")
    IN_TRANSIT = "in_transit", _("In Transit")
    ARRIVED = "arrived", _("Arrived")
    RECEIVED = "received", _("Received")
    CANCELLED = "cancelled", _("Cancelled")


class SupplierDelivery(BaseTeamModel):
    """A (partial or full) delivery from a supplier against a Purchase Order."""

    purchase_order = models.ForeignKey(
        PurchaseOrder,
        on_delete=models.CASCADE,
        related_name="supplier_deliveries",
        verbose_name=_("Purchase Order"),
    )
    supplier = models.CharField(_("Supplier"), max_length=255, blank=True)
    delivery_reference = models.CharField(_("Delivery Reference"), max_length=100)
    status = models.CharField(
        _("Status"),
        max_length=30,
        choices=SupplierDeliveryStatus.choices,
        default=SupplierDeliveryStatus.PLANNED,
    )
    planned_ship_date = models.DateField(_("Planned Ship Date"), null=True, blank=True)
    planned_arrival_date = models.DateField(_("Planned Arrival Date"), null=True, blank=True)
    actual_ship_date = models.DateField(_("Actual Ship Date"), null=True, blank=True)
    actual_arrival_date = models.DateField(_("Actual Arrival Date"), null=True, blank=True)
    notes = models.TextField(_("Notes"), blank=True)

    class Meta:
        verbose_name = _("Supplier Delivery")
        verbose_name_plural = _("Supplier Deliveries")
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["team", "status"]),
            models.Index(fields=["team", "purchase_order"]),
            models.Index(fields=["team", "-created_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.delivery_reference} — {self.purchase_order.po_number}"


class SupplierDeliveryLine(BaseTeamModel):
    """A line on a SupplierDelivery, tracking qty for a specific PO line."""

    delivery = models.ForeignKey(
        SupplierDelivery,
        on_delete=models.CASCADE,
        related_name="lines",
        verbose_name=_("Delivery"),
    )
    purchase_order_line = models.ForeignKey(
        PurchaseOrderLine,
        on_delete=models.CASCADE,
        related_name="delivery_lines",
        verbose_name=_("Purchase Order Line"),
    )
    article = models.CharField(_("Article"), max_length=255, blank=True)
    delivery_qty = models.DecimalField(_("Delivery Qty"), max_digits=12, decimal_places=3, default=0)
    unit = models.CharField(_("Unit"), max_length=50, blank=True)
    container = models.ForeignKey(
        Container,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="supplier_delivery_lines",
        verbose_name=_("Container"),
    )
    notes = models.TextField(_("Notes"), blank=True)

    class Meta:
        verbose_name = _("Supplier Delivery Line")
        verbose_name_plural = _("Supplier Delivery Lines")
        ordering = ["id"]

    def __str__(self) -> str:
        return f"{self.delivery.delivery_reference} / {self.article or self.purchase_order_line.item_no}"
