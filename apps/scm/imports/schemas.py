"""Pydantic schemas for import row normalisation and type validation."""

import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from pydantic import BaseModel, Field, field_validator

from .models import ImportJob


class ContainerImportSchema(BaseModel):
    """Normalise and validate a mapped container import row.

    Pydantic handles: whitespace trimming, uppercase normalisation, type coercion,
    and required-field checks.  Business / DB rules are handled separately in
    validators.py.
    """

    container_number: str = Field(..., description="Full ISO 6346 container ID, e.g. MSCU1234567")
    equipment_type: str | None = Field(None, description="ISO equipment type code, e.g. 22G1")
    seal_number: str | None = None
    status: str | None = None
    condition: str | None = None
    current_location: str | None = None
    notes: str | None = None
    manufacturer: str | None = None

    @field_validator("container_number", mode="before")
    @classmethod
    def normalise_container_number(cls, v: str) -> str:
        if not v or not str(v).strip():
            raise ValueError("container_number is required")
        return str(v).strip().upper().replace(" ", "")

    @field_validator("equipment_type", mode="before")
    @classmethod
    def normalise_equipment_type(cls, v: str | None) -> str | None:
        if v and str(v).strip():
            return str(v).strip().upper()
        return None

    @field_validator("status", "condition", mode="before")
    @classmethod
    def normalise_upper(cls, v: str | None) -> str | None:
        if v and str(v).strip():
            return str(v).strip().upper()
        return None

    @field_validator("seal_number", "current_location", "notes", "manufacturer", mode="before")
    @classmethod
    def strip_optional_strings(cls, v: str | None) -> str | None:
        if v and str(v).strip():
            return str(v).strip()
        return None


class ContainerMovementImportSchema(BaseModel):
    """Normalise and validate a container movement / location import row."""

    container_number: str = Field(..., description="Full ISO 6346 container ID, e.g. MSCU1234567")
    location_name: str = Field(..., description="Name of the location")
    location_type: str | None = Field(None, description="Location type, e.g. depot")
    country: str | None = None
    city: str | None = None
    address: str | None = None
    occurred_at: datetime.datetime | None = None
    source: str | None = None
    notes: str | None = None

    @field_validator("container_number", mode="before")
    @classmethod
    def normalise_container_number(cls, v: str) -> str:
        if not v or not str(v).strip():
            raise ValueError("container_number is required")
        return str(v).strip().upper().replace(" ", "")

    @field_validator("location_name", mode="before")
    @classmethod
    def strip_location_name(cls, v: str) -> str:
        if not v or not str(v).strip():
            raise ValueError("location_name is required")
        return str(v).strip()

    @field_validator("location_type", "source", mode="before")
    @classmethod
    def normalise_lower(cls, v: str | None) -> str | None:
        if v and str(v).strip():
            return str(v).strip().lower()
        return None

    @field_validator("country", "city", "address", "notes", mode="before")
    @classmethod
    def strip_optional_strings(cls, v: str | None) -> str | None:
        if v and str(v).strip():
            return str(v).strip()
        return None

    @field_validator("occurred_at", mode="before")
    @classmethod
    def parse_occurred_at(cls, v: Any) -> datetime.datetime | None:
        if not v or not str(v).strip():
            return None
        raw = str(v).strip()
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
            try:
                return datetime.datetime.strptime(raw, fmt)
            except ValueError:
                continue
        raise ValueError(f"Invalid datetime {raw!r}. Expected YYYY-MM-DD or YYYY-MM-DD HH:MM:SS")


_DATE_FORMATS = ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%m/%d/%Y", "%y-%m-%d")

# Space characters used as thousands separators in printed ERP documents.
# Business Central PDFs use NO-BREAK SPACE, e.g. "2\xa0091,68".
_THOUSANDS_SPACES = (
    "\xa0",  # NO-BREAK SPACE
    "\u202f",  # NARROW NO-BREAK SPACE
    "\u2009",  # THIN SPACE
    "\u2007",  # FIGURE SPACE
    " ",  # plain space
)


def _to_decimal(value: Any, field_label: str) -> Decimal:
    """Parse a printed number into a Decimal.

    Handles the formats produced by European ERP documents and spreadsheets:
    space thousands separators (including NO-BREAK SPACE), comma decimal
    separators and dot decimal separators.  When both ``,`` and ``.`` are
    present, the last one is the decimal separator and the other is dropped as
    a grouping mark.  A lone comma is always read as a decimal separator, which
    matches the European sources this pipeline imports from.
    """
    raw = str(value).strip()
    for space in _THOUSANDS_SPACES:
        raw = raw.replace(space, "")

    last_comma = raw.rfind(",")
    last_dot = raw.rfind(".")
    if last_comma < 0 or last_dot < 0:
        # Only one separator kind present — a lone comma is the decimal mark.
        raw = raw.replace(",", ".")
    elif last_comma > last_dot:
        raw = raw.replace(".", "").replace(",", ".")
    else:
        raw = raw.replace(",", "")

    try:
        return Decimal(raw)
    except InvalidOperation as err:
        raise ValueError(f"Invalid {field_label}: {value!r} — must be a number") from err


