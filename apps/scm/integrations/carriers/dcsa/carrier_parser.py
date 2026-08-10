"""Base parser for DCSA-conformant carriers.

DCSA carriers publish the same event shape, so the shared DcsaParser does the work
and a carrier subclass exists only to name itself and to hold genuine deviations.

Deliberately thin: adding carrier-specific handling before seeing that carrier's
real responses produces branches that can never be tested and quietly diverge from
what the carrier actually sends.
"""

from __future__ import annotations

from apps.scm.integrations.carriers.base import BaseCarrierParser
from apps.scm.integrations.carriers.exceptions import CarrierInvalidResponseError

from .parser import DcsaParser
from .schemas import NormalisedTrackingEvent

# Keys some carriers wrap the DCSA event list in.
_ENVELOPE_KEYS = ("data", "payload")


class DcsaCarrierParser(BaseCarrierParser):
    """Parses a DCSA carrier's Track & Trace response into normalised events."""

    def __init__(self) -> None:
        self._dcsa = DcsaParser(source_provider=self.provider_code)

    def parse_tracking_events(self, raw_payload) -> list[NormalisedTrackingEvent]:
        """Return normalised events from a DCSA response.

        An empty list is a valid result — the response contained no events. A payload
        that is neither an object nor an array cannot be interpreted at all and is
        rejected, so the stored response stays marked unparsed instead of being
        silently treated as empty.
        """
        if raw_payload is None:
            return []
        if not isinstance(raw_payload, dict | list):
            raise CarrierInvalidResponseError(
                f"{self.provider_code} payload must be an object or array, got {type(raw_payload).__name__}.",
                provider_code=self.provider_code,
            )

        payload = raw_payload
        if isinstance(payload, dict) and "events" not in payload:
            for envelope_key in _ENVELOPE_KEYS:
                inner = payload.get(envelope_key)
                if isinstance(inner, dict | list):
                    payload = inner
                    break

        return self._dcsa.parse(payload)
