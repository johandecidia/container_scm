from apps.scm.integrations.carriers.base import BaseCarrierClient


class ZimClient(BaseCarrierClient):
    """ZIM tracking client (placeholder — API format not confirmed; defensive stub only)."""

    provider_code = "zim"

    def test_connection(self) -> dict:
        # TODO: confirm ZIM API format before implementing
        raise NotImplementedError("ZIM API client not yet implemented — API format unconfirmed")

    def fetch_tracking(
        self, *, container_number=None, bill_of_lading_number=None, booking_number=None, purchase_order_number=None
    ) -> dict:
        # TODO: confirm ZIM API format before implementing
        raise NotImplementedError("ZIM API client not yet implemented — API format unconfirmed")
