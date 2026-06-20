"""HTTP client for the PDF purchase-order extraction FastAPI service.

This module is a pure transport layer: it sends the PDF file and returns the
raw list of row dicts returned by the API.  No database access, no validation.
"""

from typing import Any

import requests
from django.conf import settings


class PDFExtractionError(Exception):
    """Raised when PDF extraction fails for any reason."""


def extract_purchase_orders_from_pdf(file_obj) -> list[dict[str, Any]]:
    """POST the PDF file to the FastAPI extraction endpoint.

    Returns a list of flat row dicts compatible with PurchaseOrderImportRowSchema.
    Raises PDFExtractionError on timeout, HTTP errors, or malformed responses.
    """
    base_url: str = getattr(settings, "SCM_PDF_FASTAPI_BASE_URL", "")
    timeout: int = getattr(settings, "SCM_PDF_FASTAPI_TIMEOUT_SECONDS", 30)

    if not base_url:
        raise PDFExtractionError("SCM_PDF_FASTAPI_BASE_URL is not configured.")

    url = f"{base_url.rstrip('/')}/v1/purchase-orders/extract"

    try:
        response = requests.post(url, files={"file": file_obj}, timeout=timeout)
    except requests.Timeout as exc:
        raise PDFExtractionError(f"PDF extraction timed out after {timeout}s.") from exc
    except requests.RequestException as exc:
        raise PDFExtractionError(f"PDF extraction request failed: {exc}") from exc

    if response.status_code >= 400:
        raise PDFExtractionError(f"PDF extraction API returned {response.status_code}: {response.text[:200]}")

    try:
        data = response.json()
    except ValueError as exc:
        raise PDFExtractionError(f"PDF extraction API returned invalid JSON: {exc}") from exc

    if not isinstance(data, list):
        raise PDFExtractionError(
            f"PDF extraction API returned unexpected format: expected list, got {type(data).__name__}"
        )

    return data
