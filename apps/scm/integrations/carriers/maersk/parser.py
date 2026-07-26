"""Maersk payload parser.

Maersk publishes DCSA-conformant Track & Trace events, so the shared DcsaParser
does the work. This class exists to own any genuine Maersk deviation; it currently
adds only one — unwrapping an envelope — and deliberately not more. Inventing
carrier-specific handling before seeing real responses is how parsers acquire
branches that can never be tested.
"""

from __future__ import annotations

from apps.scm.integrations.carriers.base import BaseCarrierParser
from apps.scm.integrations.carriers.dcsa.parser import DcsaParser
from apps.scm.integrations.carriers.dcsa.schemas import NormalisedTrackingEvent
from apps.scm.integrations.carriers.exceptions import CarrierInvalidResponseError

PROVIDER_CODE = "maersk"


class MaerskParser(BaseCarrierParser):
    """Parses Maersk DCSA Track & Trace responses into normalised events."""

    provider_code = PROVIDER_CODE

    def __init__(self) -> None:
        self._dcsa = DcsaParser(source_provider=PROVIDER_CODE)

    def parse_tracking_events(self, raw_payload) -> list[NormalisedTrackingEvent]:
        """Return normalised events from a Maersk response.

        An empty list is a valid result — the response contained no events. A payload
        that is neither an object nor an array cannot be interpreted at all and is
        rejected, so the raw response stays marked unparsed instead of being silently
        treated as empty.
        """
        if raw_payload is None:
            return []
        if not isinstance(raw_payload, dict | list):
            raise CarrierInvalidResponseError(
                f"Maersk payload must be an object or array, got {type(raw_payload).__name__}.",
                provider_code=self.provider_code,
            )

        payload = raw_payload
        if isinstance(payload, dict) and "events" not in payload:
            # Some Maersk API products wrap the DCSA event list in an envelope.
            for envelope_key in ("data", "payload"):
                inner = payload.get(envelope_key)
                if isinstance(inner, dict | list):
                    payload = inner
                    break

        return self._dcsa.parse(payload)
