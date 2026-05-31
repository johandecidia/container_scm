from apps.scm.integrations.carriers.base import BaseCarrierClient


class HmmClient(BaseCarrierClient):
    """HMM (Hyundai Merchant Marine) tracking client (placeholder — full implementation pending API access)."""

    provider_code = "hmm"

    def test_connection(self) -> dict:
        # TODO: implement HMM API connection test
        raise NotImplementedError("HMM API client not yet implemented")

    def fetch_tracking(
        self, *, container_number=None, bill_of_lading_number=None, booking_number=None, purchase_order_number=None
    ) -> dict:
        # TODO: implement HMM Track & Trace API call
        raise NotImplementedError("HMM API client not yet implemented")
