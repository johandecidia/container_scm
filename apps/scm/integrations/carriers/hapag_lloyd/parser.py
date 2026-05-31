from apps.scm.integrations.carriers.base import TrackingParser


class HapagLloydTrackingParser(TrackingParser):
    """Hapag-Lloyd payload parser (placeholder — full implementation pending API access)."""

    def parse_events(self, payload: dict) -> list[dict]:
        # TODO: parse Hapag-Lloyd API response into normalised event dicts
        return []
