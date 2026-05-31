from apps.scm.integrations.carriers.base import BaseCarrierParser


class MscParser(BaseCarrierParser):
    """MSC payload parser (placeholder — full implementation pending API access)."""

    provider_code = "msc"

    def parse_tracking_events(self, raw_payload: dict) -> list[dict]:
        # TODO: parse MSC API response into normalised event dicts
        raise NotImplementedError("MSC parser not yet implemented")
