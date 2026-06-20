"""Business Central Purchase Order XLSX document-layout parser.

This parser handles XLSX files exported from Business Central using the
"Inköpsorder" / "Purchase Order" printed report layout.  These are
document-style exports, not clean spreadsheet tables, so the parser uses
label-based cell scanning rather than fixed column positions.

Supported layouts
-----------------
* Swedish BC export ("Inköpsorder")
* English BC export ("Purchase Order")

Both languages share the same structural pattern:
  - A header section (first ~40 rows) with label/value pairs
  - An item table with a header row + data rows below it
  - A footer section with totals (parsing stops here)

Public API
----------
    parse_bc_po_xlsx(file_obj) -> ParsedBCPO | None
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from .types import BCPOHeader, BCPOLine, ParsedBCPO

# ---------------------------------------------------------------------------
# Label → field name mapping (both English and Swedish BC labels)
# ---------------------------------------------------------------------------

# These labels appear in the document header area and identify PO header fields.
# The value is expected to be in the next non-empty cell in the same row,
# or in the same cell on the next row (rare).
_HEADER_LABEL_MAP: dict[str, str] = {
    # English labels
    "No.": "po_number",
    "Purchase Order No.": "po_number",
    "Order No.": "po_number",
    "Vendor No.": "vendor_no",
    "Buy-from Vendor No.": "vendor_no",
    "Buy-from Vendor Name": "vendor_name",
    "Vendor Name": "vendor_name",
    # "Buy-from Address" is a section header — the first value below it is the vendor name.
    "Buy-from Address": "vendor_name",
    "Buy-from Contact": "contact",
    "Contact": "contact",
    "Order Date": "order_date",
    "Payment Terms": "payment_terms",
    "Payment Terms Code": "payment_terms",
    "Purchaser": "purchaser",
    "Purchaser Code": "purchaser",
    "Currency Code": "currency",
    "Currency": "currency",
    # Swedish labels
    "Inköpsordernr.": "po_number",
    "Leverantörsnr.": "vendor_no",
    "Leverantör": "vendor_name",
    "Köp från adress": "vendor_name",
    "Kontakt": "contact",
    "Orderdatum": "order_date",
    "Betalningsvillkor": "payment_terms",
    "Inköpare": "purchaser",
    "Valutakod": "currency",
}

# Column headers that identify the item lines table.
# A row containing at least MIN_TABLE_HEADER_MATCHES of these is treated as
# the table header row.  Comparison is case-insensitive.
_TABLE_COL_HEADERS: set[str] = {
    # English
    "no.",
    "description",
    "quantity",
    "qty.",
    "unit of measure",
    "uom",
    "direct unit cost",
    "unit price",
    "amount",
    "item no.",
    # Swedish
    "nr.",
    "beskrivning",
    "antal",
    "enhet",
    "à-pris",
    "belopp",
    "à pris",
    "pris",
}

MIN_TABLE_HEADER_MATCHES = 2  # min number of _TABLE_COL_HEADERS needed to flag a row as table header

# Strings in the first cell of a row that signal the end of line items.
_STOP_PREFIXES: set[str] = {
    "total",
    "summa",
    "moms",
    "vat",
    "subtotal",
    "delsumma",
    "rabatt",
    "discount",
    "sum",
    "netto",
    "brutto",
}

# Maximum rows scanned in the header section (before the item table).
_MAX_HEADER_ROWS = 60

# Maximum consecutive empty rows allowed inside the item table before stopping.
_MAX_EMPTY_ITEM_ROWS = 3


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def parse_bc_po_xlsx(file_obj) -> ParsedBCPO | None:
    """Parse a BC PO XLSX document-layout file.

    Returns a :class:`ParsedBCPO` on success, or ``None`` if the file cannot
    be recognised as a BC PO document (e.g. wrong format, missing PO number).

    Args:
        file_obj: A file-like object (binary) containing the XLSX data.
    """
    import openpyxl

    try:
        wb = openpyxl.load_workbook(file_obj, read_only=True, data_only=True)
        ws = wb.active
        if ws is None:
            return None
    except Exception:
        return None

    # Load all rows as lists of strings (None → "")
    all_rows: list[list[str]] = []
    for row in ws.iter_rows(values_only=True):
        all_rows.append([_cell_str(v) for v in row])

    if not all_rows:
        return None

    warnings: list[str] = []

    # --- Phase 1: extract header fields ---
    header_data, table_header_row_idx = _extract_header(all_rows, warnings)

    # Validate we found enough to identify this as a PO
    if not header_data.get("po_number"):
        return None

    header = BCPOHeader(
        po_number=header_data.get("po_number", ""),
        vendor_no=header_data.get("vendor_no", ""),
        vendor_name=header_data.get("vendor_name", ""),
        vendor_address=header_data.get("vendor_address", ""),
        contact=header_data.get("contact", ""),
        order_date=header_data.get("order_date", ""),
        payment_terms=header_data.get("payment_terms", ""),
        purchaser=header_data.get("purchaser", ""),
        currency=header_data.get("currency", ""),
    )

    # --- Phase 2: extract line items ---
    lines: list[BCPOLine] = []
    if table_header_row_idx is not None:
        col_map = _build_column_map(all_rows[table_header_row_idx])
        lines = _extract_lines(all_rows, table_header_row_idx + 1, col_map, warnings)

    if not lines:
        warnings.append("No item lines found in file.")

    return ParsedBCPO(header=header, lines=lines, parse_warnings=warnings)


# ---------------------------------------------------------------------------
# Private helpers — header extraction
# ---------------------------------------------------------------------------


def _extract_header(all_rows: list[list[str]], warnings: list[str]) -> tuple[dict[str, str], int | None]:
    """Scan the header section and return (field_dict, table_header_row_index).

    BC PO XLSX files use two label/value patterns:
      - Horizontal: label in cell (r, c), value in same row at (r, c+n)
      - Vertical:   label in cell (r, c), value in same column at (r+n, c)

    Both patterns are handled by building a sparse cell map and using
    ``_find_label_value`` which searches both directions, bounded above by
    the item table header row so we never accidentally pick up item data.
    """
    header_data: dict[str, str] = {}

    # --- Step 1: find the item table header row ---
    table_header_row_idx: int | None = None
    for row_idx in range(len(all_rows)):
        if _is_table_header_row(all_rows[row_idx]):
            table_header_row_idx = row_idx
            break

    # Header section is everything before the table header (or first MAX_HEADER_ROWS)
    header_limit = table_header_row_idx if table_header_row_idx is not None else min(len(all_rows), _MAX_HEADER_ROWS)

    # --- Step 2: build sparse cell map for the header section only ---
    cell_map: dict[tuple[int, int], str] = {}
    for r in range(header_limit):
        for c, val in enumerate(all_rows[r]):
            if val:
                cell_map[(r, c)] = val

    # --- Step 3: scan for label/value pairs ---
    for row_idx in range(header_limit):
        row = all_rows[row_idx]
        for col_idx, cell_val in enumerate(row):
            normalised = _normalise_label(cell_val)
            field_name = _HEADER_LABEL_MAP.get(normalised)
            if field_name and field_name not in header_data:
                # "No." is ambiguous — also appears as an item table column header.
                # Accept it only in sparse rows (few cells filled).
                if normalised == "No." and not _is_sparse_row(row):
                    continue
                value = _find_label_value(cell_map, row_idx, col_idx, row_limit=header_limit)
                if value:
                    header_data[field_name] = value

    return header_data, table_header_row_idx


def _normalise_label(value: str) -> str:
    """Strip trailing colons, whitespace, and normalise for label matching."""
    return value.strip().rstrip(":").strip()


def _is_sparse_row(row: list[str]) -> bool:
    """Return True if the row has few filled cells (typical of a BC PO header row)."""
    return sum(1 for c in row if c) <= 6


def _find_label_value(
    cell_map: dict[tuple[int, int], str],
    row_idx: int,
    col_idx: int,
    max_horizontal: int = 12,
    max_vertical: int = 8,
    row_limit: int | None = None,
) -> str:
    """Return the first non-empty value adjacent to a label cell.

    Searches horizontally first (same row, to the right) then vertically
    (same column, below).  This handles both BC layout patterns:
      - Label and value in the same row (e.g. "Order Date" … "24-08-19")
      - Label in one row, value several rows below (e.g. "No." … "100001")

    Horizontally and vertically found cells that are themselves known labels
    are skipped.  ``row_limit`` (exclusive) caps the vertical search so we
    never cross into the item table area.
    """
    # Horizontal search: same row, to the right — skip sibling labels
    for c in range(col_idx + 1, col_idx + max_horizontal + 1):
        val = cell_map.get((row_idx, c), "")
        if val and _normalise_label(val) not in _HEADER_LABEL_MAP:
            return val
    # Vertical search: same column, below — bounded by row_limit and skip labels
    v_limit = row_limit if row_limit is not None else row_idx + max_vertical + 1
    for r in range(row_idx + 1, min(row_idx + max_vertical + 1, v_limit)):
        val = cell_map.get((r, col_idx), "")
        if val and _normalise_label(val) not in _HEADER_LABEL_MAP:
            return val
    return ""


# ---------------------------------------------------------------------------
# Private helpers — table header detection
# ---------------------------------------------------------------------------


def _is_table_header_row(row: list[str]) -> bool:
    """Return True if this row contains enough item table column headers."""
    matches = sum(1 for cell in row if cell.strip().lower().rstrip(":") in _TABLE_COL_HEADERS)
    return matches >= MIN_TABLE_HEADER_MATCHES


def _build_column_map(header_row: list[str]) -> dict[str, int]:
    """Build {normalised_label → column_index} from the item table header row."""
    col_map: dict[str, int] = {}
    for col_idx, cell in enumerate(header_row):
        key = cell.strip().lower().rstrip(":")
        if key and key not in col_map:
            col_map[key] = col_idx
    return col_map


# ---------------------------------------------------------------------------
# Private helpers — line item extraction
# ---------------------------------------------------------------------------


def _extract_lines(
    all_rows: list[list[str]],
    start_row_idx: int,
    col_map: dict[str, int],
    warnings: list[str],
) -> list[BCPOLine]:
    """Extract item lines from all_rows starting at start_row_idx.

    Args:
        all_rows: Full sheet data (0-based).
        start_row_idx: First row of item data (immediately after table header).
        col_map: Mapping of normalised column header → column index.
        warnings: Mutable list to append any parse warnings to.
    """
    lines: list[BCPOLine] = []
    line_index = 0
    empty_streak = 0

    for row_idx in range(start_row_idx, len(all_rows)):
        row = all_rows[row_idx]
        row_num = row_idx + 1  # 1-based for user-facing messages

        # Check for stop markers
        first_cell = row[0].strip().lower() if row else ""
        if first_cell and any(first_cell.startswith(prefix) for prefix in _STOP_PREFIXES):
            break

        # Skip empty rows (up to _MAX_EMPTY_ITEM_ROWS consecutive empties)
        if not any(cell for cell in row):
            empty_streak += 1
            if empty_streak >= _MAX_EMPTY_ITEM_ROWS:
                break
            continue
        empty_streak = 0

        item_no = _get_col(row, col_map, ("no.", "item no.", "nr.", "item"))
        description = _get_col(row, col_map, ("description", "beskrivning", "desc"))

        # Skip lines without an item number (comment/text lines in BC)
        if not item_no:
            continue

        line_index += 1
        qty_raw = _get_col(row, col_map, ("quantity", "qty.", "antal", "qty"))
        unit = _get_col(row, col_map, ("unit of measure", "uom", "enhet", "unit"))
        price_raw = _get_col(row, col_map, ("direct unit cost", "unit price", "à-pris", "à pris", "pris"))
        amount_raw = _get_col(row, col_map, ("amount", "belopp"))

        quantity = _parse_decimal(qty_raw)
        unit_price = _parse_decimal(price_raw)
        amount = _parse_decimal(amount_raw)

        if quantity is None and qty_raw:
            warnings.append(f"Row {row_num}: could not parse quantity {qty_raw!r} for item {item_no!r}.")
        if unit_price is None and price_raw:
            warnings.append(f"Row {row_num}: could not parse unit price {price_raw!r} for item {item_no!r}.")

        # Amount consistency check (warning only)
        if quantity is not None and unit_price is not None and amount is not None:
            expected = quantity * unit_price
            if abs(expected - amount) > Decimal("0.01"):
                warnings.append(
                    f"Row {row_num}: amount {amount} ≠ quantity {quantity} × unit price {unit_price} "
                    f"(expected {expected:.2f}) for item {item_no!r}."
                )

        lines.append(
            BCPOLine(
                line_index=line_index,
                source_row=row_num,
                item_no=item_no,
                description=description,
                quantity=quantity,
                unit_of_measure=unit,
                unit_price=unit_price,
                amount=amount,
            )
        )

    return lines


def _get_col(row: list[str], col_map: dict[str, int], candidates: tuple[str, ...]) -> str:
    """Return the cell value for the first matching candidate column name."""
    for key in candidates:
        col_idx = col_map.get(key)
        if col_idx is not None and col_idx < len(row):
            val = row[col_idx].strip()
            if val:
                return val
    return ""


def _parse_decimal(value: str) -> Decimal | None:
    """Parse a decimal string, handling Swedish (comma) and English (dot) decimals.

    Returns ``None`` if the value is empty or cannot be parsed.
    """
    if not value or not value.strip():
        return None
    # Remove thousands separators (space, NBSP, dot used as thousands)
    cleaned = value.strip().replace("\xa0", "").replace(" ", "")
    # If comma is the decimal separator (e.g. "1.234,56" → "1234.56")
    if "," in cleaned and "." in cleaned:
        # Both present → dot is thousands separator, comma is decimal
        cleaned = cleaned.replace(".", "").replace(",", ".")
    elif "," in cleaned:
        # Only comma → it's the decimal separator
        cleaned = cleaned.replace(",", ".")
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        return None


def _cell_str(value: Any) -> str:
    """Convert a cell value to a stripped string."""
    if value is None:
        return ""
    return str(value).strip()


# ---------------------------------------------------------------------------
# Flat row serialiser — converts ParsedBCPO into flat dicts for the pipeline
# ---------------------------------------------------------------------------


def to_flat_rows(parsed: ParsedBCPO) -> list[dict[str, Any]]:
    """Serialise a :class:`ParsedBCPO` into flat row dicts for the import pipeline.

    Each dict represents one PO line with header fields repeated.
    The field names match the canonical PO import schema so no column mapping
    is required (identity mapping is used for this import type).

    Args:
        parsed: The result of :func:`parse_bc_po_xlsx`.
    """
    h = parsed.header
    header_base = {
        "po_number": h.po_number,
        "supplier_no": h.vendor_no,
        "supplier_name": h.vendor_name,
        "order_date": h.order_date,
        "currency": h.currency or "EUR",
    }

    rows: list[dict[str, Any]] = []
    for line in parsed.lines:
        line_no = str(line.line_index * 10000)  # BC-style line numbers: 10000, 20000, …
        row: dict[str, Any] = {
            **header_base,
            "line_no": line_no,
            "item_no": line.item_no,
            "description": line.description,
            "ordered_qty": str(line.quantity) if line.quantity is not None else "",
            # Store source metadata for matching / audit
            "_source_row": line.source_row,
            "_line_index": line.line_index,
            "_unit_price": str(line.unit_price) if line.unit_price is not None else "",
            "_amount": str(line.amount) if line.amount is not None else "",
            "_unit_of_measure": line.unit_of_measure,
        }
        rows.append(row)

    return rows
