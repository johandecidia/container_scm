from apps.scm.integrations.carriers.base import BaseCarrierParser


class HapagLloydParser(BaseCarrierParser):
    """Hapag-Lloyd payload parser (placeholder — full implementation pending API access)."""

    provider_code = "hapag_lloyd"

    def parse_tracking_events(self, raw_payload: dict) -> list[dict]:
        # TODO: parse Hapag-Lloyd API response into normalised event dicts
        raise NotImplementedError("Hapag-Lloyd parser not yet implemented")
