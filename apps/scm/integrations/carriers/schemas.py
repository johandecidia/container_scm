"""Normalised data schemas for carrier discovery responses.

All carrier integrations that support container discovery must return
a ContainerDiscoveryResult. This ensures consistent downstream processing
regardless of which carrier API was called.
"""

from __future__ import annotations

import datetime

from pydantic import BaseModel, Field, model_validator


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


class CarrierDiscoveryQuery(BaseModel):
    """Query parameters for a shipment-based carrier discovery request.

    At least one of booking_number, bl_number, or shipment_reference must be provided.
    """

    carrier_code: str | None = Field(default=None)
    booking_number: str | None = Field(default=None)
    bl_number: str | None = Field(default=None)
    shipment_reference: str | None = Field(default=None)

    @model_validator(mode="after")
    def require_at_least_one_reference(self) -> CarrierDiscoveryQuery:
        if not any([self.booking_number, self.bl_number, self.shipment_reference]):
            raise ValueError("At least one of booking_number, bl_number, or shipment_reference must be provided.")
        return self
