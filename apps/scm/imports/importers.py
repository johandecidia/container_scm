"""Import confirmation: create or update SCM objects from validated rows."""

from django.db import transaction
from django.utils import timezone

from apps.scm.containers.models import Container, EquipmentType

from .models import ImportJob, ImportRow


def _import_container_row(row: ImportRow, job: ImportJob, *, update_existing: bool = False) -> str:
    """Import a single container row. Returns 'created', 'updated', or 'skipped'."""
    data = row.validated_data
    parts = {
        "owner_code": data.get("owner_code", ""),
        "category_id": data.get("category_id", ""),
        "serial_number": data.get("serial_number", ""),
        "check_digit": data.get("check_digit"),
    }
    if not all([parts["owner_code"], parts["category_id"], parts["serial_number"], parts["check_digit"] is not None]):
        return "skipped"

    equipment_type_code = data.get("equipment_type")
    try:
        equipment_type = EquipmentType.objects.get(iso_code=equipment_type_code)
    except EquipmentType.DoesNotExist:
        return "skipped"

    existing_qs = Container.objects.filter(
        team=job.team,
        owner_code=parts["owner_code"],
        category_id=parts["category_id"],
        serial_number=parts["serial_number"],
    )

    if existing_qs.exists():
        if update_existing:
            existing = existing_qs.first()
            if existing is None:
                return "skipped"
            existing.equipment_type = equipment_type
            if data.get("status"):
                existing.status = data["status"]
            if data.get("current_location"):
                existing.current_location = data["current_location"]
            existing.updated_by = job.created_by
            existing.save()
            return "updated"
        return "skipped"

    Container.objects.create(
        team=job.team,
        created_by=job.created_by,
        updated_by=job.created_by,
        equipment_type=equipment_type,
        owner_code=parts["owner_code"],
        category_id=parts["category_id"],
        serial_number=parts["serial_number"],
        check_digit=parts["check_digit"],
        status=data.get("status") or "AVAILABLE",
        current_location=data.get("current_location") or "",
        notes=data.get("notes") or "",
        manufacturer=data.get("manufacturer") or "",
    )
    return "created"


_IMPORTERS: dict = {
    ImportJob.ImportType.CONTAINERS: _import_container_row,
}


@transaction.atomic
def run_import(job: ImportJob, *, update_existing: bool = False) -> None:
    """Import all valid rows for a job and mark it completed."""
    importer = _IMPORTERS.get(job.import_type)
    if importer is None:
        raise NotImplementedError(f"No importer registered for type: {job.import_type}")

    job.status = ImportJob.Status.IMPORTING
    job.save(update_fields=["status", "updated_at"])

    valid_rows = list(job.rows.filter(status=ImportRow.Status.VALID))
    processed = 0

    for row in valid_rows:
        result = importer(row, job, update_existing=update_existing)
        row.status = ImportRow.Status.IMPORTED if result in ("created", "updated") else ImportRow.Status.SKIPPED
        row.save(update_fields=["status"])
        processed += 1

    job.processed_rows = processed
    job.status = ImportJob.Status.COMPLETED
    job.completed_at = timezone.now()
    job.save(update_fields=["processed_rows", "status", "completed_at", "updated_at"])
