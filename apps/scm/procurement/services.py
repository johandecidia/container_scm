"""Write operations and business logic for procurement.

BC import: upserts purchase orders and lines from normalized data.
Fulfillment engine: calculates qty aggregates from PO lines.
Event service: records timeline events on purchase orders.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any

from apps.teams.models import Team

from .models import PurchaseOrder, PurchaseOrderEvent, PurchaseOrderEventType, PurchaseOrderLine

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# BC import service
# ---------------------------------------------------------------------------


def import_purchase_orders_from_bc(team: Team, purchase_orders_data: list[dict[str, Any]]) -> list[PurchaseOrder]:
    """Upsert purchase orders (and their lines) from normalized BC data.

    Can be called multiple times with the same data without creating duplicates.
    BC is the master — this function only reads incoming data and writes to SCM.

    Args:
        team: The team that owns these purchase orders.
        purchase_orders_data: List of dicts matching the BC normalized format.

    Returns:
        List of PurchaseOrder instances (created or updated).
    """
    orders = []
    for po_data in purchase_orders_data:
        lines_data = po_data.get("lines", [])
        po, created = PurchaseOrder.objects.update_or_create(
            team=team,
            external_id=po_data["external_id"],
            defaults={
                "po_number": po_data.get("po_number", ""),
                "supplier_no": po_data.get("supplier_no", ""),
                "supplier_name": po_data.get("supplier_name", ""),
                "status": po_data.get("status", "open"),
                "order_date": po_data.get("order_date"),
                "expected_receipt_date": po_data.get("expected_receipt_date"),
                "currency": po_data.get("currency", "EUR"),
            },
        )
        if created:
            logger.info("Created PurchaseOrder %s for team %s", po.po_number, team.slug)

        _upsert_lines(team=team, purchase_order=po, lines_data=lines_data)
        orders.append(po)

    return orders


def _upsert_lines(team: Team, purchase_order: PurchaseOrder, lines_data: list[dict[str, Any]]) -> None:
    for line_data in lines_data:
        PurchaseOrderLine.objects.update_or_create(
            purchase_order=purchase_order,
            external_id=line_data["external_id"],
            defaults={
                "team": team,
                "line_no": line_data.get("line_no", ""),
                "item_no": line_data.get("item_no", ""),
                "description": line_data.get("description", ""),
                "ordered_qty": Decimal(str(line_data.get("ordered_qty", 0))),
                "shipped_qty": Decimal(str(line_data.get("shipped_qty", 0))),
                "received_qty": Decimal(str(line_data.get("received_qty", 0))),
                "expected_receipt_date": line_data.get("expected_receipt_date"),
            },
        )


# ---------------------------------------------------------------------------
# Fulfillment engine
# ---------------------------------------------------------------------------


def calculate_purchase_order_fulfillment(purchase_order: PurchaseOrder) -> dict[str, Decimal]:
    """Aggregate qty figures for a purchase order from its lines.

    Returns:
        Dict with ordered_qty, shipped_qty, in_transit_qty, arrived_qty,
        received_qty, remaining_qty — all as Decimal.
    """
    from django.db.models import Sum

    aggregates = purchase_order.lines.aggregate(
        total_ordered=Sum("ordered_qty"),
        total_shipped=Sum("shipped_qty"),
        total_received=Sum("received_qty"),
    )

    ordered = aggregates["total_ordered"] or Decimal("0")
    shipped = aggregates["total_shipped"] or Decimal("0")
    received = aggregates["total_received"] or Decimal("0")

    in_transit = max(shipped - received, Decimal("0"))
    remaining = max(ordered - received, Decimal("0"))
    # arrived_qty requires shipment/tracking integration — set to 0 until then
    arrived = Decimal("0")

    return {
        "ordered_qty": ordered,
        "shipped_qty": shipped,
        "in_transit_qty": in_transit,
        "arrived_qty": arrived,
        "received_qty": received,
        "remaining_qty": remaining,
    }


# ---------------------------------------------------------------------------
# Event service
# ---------------------------------------------------------------------------


def create_purchase_order_event(
    purchase_order: PurchaseOrder,
    event_type: str,
    description: str = "",
    metadata: dict[str, Any] | None = None,
) -> PurchaseOrderEvent:
    """Record a timeline event on a purchase order.

    Args:
        purchase_order: The PO to attach the event to.
        event_type: One of PurchaseOrderEventType choices.
        description: Optional human-readable description.
        metadata: Optional dict with extra context.

    Returns:
        The created PurchaseOrderEvent instance.
    """
    if event_type not in PurchaseOrderEventType.values:
        raise ValueError(f"Unknown event type: {event_type!r}")

    return PurchaseOrderEvent.objects.create(
        purchase_order=purchase_order,
        event_type=event_type,
        description=description,
        metadata=metadata or {},
    )
