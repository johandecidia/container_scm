from apps.scm.integrations.carriers.base import BaseCarrierClient


class CmaCgmClient(BaseCarrierClient):
    """CMA CGM tracking client (placeholder — full implementation pending API access)."""

    provider_code = "cma_cgm"

    def test_connection(self) -> dict:
        # TODO: implement CMA CGM API connection test
        raise NotImplementedError("CMA CGM API client not yet implemented")

    def fetch_tracking(
        self, *, container_number=None, bill_of_lading_number=None, booking_number=None, purchase_order_number=None
    ) -> dict:
        # TODO: implement CMA CGM Track & Trace API call
        raise NotImplementedError("CMA CGM API client not yet implemented")