class PurchaseOrderImportRowSchema(BaseModel):
    """Normalise and validate a single flat PO import row (one row = one PO line).

    Pydantic handles: whitespace trimming, type coercion, date parsing, quantity
    validation and currency normalisation.  Business / DB rules (duplicate checks
    etc.) are handled separately in validators.py.
    """

    po_number: str = Field(..., description="PO number, e.g. PO-2026-001")
    supplier_no: str = Field(default="", description="Supplier number")
    supplier_name: str = Field(default="", description="Supplier name")
    order_date: datetime.date | None = None
    expected_receipt_date: datetime.date | None = None
    currency: str = Field(default="EUR", description="ISO 3-letter currency code")
    line_no: str = Field(..., description="PO line number, e.g. 10000")
    item_no: str = Field(..., description="Item / SKU number")
    description: str = Field(default="")
    ordered_qty: Decimal = Field(..., description="Ordered quantity, must be positive")
    unit_price: Decimal | None = Field(None, description="Unit price")

    @field_validator("po_number", "line_no", "item_no", mode="before")
    @classmethod
    def strip_required_str(cls, v: Any) -> str:
        if not v or not str(v).strip():
            raise ValueError("This field is required")
        return str(v).strip()

    @field_validator("supplier_no", "supplier_name", mode="before")
    @classmethod
    def strip_optional_str(cls, v: Any) -> str:
        return str(v).strip() if v else ""

    @field_validator("description", mode="before")
    @classmethod
    def strip_description(cls, v: Any) -> str:
        return str(v).strip() if v else ""

    @field_validator("currency", mode="before")
    @classmethod
    def normalise_currency(cls, v: Any) -> str:
        if not v or not str(v).strip():
            return "EUR"
        return str(v).strip().upper()

    @field_validator("ordered_qty", mode="before")
    @classmethod
    def validate_ordered_qty(cls, v: Any) -> Decimal:
        if v is None or str(v).strip() == "":
            raise ValueError("ordered_qty is required")
        qty = _to_decimal(v, "quantity")
        if qty <= 0:
            raise ValueError(f"ordered_qty must be positive, got {qty}")
        return qty

    @field_validator("unit_price", mode="before")
    @classmethod
    def validate_unit_price(cls, v: Any) -> Decimal | None:
        if v is None or str(v).strip() == "":
            return None
        price = _to_decimal(v, "unit price")
        if price < 0:
            raise ValueError(f"unit_price must be non-negative, got {price}")
        return price

    @field_validator("order_date", "expected_receipt_date", mode="before")
    @classmethod
    def parse_date_field(cls, v: Any) -> datetime.date | None:
        if not v or not str(v).strip():
            return None
        raw = str(v).strip()
        for fmt in _DATE_FORMATS:
            try:
                return datetime.datetime.strptime(raw, fmt).date()
            except ValueError:
                continue
        raise ValueError(f"Invalid date {raw!r}. Expected YYYY-MM-DD, DD-MM-YYYY or YY-MM-DD")


# Registry of schema classes per import type.
_SCHEMA_REGISTRY: dict[str, type[BaseModel]] = {
    ImportJob.ImportType.CONTAINERS: ContainerImportSchema,
    ImportJob.ImportType.CONTAINER_MOVEMENTS: ContainerMovementImportSchema,
    ImportJob.ImportType.PURCHASE_ORDERS: PurchaseOrderImportRowSchema,
}


def validate_row_data(import_type: str, mapped_data: dict) -> tuple[dict, list[dict]]:
    """Validate and normalise mapped row data using the appropriate Pydantic schema.

    Returns ``(validated_dict, errors_list)`` where each error dict contains
    ``field``, ``message``, and ``code`` keys.
    """
    schema_cls = _SCHEMA_REGISTRY.get(import_type)
    if schema_cls is None:
        return mapped_data, []

    try:
        instance = schema_cls.model_validate(mapped_data)
        return instance.model_dump(mode="json"), []
    except Exception as exc:
        errors: list[dict] = []
        if hasattr(exc, "errors"):
            for err in exc.errors():
                field = ".".join(str(loc) for loc in err.get("loc", []))
                errors.append(
                    {
                        "field": field,
                        "message": err.get("msg", str(err)),
                        "code": err.get("type", "validation_error"),
                    }
                )
        else:
            errors.append({"field": "", "message": str(exc), "code": "validation_error"})
        return {}, errors
