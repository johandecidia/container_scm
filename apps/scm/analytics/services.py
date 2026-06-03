# Analytics services — computation and aggregation logic.

import datetime
from decimal import Decimal

from django.db.models import Count
from django.utils import timezone

from apps.scm.containers.choices import ContainerStatus
from apps.scm.containers.models import Container
from apps.scm.shipments.models import Shipment
from apps.teams.models import Team

from .models import AnalyticsSnapshot

# Shipment statuses considered "active" (booked, in transit, or arrived but not yet delivered)
_ACTIVE_STATUSES = {Shipment.Status.BOOKED, Shipment.Status.IN_TRANSIT, Shipment.Status.ARRIVED}
_COMPLETED_STATUSES = {Shipment.Status.DELIVERED}


# ---------------------------------------------------------------------------
# Basic KPI functions
# ---------------------------------------------------------------------------


def get_total_shipments(team: Team) -> int:
    return Shipment.objects.filter(team=team).count()


def get_active_shipments(team: Team) -> int:
    return Shipment.objects.filter(team=team, status__in=_ACTIVE_STATUSES).count()


def get_completed_shipments(team: Team) -> int:
    return Shipment.objects.filter(team=team, status__in=_COMPLETED_STATUSES).count()


def get_containers_in_transit(team: Team) -> int:
    return Container.objects.filter(team=team, status=ContainerStatus.IN_TRANSIT).count()


def get_containers_delivered(team: Team) -> int:
    """Count distinct containers that appear on at least one delivered shipment."""
    return (
        Container.objects.filter(
            team=team,
            shipment_containers__shipment__status=Shipment.Status.DELIVERED,
        )
        .distinct()
        .count()
    )


def get_average_transit_days(team: Team) -> Decimal | None:
    """Return mean transit duration in days for delivered shipments with both dates set."""
    shipments = Shipment.objects.filter(
        team=team,
        status=Shipment.Status.DELIVERED,
        actual_departure_at__isnull=False,
        actual_arrival_at__isnull=False,
    ).only("actual_departure_at", "actual_arrival_at")

    total_days = 0.0
    count = 0
    for s in shipments:
        if s.actual_arrival_at is None or s.actual_departure_at is None:
            continue
        delta = s.actual_arrival_at - s.actual_departure_at
        total_days += delta.total_seconds() / 86400
        count += 1

    if count == 0:
        return None
    return Decimal(str(round(total_days / count, 2)))


# ---------------------------------------------------------------------------
# Transit time analytics
# ---------------------------------------------------------------------------


def get_transit_time_analytics(
    team: Team,
    date_from: datetime.date | None = None,
    date_to: datetime.date | None = None,
) -> dict:
    """Transit time stats for delivered shipments with both departure and arrival dates.

    Returns count, avg/min/max days, and delayed vs on-time counts.
    Shipments without a reference ETA are counted but not classified as on-time or delayed.
    """
    qs = Shipment.objects.filter(
        team=team,
        status=Shipment.Status.DELIVERED,
        actual_departure_at__isnull=False,
        actual_arrival_at__isnull=False,
    ).only("actual_departure_at", "actual_arrival_at", "original_eta", "eta")

    if date_from:
        qs = qs.filter(actual_arrival_at__date__gte=date_from)
    if date_to:
        qs = qs.filter(actual_arrival_at__date__lte=date_to)

    days_list: list[float] = []
    delayed_count = 0
    on_time_count = 0

    for s in qs:
        delta_days = (s.actual_arrival_at - s.actual_departure_at).total_seconds() / 86400
        days_list.append(delta_days)
        ref_eta = s.original_eta or s.eta
        if ref_eta:
            if s.actual_arrival_at.date() > ref_eta:
                delayed_count += 1
            else:
                on_time_count += 1

    count = len(days_list)
    if count == 0:
        return {
            "count": 0,
            "avg_days": None,
            "min_days": None,
            "max_days": None,
            "delayed_count": 0,
            "on_time_count": 0,
        }

    return {
        "count": count,
        "avg_days": round(sum(days_list) / count, 1),
        "min_days": round(min(days_list), 1),
        "max_days": round(max(days_list), 1),
        "delayed_count": delayed_count,
        "on_time_count": on_time_count,
    }


# ---------------------------------------------------------------------------
# Carrier analytics
# ---------------------------------------------------------------------------


def get_carrier_analytics(
    team: Team,
    date_from: datetime.date | None = None,
    date_to: datetime.date | None = None,
) -> list[dict]:
    """Per-carrier KPIs based on shipment data for the given team.

    For each carrier: shipment count, exception count, delayed count,
    on-time/late delivered counts, ETA change count, and on-time percentage.
    """
    today = timezone.now().date()

    qs = Shipment.objects.filter(team=team).exclude(carrier="")
    if date_from:
        qs = qs.filter(created_at__date__gte=date_from)
    if date_to:
        qs = qs.filter(created_at__date__lte=date_to)

    carriers = list(qs.values_list("carrier", flat=True).distinct())
    result: list[dict] = []

    for carrier in carriers:
        carrier_qs = qs.filter(carrier=carrier)
        total = carrier_qs.count()
        exceptions = carrier_qs.filter(status=Shipment.Status.EXCEPTION).count()
        delayed = carrier_qs.filter(
            status__in=[Shipment.Status.BOOKED, Shipment.Status.IN_TRANSIT],
            eta__lt=today,
        ).count()

        delivered_qs = carrier_qs.filter(
            status=Shipment.Status.DELIVERED,
            actual_arrival_at__isnull=False,
        ).only("actual_arrival_at", "original_eta", "eta")

        on_time = 0
        late = 0
        for s in delivered_qs:
            ref_eta = s.original_eta or s.eta
            if ref_eta:
                if s.actual_arrival_at.date() <= ref_eta:
                    on_time += 1
                else:
                    late += 1

        # ETA change count from tracking history — graceful if model is unavailable
        eta_change_count = 0
        try:
            from apps.scm.tracking.models import ETAHistory

            eta_change_count = ETAHistory.objects.filter(
                team=team,
                shipment__carrier=carrier,
            ).count()
        except Exception:
            pass

        on_time_pct: float | None = None
        if (on_time + late) > 0:
            on_time_pct = round(on_time / (on_time + late) * 100, 1)

        result.append(
            {
                "carrier": carrier,
                "shipment_count": total,
                "exception_count": exceptions,
                "delayed_count": delayed,
                "on_time_count": on_time,
                "late_count": late,
                "eta_change_count": eta_change_count,
                "on_time_pct": on_time_pct,
            }
        )

    result.sort(key=lambda x: x["shipment_count"], reverse=True)
    return result


