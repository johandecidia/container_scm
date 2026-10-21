"""Maps raw Business Central OData responses to normalised DTO objects.

Field names follow the standard API v2.0 ``purchaseOrder`` / ``purchaseOrderLine``
resources. Older fixture-style names (``itemNumber``, a header
``expectedReceiptDate``) are still accepted as fallbacks.
"""

import logging
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation

from apps.scm.integrations.business_systems.base import BaseBusinessSystemMapper

from .schemas import (
    NormalizedCustomerOrder,
    NormalizedPurchaseOrder,
    NormalizedPurchaseOrderLine,
)

logger = logging.getLogger(__name__)

# The API's status enum does not use the Business Central UI's words: API "Draft"
# is a UI "Open" document, "In Review" is "Pending Approval", and API "Open" is a
# *Released* document. The UI-style keys are kept for document imports/fixtures.
_STATUS_MAP = {
    "draft": "open",
    "in review": "open",
    "open": "released",
    "released": "released",
    "partially received": "partially_received",
    "fully received": "fully_received",
    "closed": "closed",
}

# Business Central returns its "blank" date instead of null.
_BC_BLANK_DATE = date(1, 1, 1)


def _normalize_status(raw_status: str | None, *, fully_received: bool = False) -> str:
    if fully_received:
        return "fully_received"
    status = (raw_status or "").strip().lower()
    mapped = _STATUS_MAP.get(status)
    if mapped is None:
        if status:
            logger.warning("Unexpected Business Central purchase order status %r — treated as open", raw_status)
        return "open"
    return mapped


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        parsed = date.fromisoformat(str(value)[:10])
    except ValueError, TypeError:
        return None
    return None if parsed == _BC_BLANK_DATE else parsed


def _parse_datetime(value: str | None) -> datetime | None:
    """Parse a Business Central lastModifiedDateTime (ISO-8601, may end in 'Z') as aware UTC."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError, TypeError, AttributeError:
        return None
    if parsed.year == 1:
        return None  # blank datetime
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _parse_decimal(value, *, field: str) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError(f"invalid decimal in '{field}': {value!r}") from exc


def _require_id(raw: dict, what: str) -> str:
    value = raw.get("id")
    if not value:
        raise ValueError(f"Business Central {what} is missing 'id'")
    return str(value)


def _text(raw: dict, *keys: str) -> str:
    for key in keys:
        value = raw.get(key)
        if value:
            return str(value)
    return ""


def _map_purchase_order_line(raw_line: dict) -> NormalizedPurchaseOrderLine:
    return NormalizedPurchaseOrderLine(
        external_id=_require_id(raw_line, "purchase order line"),
        line_no=str(raw_line.get("sequence") or ""),
        item_no=_text(raw_line, "lineObjectNumber", "itemNumber"),
        description=_text(raw_line, "description"),
        ordered_qty=_parse_decimal(raw_line.get("quantity"), field="quantity") or Decimal("0"),
        # receivedQuantity stays in raw_payload only: receipts are not synced in this milestone.
        unit_price=_parse_decimal(raw_line.get("directUnitCost"), field="directUnitCost"),
        expected_receipt_date=_parse_date(raw_line.get("expectedReceiptDate")),
        # purchaseOrderLine has no lastModifiedDateTime in API v2.0.
        source_last_modified=_parse_datetime(raw_line.get("lastModifiedDateTime")),
        raw_payload=raw_line,
    )


class BusinessCentralMapper(BaseBusinessSystemMapper):
    """Maps raw Business Central OData responses to normalised DTO objects."""

    system_code = "business_central"

    def map_sales_order(self, raw: dict) -> NormalizedCustomerOrder:
        # TODO: implement full Business Central sales order mapping
        raise NotImplementedError("BusinessCentralMapper.map_sales_order not yet implemented")

    def map_purchase_order(self, raw: dict, lines: list[dict] | None = None) -> NormalizedPurchaseOrder:
        """Map a raw BC purchase order (and its lines) to a NormalizedPurchaseOrder.

        Raises ValueError when a required identifier (PO or line ``id``, PO
        ``number``) is missing or a numeric field is not a number.
        """
        external_id = _require_id(raw, "purchase order")
        po_number = _text(raw, "number")
        if not po_number:
            raise ValueError(f"Business Central purchase order {external_id} is missing 'number'")
        return NormalizedPurchaseOrder(
            external_id=external_id,
            po_number=po_number,
            supplier_no=_text(raw, "vendorNumber"),
            supplier_name=_text(raw, "vendorName"),
            status=_normalize_status(raw.get("status"), fully_received=raw.get("fullyReceived") is True),
            order_date=_parse_date(raw.get("orderDate")),
            # The v2.0 header exposes requestedReceiptDate, not expectedReceiptDate.
            expected_receipt_date=_parse_date(raw.get("expectedReceiptDate") or raw.get("requestedReceiptDate")),
            currency=_text(raw, "currencyCode") or "EUR",
            source_last_modified=_parse_datetime(raw.get("lastModifiedDateTime")),
            raw_payload=raw,
            lines=[_map_purchase_order_line(line) for line in (lines or [])],
        )
