"""Write operations and business logic for container pool management.

Handles creating, querying, and transitioning ContainerPool entries.
"""

from __future__ import annotations

import logging

from django.core.exceptions import ValidationError

from apps.teams.models import Team

from .models import ContainerPool, ContainerPoolStatus

logger = logging.getLogger(__name__)


def create_planned_container(team: Team, container_number: str, notes: str = "") -> ContainerPool:
    """Create a new planned container entry for the team.

    Raises ValidationError if container_number already exists for the team.
    """
    container_number = container_number.strip().upper()
    if ContainerPool.objects.filter(team=team, container_number=container_number).exists():
        raise ValidationError(f"Container {container_number} already exists in the pool for this team.")
    entry = ContainerPool.objects.create(
        team=team,
        container_number=container_number,
        status=ContainerPoolStatus.PLANNED,
        notes=notes,
    )
    logger.info("Created planned container %s for team %s", container_number, team.slug)
    return entry


def mark_container_detected(container_pool: ContainerPool) -> ContainerPool:
    """Transition a PLANNED container to DETECTED.

    Only PLANNED containers may be marked detected.
    """
    if container_pool.status != ContainerPoolStatus.PLANNED:
        logger.warning(
            "Cannot mark container %s as detected — current status: %s",
            container_pool.container_number,
            container_pool.status,
        )
        return container_pool
    container_pool.status = ContainerPoolStatus.DETECTED
    container_pool.save(update_fields=["status", "updated_at"])
    logger.info("Container %s marked as DETECTED", container_pool.container_number)
    return container_pool


def retire_planned_container(container_pool: ContainerPool) -> ContainerPool:
    """Mark a container pool entry as RETIRED (no longer active)."""
    container_pool.status = ContainerPoolStatus.RETIRED
    container_pool.save(update_fields=["status", "updated_at"])
    logger.info("Container %s marked as RETIRED", container_pool.container_number)
    return container_pool
