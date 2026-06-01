from apps.scm.integrations.business_systems.base import BaseBusinessSystemClient, BusinessSystemCapability


class BusinessCentralClient(BaseBusinessSystemClient):
    """Microsoft Business Central API client (placeholder — full implementation pending).

    Supports the following Business Central entity sets (via OData API):
      - salesOrders / salesOrderLines
      - purchaseOrders / purchaseOrderLines
      - customers
      - vendors
      - items
      - companies
    """

    system_code = "business_central"
    capabilities = BusinessSystemCapability(
        supports_sales_orders=True,
        supports_purchase_orders=True,
        supports_customers=True,
        supports_vendors=True,
        supports_items=True,
        supports_webhooks=True,
        supports_polling=True,
    )

    def test_connection(self) -> dict:
        # TODO: implement Business Central OAuth2 + OData connection test
        raise NotImplementedError("Business Central client not yet implemented")

    def fetch_sales_orders(self, **kwargs) -> list[dict]:
        # TODO: GET /v2.0/{tenant_id}/{environment}/api/v2.0/companies({company_id})/salesOrders
        raise NotImplementedError("Business Central fetch_sales_orders not yet implemented")

    def fetch_purchase_orders(self, **kwargs) -> list[dict]:
        # TODO: GET /v2.0/{tenant_id}/{environment}/api/v2.0/companies({company_id})/purchaseOrders
        raise NotImplementedError("Business Central fetch_purchase_orders not yet implemented")

    def fetch_customers(self, **kwargs) -> list[dict]:
        # TODO: GET .../customers
        raise NotImplementedError("Business Central fetch_customers not yet implemented")

    def fetch_vendors(self, **kwargs) -> list[dict]:
        # TODO: GET .../vendors
        raise NotImplementedError("Business Central fetch_vendors not yet implemented")

    def fetch_items(self, **kwargs) -> list[dict]:
        # TODO: GET .../items
        raise NotImplementedError("Business Central fetch_items not yet implemented")

    def fetch_companies(self, **kwargs) -> list[dict]:
        # TODO: GET .../companies
        raise NotImplementedError("Business Central fetch_companies not yet implemented")
