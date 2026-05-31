# Base classes for business system integrations.
from dataclasses import dataclass, field


@dataclass
class BusinessSystemCapability:
    """Describes what a business system integration supports."""

    supports_sales_orders: bool = False
    supports_purchase_orders: bool = False
    supports_customers: bool = False
    supports_vendors: bool = False
    supports_items: bool = False
    supports_webhooks: bool = False
    supports_polling: bool = False


class BaseBusinessSystemClient:
    """Base class for all business system API clients."""

    system_code: str = ""
    capabilities: BusinessSystemCapability = field(default_factory=BusinessSystemCapability)

    def test_connection(self) -> dict:
        """Verify connectivity and credentials.

        Must return {"success": bool, "message": str}.
        Raise an explicit exception on failure — never fail silently.
        """
        raise NotImplementedError(f"{self.__class__.__name__} must implement test_connection()")

    def fetch_sales_orders(self, **kwargs) -> list[dict]:
        raise NotImplementedError(f"{self.__class__.__name__} must implement fetch_sales_orders()")

    def fetch_purchase_orders(self, **kwargs) -> list[dict]:
        raise NotImplementedError(f"{self.__class__.__name__} must implement fetch_purchase_orders()")


class BaseBusinessSystemMapper:
    """Base class for business system data mappers."""

    system_code: str = ""

    def map_sales_order(self, raw: dict) -> dict:
        raise NotImplementedError(f"{self.__class__.__name__} must implement map_sales_order()")

    def map_purchase_order(self, raw: dict) -> dict:
        raise NotImplementedError(f"{self.__class__.__name__} must implement map_purchase_order()")
