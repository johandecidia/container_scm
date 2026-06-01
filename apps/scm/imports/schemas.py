"""Pydantic schemas for import row normalisation and type validation."""

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


# Registry of schema classes per import type.
_SCHEMA_REGISTRY: dict[str, type[BaseModel]] = {
    ImportJob.ImportType.CONTAINERS: ContainerImportSchema,
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
        return instance.model_dump(), []
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
