from apps.scm.integrations.business_systems.base import BaseBusinessSystemMapper

from .schemas import (
    NormalizedCustomerOrder,
    NormalizedPurchaseOrder,
)


class BusinessCentralMapper(BaseBusinessSystemMapper):
    """Maps raw Business Central OData responses to normalised DTO objects."""

    system_code = "business_central"

    def map_sales_order(self, raw: dict) -> NormalizedCustomerOrder:
        # TODO: implement full Business Central sales order mapping
        raise NotImplementedError("BusinessCentralMapper.map_sales_order not yet implemented")

    def map_purchase_order(self, raw: dict) -> NormalizedPurchaseOrder:
        # TODO: implement full Business Central purchase order mapping
        raise NotImplementedError("BusinessCentralMapper.map_purchase_order not yet implemented")
