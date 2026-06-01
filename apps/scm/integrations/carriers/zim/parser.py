from apps.scm.integrations.carriers.base import BaseCarrierParser


class ZimParser(BaseCarrierParser):
    """ZIM payload parser (placeholder — API format not confirmed; defensive stub only)."""

    provider_code = "zim"

    def parse_tracking_events(self, raw_payload: dict) -> list[dict]:
        # TODO: confirm ZIM API format before implementing
        raise NotImplementedError("ZIM parser not yet implemented — API format unconfirmed")
