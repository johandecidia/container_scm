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
