"""Purchase order PDF extractor.

Delegates HTTP transport to the FastAPI client, then normalises the API
response into flat row dicts ready for the standard import pipeline
(mapping → Pydantic validation → DB validation → confirm).

Expected API response shape
---------------------------
{
    "status": "completed",
    "confidence": 0.95,
    "requires_review": false,
    "warnings": [],
    "data": {
        "purchase_order_number": "100000",
        "vendor_number": "L00141",
        "vendor": {"company": "SAF Holland GmbH", ...},
        "order_date": "2024-08-16",
        "currency": "EUR",
        "line_items": [
            {"item_no": "ART10247", "description": "...", "quantity": "1", ...}
        ]
    }
}

Each line_item becomes one flat row dict with PO header fields repeated.

The service answers HTTP 200 even when it only partially recognised the
document — an unsupported layout comes back with ``line_items: []`` and a
populated ``warnings`` list.  That is an extraction failure, not an empty
import, so it raises instead of producing zero rows.
"""

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class PurchaseOrderExtraction:
    """Flat PO rows plus the extraction-quality fields reported by the API."""

    rows: list[dict[str, Any]]
    status: str = ""
    confidence: float | None = None
    requires_review: bool = False
    warnings: list[str] = field(default_factory=list)

    def as_metadata(self) -> dict[str, Any]:
        """Return the quality fields as a dict to merge into ``ImportJob.metadata``."""
        return {
            "extraction_status": self.status,
            "extraction_confidence": self.confidence,
            "extraction_requires_review": self.requires_review,
            "extraction_warnings": list(self.warnings),
        }


def _flatten_api_response(api_response: dict[str, Any]) -> list[dict[str, Any]]:
    """Convert the FastAPI extraction response to a list of flat PO row dicts."""
    data = api_response.get("data") or {}
    vendor = data.get("vendor") or {}

    header = {
        "po_number": data.get("purchase_order_number", ""),
        "supplier_no": data.get("vendor_number", ""),
        "supplier_name": vendor.get("company", ""),
        "order_date": data.get("order_date", ""),
        "expected_receipt_date": "",
        "currency": data.get("currency", "EUR"),
    }

    rows = []
    for i, line in enumerate(data.get("line_items") or [], start=1):
        row: dict = {
            **header,
            "line_no": str(i * 10000),
            "item_no": line.get("item_no", ""),
            "description": line.get("description", ""),
            "ordered_qty": line.get("quantity", "0"),
        }
        unit_price = line.get("unit_price") or line.get("price")
        if unit_price is not None and str(unit_price).strip():
            row["unit_price"] = str(unit_price)
        rows.append(row)

    return rows


def _no_rows_message(status: str, warnings: list[str], *, has_data: bool) -> str:
    """Build a diagnosable error message for an extraction that produced no rows."""
    reason = "no purchase order lines" if has_data else "no purchase order data"
    message = f"PDF extraction returned {reason} (status={status or 'unknown'})."
    if warnings:
        message += " Service warnings: " + "; ".join(warnings)
    return message


def extract_pdf_purchase_orders(file_obj) -> PurchaseOrderExtraction:
    """Extract purchase order rows from a PDF file via the FastAPI service.

    Raises PDFExtractionError when the service returned no usable rows, so an
    unrecognised document layout fails the import job instead of completing it
    with zero rows.
    """
    from .clients.pdf_fastapi import PDFExtractionError, extract_purchase_orders_from_pdf

    api_response = extract_purchase_orders_from_pdf(file_obj)
    data = api_response.get("data")
    status = str(api_response.get("status") or "")
    warnings = [str(warning) for warning in api_response.get("warnings") or []]
    confidence = api_response.get("confidence")

    rows = _flatten_api_response(api_response) if data else []
    if not rows:
        raise PDFExtractionError(_no_rows_message(status, warnings, has_data=bool(data)))

    return PurchaseOrderExtraction(
        rows=rows,
        status=status,
        confidence=float(confidence) if isinstance(confidence, int | float) else None,
        requires_review=bool(api_response.get("requires_review")),
        warnings=warnings,
    )
