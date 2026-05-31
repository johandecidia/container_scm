# Normalised DTO schemas for John Evans International data.
# API format not yet confirmed — defensive placeholder.
from dataclasses import dataclass, field


@dataclass
class JohnEvansShipment:
    """Normalised shipment record from John Evans International (placeholder)."""

    external_id: str = ""
    reference: str = ""
    status: str = ""
    raw: dict = field(default_factory=dict)
