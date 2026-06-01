"""Auto-link service for connecting discovered containers to existing shipments.

Confidence levels:
  HIGH   — exact carrier_booking_reference match → auto-link is safe.
  LOW    — partial or reference-only matches → do not auto-link, leave for manual review.

Only HIGH confidence matches are linked automatically.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum

from apps.scm.integrations.carriers.schemas import ContainerDiscoveryResult

logger = logging.getLogger(__name__)


class MatchConfidence(Enum):
    HIGH = "high"
    LOW = "low"
    NONE = "none"


@dataclass
class ShipmentMatchResult:
    confidence: MatchConfidence
    shipment_id: int | None = None
    matched_on: str | None = None


def auto_link_detected_container(result: ContainerDiscoveryResult) -> ShipmentMatchResult:
    """Attempt to find and link a detected container to an existing shipment.

    Strategy (ordered by confidence):
      1. Exact carrier_booking_reference match → HIGH confidence → auto-link.
      2. Exact bill_of_lading_number match → LOW confidence → no auto-link.
      3. Exact shipment_reference match → LOW confidence → no auto-link.

    Returns a ShipmentMatchResult describing the outcome. Callers should check
    the confidence level before acting; only HIGH results are auto-linked here.

    TODO: When the Shipment model gains a direct container FK or a
    ShipmentContainer through-model is available for linking, add the actual
    ORM write here (currently returns the match result only).
    """
    # Guard: nothing to match without at least one reference
    if not any([result.booking_number, result.bl_number, result.shipment_reference]):
        return ShipmentMatchResult(confidence=MatchConfidence.NONE)

    # Attempt HIGH-confidence match: exact booking_number
    if result.booking_number:
        match = _find_shipment_by_booking(result.booking_number)
        if match is not None:
            logger.info(
                "AUTO-LINK HIGH: container %s matched shipment %d via booking_number %s",
                result.container_number,
                match,
                result.booking_number,
            )
            # TODO: create ShipmentContainer link here once model supports it.
            return ShipmentMatchResult(
                confidence=MatchConfidence.HIGH,
                shipment_id=match,
                matched_on="booking_number",
            )

    # LOW-confidence fallbacks — log but do not auto-link
    if result.bl_number:
        match = _find_shipment_by_bl(result.bl_number)
        if match is not None:
            logger.info(
                "AUTO-LINK LOW: container %s — bl_number %s matches shipment %d, skipping auto-link",
                result.container_number,
                result.bl_number,
                match,
            )
            return ShipmentMatchResult(confidence=MatchConfidence.LOW, shipment_id=match, matched_on="bl_number")

    if result.shipment_reference:
        match = _find_shipment_by_reference(result.shipment_reference)
        if match is not None:
            logger.info(
                "AUTO-LINK LOW: container %s — shipment_reference %s matches shipment %d, skipping auto-link",
                result.container_number,
                result.shipment_reference,
                match,
            )
            return ShipmentMatchResult(
                confidence=MatchConfidence.LOW, shipment_id=match, matched_on="shipment_reference"
            )

    return ShipmentMatchResult(confidence=MatchConfidence.NONE)


# ---------------------------------------------------------------------------
# Internal match helpers — query the Shipment model
# ---------------------------------------------------------------------------


def _find_shipment_by_booking(booking_number: str) -> int | None:
    """Return the shipment PK that exactly matches carrier_booking_reference, or None."""
    from apps.scm.shipments.models import Shipment

    shipment = Shipment.objects.filter(carrier_booking_reference=booking_number).first()
    return shipment.pk if shipment else None


def _find_shipment_by_bl(bl_number: str) -> int | None:
    """Return the shipment PK that exactly matches bill_of_lading_number, or None."""
    from apps.scm.shipments.models import Shipment

    shipment = Shipment.objects.filter(bill_of_lading_number=bl_number).first()
    return shipment.pk if shipment else None


def _find_shipment_by_reference(reference: str) -> int | None:
    """Return the shipment PK that exactly matches the reference field, or None."""
    from apps.scm.shipments.models import Shipment

    shipment = Shipment.objects.filter(reference=reference).first()
    return shipment.pk if shipment else None
