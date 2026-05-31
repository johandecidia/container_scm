from apps.scm.integrations.carriers.base import BaseCarrierParser


class MaerskParser(BaseCarrierParser):
    """Maersk payload parser (placeholder — DCSA-style; full implementation pending API access)."""

    provider_code = "maersk"

    def parse_tracking_events(self, raw_payload: dict) -> list[dict]:
        # TODO: parse Maersk DCSA-style API response into normalised event dicts
        raise NotImplementedError("Maersk parser not yet implemented")
