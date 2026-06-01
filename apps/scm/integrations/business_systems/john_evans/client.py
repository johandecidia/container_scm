from apps.scm.integrations.business_systems.base import BaseBusinessSystemClient, BusinessSystemCapability


class JohnEvansClient(BaseBusinessSystemClient):
    """John Evans International API client (defensive placeholder — API format unconfirmed).

    This client will be implemented once the John Evans API specification is confirmed.
    """

    system_code = "john_evans"
    capabilities = BusinessSystemCapability(
        supports_polling=True,
    )

    def test_connection(self) -> dict:
        # TODO: confirm John Evans API format before implementing
        raise NotImplementedError("John Evans client not yet implemented — API format unconfirmed")

    def fetch_sales_orders(self, **kwargs) -> list[dict]:
        # TODO: confirm John Evans API format before implementing
        raise NotImplementedError("John Evans fetch_sales_orders not yet implemented — API format unconfirmed")

    def fetch_purchase_orders(self, **kwargs) -> list[dict]:
        # TODO: confirm John Evans API format before implementing
        raise NotImplementedError("John Evans fetch_purchase_orders not yet implemented — API format unconfirmed")
