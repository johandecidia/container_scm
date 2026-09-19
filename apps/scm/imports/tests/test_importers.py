"""Tests for the import confirmation / importer logic."""

from unittest import mock

from django.test import TestCase, override_settings

from apps.scm.containers.models import Container
from apps.scm.imports.importers import run_import
from apps.scm.imports.models import ImportJob, ImportRow
from apps.scm.imports.services import confirm_import_job
from apps.scm.tracking.manual_refresh import RefreshResult
from apps.scm.tracking.models import TrackingSubscription
from apps.scm.tracking.preferences import set_team_auto_start_tracking

from .helpers import (
    CAT,
    CHECK,
    OWNER,
    SERIAL,
    make_equipment_type,
    make_import_job,
    make_parsed_job,
    make_team,
    make_user,
)


class RunImportTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = make_team(slug="imp-team")
        cls.user = make_user("imp@example.com")
        cls.team.members.add(cls.user)
        cls.et = make_equipment_type()

    def test_valid_row_creates_container(self):
        job = make_parsed_job(self.team, self.user)
        run_import(job)
        self.assertTrue(Container.objects.filter(team=self.team, owner_code=OWNER, serial_number=SERIAL).exists())

    def test_invalid_row_not_imported(self):
        job = make_import_job(self.team, self.user)
        ImportRow.objects.create(
            import_job=job,
            row_number=1,
            validated_data={"container_number": "INVALID"},
            status=ImportRow.Status.INVALID,
        )
        job.status = ImportJob.Status.VALIDATED
        job.save()
        run_import(job)
        self.assertFalse(Container.objects.filter(team=self.team).exists())

    def test_duplicate_creates_skipped_row(self):
        Container.objects.create(
            team=self.team,
            owner_code=OWNER,
            category_id=CAT,
            serial_number=SERIAL,
            check_digit=CHECK,
            equipment_type=self.et,
        )
        job = make_parsed_job(self.team, self.user)
        run_import(job)
        row = job.rows.first()
        self.assertEqual(row.status, ImportRow.Status.SKIPPED)

    def test_import_job_status_completed(self):
        job = make_parsed_job(self.team, self.user)
        run_import(job)
        job.refresh_from_db()
        self.assertEqual(job.status, ImportJob.Status.COMPLETED)

    def test_imported_row_status_set(self):
        job = make_parsed_job(self.team, self.user)
        run_import(job)
        row = job.rows.first()
        self.assertEqual(row.status, ImportRow.Status.IMPORTED)

    def test_update_existing_updates_container(self):
        Container.objects.create(
            team=self.team,
            owner_code=OWNER,
            category_id=CAT,
            serial_number=SERIAL,
            check_digit=CHECK,
            equipment_type=self.et,
            location_text="Old Location",
        )
        job = make_parsed_job(self.team, self.user)
        # Add current_location (text) to validated data
        row = job.rows.first()
        data = dict(row.validated_data)
        data["current_location"] = "New Location"  # maps to location_text in importer
        row.validated_data = data
        row.save()
        run_import(job, update_existing=True)
        row.refresh_from_db()
        self.assertEqual(row.status, ImportRow.Status.IMPORTED)

    def test_run_import_reports_the_containers_it_created(self):
        """The created-state the tracking auto-start acts on, from the persist result."""
        job = make_parsed_job(self.team, self.user)

        created = run_import(job)

        self.assertEqual([container.serial_number for container in created], [SERIAL])

    def test_a_container_that_already_existed_is_not_reported_as_created(self):
        Container.objects.create(
            team=self.team,
            owner_code=OWNER,
            category_id=CAT,
            serial_number=SERIAL,
            check_digit=CHECK,
            equipment_type=self.et,
        )
        job = make_parsed_job(self.team, self.user)

        self.assertEqual(run_import(job, update_existing=True), [])


_LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache", "LOCATION": "import-tracking"}}


@override_settings(CACHES=_LOCMEM)
class ConfirmImportTrackingTest(TestCase):
    """Confirming a container import can start tracking what it created.

    The same lifecycle the Start button and the paste import use, applied to the
    containers this job created — and applied *after* ``run_import``, because that is
    atomic and a provider call inside it would roll a stored container back on a
    carrier's outage.
    """

    @classmethod
    def setUpTestData(cls):
        cls.team = make_team(slug="imp-tracking")
        cls.user = make_user("tracking@example.com")
        cls.team.members.add(cls.user)
        cls.et = make_equipment_type()

    def _confirm(self, *, start_tracking=None, behaviour=None):
        job = make_parsed_job(self.team, self.user)
        calls: list[str] = []

        def _start(*, team, container, actor=None):  # noqa: ARG001 — signature match
            calls.append(container.container_id)
            if behaviour is not None:
                return behaviour(container)
            return RefreshResult(level="success", message="Tracking updated.", tracked=True)

        with mock.patch("apps.scm.tracking.lifecycle.start_container_tracking", _start):
            confirm_import_job(job, start_tracking=start_tracking)
        return job, calls

    def test_the_team_default_off_tracks_nothing(self):
        _job, calls = self._confirm()

        self.assertEqual(calls, [])
        self.assertTrue(Container.objects.filter(team=self.team, serial_number=SERIAL).exists())

    def test_the_team_default_on_tracks_the_created_container(self):
        set_team_auto_start_tracking(self.team, True)

        _job, calls = self._confirm()

        self.assertEqual(len(calls), 1)

    def test_the_confirm_can_override_the_team_default_on(self):
        _job, calls = self._confirm(start_tracking=True)

        self.assertEqual(len(calls), 1)

    def test_the_confirm_can_override_the_team_default_off(self):
        set_team_auto_start_tracking(self.team, True)

        _job, calls = self._confirm(start_tracking=False)

        self.assertEqual(calls, [])

    def test_a_container_that_already_existed_is_not_tracked(self):
        Container.objects.create(
            team=self.team,
            owner_code=OWNER,
            category_id=CAT,
            serial_number=SERIAL,
            check_digit=CHECK,
            equipment_type=self.et,
        )
        set_team_auto_start_tracking(self.team, True)

        _job, calls = self._confirm()

        self.assertEqual(calls, [])

    def test_a_tracking_failure_leaves_the_import_completed(self):
        def _unreachable(container):  # noqa: ARG001
            return RefreshResult(level="warning", message="Maersk could not be reached.", tracked=False)

        job, calls = self._confirm(start_tracking=True, behaviour=_unreachable)

        self.assertEqual(len(calls), 1)
        job.refresh_from_db()
        self.assertEqual(job.status, ImportJob.Status.COMPLETED)
        self.assertEqual(job.rows.first().status, ImportRow.Status.IMPORTED)
        self.assertTrue(Container.objects.filter(team=self.team, serial_number=SERIAL).exists())
        self.assertFalse(TrackingSubscription.objects.filter(team=self.team).exists())

    def test_an_unexpected_tracking_error_leaves_the_import_completed(self):
        def _explodes(container):  # noqa: ARG001
            raise RuntimeError("bug in the lifecycle")

        with self.assertLogs("apps.scm.tracking.lifecycle", level="ERROR"):
            job, _calls = self._confirm(start_tracking=True, behaviour=_explodes)

        job.refresh_from_db()
        self.assertEqual(job.status, ImportJob.Status.COMPLETED)
        self.assertTrue(Container.objects.filter(team=self.team, serial_number=SERIAL).exists())
