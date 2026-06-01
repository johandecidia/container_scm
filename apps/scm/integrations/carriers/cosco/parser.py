from apps.scm.integrations.carriers.base import BaseCarrierParser


class CoscoParser(BaseCarrierParser):
    """COSCO payload parser (placeholder — full implementation pending API access)."""

    provider_code = "cosco"

    def parse_tracking_events(self, raw_payload: dict) -> list[dict]:
        # TODO: parse COSCO API response into normalised event dicts
        raise NotImplementedError("COSCO parser not yet implemented")
