from apps.scm.integrations.carriers.base import BaseCarrierParser


class CmaCgmParser(BaseCarrierParser):
    """CMA CGM payload parser (placeholder — full implementation pending API access)."""

    provider_code = "cma_cgm"

    def parse_tracking_events(self, raw_payload: dict) -> list[dict]:
        # TODO: parse CMA CGM API response into normalised event dicts
        raise NotImplementedError("CMA CGM parser not yet implemented")
