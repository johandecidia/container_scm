"""Normalised data schemas for carrier discovery responses.

All carrier integrations that support container discovery must return
a ContainerDiscoveryResult. This ensures consistent downstream processing
regardless of which carrier API was called.
"""

from __future__ import annotations

import datetime

from pydantic import BaseModel, Field


class ContainerDiscoveryResult(BaseModel):
    """Normalised result from a carrier container discovery query."""

    container_number: str
    carrier_code: str
    carrier_name: str

    booking_number: str | None = Field(default=None)
    bl_number: str | None = Field(default=None)
    shipment_reference: str | None = Field(default=None)

    current_status: str | None = Field(default=None)
    first_seen_at: datetime.datetime | None = Field(default=None)
    last_seen_at: datetime.datetime | None = Field(default=None)

    raw_payload: dict = Field(default_factory=dict)
