"""Business-rule validation for import rows (DB / domain constraints).

Pydantic (schemas.py) handles format/type/normalisation.
This module handles Django / database rules.
"""

from django.core.exceptions import ValidationError

from apps.scm.containers.models import Container, EquipmentType
from apps.scm.containers.utils import parse_container_id, validate_container_id
from apps.teams.models import Team

from .models import ImportError, ImportJob, ImportRow


def _validate_purchase_order_row(row: ImportRow, validated_data: dict, team: Team) -> list[dict]:
    """Run DB/business-rule checks on a single PO import row.

    Derives and stores ``po_external_id`` and ``line_external_id`` on
    ``validated_data`` so the importer can use them without re-computing.
    Issues a WARNING (not ERROR) when a PO line already exists — duplicates
    are skipped at import time unless ``update_existing=True`` is requested.

    Returns a list of error dicts:
        {"field": str, "message": str, "code": str, "severity": str}
    """
    from apps.scm.procurement.models import PurchaseOrder, PurchaseOrderLine

    errors: list[dict] = []
    po_number = validated_data.get("po_number", "")
    line_no = validated_data.get("line_no", "")

    # Derive stable external IDs for upserts (CSV has no BC GUID).
    validated_data["po_external_id"] = po_number
    validated_data["line_external_id"] = f"{po_number}-{line_no}"

    # Duplicate PO line against existing DB data → warning, not error.
    try:
        po = PurchaseOrder.objects.get(team=team, external_id=po_number)
        if PurchaseOrderLine.objects.filter(purchase_order=po, external_id=validated_data["line_external_id"]).exists():
            errors.append(
                {
                    "field": "line_no",
                    "message": (
                        f"PO line {po_number}/{line_no} already exists. "
                        "Row will be skipped unless update_existing is enabled."
                    ),
                    "code": "duplicate_po_line",
                    "severity": ImportError.Severity.WARNING,
                }
            )
    except PurchaseOrder.DoesNotExist:
        pass

    return errors


def _validate_container_row(row: ImportRow, validated_data: dict, team: Team) -> list[dict]:
    """Run DB/business-rule checks on a single container row.

    Returns a list of error dicts:
        {"field": str, "message": str, "code": str, "severity": str}
    """
    errors: list[dict] = []
    container_number = validated_data.get("container_number", "")

    # ISO 6346 format + check digit
    try:
        parts = parse_container_id(container_number)
        validate_container_id(
            parts["owner_code"],
            parts["category_id"],
            parts["serial_number"],
            parts["check_digit"],
        )
        # Embed parsed parts into validated_data so the importer can use them.
        validated_data.update(parts)
    except ValidationError as exc:
        errors.append(
            {
                "field": "container_number",
                "message": str(exc.message),
                "code": "invalid_container_id",
                "severity": ImportError.Severity.ERROR,
            }
        )
        # Further checks are meaningless without a valid container ID.
        return errors

    # Duplicate within team
    if Container.objects.filter(
        team=team,
        owner_code=parts["owner_code"],
        category_id=parts["category_id"],
        serial_number=parts["serial_number"],
    ).exists():
        errors.append(
            {
                "field": "container_number",
                "message": f"Container {container_number} already exists for this team.",
                "code": "duplicate_container",
                "severity": ImportError.Severity.WARNING,
            }
        )

    # Equipment type check
    equipment_type_code = validated_data.get("equipment_type")
    if equipment_type_code:
        if not EquipmentType.objects.filter(iso_code=equipment_type_code).exists():
            errors.append(
                {
                    "field": "equipment_type",
                    "message": f"Equipment type '{equipment_type_code}' does not exist.",
                    "code": "invalid_equipment_type",
                    "severity": ImportError.Severity.ERROR,
                }
            )
    else:
        errors.append(
            {
                "field": "equipment_type",
                "message": "Equipment type is required.",
                "code": "missing_equipment_type",
                "severity": ImportError.Severity.ERROR,
            }
        )

    return errors


_VALIDATORS: dict = {
    ImportJob.ImportType.CONTAINERS: _validate_container_row,
    ImportJob.ImportType.PURCHASE_ORDERS: _validate_purchase_order_row,
}


def validate_import_row(row: ImportRow, team: Team) -> None:
    """Validate a single import row and persist errors + status."""
    validator = _VALIDATORS.get(row.import_job.import_type)
    if validator is None:
        row.status = ImportRow.Status.VALID
        row.save(update_fields=["status"])
        return

    validated_data = dict(row.validated_data)
    errors = validator(row, validated_data, team)

    # Persist any parsed parts back onto the row
    row.validated_data = validated_data
    row.errors = errors

    has_error = any(e.get("severity") == ImportError.Severity.ERROR for e in errors)
    row.status = ImportRow.Status.INVALID if has_error else ImportRow.Status.VALID
    row.save(update_fields=["status", "errors", "validated_data"])

    # Persist ImportError records for admin / query visibility
    ImportError.objects.filter(import_row=row).delete()
    ImportError.objects.bulk_create(
        [
            ImportError(
                import_job=row.import_job,
                import_row=row,
                code=e["code"],
                message=e["message"],
                field_name=e.get("field", ""),
                severity=e["severity"],
            )
            for e in errors
        ]
    )


def validate_all_rows(job: ImportJob, team: Team) -> None:
    """Validate all rows for a job and update job-level counters."""
    for row in job.rows.all():
        validate_import_row(row, team)

    job.valid_rows = job.rows.filter(status=ImportRow.Status.VALID).count()
    job.invalid_rows = job.rows.filter(status=ImportRow.Status.INVALID).count()
    job.save(update_fields=["valid_rows", "invalid_rows", "updated_at"])
