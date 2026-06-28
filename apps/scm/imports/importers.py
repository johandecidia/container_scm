"""Import confirmation: create or update SCM objects from validated rows."""

from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from apps.scm.containers.choices import LocationSource, LocationType, MovementType
from apps.scm.containers.models import Container, ContainerLocation, ContainerMovement, EquipmentType

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
                existing.location_text = data["current_location"]
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
        location_text=data.get("current_location") or "",
        notes=data.get("notes") or "",
        manufacturer=data.get("manufacturer") or "",
    )
    return "created"


def _import_purchase_order_row(row: ImportRow, job: ImportJob, *, update_existing: bool = False) -> str:
    """Import a single PO row (one PO line). Returns 'created', 'updated', or 'skipped'."""
    from apps.scm.procurement.models import PurchaseOrder, PurchaseOrderLine

    data = row.validated_data
    po_external_id = data.get("po_external_id") or data.get("po_number", "")
    line_external_id = data.get("line_external_id") or f"{data.get('po_number', '')}-{data.get('line_no', '')}"

    if not po_external_id or not data.get("line_no"):
        return "skipped"

    # When not updating existing data, check for a duplicate line *before* touching the PO
    # header.  This prevents a skipped duplicate row from silently overwriting PO header fields.
    if not update_existing:
        try:
            existing_po = PurchaseOrder.objects.get(team=job.team, external_id=po_external_id)
        except PurchaseOrder.DoesNotExist:
            existing_po = None
        if (
            existing_po is not None
            and PurchaseOrderLine.objects.filter(purchase_order=existing_po, external_id=line_external_id).exists()
        ):
            return "skipped"

    # Upsert PO header (idempotent — multiple rows share the same PO).
    po, _ = PurchaseOrder.objects.update_or_create(
        team=job.team,
        external_id=po_external_id,
        defaults={
            "po_number": data.get("po_number", ""),
            "supplier_no": data.get("supplier_no", ""),
            "supplier_name": data.get("supplier_name", ""),
            "order_date": data.get("order_date"),
            "expected_receipt_date": data.get("expected_receipt_date"),
            "currency": data.get("currency", "EUR"),
            "status": "open",
        },
    )

    _, line_created = PurchaseOrderLine.objects.update_or_create(
        purchase_order=po,
        external_id=line_external_id,
        defaults={
            "team": job.team,
            "line_no": data.get("line_no", ""),
            "item_no": data.get("item_no", ""),
            "description": data.get("description", ""),
            "ordered_qty": data.get("ordered_qty", Decimal("0")),
            "shipped_qty": Decimal("0"),
            "received_qty": Decimal("0"),
            "unit_price": data.get("unit_price"),
            "expected_receipt_date": data.get("expected_receipt_date"),
        },
    )

    return "created" if line_created else "updated"


def _import_container_movement_row(row: ImportRow, job: ImportJob, *, update_existing: bool = False) -> str:  # noqa: ARG001
    """Import a single container movement row. Returns 'created', 'updated', or 'skipped'."""
    data = row.validated_data
    container_number = data.get("container_number", "")
    location_name = data.get("location_name", "")

    if not container_number or not location_name:
        return "skipped"

    # Parse container number into parts (owner_code + category + serial)
    if len(container_number) < 10:
        return "skipped"
    owner_code = container_number[:3].upper()
    category_id = container_number[3:4].upper()
    serial_number = container_number[4:10]

    try:
        container = Container.objects.get(
            team=job.team,
            owner_code=owner_code,
            category_id=category_id,
            serial_number=serial_number,
        )
    except Container.DoesNotExist:
        return "skipped"

    # Get or create the location
    location_type = data.get("location_type") or LocationType.UNKNOWN
    valid_types = [lt[0] for lt in LocationType.choices]
    if location_type not in valid_types:
        location_type = LocationType.UNKNOWN

    location, _ = ContainerLocation.objects.get_or_create(
        team=job.team,
        name=location_name,
        defaults={
            "location_type": location_type,
            "country": data.get("country") or "",
            "city": data.get("city") or "",
            "address": data.get("address") or "",
        },
    )

    # Determine occurred_at
    occurred_at = data.get("occurred_at")
    if occurred_at is None:
        occurred_at = timezone.now()
    elif not timezone.is_aware(occurred_at):
        occurred_at = timezone.make_aware(occurred_at)

    # Create movement and update container location
    old_location = container.current_location
    ContainerMovement.objects.create(
        team=job.team,
        container=container,
        from_location=old_location,
        to_location=location,
        movement_type=MovementType.POSITION_UPDATE,
        occurred_at=occurred_at,
        source=LocationSource.IMPORT,
        notes=data.get("notes") or "",
    )
    container.current_location = location
    container.location_source = LocationSource.IMPORT
    container.last_location_update = occurred_at
    container.save(update_fields=["current_location", "location_source", "last_location_update"])

    return "created"


_IMPORTERS: dict = {
    ImportJob.ImportType.CONTAINERS: _import_container_row,
    ImportJob.ImportType.CONTAINER_MOVEMENTS: _import_container_movement_row,
    ImportJob.ImportType.PURCHASE_ORDERS: _import_purchase_order_row,
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
