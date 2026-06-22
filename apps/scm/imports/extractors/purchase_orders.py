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
"""

from typing import Any


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
        rows.append(
            {
                **header,
                "line_no": str(i * 10000),
                "item_no": line.get("item_no", ""),
                "description": line.get("description", ""),
                "ordered_qty": line.get("quantity", "0"),
            }
        )

    return rows


def extract_pdf_purchase_orders(file_obj) -> list[dict[str, Any]]:
    """Extract purchase order rows from a PDF file via the FastAPI service."""
    from .clients.pdf_fastapi import extract_purchase_orders_from_pdf

    api_response = extract_purchase_orders_from_pdf(file_obj)
    return _flatten_api_response(api_response)
