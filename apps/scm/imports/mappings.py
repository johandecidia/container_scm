"""Column mapping logic for import jobs."""

from .models import ImportJob, ImportRow, ImportTemplate

# Default CSV column → field name mapping for containers.
DEFAULT_CONTAINER_MAPPING: dict[str, str] = {
    "Container No": "container_number",
    "Container Number": "container_number",
    "Container ID": "container_number",
    "Container": "container_number",
    "Type": "equipment_type",
    "Equipment Type": "equipment_type",
    "ISO Code": "equipment_type",
    "Seal": "seal_number",
    "Seal No": "seal_number",
    "Seal Number": "seal_number",
    "Status": "status",
    "Condition": "condition",
    "Location": "current_location",
    "Current Location": "current_location",
    "Notes": "notes",
    "Manufacturer": "manufacturer",
}

DEFAULT_CONTAINER_MOVEMENT_MAPPING: dict[str, str] = {
    "Container No": "container_number",
    "Container Number": "container_number",
    "Container ID": "container_number",
    "Container": "container_number",
    "Location": "location_name",
    "Location Name": "location_name",
    "Depot": "location_name",
    "Location Type": "location_type",
    "Type": "location_type",
    "Country": "country",
    "City": "city",
    "Address": "address",
    "Occurred At": "occurred_at",
    "Date": "occurred_at",
    "Event Date": "occurred_at",
    "Source": "source",
    "Notes": "notes",
}

DEFAULT_PO_MAPPING: dict[str, str] = {
    # PO header
    "PO Number": "po_number",
    "PO No": "po_number",
    "Purchase Order": "po_number",
    "Order Number": "po_number",
    "Supplier No": "supplier_no",
    "Supplier Number": "supplier_no",
    "Vendor No": "supplier_no",
    "Vendor Number": "supplier_no",
    "Supplier Name": "supplier_name",
    "Vendor Name": "supplier_name",
    "Order Date": "order_date",
    "Expected Receipt Date": "expected_receipt_date",
    "Expected Delivery Date": "expected_receipt_date",
    "Delivery Date": "expected_receipt_date",
    "Currency": "currency",
    # PO line
    "Line No": "line_no",
    "Line Number": "line_no",
    "Item No": "item_no",
    "Item Number": "item_no",
    "SKU": "item_no",
    "Article": "item_no",
    "Article No": "item_no",
    "Description": "description",
    "Ordered Qty": "ordered_qty",
    "Quantity": "ordered_qty",
    "Qty": "ordered_qty",
    "Order Qty": "ordered_qty",
    # Identity mappings for pre-normalised rows (e.g. XML imports where field
    # names already match the target schema field names).
    "po_number": "po_number",
    "supplier_no": "supplier_no",
    "supplier_name": "supplier_name",
    "order_date": "order_date",
    "expected_receipt_date": "expected_receipt_date",
    "currency": "currency",
    "line_no": "line_no",
    "item_no": "item_no",
    "description": "description",
    "ordered_qty": "ordered_qty",
}

_DEFAULT_MAPPINGS: dict[str, dict[str, str]] = {
    ImportJob.ImportType.CONTAINERS: DEFAULT_CONTAINER_MAPPING,
    ImportJob.ImportType.CONTAINER_MOVEMENTS: DEFAULT_CONTAINER_MOVEMENT_MAPPING,
    ImportJob.ImportType.PURCHASE_ORDERS: DEFAULT_PO_MAPPING,
    # BC PO XLSX plugin — parser emits pre-normalised field names, identity mapping passes them through.
    ImportJob.ImportType.BC_PO_XLSX: DEFAULT_PO_MAPPING,
}


def get_default_mapping(import_type: str) -> dict[str, str]:
    """Return the built-in default column mapping for an import type."""
    return _DEFAULT_MAPPINGS.get(import_type, {})


def apply_mapping(raw_data: dict, mapping: dict[str, str]) -> dict:
    """Map raw column names to normalised field names.

    Unknown columns are dropped. If multiple raw columns map to the same
    target field the last non-empty value wins.
    """
    mapped: dict = {}
    for raw_key, value in raw_data.items():
        target = mapping.get(raw_key)
        if target and (value or target not in mapped):
            mapped[target] = value
    return mapped


def get_mapping_for_job(job: ImportJob) -> dict[str, str]:
    """Return the active column mapping for a job.

    Prefers the team's default template for the import type; falls back to
    the built-in default.
    """
    template = (
        ImportTemplate.objects.filter(
            team=job.team,
            import_type=job.import_type,
            is_default=True,
        )
        .order_by("-pk")
        .first()
    )
    if template:
        return template.mapping
    return get_default_mapping(job.import_type)


def map_import_rows(job: ImportJob) -> None:
    """Apply column mapping to all ImportRow instances for a job in bulk."""
    mapping = get_mapping_for_job(job)
    rows = list(job.rows.all())
    for row in rows:
        row.mapped_data = apply_mapping(row.raw_data, mapping)
    ImportRow.objects.bulk_update(rows, ["mapped_data"])
