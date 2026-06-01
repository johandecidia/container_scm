"""Read-only queries for supplier deliveries. All queries are team-scoped."""

from __future__ import annotations

import datetime
from decimal import Decimal

from django.db.models import Sum

from apps.scm.procurement.models import PurchaseOrder
from apps.teams.models import Team

from .models import SupplierDelivery, SupplierDeliveryLine, SupplierDeliveryStatus


def get_supplier_deliveries_for_team(team: Team):
    """Return all supplier deliveries for the team, newest first."""
    return SupplierDelivery.objects.filter(team=team).select_related("purchase_order", "team").order_by("-created_at")


def get_supplier_delivery_detail(team: Team, delivery_id: int) -> SupplierDelivery:
    """Return a single delivery for the team, or raise DoesNotExist."""
    return SupplierDelivery.objects.get(team=team, pk=delivery_id)


def get_supplier_deliveries_for_purchase_order(team: Team, purchase_order: PurchaseOrder):
    """Return all deliveries for a specific purchase order."""
    return (
        SupplierDelivery.objects.filter(team=team, purchase_order=purchase_order)
        .select_related("purchase_order")
        .order_by("-created_at")
    )


def get_delivery_lines_for_delivery(team: Team, delivery: SupplierDelivery):
    """Return all lines for a delivery."""
    return SupplierDeliveryLine.objects.filter(team=team, delivery=delivery).select_related(
        "purchase_order_line", "container"
    )


def get_po_delivery_summary(team: Team, purchase_order: PurchaseOrder) -> dict:
    """Aggregate delivery quantities across all deliveries for a purchase order.

    Returns:
        Dict with ordered_qty, planned_qty, shipped_qty, received_qty, remaining_qty.
    """
    ordered_agg = purchase_order.lines.aggregate(total=Sum("ordered_qty"))
    ordered_qty = ordered_agg["total"] or Decimal("0")

    deliveries = SupplierDelivery.objects.filter(team=team, purchase_order=purchase_order)

    planned_statuses = [
        SupplierDeliveryStatus.PLANNED,
        SupplierDeliveryStatus.BOOKED,
        SupplierDeliveryStatus.IN_PRODUCTION,
        SupplierDeliveryStatus.READY,
        SupplierDeliveryStatus.SHIPPED,
        SupplierDeliveryStatus.IN_TRANSIT,
        SupplierDeliveryStatus.ARRIVED,
        SupplierDeliveryStatus.RECEIVED,
    ]
    planned_ids = deliveries.filter(status__in=planned_statuses).values_list("id", flat=True)
    planned_agg = SupplierDeliveryLine.objects.filter(team=team, delivery__in=planned_ids).aggregate(
        total=Sum("delivery_qty")
    )
    planned_qty = planned_agg["total"] or Decimal("0")

    shipped_statuses = [
        SupplierDeliveryStatus.SHIPPED,
        SupplierDeliveryStatus.IN_TRANSIT,
        SupplierDeliveryStatus.ARRIVED,
        SupplierDeliveryStatus.RECEIVED,
    ]
    shipped_ids = deliveries.filter(status__in=shipped_statuses).values_list("id", flat=True)
    shipped_agg = SupplierDeliveryLine.objects.filter(team=team, delivery__in=shipped_ids).aggregate(
        total=Sum("delivery_qty")
    )
    shipped_qty = shipped_agg["total"] or Decimal("0")

    received_ids = deliveries.filter(status=SupplierDeliveryStatus.RECEIVED).values_list("id", flat=True)
    received_agg = SupplierDeliveryLine.objects.filter(team=team, delivery__in=received_ids).aggregate(
        total=Sum("delivery_qty")
    )
    received_qty = received_agg["total"] or Decimal("0")

    remaining_qty = max(ordered_qty - received_qty, Decimal("0"))

    return {
        "ordered_qty": ordered_qty,
        "planned_qty": planned_qty,
        "shipped_qty": shipped_qty,
        "received_qty": received_qty,
        "remaining_qty": remaining_qty,
    }


def get_supplier_delivery_dashboard(team: Team) -> dict:
    """Return dashboard counts for supplier deliveries.

    Returns:
        Dict with open_count, partial_count, completed_count, in_transit_count,
        arriving_soon_count, delayed_count.

    Definitions:
        Open: PLANNED, BOOKED, IN_PRODUCTION, READY
        Partial: SHIPPED, IN_TRANSIT, ARRIVED
        Completed: RECEIVED
        Delayed: planned_arrival_date < today and not RECEIVED/CANCELLED
    """
    today = datetime.date.today()
    soon_cutoff = today + datetime.timedelta(days=7)

    deliveries = SupplierDelivery.objects.filter(team=team)

    open_statuses = [
        SupplierDeliveryStatus.PLANNED,
        SupplierDeliveryStatus.BOOKED,
        SupplierDeliveryStatus.IN_PRODUCTION,
        SupplierDeliveryStatus.READY,
    ]
    partial_statuses = [
        SupplierDeliveryStatus.SHIPPED,
        SupplierDeliveryStatus.IN_TRANSIT,
        SupplierDeliveryStatus.ARRIVED,
    ]

    open_count = deliveries.filter(status__in=open_statuses).count()
    partial_count = deliveries.filter(status__in=partial_statuses).count()
    completed_count = deliveries.filter(status=SupplierDeliveryStatus.RECEIVED).count()
    in_transit_count = deliveries.filter(status=SupplierDeliveryStatus.IN_TRANSIT).count()
    arriving_soon_count = deliveries.filter(
        status__in=partial_statuses,
        planned_arrival_date__gte=today,
        planned_arrival_date__lte=soon_cutoff,
    ).count()
    delayed_count = (
        deliveries.filter(planned_arrival_date__lt=today)
        .exclude(status__in=[SupplierDeliveryStatus.RECEIVED, SupplierDeliveryStatus.CANCELLED])
        .count()
    )

    return {
        "open_count": open_count,
        "partial_count": partial_count,
        "completed_count": completed_count,
        "in_transit_count": in_transit_count,
        "arriving_soon_count": arriving_soon_count,
        "delayed_count": delayed_count,
    }
