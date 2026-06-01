from apps.scm.integrations.carriers.base import BaseCarrierClient


class HapagLloydClient(BaseCarrierClient):
    """Hapag-Lloyd tracking client (placeholder — full implementation pending API access)."""

    provider_code = "hapag_lloyd"

    def test_connection(self) -> dict:
        # TODO: implement Hapag-Lloyd API connection test
        raise NotImplementedError("Hapag-Lloyd API client not yet implemented")

    def fetch_tracking(
        self, *, container_number=None, bill_of_lading_number=None, booking_number=None, purchase_order_number=None
    ) -> dict:
        # TODO: implement Hapag-Lloyd Track & Trace API call
        raise NotImplementedError("Hapag-Lloyd API client not yet implemented")
