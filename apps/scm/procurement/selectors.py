"""Read-only queries for procurement data. All queries are team-scoped."""

from apps.teams.models import Team

from .models import PurchaseOrder, PurchaseOrderEvent, PurchaseOrderLine


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
