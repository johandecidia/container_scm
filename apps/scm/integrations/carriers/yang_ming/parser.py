from apps.scm.integrations.carriers.base import BaseCarrierParser


class YangMingParser(BaseCarrierParser):
    """Yang Ming payload parser (placeholder — API format not confirmed; defensive stub only)."""

    provider_code = "yang_ming"

    def parse_tracking_events(self, raw_payload: dict) -> list[dict]:
        # TODO: confirm Yang Ming API format before implementing
        raise NotImplementedError("Yang Ming parser not yet implemented — API format unconfirmed")
