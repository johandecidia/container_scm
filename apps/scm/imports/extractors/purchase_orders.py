"""Purchase order PDF extractor.

Delegates to the FastAPI client and returns flat row dicts ready for the
standard import pipeline (mapping → Pydantic validation → DB validation → confirm).
"""

from typing import Any


def extract_pdf_purchase_orders(file_obj) -> list[dict[str, Any]]:
    """Extract purchase order rows from a PDF file via the FastAPI service."""
    from .clients.pdf_fastapi import extract_purchase_orders_from_pdf

    return extract_purchase_orders_from_pdf(file_obj)
