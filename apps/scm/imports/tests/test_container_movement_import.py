"""Tests for CONTAINER_MOVEMENTS import type."""

from django.test import TestCase

from apps.scm.containers.choices import LocationSource, LocationType
from apps.scm.containers.models import Container, ContainerLocation, ContainerMovement, EquipmentType
from apps.scm.containers.utils import calculate_check_digit
from apps.scm.imports.importers import run_import
from apps.scm.imports.models import ImportJob, ImportRow
from apps.teams.models import Team
from apps.users.models import CustomUser

OWNER = "CSQ"
CAT = "U"
SERIAL = "305418"
CHECK = calculate_check_digit(OWNER, CAT, SERIAL)
CONTAINER_ID = f"{OWNER}{CAT}{SERIAL}{CHECK}"


def _et() -> EquipmentType:
    return EquipmentType.objects.get_or_create(
        iso_code="22G1",
        defaults={"category": "GP", "length_ft": 20, "high_cube": False, "description": "20' GP"},
    )[0]


def _make_container(team) -> Container:
    return Container.objects.create(
        team=team,
        owner_code=OWNER,
        category_id=CAT,
        serial_number=SERIAL,
        check_digit=CHECK,
        equipment_type=_et(),
    )


def _make_movement_job(team, user, rows: list[dict]) -> ImportJob:
    """Create a validated CONTAINER_MOVEMENTS import job with the given rows."""
    from django.core.files.uploadedfile import SimpleUploadedFile

    f = SimpleUploadedFile("movements.csv", b"dummy", content_type="text/csv")
    job = ImportJob.objects.create(
        team=team,
        created_by=user,
        file=f,
        original_filename="movements.csv",
        import_type=ImportJob.ImportType.CONTAINER_MOVEMENTS,
        status=ImportJob.Status.VALIDATED,
        total_rows=len(rows),
        valid_rows=len(rows),
    )
    for i, row_data in enumerate(rows, start=1):
        ImportRow.objects.create(
            import_job=job,
            row_number=i,
            raw_data=row_data,
            mapped_data=row_data,
            validated_data=row_data,
            status=ImportRow.Status.VALID,
        )
    return job


class ContainerMovementImportTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="Mov Import Team", slug="mov-import-team")
        cls.user = CustomUser.objects.create_user(username="movimp@example.com", password="pass")
        cls.container = _make_container(cls.team)

    def test_import_creates_movement(self):
        job = _make_movement_job(
            self.team,
            self.user,
            [{"container_number": CONTAINER_ID, "location_name": "Test Depot", "location_type": "depot"}],
        )
        run_import(job)
        self.assertEqual(ContainerMovement.objects.filter(team=self.team).count(), 1)

    def test_import_creates_location_if_missing(self):
        job = _make_movement_job(
            self.team,
            self.user,
            [{"container_number": CONTAINER_ID, "location_name": "New Port", "location_type": "port"}],
        )
        run_import(job)
        self.assertTrue(ContainerLocation.objects.filter(team=self.team, name="New Port").exists())

    def test_import_updates_container_current_location(self):
        loc = ContainerLocation.objects.create(team=self.team, name="Hamburg Port", location_type=LocationType.PORT)
        job = _make_movement_job(
            self.team,
            self.user,
            [{"container_number": CONTAINER_ID, "location_name": loc.name, "location_type": "port"}],
        )
        run_import(job)
        self.container.refresh_from_db()
        self.assertEqual(self.container.current_location, loc)

    def test_import_sets_location_source_to_import(self):
        job = _make_movement_job(
            self.team,
            self.user,
            [{"container_number": CONTAINER_ID, "location_name": "Some Depot"}],
        )
        run_import(job)
        self.container.refresh_from_db()
        self.assertEqual(self.container.location_source, LocationSource.IMPORT)

    def test_import_skips_unknown_container(self):
        job = _make_movement_job(
            self.team,
            self.user,
            [{"container_number": "XXXX1234567", "location_name": "Anywhere"}],
        )
        run_import(job)
        row = job.rows.first()
        self.assertEqual(row.status, ImportRow.Status.SKIPPED)

    def test_import_skips_missing_location_name(self):
        job = _make_movement_job(
            self.team,
            self.user,
            [{"container_number": CONTAINER_ID, "location_name": ""}],
        )
        run_import(job)
        row = job.rows.first()
        self.assertEqual(row.status, ImportRow.Status.SKIPPED)

    def test_team_isolation_containers_not_accessible_across_teams(self):
        other_team = Team.objects.create(name="Other Mov Team", slug="other-mov-team")
        job = _make_movement_job(
            other_team,
            self.user,
            [{"container_number": CONTAINER_ID, "location_name": "Test Depot"}],
        )
        run_import(job)
        # Container belongs to self.team, not other_team — should be skipped
        row = job.rows.first()
        self.assertEqual(row.status, ImportRow.Status.SKIPPED)
