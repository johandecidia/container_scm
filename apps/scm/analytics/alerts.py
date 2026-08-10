# SCM alerts — compute actionable alerts from live operational data.

from dataclasses import dataclass

from django.utils import timezone

from apps.teams.models import Team


@dataclass
class SCMAlert:
    type: str
    severity: str  # "info" | "warning" | "error"
    title: str
    message: str
    url: str = ""
    reference_date: str = ""


def get_scm_alerts(team: Team) -> list[SCMAlert]:
    """Return current SCM alerts for the team, derived from live data.

    Checks for:
    - Delayed active shipments (ETA passed)
    - Shipments in EXCEPTION status
    - Failed tracking subscriptions
    - Overdue supplier deliveries (planned arrival date passed)
    """
    from django.urls import reverse

    from apps.scm.shipments.models import Shipment
    from apps.scm.supplier_deliveries.models import SupplierDelivery, SupplierDeliveryStatus
    from apps.scm.tracking.models import TrackingSubscription

    alerts: list[SCMAlert] = []
    today = timezone.now().date()

    # Delayed active shipments
    delayed = Shipment.objects.filter(
        team=team,
        status__in=[Shipment.Status.BOOKED, Shipment.Status.IN_TRANSIT],
        eta__isnull=False,
        eta__lt=today,
    ).order_by("eta")[:5]

    for s in delayed:
        alerts.append(
            SCMAlert(
                type="delayed_shipment",
                severity="warning",
                title=f"Delayed: {s}",
                message=f"ETA was {s.eta:%Y-%m-%d}. Status: {s.get_status_display()}.",
                url=reverse("shipments:detail", kwargs={"pk": s.pk}),
                reference_date=str(s.eta),
            )
        )

    # Shipments in exception
    exceptions = Shipment.objects.filter(
        team=team,
        status=Shipment.Status.EXCEPTION,
    ).order_by("-updated_at")[:5]

    for s in exceptions:
        alerts.append(
            SCMAlert(
                type="shipment_exception",
                severity="error",
                title=f"Exception: {s}",
                message="This shipment has an active exception.",
                url=reverse("shipments:detail", kwargs={"pk": s.pk}),
            )
        )

    # Failed tracking subscriptions
    failed = TrackingSubscription.objects.filter(
        team=team,
        status="FAILED",
    ).select_related("shipment", "container")[:5]

    for sub in failed:
        ref = sub.tracking_reference or str(sub.pk)
        alerts.append(
            SCMAlert(
                type="tracking_failed",
                severity="warning",
                title=f"Tracking failed: {ref}",
                message=sub.last_error_message or "Tracking sync failed.",
                url=reverse("tracking:detail", kwargs={"pk": sub.pk}),
            )
        )

    # Overdue supplier deliveries
    overdue = (
        SupplierDelivery.objects.filter(
            team=team,
            planned_arrival_date__isnull=False,
            planned_arrival_date__lt=today,
        )
        .exclude(status__in=[SupplierDeliveryStatus.RECEIVED, SupplierDeliveryStatus.CANCELLED])
        .order_by("planned_arrival_date")[:5]
    )

    for delivery in overdue:
        alerts.append(
            SCMAlert(
                type="overdue_delivery",
                severity="warning",
                title=f"Overdue delivery: {delivery.delivery_reference}",
                message=(
                    f"Planned arrival: {delivery.planned_arrival_date:%Y-%m-%d}. "
                    f"Status: {delivery.get_status_display()}."
                ),
                url=reverse("supplier_deliveries:detail", kwargs={"delivery_id": delivery.pk}),
                reference_date=str(delivery.planned_arrival_date),
            )
        )

    return alerts
