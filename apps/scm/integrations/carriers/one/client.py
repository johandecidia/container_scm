from apps.scm.integrations.carriers.base import BaseCarrierClient


class OneClient(BaseCarrierClient):
    """ONE (Ocean Network Express) tracking client (placeholder — full implementation pending API access)."""

    provider_code = "one"

    def test_connection(self) -> dict:
        # TODO: implement ONE API connection test
        raise NotImplementedError("ONE API client not yet implemented")

    def fetch_tracking(
        self, *, container_number=None, bill_of_lading_number=None, booking_number=None, purchase_order_number=None
    ) -> dict:
        # TODO: implement ONE Track & Trace API call
        raise NotImplementedError("ONE API client not yet implemented")
