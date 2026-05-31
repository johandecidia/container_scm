from apps.scm.integrations.carriers.base import BaseCarrierParser


class EvergreenParser(BaseCarrierParser):
    """Evergreen payload parser (placeholder — API format not confirmed; defensive stub only)."""

    provider_code = "evergreen"

    def parse_tracking_events(self, raw_payload: dict) -> list[dict]:
        # TODO: confirm Evergreen API format before implementing
        raise NotImplementedError("Evergreen parser not yet implemented — API format unconfirmed")