# ---------------------------------------------------------------------------
# Container analytics
# ---------------------------------------------------------------------------


def get_container_analytics(team: Team) -> dict:
    """Container status breakdown and tracking coverage for the given team."""
    qs = Container.objects.filter(team=team)
    status_counts: dict[str, int] = {s: 0 for s in ContainerStatus.values}
    for row in qs.values("status").annotate(cnt=Count("id")):
        status_counts[row["status"]] = row["cnt"]

    from apps.scm.containers.models import PlannedContainer

    planned_count = PlannedContainer.objects.filter(team=team).count()

    # In-transit containers without a recent tracking event (last 7 days)
    in_transit_no_tracking = 0
    try:
        from apps.scm.tracking.models import TrackingEvent

        week_ago = timezone.now() - datetime.timedelta(days=7)
        recently_tracked_ids = (
            TrackingEvent.objects.filter(
                team=team,
                event_datetime__gte=week_ago,
                container__isnull=False,
            )
            .values_list("container_id", flat=True)
            .distinct()
        )
        in_transit_no_tracking = (
            Container.objects.filter(team=team, status=ContainerStatus.IN_TRANSIT)
            .exclude(id__in=recently_tracked_ids)
            .count()
        )
    except Exception:
        pass

    return {
        "total": qs.count(),
        "available": status_counts.get(ContainerStatus.AVAILABLE, 0),
        "booked": status_counts.get(ContainerStatus.BOOKED, 0),
        "in_transit": status_counts.get(ContainerStatus.IN_TRANSIT, 0),
        "repair": status_counts.get(ContainerStatus.REPAIR, 0),
        "decommissioned": status_counts.get(ContainerStatus.DECOMMISSIONED, 0),
        "planned_count": planned_count,
        "in_transit_no_tracking": in_transit_no_tracking,
    }


# ---------------------------------------------------------------------------
# Supplier analytics
# ---------------------------------------------------------------------------


def get_supplier_analytics(
    team: Team,
    date_from: datetime.date | None = None,
    date_to: datetime.date | None = None,
) -> list[dict]:
    """Per-supplier KPIs from purchase orders and supplier deliveries.

    Supplier data is derived from PurchaseOrder records (synced from Business Central).
    Delivery completion comes from SupplierDelivery statuses.
    """
    from apps.scm.procurement.models import PurchaseOrder
    from apps.scm.supplier_deliveries.models import SupplierDelivery, SupplierDeliveryStatus

    po_qs = PurchaseOrder.objects.filter(team=team)
    if date_from:
        po_qs = po_qs.filter(order_date__gte=date_from)
    if date_to:
        po_qs = po_qs.filter(order_date__lte=date_to)

    suppliers = list(po_qs.values_list("supplier_no", "supplier_name").distinct())
    result: list[dict] = []

    for supplier_no, supplier_name in suppliers:
        po_count = po_qs.filter(supplier_no=supplier_no).count()

        deliveries = SupplierDelivery.objects.filter(
            team=team,
            purchase_order__supplier_no=supplier_no,
        )
        completed = deliveries.filter(status=SupplierDeliveryStatus.RECEIVED).count()
        partial = deliveries.filter(
            status__in=[
                SupplierDeliveryStatus.SHIPPED,
                SupplierDeliveryStatus.IN_TRANSIT,
                SupplierDeliveryStatus.ARRIVED,
            ]
        ).count()

        result.append(
            {
                "supplier_no": supplier_no,
                "supplier_name": supplier_name,
                "po_count": po_count,
                "completed_delivery_count": completed,
                "partial_delivery_count": partial,
            }
        )

    result.sort(key=lambda x: x["po_count"], reverse=True)
    return result


# ---------------------------------------------------------------------------
# Snapshot generation
# ---------------------------------------------------------------------------


def create_or_update_snapshot(team: Team, date: datetime.date | None = None) -> AnalyticsSnapshot:
    """Compute KPIs and persist (or refresh) the snapshot for *team* on *date*."""
    if date is None:
        date = timezone.localdate()

    defaults = {
        "total_shipments": get_total_shipments(team),
        "active_shipments": get_active_shipments(team),
        "completed_shipments": get_completed_shipments(team),
        "containers_in_transit": get_containers_in_transit(team),
        "containers_delivered": get_containers_delivered(team),
        "avg_transit_days": get_average_transit_days(team),
    }

    snapshot, _ = AnalyticsSnapshot.objects.update_or_create(
        team=team,
        date=date,
        defaults=defaults,
    )
    return snapshot
