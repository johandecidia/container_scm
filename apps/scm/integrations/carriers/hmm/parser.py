from apps.scm.integrations.carriers.base import BaseCarrierParser


class HmmParser(BaseCarrierParser):
    """HMM payload parser (placeholder — full implementation pending API access)."""

    provider_code = "hmm"

    def parse_tracking_events(self, raw_payload: dict) -> list[dict]:
        # TODO: parse HMM API response into normalised event dicts
        raise NotImplementedError("HMM parser not yet implemented")
