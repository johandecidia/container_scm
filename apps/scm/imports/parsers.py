"""CSV and XLSX file parsing for import jobs."""

import csv
import io
from typing import Any

from .models import ImportJob, ImportRow


def _parse_csv(file_obj) -> list[dict[str, Any]]:
    """Parse a CSV file object and return a list of row dicts."""
    content = file_obj.read()
    if isinstance(content, bytes):
        content = content.decode("utf-8-sig")  # strip BOM if present
    reader = csv.DictReader(io.StringIO(content))
    rows = []
    for row in reader:
        stripped = {k.strip(): (v.strip() if v else "") for k, v in row.items() if k}
        if any(v for v in stripped.values()):
            rows.append(stripped)
    return rows


def _parse_xlsx(file_obj) -> list[dict[str, Any]]:
    """Parse an XLSX file object and return a list of row dicts."""
    import openpyxl

    wb = openpyxl.load_workbook(file_obj, read_only=True, data_only=True)
    ws = wb.active
    if ws is None:
        return []
    all_rows = list(ws.iter_rows(values_only=True))
    if not all_rows:
        return []
    headers = [str(h).strip() if h is not None else "" for h in all_rows[0]]
    result = []
    for row in all_rows[1:]:
        row_dict = {headers[i]: str(cell).strip() if cell is not None else "" for i, cell in enumerate(row)}
        if any(v for v in row_dict.values()):
            result.append(row_dict)
    return result


def parse_file(job: ImportJob) -> list[dict[str, Any]]:
    """Parse the uploaded file for a job and return raw row dicts.

    Supports .csv and .xlsx. Falls back to CSV for unknown extensions.
    """
    filename = job.original_filename.lower()
    job.file.seek(0)
    if filename.endswith(".xlsx"):
        return _parse_xlsx(job.file)
    return _parse_csv(job.file)


def create_import_rows(job: ImportJob, raw_rows: list[dict[str, Any]]) -> None:
    """Replace all ImportRow instances for a job with the given raw rows."""
    ImportRow.objects.filter(import_job=job).delete()
    rows = [ImportRow(import_job=job, row_number=i + 1, raw_data=row) for i, row in enumerate(raw_rows)]
    ImportRow.objects.bulk_create(rows)
    job.total_rows = len(rows)
    job.save(update_fields=["total_rows", "updated_at"])
