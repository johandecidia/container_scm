"""Read-only queries for procurement data. All queries are team-scoped."""

from apps.teams.models import Team

from .models import PurchaseOrder, PurchaseOrderEvent, PurchaseOrderLine, PurchaseOrderLogisticsStatus
from .workspace import (
    PurchaseOrderWorkspace,
    get_purchase_order_line_summaries,
    get_purchase_order_workspace,
)


def get_team_purchase_orders(team: Team):
    """Return all purchase orders for the given team, newest first."""
    return (
        PurchaseOrder.objects.filter(team=team)
        .select_related("team")
        .prefetch_related("lines")
        .order_by("-order_date", "po_number")
    )


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
