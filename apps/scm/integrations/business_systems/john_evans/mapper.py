from apps.scm.integrations.business_systems.base import BaseBusinessSystemMapper


class JohnEvansMapper(BaseBusinessSystemMapper):
    """Maps raw John Evans API responses to normalised DTOs (placeholder — API format unconfirmed)."""

    system_code = "john_evans"

    def map_sales_order(self, raw: dict) -> dict:
        # TODO: confirm John Evans API format before implementing
        raise NotImplementedError("JohnEvansMapper.map_sales_order not yet implemented — API format unconfirmed")

    def map_purchase_order(self, raw: dict) -> dict:
        # TODO: confirm John Evans API format before implementing
        raise NotImplementedError("JohnEvansMapper.map_purchase_order not yet implemented — API format unconfirmed")
