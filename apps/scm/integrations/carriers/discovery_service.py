"""Shipment-based carrier discovery service.

Discovers containers for a shipment by querying carrier APIs via
booking reference, bill of lading number, or shipment reference.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .schemas import ContainerDiscoveryResult

if TYPE_CHECKING:
    from apps.scm.shipments.models import Shipment

logger = logging.getLogger(__name__)


def discover_containers_for_shipment(
    shipment: Shipment,
    providers: list | None = None,
) -> dict:
    """Discover containers for a shipment via carrier API providers.

    Queries all providers that support discovery using shipment's
    carrier_booking_reference, bill_of_lading_number, and reference fields.

    Args:
        shipment: The Shipment instance to discover containers for.
        providers: Optional list of BaseCarrierClient instances to query.
                   Defaults to all registered providers that support discovery.

    Returns:
        Summary dict with keys:
          - discovered_count (int)
          - containers_created (int)
          - containers_linked (int)
          - subscriptions_created (int)
          - errors (list[str])
          - skipped (bool) — True if shipment had no references to query
    """
    from .auto_link import create_or_link_discovered_container

    booking_number = shipment.carrier_booking_reference or None
    bl_number = shipment.bill_of_lading_number or None
    shipment_reference = shipment.reference or None

    if not any([booking_number, bl_number, shipment_reference]):
        logger.debug("Shipment %s has no discovery references — skipping.", shipment.pk)
        return {
            "discovered_count": 0,
            "containers_created": 0,
            "containers_linked": 0,
            "subscriptions_created": 0,
            "errors": [],
            "skipped": True,
        }

    if providers is None:
        providers = _get_discovery_providers()

    all_results: list[ContainerDiscoveryResult] = []
    errors: list[str] = []

    for provider in providers:
        try:
            results = provider.discover_containers(
                booking_number=booking_number,
                bill_of_lading_number=bl_number,
                shipment_reference=shipment_reference,
            )
            all_results.extend(results)
        except Exception as exc:  # noqa: BLE001
            msg = f"{provider.__class__.__name__}: {exc}"
            logger.exception("Discovery error for shipment %s — %s", shipment.pk, msg)
            errors.append(msg)

    summary = {
        "discovered_count": len(all_results),
        "containers_created": 0,
        "containers_linked": 0,
        "subscriptions_created": 0,
        "errors": errors,
        "skipped": False,
    }

    for result in all_results:
        try:
            link_summary = create_or_link_discovered_container(
                team=shipment.team,
                shipment=shipment,
                result=result,
            )
            summary["containers_created"] += link_summary.get("container_created", 0)
            summary["containers_linked"] += link_summary.get("shipment_container_created", 0)
            summary["subscriptions_created"] += link_summary.get("subscription_created", 0)
        except Exception as exc:  # noqa: BLE001
            msg = f"Link failed for {result.container_number}: {exc}"
            logger.exception("Auto-link error for shipment %s — %s", shipment.pk, msg)
            errors.append(msg)

    return summary


def _get_discovery_providers() -> list:
    """Return all registered carrier clients that support discovery.

    Currently returns an empty list since carrier clients are not yet implemented.
    Real implementations should override discover_containers() in BaseCarrierClient.
    """
    return []
