"""Write operations and business logic for supplier deliveries.

Quantity validation: total delivery qty for a PO line must not exceed ordered_qty.
Status services: mark deliveries as shipped or received.
"""

from __future__ import annotations

import datetime
import logging
from decimal import Decimal
from typing import Any

from django.core.exceptions import ValidationError
from django.db.models import Sum

from apps.scm.procurement.models import PurchaseOrder, PurchaseOrderLine
from apps.teams.models import Team

from .models import SupplierDelivery, SupplierDeliveryLine, SupplierDeliveryStatus

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Quantity validation
# ---------------------------------------------------------------------------


def _validate_delivery_line_qty(
    purchase_order_line: PurchaseOrderLine,
    delivery_qty: Decimal,
    exclude_delivery_line_id: int | None = None,
) -> None:
    """Validate that the total delivery qty for a PO line does not exceed ordered_qty.

    Args:
        purchase_order_line: The PO line being delivered against.
        delivery_qty: The qty being added or updated.
        exclude_delivery_line_id: When updating an existing line, exclude it from the sum.

    Raises:
        ValidationError: If adding delivery_qty would exceed ordered_qty.
    """
    existing_qs = SupplierDeliveryLine.objects.filter(purchase_order_line=purchase_order_line)
    if exclude_delivery_line_id is not None:
        existing_qs = existing_qs.exclude(pk=exclude_delivery_line_id)

    agg = existing_qs.aggregate(total=Sum("delivery_qty"))
    existing_total = agg["total"] or Decimal("0")
    new_total = existing_total + delivery_qty

    if new_total > purchase_order_line.ordered_qty:
        raise ValidationError(
            f"Total delivery qty ({new_total}) would exceed ordered qty "
            f"({purchase_order_line.ordered_qty}) for PO line {purchase_order_line.line_no}."
        )


# ---------------------------------------------------------------------------
# Supplier Delivery services
# ---------------------------------------------------------------------------


def create_supplier_delivery(
    team: Team,
    purchase_order: PurchaseOrder,
    delivery_reference: str,
    supplier: str = "",
    status: str = SupplierDeliveryStatus.PLANNED,
    planned_ship_date: datetime.date | None = None,
    planned_arrival_date: datetime.date | None = None,
    notes: str = "",
) -> SupplierDelivery:
    """Create a new supplier delivery linked to a purchase order."""
    delivery = SupplierDelivery.objects.create(
        team=team,
        purchase_order=purchase_order,
        delivery_reference=delivery_reference,
        supplier=supplier,
        status=status,
        planned_ship_date=planned_ship_date,
        planned_arrival_date=planned_arrival_date,
        notes=notes,
    )
    logger.info("Created SupplierDelivery %s for team %s", delivery.delivery_reference, team.slug)
    return delivery


def update_supplier_delivery(delivery: SupplierDelivery, data: dict[str, Any]) -> SupplierDelivery:
    """Update an existing supplier delivery with the provided field data."""
    allowed_fields = {
        "delivery_reference",
        "supplier",
        "status",
        "planned_ship_date",
        "planned_arrival_date",
        "actual_ship_date",
        "actual_arrival_date",
        "notes",
    }
    for field, value in data.items():
        if field in allowed_fields:
            setattr(delivery, field, value)
    delivery.save()
    return delivery


def mark_supplier_delivery_shipped(
    delivery: SupplierDelivery,
    actual_ship_date: datetime.date | None = None,
) -> SupplierDelivery:
    """Mark a delivery as shipped and record the actual ship date."""
    delivery.status = SupplierDeliveryStatus.SHIPPED
    delivery.actual_ship_date = actual_ship_date or datetime.date.today()
    delivery.save()
    logger.info("Marked SupplierDelivery %s as SHIPPED", delivery.delivery_reference)
    return delivery


def mark_supplier_delivery_received(
    delivery: SupplierDelivery,
    actual_arrival_date: datetime.date | None = None,
) -> SupplierDelivery:
    """Mark a delivery as received and record the actual arrival date."""
    delivery.status = SupplierDeliveryStatus.RECEIVED
    delivery.actual_arrival_date = actual_arrival_date or datetime.date.today()
    delivery.save()
    logger.info("Marked SupplierDelivery %s as RECEIVED", delivery.delivery_reference)
    return delivery


# ---------------------------------------------------------------------------
# Supplier Delivery Line services
# ---------------------------------------------------------------------------


def create_supplier_delivery_line(
    team: Team,
    delivery: SupplierDelivery,
    purchase_order_line: PurchaseOrderLine,
    delivery_qty: Decimal,
    article: str = "",
    unit: str = "",
    container=None,
    notes: str = "",
) -> SupplierDeliveryLine:
    """Create a delivery line after validating that qty does not exceed PO line ordered_qty."""
    _validate_delivery_line_qty(purchase_order_line=purchase_order_line, delivery_qty=delivery_qty)

    return SupplierDeliveryLine.objects.create(
        team=team,
        delivery=delivery,
        purchase_order_line=purchase_order_line,
        article=article,
        delivery_qty=delivery_qty,
        unit=unit,
        container=container,
        notes=notes,
    )


def update_supplier_delivery_line(
    delivery_line: SupplierDeliveryLine,
    data: dict[str, Any],
) -> SupplierDeliveryLine:
    """Update an existing delivery line, re-validating qty if it changes."""
    new_qty = data.get("delivery_qty", delivery_line.delivery_qty)
    if new_qty != delivery_line.delivery_qty:
        _validate_delivery_line_qty(
            purchase_order_line=delivery_line.purchase_order_line,
            delivery_qty=new_qty,
            exclude_delivery_line_id=delivery_line.pk,
        )

    allowed_fields = {"article", "delivery_qty", "unit", "container", "notes"}
    for field, value in data.items():
        if field in allowed_fields:
            setattr(delivery_line, field, value)
    delivery_line.save()
    return delivery_line
