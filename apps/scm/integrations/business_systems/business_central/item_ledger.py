"""Business Central item ledger entries: serial classification and mapping.

Raw rows come from the Analytics API (``api/microsoft/analytics/v1.0/…/itemLedgerEntries``).
Mapping is deliberately lenient: only ``entryNo`` is required, every other value
is kept as BC sent it (``raw_payload``) even when it cannot be interpreted, so no
posted history is lost to bad data quality.

Serial numbers are normalised and checked with the existing ISO 6346 helpers
(``apps.scm.containers.intake``) — trimming, inner whitespace and case only; a
wrong check digit is never corrected. Whether the *item* is a container is a
separate question and stays unclassified here.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from django.core.exceptions import ValidationError

from apps.scm.integrations.models import BusinessCentralItemLedgerEntry

SerialStatus = BusinessCentralItemLedgerEntry.SerialStatus

# BC's empty date.
_BC_BLANK_DATE = "0001-01-01"
_QUANTITY_STEP = Decimal("0.00001")
_MAX_QUANTITY = Decimal(10) ** 15


def classify_serial(raw: str | None) -> tuple[str, str]:
    """Return ``(normalised_serial, SerialStatus)`` for a BC ``serialNo``."""
    from apps.scm.containers.intake import normalise_container_number, parse_and_validate_container_number

    normalised = normalise_container_number(raw or "")
    if not normalised:
        return "", SerialStatus.MISSING_SERIAL
    try:
        parse_and_validate_container_number(normalised)
    except ValidationError:
        return normalised, SerialStatus.INVALID_ISO
    return normalised, SerialStatus.VALID_ISO


def map_item_ledger_entry(raw: dict) -> dict[str, Any]:
    """Model field values for one raw BC item ledger entry.

    Raises ValueError when ``entryNo`` is missing or not an integer — the only
    field the entry cannot be stored without.
    """
    entry_no = raw.get("entryNo")
    if isinstance(entry_no, bool) or not isinstance(entry_no, int):
        raise ValueError(f"Business Central item ledger entry has no integer 'entryNo': {entry_no!r}")
    serial_raw = _text(raw, "serialNo")
    serial_no, serial_status = classify_serial(serial_raw)
    return {
        "entry_no": entry_no,
        "item_no": _text(raw, "itemNo"),
        "serial_no_raw": serial_raw,
        "serial_no": serial_no,
        "serial_status": serial_status,
        "lot_no": _text(raw, "lotNo"),
        "entry_type": _text(raw, "entryType"),
        "document_type": _text(raw, "documentType").strip(),
        "document_no": _text(raw, "documentNo"),
        "posting_date": _date(raw.get("postingDate")),
        "quantity": _decimal(raw.get("quantity")),
        "location_code": _text(raw, "locationCode"),
        "source_type": _text(raw, "sourceType"),
        "source_no": _text(raw, "sourceNo"),
        "raw_payload": raw,
        "sync_hash": compute_sync_hash(raw),
    }


def compute_sync_hash(raw: dict) -> str:
    """Deterministic hash of the whole BC row (including fields BC updates later)."""
    payload = {k: v for k, v in raw.items() if not k.startswith("@odata.")}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def _text(raw: dict, key: str) -> str:
    value = raw.get(key)
    return value if isinstance(value, str) else ("" if value is None else str(value))


def _date(value: Any) -> date | None:
    if not isinstance(value, str) or not value or value.startswith(_BC_BLANK_DATE):
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None  # kept as sent in raw_payload


def _decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        return None  # kept as sent in raw_payload
    # Fits DecimalField(max_digits=20, decimal_places=5); anything else stays raw only.
    if not number.is_finite() or abs(number) >= _MAX_QUANTITY:
        return None
    return number.quantize(_QUANTITY_STEP)
