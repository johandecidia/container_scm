# Base classes for carrier tracking integrations.
# Each carrier module must implement TrackingClient and TrackingParser.


class TrackingClient:
    """Knows how to fetch tracking data from an external carrier API."""

    def fetch_tracking(self, reference: str, reference_type: str) -> dict:
        """Fetch raw tracking data for a given reference.

        Returns the raw payload dict to be stored and parsed.
        Must be implemented by each carrier client.
        """
        raise NotImplementedError


class TrackingParser:
    """Knows how to parse a raw carrier payload into normalised tracking events."""

    def parse_events(self, payload: dict) -> list[dict]:
        """Parse a raw payload and return a list of normalised event dicts.

        Each dict should contain at minimum:
          - event_type (str): mapped to TrackingEvent.EventType
          - event_datetime (datetime | None)
          - description (str)
          - source_event_id (str, optional)
          - location_name (str, optional)
          - location_unlocode (str, optional)
          - event_code (str, optional)
          - raw_data (dict): original event data for archival

        Must be implemented by each carrier parser.
        """
        raise NotImplementedError
