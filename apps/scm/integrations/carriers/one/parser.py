from apps.scm.integrations.carriers.base import BaseCarrierParser


class OneParser(BaseCarrierParser):
    """ONE (Ocean Network Express) payload parser (placeholder — full implementation pending API access)."""

    provider_code = "one"

    def parse_tracking_events(self, raw_payload: dict) -> list[dict]:
        # TODO: parse ONE API response into normalised event dicts
        raise NotImplementedError("ONE parser not yet implemented")
