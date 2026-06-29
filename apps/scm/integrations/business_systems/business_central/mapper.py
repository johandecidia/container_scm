"""Maps raw Business Central OData responses to normalised DTO objects."""

from datetime import date
from decimal import Decimal

from apps.scm.integrations.business_systems.base import BaseBusinessSystemMapper

from .schemas import (
    NormalizedCustomerOrder,
    NormalizedPurchaseOrder,
    NormalizedPurchaseOrderLine,
)

_STATUS_MAP = {
    "open": "open",
    "released": "released",
    "partially received": "partially_received",
    "fully received": "fully_received",
    "closed": "closed",
}


def _normalize_status(raw_status: str) -> str:
    return _STATUS_MAP.get(raw_status.lower(), "open")


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError, TypeError:
        return None


def _map_purchase_order_line(raw_line: dict) -> NormalizedPurchaseOrderLine:
    return NormalizedPurchaseOrderLine(
        external_id=raw_line["id"],
        line_no=str(raw_line.get("sequence", "")),
        item_no=raw_line.get("itemNumber", ""),
        description=raw_line.get("description", ""),
        ordered_qty=Decimal(str(raw_line.get("quantity", 0))),
        unit_price=Decimal(str(raw_line["directUnitCost"])) if raw_line.get("directUnitCost") is not None else None,
        expected_receipt_date=_parse_date(raw_line.get("expectedReceiptDate")),
        raw_payload=raw_line,
    )


class BusinessCentralMapper(BaseBusinessSystemMapper):
    """Maps raw Business Central OData responses to normalised DTO objects."""

    system_code = "business_central"

    def map_sales_order(self, raw: dict) -> NormalizedCustomerOrder:
        # TODO: implement full Business Central sales order mapping
        raise NotImplementedError("BusinessCentralMapper.map_sales_order not yet implemented")

    def map_purchase_order(self, raw: dict, lines: list[dict] | None = None) -> NormalizedPurchaseOrder:
        """Map a raw BC purchase order (and its lines) to a NormalizedPurchaseOrder."""
        mapped_lines = [_map_purchase_order_line(line) for line in (lines or [])]
        return NormalizedPurchaseOrder(
            external_id=raw["id"],
            po_number=raw.get("number", ""),
            supplier_no=raw.get("vendorNumber", ""),
            supplier_name=raw.get("vendorName", ""),
            status=_normalize_status(raw.get("status", "")),
            order_date=_parse_date(raw.get("orderDate")),
            expected_receipt_date=_parse_date(raw.get("expectedReceiptDate")),
            currency=raw.get("currencyCode", "EUR"),
            raw_payload=raw,
            lines=mapped_lines,
        )
