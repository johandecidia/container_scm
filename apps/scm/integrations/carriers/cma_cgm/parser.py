from apps.scm.integrations.carriers.base import TrackingParser


class CmaCgmTrackingParser(TrackingParser):
    """CMA CGM payload parser (placeholder — full implementation pending API access)."""

    def parse_events(self, payload: dict) -> list[dict]:
        # TODO: parse CMA CGM API response into normalised event dicts
        return []
