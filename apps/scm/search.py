# SCM global search — team-scoped search across containers, shipments, POs and deliveries.
from dataclasses import dataclass

from django.db.models import Q

from apps.teams.models import Team


@dataclass
class SearchResult:
    kind: str  # "container" | "shipment" | "tracking" | "purchase_order" | "supplier_delivery"
    title: str
    subtitle: str
    url: str


def search_scm(team: Team, query: str) -> list[SearchResult]:
    """Search SCM objects for *query*, scoped to *team*.

    Searches containers, shipments, tracking subscriptions,
    purchase orders, and supplier deliveries.
    Returns at most 10 results per kind.
    Empty query always returns an empty list.
    """
    from django.urls import reverse

    from apps.scm.containers.models import Container
    from apps.scm.procurement.models import PurchaseOrder
    from apps.scm.shipments.models import Shipment
    from apps.scm.supplier_deliveries.models import SupplierDelivery
    from apps.scm.tracking.models import TrackingSubscription

    results: list[SearchResult] = []
    q = query.strip()
    if not q:
        return results

    # Containers
    containers = (
        Container.objects.filter(team=team)
        .filter(
            Q(owner_code__icontains=q)
            | Q(serial_number__icontains=q)
            | Q(current_location__name__icontains=q)
            | Q(location_text__icontains=q)
            | Q(manufacturer__icontains=q)
        )
        .select_related("equipment_type")[:10]
    )

    for c in containers:
        results.append(
            SearchResult(
                kind="container",
                title=c.container_id,
                subtitle=c.equipment_type.description if c.equipment_type_id else "",
                url=reverse("containers:detail", kwargs={"container_id": c.pk}),
            )
        )

    # Shipments
    shipments = Shipment.objects.filter(team=team).filter(
        Q(shipment_number__icontains=q)
        | Q(reference__icontains=q)
        | Q(carrier_booking_reference__icontains=q)
        | Q(bill_of_lading_number__icontains=q)
        | Q(customer_name__icontains=q)
        | Q(carrier__icontains=q)
        | Q(origin_port__icontains=q)
        | Q(destination_port__icontains=q)
    )[:10]

    for s in shipments:
        route = " → ".join(filter(None, [s.origin_port, s.destination_port]))
        results.append(
            SearchResult(
                kind="shipment",
                title=str(s),
                subtitle=route or s.get_status_display(),
                url=reverse("shipments:detail", kwargs={"pk": s.pk}),
            )
        )

    # Tracking subscriptions
    subscriptions = TrackingSubscription.objects.filter(
        team=team,
        tracking_reference__icontains=q,
    ).select_related("provider")[:5]

    for sub in subscriptions:
        results.append(
            SearchResult(
                kind="tracking",
                title=sub.tracking_reference,
                subtitle=str(sub.provider) if sub.provider_id else "",
                url=reverse("tracking:detail", kwargs={"pk": sub.pk}),
            )
        )

    # Purchase orders
    purchase_orders = PurchaseOrder.objects.filter(team=team).filter(
        Q(po_number__icontains=q) | Q(supplier_no__icontains=q) | Q(supplier_name__icontains=q)
    )[:10]

    for po in purchase_orders:
        results.append(
            SearchResult(
                kind="purchase_order",
                title=po.po_number,
                subtitle=po.supplier_name,
                url=reverse("procurement:purchase_order_detail", kwargs={"purchase_order_id": po.pk}),
            )
        )

    # Supplier deliveries
    deliveries = (
        SupplierDelivery.objects.filter(team=team)
        .filter(
            Q(delivery_reference__icontains=q) | Q(supplier__icontains=q) | Q(purchase_order__po_number__icontains=q)
        )
        .select_related("purchase_order")[:10]
    )

    for delivery in deliveries:
        results.append(
            SearchResult(
                kind="supplier_delivery",
                title=delivery.delivery_reference,
                subtitle=f"PO: {delivery.purchase_order.po_number} — {delivery.get_status_display()}",
                url=reverse("supplier_deliveries:detail", kwargs={"delivery_id": delivery.pk}),
            )
        )

    return results
