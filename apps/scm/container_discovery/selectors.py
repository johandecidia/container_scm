"""Read-only queries for container discovery. All queries are team-scoped."""

from __future__ import annotations

from django.db.models import QuerySet

from apps.teams.models import Team

from .models import ContainerPool, ContainerPoolStatus


def get_planned_containers(team: Team) -> QuerySet[ContainerPool]:
    """Return all PLANNED containers for the team, newest first."""
    return ContainerPool.objects.filter(team=team, status=ContainerPoolStatus.PLANNED).order_by("-created_at")


def get_detected_containers(team: Team) -> QuerySet[ContainerPool]:
    """Return all DETECTED containers for the team."""
    return ContainerPool.objects.filter(team=team, status=ContainerPoolStatus.DETECTED).order_by("-updated_at")


def get_all_pool_entries(team: Team) -> QuerySet[ContainerPool]:
    """Return all pool entries for the team regardless of status."""
    return ContainerPool.objects.filter(team=team).order_by("-created_at")


def get_container_discovery_dashboard(team: Team) -> dict:
    """Return KPI counts for the container discovery dashboard.

    Returns:
        planned_count: containers with status PLANNED.
        detected_count: containers with status DETECTED.
        in_transit_count: shipments linked to detected containers that are IN_TRANSIT
                          (currently 0 until shipment-linking is fully implemented).
        arrived_count: shipments linked to detected containers that are ARRIVED
                       (currently 0 until shipment-linking is fully implemented).
    """
    pool = ContainerPool.objects.filter(team=team)
    planned_count = pool.filter(status=ContainerPoolStatus.PLANNED).count()
    detected_count = pool.filter(status=ContainerPoolStatus.DETECTED).count()

    # TODO: derive in_transit and arrived from linked Shipment status once
    # ShipmentContainer linking is implemented in auto_link.py.
    in_transit_count = 0
    arrived_count = 0

    return {
        "planned_count": planned_count,
        "detected_count": detected_count,
        "in_transit_count": in_transit_count,
        "arrived_count": arrived_count,
    }
