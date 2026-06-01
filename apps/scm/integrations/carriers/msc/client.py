from apps.scm.integrations.carriers.base import BaseCarrierClient


class MscClient(BaseCarrierClient):
    """MSC tracking client (placeholder — full implementation pending API access)."""

    provider_code = "msc"

    def test_connection(self) -> dict:
        # TODO: implement MSC API connection test
        raise NotImplementedError("MSC API client not yet implemented")

    def fetch_tracking(
        self, *, container_number=None, bill_of_lading_number=None, booking_number=None, purchase_order_number=None
    ) -> dict:
        # TODO: implement MSC Track & Trace API call
        raise NotImplementedError("MSC API client not yet implemented")
