from apps.scm.integrations.carriers.base import BaseCarrierClient


class EvergreenClient(BaseCarrierClient):
    """Evergreen tracking client (placeholder — API format not confirmed; defensive stub only)."""

    provider_code = "evergreen"

    def test_connection(self) -> dict:
        # TODO: confirm Evergreen API format before implementing
        raise NotImplementedError("Evergreen API client not yet implemented — API format unconfirmed")

    def fetch_tracking(
        self, *, container_number=None, bill_of_lading_number=None, booking_number=None, purchase_order_number=None
    ) -> dict:
        # TODO: confirm Evergreen API format before implementing
        raise NotImplementedError("Evergreen API client not yet implemented — API format unconfirmed")
