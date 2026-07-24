"""Write operations and business logic for procurement.

BC import: upserts purchase orders and lines from normalized data.
Fulfillment engine: calculates qty aggregates from PO lines.
Event service: records timeline events on purchase orders.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from django.db import transaction

from apps.teams.models import Team

from .models import PurchaseOrder, PurchaseOrderEvent, PurchaseOrderEventType, PurchaseOrderLine

logger = logging.getLogger(__name__)


@dataclass
class UpsertResult:
    """Structured outcome of an upsert_purchase_orders call."""

    created: int = 0
    updated: int = 0
    unchanged: int = 0
    failed: int = 0
    purchase_orders: list[PurchaseOrder] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)

    @property
    def processed(self) -> int:
        return self.created + self.updated + self.unchanged


# ---------------------------------------------------------------------------
# Manual create service
# ---------------------------------------------------------------------------


def create_purchase_order(team: Team, **kwargs: Any) -> PurchaseOrder:
    """Create a manually entered purchase order with an auto-generated external_id."""
    external_id = f"manual-{uuid.uuid4().hex}"
    return PurchaseOrder.objects.create(team=team, external_id=external_id, **kwargs)


# ---------------------------------------------------------------------------
# Upsert service (canonical entry point for all PO sources)
# ---------------------------------------------------------------------------


def upsert_purchase_orders(team: Team, purchase_orders_data: list[dict[str, Any]]) -> UpsertResult:
    """Upsert purchase orders (and their lines) from any normalised source.

    Idempotent — can be called multiple times with the same data without
    creating duplicates, and unchanged records are detected and reported as such
    (not counted as updates). The source system is master; this function only
    reads incoming data and writes to SCM. Each purchase order is written in its
    own transaction so a single bad record fails in isolation without rolling
    back the rest of the batch.

    Args:
        team: The team that owns these purchase orders.
        purchase_orders_data: List of dicts with keys matching PurchaseOrder
            and PurchaseOrderLine fields (external_id, po_number, lines, …).

    Returns:
        UpsertResult with created/updated/unchanged/failed counts, the affected
        PurchaseOrder instances, and per-record errors keyed by external id.
    """
    result = UpsertResult()
    for po_data in purchase_orders_data:
        external_id = po_data.get("external_id")
        try:
            if not external_id:
                raise ValueError("purchase order data is missing 'external_id'")
            with transaction.atomic():
                po = _upsert_single_purchase_order(team, external_id, po_data, result)
            result.purchase_orders.append(po)
        except Exception as exc:  # noqa: BLE001 — isolate one bad record from the batch
            result.failed += 1
            result.errors.append(
                {
                    "external_id": external_id,
                    "po_number": po_data.get("po_number", ""),
                    "error": str(exc),
                }
            )
            logger.warning("Failed to upsert purchase order %s for team %s: %s", external_id, team.slug, exc)

    return result


def _upsert_single_purchase_order(
    team: Team,
    external_id: str,
    po_data: dict[str, Any],
    result: UpsertResult,
) -> PurchaseOrder:
    lines_data = po_data.get("lines", [])
    defaults = {
        "po_number": po_data.get("po_number", ""),
        "supplier_no": po_data.get("supplier_no", ""),
        "supplier_name": po_data.get("supplier_name", ""),
        "status": po_data.get("status", "open"),
        "order_date": po_data.get("order_date"),
        "expected_receipt_date": po_data.get("expected_receipt_date"),
        "currency": po_data.get("currency", "EUR"),
    }

    existing = PurchaseOrder.objects.filter(team=team, external_id=external_id).first()
    if existing is None:
        po = PurchaseOrder.objects.create(team=team, external_id=external_id, **defaults)
        _upsert_lines(team=team, purchase_order=po, lines_data=lines_data)
        result.created += 1
        logger.info("Created PurchaseOrder %s for team %s", po.po_number, team.slug)
        return po

    if _purchase_order_unchanged(existing, defaults, lines_data):
        result.unchanged += 1
        return existing

    for attr, value in defaults.items():
        setattr(existing, attr, value)
    existing.save(update_fields=[*defaults.keys(), "updated_at"])
    _upsert_lines(team=team, purchase_order=existing, lines_data=lines_data)
    result.updated += 1
    return existing


def import_purchase_orders_from_bc(team: Team, purchase_orders_data: list[dict[str, Any]]) -> list[PurchaseOrder]:
    """Backwards-compatible alias returning just the affected purchase orders."""
    return upsert_purchase_orders(team, purchase_orders_data).purchase_orders


def _line_defaults(team: Team, line_data: dict[str, Any]) -> dict[str, Any]:
    raw_price = line_data.get("unit_price")
    return {
        "team": team,
        "line_no": line_data.get("line_no", ""),
        "item_no": line_data.get("item_no", ""),
        "description": line_data.get("description", ""),
        "ordered_qty": Decimal(str(line_data.get("ordered_qty", 0))),
        "shipped_qty": Decimal(str(line_data.get("shipped_qty", 0))),
        "received_qty": Decimal(str(line_data.get("received_qty", 0))),
        "unit_price": Decimal(str(raw_price)) if raw_price is not None else None,
        "expected_receipt_date": line_data.get("expected_receipt_date"),
    }


def _upsert_lines(team: Team, purchase_order: PurchaseOrder, lines_data: list[dict[str, Any]]) -> None:
    for line_data in lines_data:
        PurchaseOrderLine.objects.update_or_create(
            purchase_order=purchase_order,
            external_id=line_data["external_id"],
            defaults=_line_defaults(team, line_data),
        )


def _purchase_order_unchanged(
    existing: PurchaseOrder,
    defaults: dict[str, Any],
    lines_data: list[dict[str, Any]],
) -> bool:
    """Return True when the stored PO and all its lines already match the source.

    Used so a re-sync of identical data is reported as "unchanged" rather than
    "updated". Only the source-owned fields are compared.
    """
    if any(getattr(existing, attr) != value for attr, value in defaults.items()):
        return False

    existing_lines = {line.external_id: line for line in existing.lines.all()}
    if len(existing_lines) != len(lines_data):
        return False

    for line_data in lines_data:
        line = existing_lines.get(line_data.get("external_id"))
        if line is None:
            return False
        expected = _line_defaults(existing.team, line_data)
        # team is a relation stored elsewhere; compare only the data fields.
        for attr in (
            "line_no",
            "item_no",
            "description",
            "ordered_qty",
            "shipped_qty",
            "received_qty",
            "unit_price",
            "expected_receipt_date",
        ):
            if getattr(line, attr) != expected[attr]:
                return False
    return True


# ---------------------------------------------------------------------------
# Fulfillment engine
# ---------------------------------------------------------------------------


def calculate_purchase_order_fulfillment(purchase_order: PurchaseOrder) -> dict[str, Decimal]:
    """Aggregate qty figures for a purchase order from its lines and supplier deliveries.

    Returns:
        Dict with ordered_qty, shipped_qty, in_transit_qty, arrived_qty,
        received_qty, remaining_qty — all as Decimal.
    """
    from django.db.models import Sum

    from apps.scm.supplier_deliveries.models import SupplierDeliveryLine, SupplierDeliveryStatus

    aggregates = purchase_order.lines.aggregate(
        total_ordered=Sum("ordered_qty"),
        total_shipped=Sum("shipped_qty"),
        total_received=Sum("received_qty"),
    )

    ordered = aggregates["total_ordered"] or Decimal("0")
    shipped = aggregates["total_shipped"] or Decimal("0")
    received = aggregates["total_received"] or Decimal("0")

    # arrived = goods that have arrived at destination but not yet formally received in ERP
    arrived_agg = SupplierDeliveryLine.objects.filter(
        delivery__purchase_order=purchase_order,
        delivery__status=SupplierDeliveryStatus.ARRIVED,
    ).aggregate(total=Sum("delivery_qty"))
    arrived = arrived_agg["total"] or Decimal("0")

    in_transit = max(shipped - received - arrived, Decimal("0"))
    remaining = max(ordered - received, Decimal("0"))

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
