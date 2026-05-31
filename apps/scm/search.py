# SCM global search — team-scoped search across containers and shipments.
from dataclasses import dataclass

from django.db.models import Q

from apps.teams.models import Team


@dataclass
class SearchResult:
    kind: str  # "container" | "shipment" | "tracking"
    title: str
    subtitle: str
    url: str


def search_scm(team: Team, query: str) -> list[SearchResult]:
    """Search containers, shipments and tracking subscriptions for *query*, scoped to *team*.

    Searches:
    - containers: container_id (owner_code + serial_number), current_location
    - shipments: shipment_number, reference, carrier_booking_reference, bill_of_lading_number, customer_name, carrier
    - tracking subscriptions: tracking_reference
    """
    from django.urls import reverse

    from apps.scm.containers.models import Container
    from apps.scm.shipments.models import Shipment
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
            | Q(current_location__icontains=q)
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

    return results
