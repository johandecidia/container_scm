from apps.scm.integrations.carriers.base import BaseCarrierClient


class MaerskClient(BaseCarrierClient):
    """Maersk tracking API client (placeholder — full implementation pending API access)."""

    provider_code = "maersk"

    def test_connection(self) -> dict:
        # TODO: implement Maersk API connection test
        raise NotImplementedError("Maersk API client not yet implemented")

    def fetch_tracking(
        self, *, container_number=None, bill_of_lading_number=None, booking_number=None, purchase_order_number=None
    ) -> dict:
        # TODO: implement Maersk Track & Trace API call
        raise NotImplementedError("Maersk API client not yet implemented")
