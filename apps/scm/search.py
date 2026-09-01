"""SCM global search — team-scoped search across the operational objects.

Two rules shape the ordering.

**A container number is an answer, not a guess.** Someone typing MCUU2009300 into
the search box knows exactly which box they mean, so that container leads the
results and is marked as an exact match. Everything else the query happens to
touch follows it.

**Kinds are ranked by how often they are the thing being looked for.** Containers,
purchase orders, shipments and locations are what people navigate to. Supplier
deliveries and tracking references stay searchable — they are how you get to a
specific reference when you have one — but they sit at the bottom rather than
pushing a container off the top.

The scale here is one team's operational data, so this is Postgres `icontains` and
nothing more. No search infrastructure.
"""

from dataclasses import dataclass

from django.db.models import Q
from django.utils.translation import gettext_lazy as _

from apps.scm.containers.utils import container_number_query
from apps.teams.models import Team

# How many of each kind to offer. Enough to recognise the one you meant, few enough
# that no kind buries the ones under it.
_PER_KIND = 10
_PER_MINOR_KIND = 5

# The order results are returned and grouped in, with the heading for each group.
KIND_LABELS: dict[str, str] = {
    "container": _("Containers"),
    "purchase_order": _("Purchase orders"),
    "shipment": _("Shipments"),
    "location": _("Locations"),
    "supplier_delivery": _("Supplier deliveries"),
    "tracking": _("Tracking references"),
}


@dataclass
class SearchResult:
    kind: str  # one of KIND_LABELS
    title: str
    subtitle: str
    url: str
    # True only for a container whose full ISO number was typed. The result is
    # unambiguous, and the UI is allowed to say so.
    is_exact: bool = False


@dataclass
class SearchGroup:
    """One kind's results, for a UI that separates them."""

    kind: str
    label: str
    results: list[SearchResult]


def search_scm(team: Team, query: str) -> list[SearchResult]:
    """Search SCM objects for *query*, scoped to *team*.

    Returns results ordered by kind — see KIND_LABELS — with an exact container
    number first. Empty query always returns an empty list.
    """
    q = query.strip()
    if not q:
        return []

    by_kind = {
        "container": _search_containers(team, q),
        "purchase_order": _search_purchase_orders(team, q),
        "shipment": _search_shipments(team, q),
        "location": _search_locations(team, q),
        "supplier_delivery": _search_supplier_deliveries(team, q),
        "tracking": _search_tracking(team, q),
    }
    return [result for kind in KIND_LABELS for result in by_kind[kind]]


def group_search_results(results: list[SearchResult]) -> list[SearchGroup]:
    """Bucket results by kind, in KIND_LABELS order, dropping empty kinds."""
    grouped: dict[str, list[SearchResult]] = {}
    for result in results:
        grouped.setdefault(result.kind, []).append(result)
    return [
        SearchGroup(kind=kind, label=str(label), results=grouped[kind])
        for kind, label in KIND_LABELS.items()
        if kind in grouped
    ]


def _search_containers(team: Team, q: str) -> list[SearchResult]:
    """Containers matching *q*, the exact ISO number first.

    A container's ISO number is stored as four columns, so `icontains` over
    `owner_code` and `serial_number` cannot match a number typed whole — the string
    MCUU2009300 exists nowhere in the database. `container_number_query` decomposes
    it, which is what makes searching for the number on the box work at all.
    """
    from apps.scm.containers.models import Container

    number_query = container_number_query(q)

    matches = (
        Q(owner_code__icontains=q)
        | Q(serial_number__icontains=q)
        | Q(current_location__name__icontains=q)
        | Q(location_text__icontains=q)
        | Q(manufacturer__icontains=q)
    )
    if number_query is not None:
        matches |= number_query.filters

    # Fetched on its own rather than picked out of the list below: a team with many
    # containers whose numbers share a prefix could otherwise push the container
    # somebody typed in full past the result cap.
    exact = None
    if number_query is not None and number_query.is_whole_number:
        exact = (
            Container.objects.filter(team=team).filter(number_query.filters).select_related("equipment_type").first()
        )

    results = [] if exact is None else [_container_result(exact, is_exact=True)]
    others = Container.objects.filter(team=team).filter(matches).select_related("equipment_type")
    if exact is not None:
        others = others.exclude(pk=exact.pk)
    results.extend(_container_result(container, is_exact=False) for container in others[:_PER_KIND])
    return results[:_PER_KIND]


