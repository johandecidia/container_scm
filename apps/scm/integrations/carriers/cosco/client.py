from apps.scm.integrations.carriers.base import BaseCarrierClient


class CoscoClient(BaseCarrierClient):
    """COSCO tracking client (placeholder — full implementation pending API access)."""

    provider_code = "cosco"

    def test_connection(self) -> dict:
        # TODO: implement COSCO API connection test
        raise NotImplementedError("COSCO API client not yet implemented")

    def fetch_tracking(
        self, *, container_number=None, bill_of_lading_number=None, booking_number=None, purchase_order_number=None
    ) -> dict:
        # TODO: implement COSCO Track & Trace API call
        raise NotImplementedError("COSCO API client not yet implemented")
