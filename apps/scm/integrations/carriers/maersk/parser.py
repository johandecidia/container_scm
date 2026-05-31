from apps.scm.integrations.carriers.base import TrackingParser


class MaerskTrackingParser(TrackingParser):
    """Maersk payload parser (placeholder — full implementation pending API access)."""

    def parse_events(self, payload: dict) -> list[dict]:
        # TODO: parse Maersk API response into normalised event dicts
        return []