def _container_result(container, *, is_exact: bool) -> SearchResult:
    from django.urls import reverse

    return SearchResult(
        kind="container",
        title=container.container_id,
        subtitle=container.equipment_type.description if container.equipment_type_id else "",
        url=reverse("containers:detail", kwargs={"container_id": container.pk}),
        is_exact=is_exact,
    )


def _search_purchase_orders(team: Team, q: str) -> list[SearchResult]:
    from django.urls import reverse

    from apps.scm.procurement.models import PurchaseOrder

    orders = PurchaseOrder.objects.filter(team=team).filter(
        Q(po_number__icontains=q) | Q(supplier_no__icontains=q) | Q(supplier_name__icontains=q)
    )[:_PER_KIND]
    return [
        SearchResult(
            kind="purchase_order",
            title=po.po_number,
            subtitle=po.supplier_name,
            url=reverse("procurement:purchase_order_detail", kwargs={"purchase_order_id": po.pk}),
        )
        for po in orders
    ]


def _search_shipments(team: Team, q: str) -> list[SearchResult]:
    from django.urls import reverse

    from apps.scm.shipments.models import Shipment

    shipments = Shipment.objects.filter(team=team).filter(
        Q(shipment_number__icontains=q)
        | Q(reference__icontains=q)
        | Q(carrier_booking_reference__icontains=q)
        | Q(bill_of_lading_number__icontains=q)
        | Q(customer_name__icontains=q)
        | Q(carrier__icontains=q)
        | Q(origin_port__icontains=q)
        | Q(destination_port__icontains=q)
    )[:_PER_KIND]
    return [
        SearchResult(
            kind="shipment",
            title=str(s),
            subtitle=s.route_label or s.get_status_display(),
            url=reverse("shipments:detail", kwargs={"pk": s.pk}),
        )
        for s in shipments
    ]


def _search_locations(team: Team, q: str) -> list[SearchResult]:
    """Locations matching *q*, each leading to its own workspace.

    The destination used to be the container list filtered to the location, because
    no location page existed. It does now, and it answers more of what somebody
    searching for a depot wants to know — what is standing there, and what has moved
    — so the filtered list is no longer the best available answer.
    """
    from django.urls import reverse

    from apps.scm.containers.models import ContainerLocation

    locations = ContainerLocation.objects.filter(team=team, is_active=True).filter(
        Q(name__icontains=q) | Q(city__icontains=q) | Q(country__icontains=q) | Q(external_reference__icontains=q)
    )[:_PER_KIND]
    return [
        SearchResult(
            kind="location",
            title=location.name,
            subtitle=", ".join(part for part in (location.city, location.country) if part)
            or location.get_location_type_display(),
            url=reverse("containers:location_detail", kwargs={"location_id": location.pk}),
        )
        for location in locations
    ]


def _search_supplier_deliveries(team: Team, q: str) -> list[SearchResult]:
    from django.urls import reverse

    from apps.scm.supplier_deliveries.models import SupplierDelivery

    deliveries = (
        SupplierDelivery.objects.filter(team=team)
        .filter(
            Q(delivery_reference__icontains=q) | Q(supplier__icontains=q) | Q(purchase_order__po_number__icontains=q)
        )
        .select_related("purchase_order")[:_PER_MINOR_KIND]
    )
    return [
        SearchResult(
            kind="supplier_delivery",
            title=delivery.delivery_reference,
            subtitle=f"PO: {delivery.purchase_order.po_number} — {delivery.get_status_display()}",
            url=reverse("supplier_deliveries:detail", kwargs={"delivery_id": delivery.pk}),
        )
        for delivery in deliveries
    ]


def _search_tracking(team: Team, q: str) -> list[SearchResult]:
    from django.urls import reverse

    from apps.scm.tracking.models import TrackingSubscription

    subscriptions = TrackingSubscription.objects.filter(
        team=team,
        tracking_reference__icontains=q,
    ).select_related("provider")[:_PER_MINOR_KIND]
    return [
        SearchResult(
            kind="tracking",
            title=sub.tracking_reference,
            subtitle=str(sub.provider) if sub.provider_id else "",
            url=reverse("tracking:detail", kwargs={"pk": sub.pk}),
        )
        for sub in subscriptions
    ]
