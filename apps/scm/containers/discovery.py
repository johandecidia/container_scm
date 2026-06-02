"""Container discovery service for planned container numbers.

Planned containers have a known container number (e.g. MCUU1000001) but may
not yet be confirmed at the carrier. This service polls carrier integrations
and transitions planned containers through:

    planned → detected → in_transit → arrived

It is intentionally separate from the shipment-based discovery in
apps/scm/integrations/carriers/discovery_service.py.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from django.db.models import QuerySet
from django.utils import timezone

from apps.teams.models import Team

from .models import PlannedContainer, PlannedContainerStatus

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Selectors
# ---------------------------------------------------------------------------


def get_planned_containers(team: Team, status: str | None = None) -> QuerySet[PlannedContainer]:
    """Return planned containers for a team, optionally filtered by status."""
    qs = PlannedContainer.objects.filter(team=team).select_related("shipment", "container")
    if status:
        qs = qs.filter(status=status)
    return qs.order_by("-created_at")


def get_planned_containers_for_discovery(team: Team) -> QuerySet[PlannedContainer]:
    """Return containers eligible for the next discovery run (status=PLANNED)."""
    return PlannedContainer.objects.filter(team=team, status=PlannedContainerStatus.PLANNED).order_by("created_at")


# ---------------------------------------------------------------------------
# Services
# ---------------------------------------------------------------------------


def add_planned_container(
    team: Team,
    container_number: str,
    carrier: str = "",
    shipment=None,
    notes: str = "",
) -> PlannedContainer:
    """Add a container number to the planned pool for a team.

    The container number is stored as-is (uppercased). If it already exists for
    the team, the existing record is returned without modification.
    """
    planned, created = PlannedContainer.objects.get_or_create(
        team=team,
        container_number=container_number.upper().strip(),
        defaults={
            "carrier": carrier,
            "shipment": shipment,
            "notes": notes,
        },
    )
    if created:
        logger.info("Added planned container %s for team %s", container_number, team.slug)
    return planned


def mark_planned_container_detected(
    planned: PlannedContainer,
    container=None,
) -> PlannedContainer:
    """Transition a planned container from PLANNED → DETECTED.

    Optionally links to a verified Container record.
    """
    planned.status = PlannedContainerStatus.DETECTED
    planned.detected_at = timezone.now()
    planned.last_checked_at = timezone.now()
    if container is not None:
        planned.container = container
    planned.save(update_fields=["status", "detected_at", "last_checked_at", "container", "updated_at"])
    return planned


def mark_planned_container_in_transit(planned: PlannedContainer) -> PlannedContainer:
    """Transition DETECTED → IN_TRANSIT when tracking shows movement."""
    planned.status = PlannedContainerStatus.IN_TRANSIT
    planned.last_checked_at = timezone.now()
    planned.save(update_fields=["status", "last_checked_at", "updated_at"])
    return planned


def mark_planned_container_arrived(planned: PlannedContainer) -> PlannedContainer:
    """Transition IN_TRANSIT → ARRIVED when arrival event is confirmed."""
    planned.status = PlannedContainerStatus.ARRIVED
    planned.last_checked_at = timezone.now()
    planned.save(update_fields=["status", "last_checked_at", "updated_at"])
    return planned


def cancel_planned_container(planned: PlannedContainer) -> PlannedContainer:
    """Cancel a planned container (terminal state)."""
    planned.status = PlannedContainerStatus.CANCELLED
    planned.save(update_fields=["status", "updated_at"])
    return planned


# ---------------------------------------------------------------------------
# Discovery run
# ---------------------------------------------------------------------------


def run_discovery_for_team(team: Team, providers: list | None = None) -> dict:
    """Run one discovery pass for all PLANNED containers of a team.

    For each planned container, queries carrier providers (if any) to check
    whether the container number is now known. Falls back gracefully when no
    providers are configured.

    Args:
        team: The team to run discovery for.
        providers: Optional list of carrier client instances. Defaults to empty
                   list (providers will be added as carriers are implemented).

    Returns:
        Summary dict: detected, checked, errors.
    """
    if providers is None:
        providers = []

    planned_qs = get_planned_containers_for_discovery(team=team)
    checked = 0
    detected = 0
    errors: list[str] = []

    for planned in planned_qs:
        checked += 1
        try:
            result = _check_single_container(planned=planned, providers=providers)
            if result:
                detected += 1
        except Exception as exc:  # noqa: BLE001
            msg = f"{planned.container_number}: {exc}"
            logger.exception("Discovery error for %s — %s", planned.container_number, msg)
            errors.append(msg)

        # Always update last_checked_at
        PlannedContainer.objects.filter(pk=planned.pk).update(last_checked_at=timezone.now())

    return {"checked": checked, "detected": detected, "errors": errors}


def _check_single_container(planned: PlannedContainer, providers: list) -> bool:
    """Ask each provider if the container number exists.

    Returns True if the container was detected by any provider.
    Providers must implement: check_container_exists(container_number) -> bool.
    """
    for provider in providers:
        try:
            if provider.check_container_exists(planned.container_number):
                mark_planned_container_detected(planned=planned)
                _try_auto_link_to_shipment(planned=planned)
                return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("Provider %s error for %s: %s", provider, planned.container_number, exc)

    # No providers or none found — not detected yet
    return False


def _try_auto_link_to_shipment(planned: PlannedContainer) -> None:
    """If the planned container already has a shipment FK, no action is needed.

    If no shipment is set, we leave it unlinked (safer than guessing).
    Auto-linking to a new shipment based on ambiguous data risks incorrect assignment.
    """
    if planned.shipment_id:
        logger.debug(
            "Planned container %s already linked to shipment %s", planned.container_number, planned.shipment_id
        )
