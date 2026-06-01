"""Shared test helpers for import tests."""

import io

from django.core.files.uploadedfile import SimpleUploadedFile

from apps.scm.containers.models import EquipmentType
from apps.scm.containers.utils import calculate_check_digit
from apps.scm.imports.models import ImportJob, ImportRow
from apps.teams.models import Team
from apps.users.models import CustomUser

# A known-valid container ID: CSQU3054187
OWNER = "CSQ"
CAT = "U"
SERIAL = "305418"
CHECK = calculate_check_digit(OWNER, CAT, SERIAL)
CONTAINER_ID = f"{OWNER}{CAT}{SERIAL}{CHECK}"  # e.g. CSQU3054187


def make_team(name="Test Team", slug="test-team") -> Team:
    return Team.objects.get_or_create(slug=slug, defaults={"name": name})[0]


def make_user(email="importer@example.com") -> CustomUser:
    return CustomUser.objects.get_or_create(
        username=email,
        defaults={"email": email},
    )[0]


def make_equipment_type(iso_code="22G1") -> EquipmentType:
    return EquipmentType.objects.get_or_create(
        iso_code=iso_code,
        defaults={
            "category": "GP",
            "length_ft": 20,
            "high_cube": False,
            "description": "20' GP (test)",
        },
    )[0]


def make_csv_file(rows: list[dict], filename: str = "test.csv") -> SimpleUploadedFile:
    """Build an in-memory CSV file from a list of dicts."""
    if not rows:
        content = b""
    else:
        headers = list(rows[0].keys())
        lines = [",".join(headers)]
        for row in rows:
            lines.append(",".join(str(row.get(h, "")) for h in headers))
        content = "\n".join(lines).encode("utf-8")
    return SimpleUploadedFile(filename, content, content_type="text/csv")


def make_xlsx_file(rows: list[dict], filename: str = "test.xlsx") -> SimpleUploadedFile:
    """Build an in-memory XLSX file from a list of dicts."""
    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    if ws is None:
        raise RuntimeError("openpyxl Workbook has no active sheet")
    if rows:
        headers = list(rows[0].keys())
        ws.append(headers)
        for row in rows:
            ws.append([row.get(h, "") for h in headers])
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return SimpleUploadedFile(
        filename, buf.read(), content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )


def make_import_job(team: Team, user: CustomUser, import_type: str = ImportJob.ImportType.CONTAINERS) -> ImportJob:
    """Create an ImportJob with a minimal uploaded CSV."""
    f = make_csv_file([{"Container No": CONTAINER_ID, "Equipment Type": "22G1"}])
    return ImportJob.objects.create(
        team=team,
        created_by=user,
        file=f,
        original_filename=f.name or "",
        import_type=import_type,
        status=ImportJob.Status.UPLOADED,
    )


def make_parsed_job(team: Team, user: CustomUser) -> ImportJob:
    """Create an ImportJob with one valid ImportRow in PARSED state."""
    job = make_import_job(team, user)
    make_equipment_type()
    ImportRow.objects.create(
        import_job=job,
        row_number=1,
        raw_data={"Container No": CONTAINER_ID, "Equipment Type": "22G1"},
        mapped_data={"container_number": CONTAINER_ID, "equipment_type": "22G1"},
        validated_data={
            "container_number": CONTAINER_ID,
            "equipment_type": "22G1",
            "owner_code": OWNER,
            "category_id": CAT,
            "serial_number": SERIAL,
            "check_digit": CHECK,
        },
        status=ImportRow.Status.VALID,
    )
    job.total_rows = 1
    job.valid_rows = 1
    job.status = ImportJob.Status.VALIDATED
    job.save()
    return job
