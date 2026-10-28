"""Read-only queries for procurement data. All queries are team-scoped."""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING

from django.db.models import Case, DecimalField, F, IntegerField, Q, QuerySet, Sum, Value, When
from django.db.models.functions import Coalesce, Greatest, Lower

from apps.teams.models import Team

from .models import (
    PurchaseOrder,
    PurchaseOrderEvent,
    PurchaseOrderLine,
    PurchaseOrderLogisticsStatus,
    PurchaseOrderStatus,
)
from .workspace import (
    PurchaseOrderWorkspace,
    get_purchase_order_line_summaries,
    get_purchase_order_workspace,
)

if TYPE_CHECKING:
    from .list_table import PurchaseOrderListFilters


def get_team_purchase_orders(team: Team):
    """Return all purchase orders for the given team, newest first."""
    return (
        PurchaseOrder.objects.filter(team=team)
        .select_related("team")
        .prefetch_related("lines")
        .order_by("-order_date", "po_number")
    )


# The list's default order — the model's own — plus the primary key, so rows that tie
# keep one place across pages instead of trading them between requests.
PURCHASE_ORDER_LIST_DEFAULT_ORDER = ("-order_date", "po_number", "pk")

_QTY = DecimalField(max_digits=14, decimal_places=3)


def annotate_purchase_order_totals(queryset):
    """Add each order's ordered, shipped, received and remaining quantity, summed in SQL.

    The same arithmetic as
    :func:`~apps.scm.procurement.services.calculate_purchase_order_fulfillment` —
    line sums, a missing sum read as zero, remaining never below zero — done in the
    database so the list can sort and filter on it and render a page in one query
    rather than two per row. A test holds the two to the same answers.
    """
    zero = Value(Decimal("0"), output_field=_QTY)
    ordered = Coalesce(Sum("lines__ordered_qty"), zero, output_field=_QTY)
    received = Coalesce(Sum("lines__received_qty"), zero, output_field=_QTY)
    return queryset.annotate(
        ordered_total=ordered,
        shipped_total=Coalesce(Sum("lines__shipped_qty"), zero, output_field=_QTY),
        received_total=received,
        remaining_total=Greatest(ordered - received, zero, output_field=_QTY),
    )


# Sortable columns: the URL's key to the expression behind it. Nothing outside this
# mapping ever reaches order_by.
PURCHASE_ORDER_SORTS = {
    "po_number": F("po_number"),
    "supplier": Lower("supplier_name"),
    # Status sorts in the order an order moves through, not alphabetically.
    "status": Case(
        *(When(status=value, then=Value(rank)) for rank, value in enumerate(PurchaseOrderStatus.values)),
        default=Value(len(PurchaseOrderStatus.values)),
        output_field=IntegerField(),
    ),
    "ordered": F("ordered_total"),
    "shipped": F("shipped_total"),
    "received": F("received_total"),
    "remaining": F("remaining_total"),
}


def filter_purchase_orders(team: Team, filters: PurchaseOrderListFilters) -> QuerySet[PurchaseOrder]:
    """The purchase order list for *team*: filtered, sorted, totals annotated.

    Filtered and sorted in the database, before pagination, so a page is a slice of
    the whole matching list. Built on :func:`get_team_purchase_orders` for its team
    scope, without its line prefetch — the totals come from the annotation.
    """
    queryset = annotate_purchase_order_totals(get_team_purchase_orders(team=team).prefetch_related(None))
    if filters.po:
        queryset = queryset.filter(po_number__icontains=filters.po)
    if filters.supplier:
        queryset = queryset.filter(supplier_name__icontains=filters.supplier)
    if filters.statuses:
        queryset = queryset.filter(status__in=filters.statuses)
    if filters.sources:
        queryset = queryset.filter(source_system__in=filters.sources)
    for column, (low, high) in filters.ranges.items():
        bounds = Q()
        if low is not None:
            bounds &= Q(**{f"{column}_total__gte": low})
        if high is not None:
            bounds &= Q(**{f"{column}_total__lte": high})
        queryset = queryset.filter(bounds)

    sort = filters.sort
    if not sort.field:
        return queryset.order_by(*PURCHASE_ORDER_LIST_DEFAULT_ORDER)
    expression = PURCHASE_ORDER_SORTS[sort.field]
    primary = expression.desc() if sort.direction == "desc" else expression.asc()
    return queryset.order_by(primary, *PURCHASE_ORDER_LIST_DEFAULT_ORDER)


def get_purchase_order_for_team(team: Team, purchase_order_id: int) -> PurchaseOrder:
    """Return a single purchase order that belongs to the team, or raise DoesNotExist."""
    return PurchaseOrder.objects.get(team=team, pk=purchase_order_id)


def get_purchase_order_lines(purchase_order: PurchaseOrder):
    """Return all lines for a purchase order, ordered by line number."""
    return PurchaseOrderLine.objects.filter(purchase_order=purchase_order).order_by("line_no")


def get_purchase_order_events(purchase_order: PurchaseOrder):
    """Return all timeline events for a purchase order, oldest first."""
    return PurchaseOrderEvent.objects.filter(purchase_order=purchase_order).order_by("timestamp")


def get_purchase_order_logistics_status(purchase_order: PurchaseOrder) -> str:
    """Compute the SCM logistics status for a purchase order.

    This is the single canonical implementation of SCM logistics status. It is
    derived from the fulfillment quantities (PO lines + supplier deliveries) via
    ``calculate_purchase_order_fulfillment`` — it is NOT stored on the model and
    is never written by the Business Central sync. ``PurchaseOrder.status`` holds
    the Business Central *document* status and is a separate concept.

    Returns a value from ``PurchaseOrderLogisticsStatus``. Precedence (a PO can
    match several conditions; the earliest wins):
      - EXCEPTION: received more than ordered (data/receipt anomaly)
      - COMPLETED: received the full ordered quantity
      - PARTIALLY_RECEIVED: some (but not all) received
      - ARRIVED: goods arrived at destination, none received yet
      - FULLY_SHIPPED: whole order shipped, none arrived/received yet
      - PARTIALLY_SHIPPED: some shipped, none arrived/received yet
      - NOT_STARTED: nothing shipped, arrived, or received

    Note: "in transit" is represented by the shipped states (shipped but not yet
    arrived). A distinct in-transit state and cancellation-driven exceptions are
    out of scope until richer source signals are synced.
    """
    from .services import calculate_purchase_order_fulfillment

    f = calculate_purchase_order_fulfillment(purchase_order)
    ordered = f["ordered_qty"]
    shipped = f["shipped_qty"]
    arrived = f["arrived_qty"]
    received = f["received_qty"]

    S = PurchaseOrderLogisticsStatus
    if ordered <= 0:
        return S.NOT_STARTED
    if received > ordered:
        return S.EXCEPTION
    if received >= ordered:
        return S.COMPLETED
    if received > 0:
        return S.PARTIALLY_RECEIVED
    if arrived > 0:
        return S.ARRIVED
    if shipped >= ordered:
        return S.FULLY_SHIPPED
    if shipped > 0:
        return S.PARTIALLY_SHIPPED
    return S.NOT_STARTED


# The purchase order workspace read model lives in workspace.py; re-exported here so
# callers keep importing selectors for reads, as they do for containers.
__all__ = [
    "PurchaseOrderWorkspace",
    "get_purchase_order_events",
    "get_purchase_order_for_team",
    "get_purchase_order_line_summaries",
    "get_purchase_order_lines",
    "get_purchase_order_logistics_status",
    "get_purchase_order_workspace",
    "get_team_purchase_orders",
]
