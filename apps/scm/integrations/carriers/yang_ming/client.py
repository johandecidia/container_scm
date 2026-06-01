from apps.scm.integrations.carriers.base import BaseCarrierClient


class YangMingClient(BaseCarrierClient):
    """Yang Ming tracking client (placeholder — API format not confirmed; defensive stub only)."""

    provider_code = "yang_ming"

    def test_connection(self) -> dict:
        # TODO: confirm Yang Ming API format before implementing
        raise NotImplementedError("Yang Ming API client not yet implemented — API format unconfirmed")

    def fetch_tracking(
        self, *, container_number=None, bill_of_lading_number=None, booking_number=None, purchase_order_number=None
    ) -> dict:
        # TODO: confirm Yang Ming API format before implementing
        raise NotImplementedError("Yang Ming API client not yet implemented — API format unconfirmed")
