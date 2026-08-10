# Normalised DTO schemas for Microsoft Business Central data.
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel
from pydantic import Field as PydanticField

# ---------------------------------------------------------------------------
# Customer order schemas (dataclasses — not yet migrated)
# ---------------------------------------------------------------------------


@dataclass
class NormalizedCustomerOrderLine:
    """A single line on a customer sales order."""

    line_number: int = 0
    item_number: str = ""
    description: str = ""
    quantity: float = 0.0
    unit_of_measure: str = ""
    unit_price: float = 0.0
    amount: float = 0.0
    raw: dict = field(default_factory=dict)


@dataclass
class NormalizedCustomerOrder:
    """A customer sales order from Business Central."""

    external_id: str = ""
    order_number: str = ""
    customer_number: str = ""
    customer_name: str = ""
    order_date: date | None = None
    shipment_date: date | None = None
    status: str = ""
    currency_code: str = ""
    total_amount: float = 0.0
    lines: list[NormalizedCustomerOrderLine] = field(default_factory=list)
    raw: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Purchase order schemas (Pydantic)
# ---------------------------------------------------------------------------


class NormalizedPurchaseOrderLine(BaseModel):
    """A single line on a purchase order, normalised from Business Central."""

    external_id: str
    line_no: str
    item_no: str
    description: str = ""
    ordered_qty: Decimal = Decimal("0")
    shipped_qty: Decimal = Decimal("0")
    received_qty: Decimal = Decimal("0")
    unit_price: Decimal | None = None
    expected_receipt_date: date | None = None
    source_last_modified: datetime | None = None
    raw_payload: dict[str, Any] = PydanticField(default_factory=dict)


class NormalizedPurchaseOrder(BaseModel):
    """A purchase order from Business Central, normalised for SCM import."""

    source_system: Literal["business_central"] = "business_central"
    external_id: str
    po_number: str
    supplier_no: str = ""
    supplier_name: str = ""
    status: str = "open"
    order_date: date | None = None
    expected_receipt_date: date | None = None
    currency: str = "EUR"
    # Source lastModifiedDateTime — used to advance the incremental sync watermark.
    # Not persisted to the PurchaseOrder model in this milestone.
    source_last_modified: datetime | None = None
    raw_payload: dict[str, Any] = PydanticField(default_factory=dict)
    lines: list[NormalizedPurchaseOrderLine] = PydanticField(default_factory=list)
