# Normalised DTO schemas for Microsoft Business Central data.
# These are plain dataclasses — not Django models.
from dataclasses import dataclass, field
from datetime import date


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


@dataclass
class NormalizedPurchaseOrderLine:
    """A single line on a purchase order."""

    line_number: int = 0
    item_number: str = ""
    description: str = ""
    quantity: float = 0.0
    unit_of_measure: str = ""
    direct_unit_cost: float = 0.0
    amount: float = 0.0
    raw: dict = field(default_factory=dict)


@dataclass
class NormalizedPurchaseOrder:
    """A purchase order from Business Central."""

    external_id: str = ""
    order_number: str = ""
    vendor_number: str = ""
    vendor_name: str = ""
    order_date: date | None = None
    expected_receipt_date: date | None = None
    status: str = ""
    currency_code: str = ""
    total_amount: float = 0.0
    lines: list[NormalizedPurchaseOrderLine] = field(default_factory=list)
    raw: dict = field(default_factory=dict)
