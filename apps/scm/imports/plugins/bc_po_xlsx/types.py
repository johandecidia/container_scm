"""Type definitions for the Business Central PO XLSX importer plugin."""

from dataclasses import dataclass, field
from decimal import Decimal


@dataclass
class BCPOLine:
    """A single line item parsed from a BC PO XLSX document-layout file."""

    line_index: int  # 1-based index within the PO
    source_row: int  # Source row number in the XLSX sheet (1-based)
    item_no: str
    description: str
    quantity: Decimal | None
    unit_of_measure: str
    unit_price: Decimal | None
    amount: Decimal | None


@dataclass
class BCPOHeader:
    """PO header fields parsed from a BC PO XLSX document-layout file."""

    po_number: str
    vendor_no: str
    vendor_name: str
    vendor_address: str
    contact: str
    order_date: str  # Raw string — coerced by Pydantic schema downstream
    payment_terms: str
    purchaser: str
    currency: str


@dataclass
class ParsedBCPO:
    """Complete parsed Business Central Purchase Order from an XLSX file."""

    header: BCPOHeader
    lines: list[BCPOLine] = field(default_factory=list)
    parse_warnings: list[str] = field(default_factory=list)
