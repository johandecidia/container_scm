"""Business Central API client.

Supports a dummy mode for local development and testing, loading data from
JSON fixtures instead of hitting the live API.
"""

import json
from pathlib import Path

from apps.scm.integrations.business_systems.base import BaseBusinessSystemClient, BusinessSystemCapability

_DEFAULT_FIXTURES_PATH = Path(__file__).parent / "tests" / "fixtures"


class BusinessCentralClient(BaseBusinessSystemClient):
    """Microsoft Business Central API client.

    Pass ``use_dummy=True`` to load data from local JSON fixtures instead of
    the live API. Useful in tests and local development.

    Supports the following Business Central entity sets (via OData API v2.0):
      - salesOrders / salesOrderLines
      - purchaseOrders / purchaseOrderLines
      - customers / vendors / items / companies
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

    def __init__(self, use_dummy: bool = False, fixtures_path: str | None = None) -> None:
        self.use_dummy = use_dummy
        self.fixtures_path = Path(fixtures_path) if fixtures_path else _DEFAULT_FIXTURES_PATH

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def test_connection(self) -> dict:
        # TODO: implement Business Central OAuth2 + OData connection test
        raise NotImplementedError("Business Central client not yet implemented")

    def fetch_sales_orders(self, **kwargs) -> list[dict]:
        # TODO: GET /v2.0/{tenant_id}/{environment}/api/v2.0/companies({company_id})/salesOrders
        raise NotImplementedError("Business Central fetch_sales_orders not yet implemented")

    def fetch_purchase_orders(self, **kwargs) -> list[dict]:
        """Return all purchase orders.

        In dummy mode, reads from ``business_central_purchase_orders.json``.
        """
        if self.use_dummy:
            return self._load_fixture("business_central_purchase_orders.json")
        # TODO: GET /v2.0/{tenant_id}/{environment}/api/v2.0/companies({company_id})/purchaseOrders
        raise NotImplementedError("Business Central fetch_purchase_orders not yet implemented")

    def fetch_purchase_order_lines(self, purchase_order_id: str) -> list[dict]:
        """Return lines for a single purchase order, identified by PO number or GUID.

        In dummy mode, reads from
        ``business_central_purchase_order_lines_{purchase_order_id}.json``.
        """
        if self.use_dummy:
            return self._load_fixture(f"business_central_purchase_order_lines_{purchase_order_id}.json")
        # TODO: GET .../purchaseOrders({id})/purchaseOrderLines
        raise NotImplementedError("Business Central fetch_purchase_order_lines not yet implemented")

    def fetch_customers(self, **kwargs) -> list[dict]:
        raise NotImplementedError("Business Central fetch_customers not yet implemented")

    def fetch_vendors(self, **kwargs) -> list[dict]:
        raise NotImplementedError("Business Central fetch_vendors not yet implemented")

    def fetch_items(self, **kwargs) -> list[dict]:
        raise NotImplementedError("Business Central fetch_items not yet implemented")

    def fetch_companies(self, **kwargs) -> list[dict]:
        raise NotImplementedError("Business Central fetch_companies not yet implemented")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _load_fixture(self, filename: str) -> list[dict]:
        path = self.fixtures_path / filename
        with open(path) as f:
            data = json.load(f)
        return data.get("value", [])
