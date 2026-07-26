"""Auto-link service for connecting discovered containers to shipments.

Creates Container, ShipmentContainer, and TrackingSubscription records
from a ContainerDiscoveryResult. All operations are idempotent.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from django.db import transaction

from .schemas import ContainerDiscoveryResult

if TYPE_CHECKING:
    from apps.scm.shipments.models import Shipment
    from apps.teams.models import Team

logger = logging.getLogger(__name__)


def create_or_link_discovered_container(
    *,
    team: Team,
    shipment: Shipment | None,
    result: ContainerDiscoveryResult,
) -> dict:
    """Create or retrieve a Container and link it to a shipment and tracking.

    Steps:
      1. Normalise container_number to uppercase and parse its ISO 6346 components.
      2. Get or create the Container (skips creation if the ID is invalid).
      3. Link it to the shipment, when one is known.
      4. Get or create a TrackingSubscription for the container.

    All writes happen in one transaction, so a discovered container can never end
    up without its tracking subscription (or the reverse). ``shipment`` may be None:
    a container discovered from a planned number is not always attached to a
    shipment yet, and guessing one risks the wrong assignment.

    Returns a summary dict: the container (or None) plus flags for what was created.
    """
    from django.core.exceptions import ValidationError

    from apps.scm.containers.utils import parse_container_id

    container_number = result.container_number.strip().upper()
    summary = {
        "container_number": container_number,
        "container": None,
        "container_created": False,
        "shipment_container_created": False,
        "subscription_created": False,
    }

    try:
        parts = parse_container_id(container_number)
    except (ValidationError, ValueError) as exc:
        logger.warning("Cannot parse container ID %r: %s — skipping.", container_number, exc)
        return summary

    with transaction.atomic():
        container = _get_or_create_container(team=team, parts=parts, result=result, summary=summary)
        if container is None:
            return summary
        summary["container"] = container

        if shipment is not None:
            _get_or_create_shipment_container(shipment=shipment, container=container, summary=summary)

        _get_or_create_tracking_subscription(
            team=team, shipment=shipment, container=container, result=result, summary=summary
        )

    return summary


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_or_create_container(*, team, parts: dict, result: ContainerDiscoveryResult, summary: dict):
    """Get or create a Container from parsed ISO 6346 parts."""
    from apps.scm.containers.models import Container, EquipmentType

    try:
        container, created = Container.objects.get_or_create(
            team=team,
            owner_code=parts["owner_code"],
            category_id=parts["category_id"],
            serial_number=parts["serial_number"],
            defaults={
                "check_digit": parts["check_digit"],
                "equipment_type": _get_default_equipment_type(),
            },
        )
    except EquipmentType.DoesNotExist:
        logger.warning(
            "No EquipmentType available — cannot create container %s.",
            result.container_number,
        )
        return None
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to get/create container %s: %s", result.container_number, exc)
        return None

    if created:
        summary["container_created"] = True
        logger.info("Created container %s (team=%s).", container.container_id, team.pk)
    else:
        logger.debug("Container %s already exists (team=%s).", container.container_id, team.pk)

    return container


def _get_or_create_shipment_container(*, shipment, container, summary: dict) -> None:
    """Create ShipmentContainer link if it does not already exist."""
    from apps.scm.shipments.models import ShipmentContainer

    _, created = ShipmentContainer.objects.get_or_create(
        shipment=shipment,
        container=container,
    )
    if created:
        summary["shipment_container_created"] = True
        logger.info(
            "Linked container %s to shipment %s.",
            container.container_id,
            shipment.pk,
        )


def _get_or_create_tracking_subscription(
    *, team, shipment, container, result: ContainerDiscoveryResult, summary: dict
) -> None:
    """Create a TrackingSubscription for the discovered container if one does not exist."""
    from apps.scm.tracking.models import TrackingSubscription

    provider = _get_or_create_tracking_provider(carrier_code=result.carrier_code, carrier_name=result.carrier_name)
    if provider is None:
        return

    _, created = TrackingSubscription.objects.get_or_create(
        team=team,
        provider=provider,
        container=container,
        reference_type=TrackingSubscription.ReferenceType.CONTAINER_NUMBER,
        defaults={
            "shipment": shipment,
            "tracking_reference": container.container_id,
            "status": TrackingSubscription.Status.ACTIVE,
        },
    )
    if created:
        summary["subscription_created"] = True
        logger.info(
            "Created TrackingSubscription for container %s / provider %s.",
            container.container_id,
            provider.code,
        )


def _get_or_create_tracking_provider(*, carrier_code: str, carrier_name: str):
    """Get or create a TrackingProvider for the given carrier code."""
    from apps.scm.tracking.models import TrackingProvider

    if not carrier_code:
        return None

    provider, _ = TrackingProvider.objects.get_or_create(
        code=carrier_code,
        defaults={
            "name": carrier_name or carrier_code,
            "provider_type": TrackingProvider.ProviderType.API,
        },
    )
    return provider


def _get_default_equipment_type():
    """Return a fallback EquipmentType for discovered containers with unknown type.

    Uses the first available EquipmentType ordered by ISO code.
    Raises EquipmentType.DoesNotExist if no equipment types are configured.
    """
    from apps.scm.containers.models import EquipmentType

    return EquipmentType.objects.order_by("iso_code").first()
