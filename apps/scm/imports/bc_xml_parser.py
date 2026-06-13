"""Business Central Purchase Order XML parser.

Parses the PEB Purchase Order report export (BC report id 12047977) and
returns normalized dicts consumed by
``apps.scm.procurement.services.import_purchase_orders_from_bc``.

Supported XML structure
-----------------------
The BC report layout produces one ``Purchase_Header`` DataItem per order, with
a nested ``CopyLoop`` DataItem that contains:

* Named ``Column`` elements for the PO header (PO number, vendor, dates,
  currency).  These are the *primary* data source when present.
* One or more ``TempPurchDocLine`` DataItems for the order lines.
  Lines where ``IsTextLine`` is ``True`` are comment / section-header rows and
  are skipped.

Column name mapping (header)
----------------------------
The following ``Column`` names are read from ``CopyLoop``:

======================  ==========================
Column name             Mapped field
======================  ==========================
``Vendor_No``           supplier_no
``Vendor_Name``         supplier_name
``Order_Date``          order_date (ISO 8601)
``Expected_Receipt_Date`` expected_receipt_date
``EmailBodyTextLine4``  po_number (pattern: "Order No.: <value>")
``EmailBodyTextLine5``  currency  (pattern: "<amount> <CUR> excl. VAT")
======================  ==========================

Column name mapping (line)
--------------------------
==========  =============
Column name Mapped field
==========  =============
``Line01``  external_id
``Column01`` item_no
``Column02`` description
``Column03`` ordered_qty (Swedish decimal: comma as decimal separator)
``IsTextLine`` skip when "True"
==========  =============
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def parse_bc_po_xml(file_obj) -> list[dict[str, Any]]:
    """Parse a BC PO XML export and return a list of normalized PO dicts.

    Each dict in the returned list is compatible with
    ``import_purchase_orders_from_bc`` in ``apps.scm.procurement.services``.

    Args:
        file_obj: A file-like object (binary or text) containing the XML.

    Returns:
        List of PO dicts.  An empty list is returned for an empty or
        unrecognised file.
    """
    content = file_obj.read()
    if isinstance(content, bytes):
        content = content.decode("utf-8-sig")  # strip BOM if present
    root = ET.fromstring(content)

    purchase_orders = []
    for header_item in root.findall('.//DataItem[@name="Purchase_Header"]'):
        po_data = _parse_purchase_header(header_item)
        if po_data:
            purchase_orders.append(po_data)

    return purchase_orders


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _parse_purchase_header(header_item: ET.Element) -> dict[str, Any] | None:
    """Extract PO header + lines from a ``Purchase_Header`` DataItem."""
    external_id = header_item.get("systemId", "").strip()
    if not external_id:
        return None

    copy_loop = header_item.find('.//DataItem[@name="CopyLoop"]')
    if copy_loop is None:
        return None

    cols = _get_columns(copy_loop)

    return {
        "external_id": external_id,
        "po_number": _extract_po_number(cols),
        "supplier_no": cols.get("Vendor_No", ""),
        "supplier_name": cols.get("Vendor_Name", ""),
        "status": "open",
        "order_date": _parse_date(cols.get("Order_Date")),
        "expected_receipt_date": _parse_date(cols.get("Expected_Receipt_Date")),
        "currency": _extract_currency(cols),
        "lines": _parse_po_lines(copy_loop),
    }


def _get_columns(elem: ET.Element) -> dict[str, str]:
    """Return ``{column_name: text}`` for all direct ``Columns/Column`` children."""
    return {col.get("name", ""): (col.text or "").strip() for col in elem.findall("Columns/Column") if col.get("name")}


def _extract_po_number(cols: dict[str, str]) -> str:
    """Extract the PO number from header columns.

    Tries ``EmailBodyTextLine4`` first (BC email body pattern
    "Order No.: <value>"), then falls back to a ``PO_Number`` column.
    """
    line4 = cols.get("EmailBodyTextLine4", "")
    match = re.search(r"Order No\.\s*[:\-]?\s*(\S+)", line4)
    if match:
        return match.group(1)
    return cols.get("PO_Number", "")


def _extract_currency(cols: dict[str, str]) -> str:
    """Extract a 3-letter ISO currency code from header columns.

    Checks ``Currency_Code`` first, then parses ``EmailBodyTextLine5`` for
    the pattern "… <CUR> excl. VAT".
    """
    if cols.get("Currency_Code"):
        return cols["Currency_Code"]
    line5 = cols.get("EmailBodyTextLine5", "")
    match = re.search(r"([A-Z]{3})\s+excl", line5)
    if match:
        return match.group(1)
    return "EUR"


def _parse_date(value: str | None) -> date | None:
    """Parse a date string.  Tries ISO (YYYY-MM-DD) then common European formats."""
    if not value:
        return None
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    return None


def _parse_qty(value: str) -> Decimal:
    """Parse a BC quantity string.

    BC (Swedish locale) uses comma as the decimal separator and space / NBSP
    as the thousands separator, e.g. ``"1 615,00"`` or ``"15,000"``.
    """
    if not value:
        return Decimal("0")
    cleaned = value.replace("\xa0", "").replace(" ", "").replace(",", ".")
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        return Decimal("0")


def _parse_po_lines(copy_loop: ET.Element) -> list[dict[str, Any]]:
    """Extract PO line items from ``TempPurchDocLine`` DataItems.

    Lines where ``IsTextLine`` is ``"True"`` (case-insensitive) are skipped
    because they are comment or section-header rows without an item number.
    """
    lines = []
    for line_item in copy_loop.findall('.//DataItem[@name="TempPurchDocLine"]'):
        cols = _get_columns(line_item)
        if cols.get("IsTextLine", "").strip().lower() == "true":
            continue
        item_no = cols.get("Column01", "").strip()
        if not item_no:
            continue
        external_id = cols.get("Line01", "").strip()
        lines.append(
            {
                "external_id": external_id,
                "line_no": _derive_line_no(external_id),
                "item_no": item_no,
                "description": cols.get("Column02", ""),
                "ordered_qty": _parse_qty(cols.get("Column03", "")),
                "shipped_qty": Decimal("0"),
                "received_qty": Decimal("0"),
                "expected_receipt_date": None,
            }
        )
    return lines


def _derive_line_no(external_id: str) -> str:
    """Derive a short line number from a BC line external ID.

    BC line external IDs embed the line number in the last digits of a
    zero-padded string, e.g. ``"10000100000000000000020000"`` → ``"20000"``.
    The last five characters contain the line number.
    """
    if not external_id or len(external_id) < 5:
        return external_id
    try:
        return str(int(external_id[-5:]))
    except ValueError:
        return external_id
