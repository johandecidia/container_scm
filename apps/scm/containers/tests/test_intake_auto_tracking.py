"""Automatic tracking for containers an import creates.

Three things decide what happens, and they are tested separately because they fail
separately:

    *which* containers      only the ones this run created, from the persist result
    *whether*               the team's setting, unless this import overrode it
    *what if it fails*      the import stays successful and the failure stays visible

The last one is the reason the wiring is shaped the way it is. Tracking is downstream of
persistence: the containers are saved, and no provider outage may take that back.
"""

from unittest import mock

from django.test import Client, TestCase, override_settings

from apps.scm.containers.intake import bulk_create_containers
from apps.scm.containers.models import Container, EquipmentType
from apps.scm.containers.utils import calculate_check_digit
from apps.scm.tracking.lifecycle import auto_start_tracking_for_containers
from apps.scm.tracking.manual_refresh import (
    NOT_CONFIGURED,
    UNAVAILABLE,
    WARNING,
    RefreshResult,
    get_or_create_container_subscription,
)
from apps.scm.tracking.models import CarrierSource, TrackingSubscription
from apps.scm.tracking.preferences import set_team_auto_start_tracking
from apps.teams.models import Team
from apps.teams.roles import ROLE_ADMIN
from apps.users.models import CustomUser

_LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache", "LOCATION": "intake-tracking"}}

# Three valid ISO numbers, so a preview classifies them rather than rejecting them.
EXISTING = "ABCU1234560"
NEW_ONE = "ONEU7654329"
NEW_TWO = "MSCU1111113"


def _equipment_type() -> EquipmentType:
    return EquipmentType.objects.get_or_create(
        iso_code="22G1",
        defaults={"category": "GP", "length_ft": 20, "high_cube": False, "description": "20' GP"},
    )[0]


def _number(owner: str, serial: str) -> str:
    return f"{owner}U{serial}{calculate_check_digit(owner, 'U', serial)}"


def _container(team, owner="ABC", serial="123456") -> Container:
    return Container.objects.create(
        team=team,
        owner_code=owner,
        category_id="U",
        serial_number=serial,
        check_digit=calculate_check_digit(owner, "U", serial),
        equipment_type=_equipment_type(),
    )


def _started(container):
    """What a successful start looks like, without calling a provider to get one."""
    get_or_create_container_subscription(
        team=container.team,
        container=container,
        provider_code="maersk",
        provider_name="Maersk",
        carrier_code="maersk",
        carrier_name="Maersk",
        carrier_source=CarrierSource.DIRECT_API,
    )
    return RefreshResult(level="success", message="Tracking updated.", tracked=True)


def _unreachable(container):  # noqa: ARG001 — a failure that names no container
    return RefreshResult(
        level=WARNING,
        state=UNAVAILABLE,
        message="Maersk could not be reached.",
        tracked=False,
    )


class _SpyStart:
    """Stands in for start_container_tracking and records which boxes it was given."""

    def __init__(self, behaviour=_started):
        self.behaviour = behaviour
        self.containers: list[str] = []

    def __call__(self, *, team, container, actor=None):  # noqa: ARG002 — signature match
        self.containers.append(container.container_id)
        return self.behaviour(container)


def _patch_start(spy):
    return mock.patch("apps.scm.tracking.lifecycle.start_container_tracking", spy)


# ---------------------------------------------------------------------------
# The service
# ---------------------------------------------------------------------------


@override_settings(CACHES=_LOCMEM)
class AutoStartServiceTest(TestCase):
    """The team setting decides, unless the caller overrode it."""

    def setUp(self):
        self.team = Team.objects.create(name="auto", slug="auto-start-service")
        self.container = _container(self.team)

    def _run(self, *, enabled=None, spy=None):
        spy = spy or _SpyStart()
        with _patch_start(spy):
            summary = auto_start_tracking_for_containers(
                team=self.team,
                containers=[self.container],
                enabled=enabled,
            )
        return summary, spy

    def test_the_team_default_is_off_so_nothing_is_started(self):
        summary, spy = self._run()

        self.assertFalse(summary.enabled)
        self.assertFalse(summary.attempted)
        self.assertEqual(spy.containers, [])

    def test_the_team_default_on_starts_the_container(self):
        set_team_auto_start_tracking(self.team, True)

        summary, spy = self._run()

        self.assertTrue(summary.enabled)
        self.assertEqual(summary.started, 1)
        self.assertEqual(spy.containers, [self.container.container_id])

    def test_an_override_on_beats_a_team_default_of_off(self):
        summary, spy = self._run(enabled=True)

        self.assertTrue(summary.enabled)
        self.assertEqual(summary.started, 1)
        self.assertEqual(spy.containers, [self.container.container_id])

    def test_an_override_off_beats_a_team_default_of_on(self):
        set_team_auto_start_tracking(self.team, True)

        summary, spy = self._run(enabled=False)

        self.assertFalse(summary.enabled)
        self.assertEqual(spy.containers, [])

    def test_a_container_that_is_already_tracked_is_not_started_again(self):
        set_team_auto_start_tracking(self.team, True)
        spy = _SpyStart(
            behaviour=lambda container: RefreshResult(  # noqa: ARG005
                level="info",
                state="tracking_already_active",
                message="Already tracked.",
                tracked=True,
            )
        )

        summary, _spy = self._run(spy=spy)

        self.assertEqual(summary.already_tracked, 1)
        self.assertEqual(summary.started, 0)

    def test_a_provider_that_cannot_be_reached_is_recorded_as_a_failure(self):
        set_team_auto_start_tracking(self.team, True)

        summary, _spy = self._run(spy=_SpyStart(behaviour=_unreachable))

        self.assertEqual(summary.failure_count, 1)
        self.assertEqual(summary.started, 0)
        number, reason = summary.failed[0]
        self.assertEqual(number, self.container.container_id)
        self.assertIn("could not be reached", reason)

    def test_one_containers_failure_does_not_cost_the_next_one_its_tracking(self):
        set_team_auto_start_tracking(self.team, True)
        second = _container(self.team, owner="MSC", serial="111111")

        def _first_fails(container):
            if container.pk == self.container.pk:
                return _unreachable(container)
            return _started(container)

        spy = _SpyStart(behaviour=_first_fails)
        with _patch_start(spy):
            summary = auto_start_tracking_for_containers(
                team=self.team,
                containers=[self.container, second],
            )

        self.assertEqual(summary.started, 1)
        self.assertEqual(summary.failure_count, 1)
        self.assertEqual(len(spy.containers), 2)

    def test_an_unexpected_error_is_logged_and_does_not_stop_the_batch(self):
        """A bug in the lifecycle must not discard the rest, or the import's success."""
        set_team_auto_start_tracking(self.team, True)
        second = _container(self.team, owner="MSC", serial="111111")

        def _explodes(container):
            if container.pk == self.container.pk:
                raise RuntimeError("bug")
            return _started(container)

        spy = _SpyStart(behaviour=_explodes)
        with _patch_start(spy), self.assertLogs("apps.scm.tracking.lifecycle", level="ERROR") as logs:
            summary = auto_start_tracking_for_containers(
                team=self.team,
                containers=[self.container, second],
            )

        self.assertEqual(summary.failure_count, 1)
        self.assertEqual(summary.started, 1)
        self.assertTrue(any("RuntimeError" in line for line in logs.output))


# ---------------------------------------------------------------------------
# The intake, end to end
# ---------------------------------------------------------------------------


@override_settings(CACHES=_LOCMEM)
class IntakeCreatedStateTest(TestCase):
    """The persist result is what says which containers are new."""

    def setUp(self):
        self.team = Team.objects.create(name="created", slug="created-state")
        self.user = CustomUser.objects.create(username="i@created.test", email="i@created.test")
        self.existing = _container(self.team, owner="ABC", serial="123456")

    def test_the_result_separates_the_containers_it_created(self):
        result = bulk_create_containers(
            team=self.team,
            user=self.user,
            entries=[(EXISTING, ""), (NEW_ONE, ""), (NEW_TWO, "")],
        )

        self.assertEqual(sorted(result.created), sorted([NEW_ONE, NEW_TWO]))
        self.assertEqual(result.existed, [EXISTING])
        self.assertEqual(
            sorted(container.container_id for container in result.created_containers),
            sorted([NEW_ONE, NEW_TWO]),
        )
        self.assertNotIn(self.existing, result.created_containers)

    def test_re_importing_the_same_file_creates_nothing_the_second_time(self):
        bulk_create_containers(team=self.team, user=self.user, entries=[(NEW_ONE, "")])

        again = bulk_create_containers(team=self.team, user=self.user, entries=[(NEW_ONE, "")])

        self.assertEqual(again.created_containers, [])
        self.assertEqual(again.existed, [NEW_ONE])


@override_settings(CACHES=_LOCMEM)
class IntakeImportTrackingTest(TestCase):
    """The paste/CSV import, from the checkbox to the subscriptions."""

    def setUp(self):
        self.team = Team.objects.create(name="MCR", slug="mcr-intake-tracking")
        self.user = CustomUser.objects.create(username="admin@intake.test", email="admin@intake.test")
        self.team.members.add(self.user, through_defaults={"role": ROLE_ADMIN})
        self.existing = _container(self.team, owner="ABC", serial="123456")
        self.client = Client()
        self.client.force_login(self.user)

    def _confirm(self, *, start_tracking=None, numbers=(EXISTING, NEW_ONE, NEW_TWO)):
        import json

        data = {
            "entries": json.dumps([[number, ""] for number in numbers]),
            "tab": "paste",
        }
        if start_tracking is not None:
            data["start_tracking"] = start_tracking
        spy = _SpyStart()
        with _patch_start(spy):
            response = self.client.post("/scm/containers/import/confirm/", data)
        return response, spy

    def test_the_checkbox_starts_from_the_team_setting(self):
        response = self.client.get("/scm/containers/import/paste/")
        self.assertFalse(response.context["form"].fields["start_tracking"].initial)

        set_team_auto_start_tracking(self.team, True)

        response = self.client.get("/scm/containers/import/paste/")
        self.assertTrue(response.context["form"].fields["start_tracking"].initial)

    def test_the_choice_travels_through_the_preview_as_an_explicit_flag(self):
        set_team_auto_start_tracking(self.team, True)

        response = self.client.post(
            "/scm/containers/import/paste/",
            {"numbers": f"{NEW_ONE}\n{NEW_TWO}", "start_tracking": "on"},
        )

        self.assertContains(response, 'name="start_tracking" value="1"')

    def test_an_unticked_box_travels_as_an_explicit_off(self):
        """Not as an absent field, which would read back as "the team default decides"."""
        set_team_auto_start_tracking(self.team, True)

        response = self.client.post("/scm/containers/import/paste/", {"numbers": NEW_ONE})

        self.assertContains(response, 'name="start_tracking" value="0"')

    def test_only_the_newly_created_containers_are_tracked(self):
        _response, spy = self._confirm(start_tracking="1")

        self.assertEqual(sorted(spy.containers), sorted([NEW_ONE, NEW_TWO]))
        self.assertNotIn(self.existing.container_id, spy.containers)

    def test_an_existing_container_is_not_tracked_for_appearing_again(self):
        """The case the whole created-state distinction exists for."""
        _response, spy = self._confirm(start_tracking="1", numbers=(EXISTING,))

        self.assertEqual(spy.containers, [])

    def test_team_off_and_no_override_tracks_nothing(self):
        _response, spy = self._confirm()

        self.assertEqual(spy.containers, [])

    def test_team_on_and_no_override_tracks_the_new_containers(self):
        set_team_auto_start_tracking(self.team, True)

        _response, spy = self._confirm()

        self.assertEqual(sorted(spy.containers), sorted([NEW_ONE, NEW_TWO]))

    def test_team_off_and_the_import_asking_for_it_tracks_them(self):
        _response, spy = self._confirm(start_tracking="1")

        self.assertEqual(sorted(spy.containers), sorted([NEW_ONE, NEW_TWO]))

    def test_team_on_and_the_import_declining_tracks_nothing(self):
        set_team_auto_start_tracking(self.team, True)

        _response, spy = self._confirm(start_tracking="0")

        self.assertEqual(spy.containers, [])

    def test_the_import_summary_says_tracking_started(self):
        response, _spy = self._confirm(start_tracking="1")

        self.assertContains(response, "Tracking started for 2 new containers")

    def test_a_single_add_tracks_only_a_container_it_created(self):
        spy = _SpyStart()
        with _patch_start(spy):
            self.client.post(
                "/scm/containers/create/",
                {"container_number": NEW_ONE, "start_tracking": "on"},
            )
        self.assertEqual(spy.containers, [NEW_ONE])

        spy = _SpyStart()
        with _patch_start(spy):
            self.client.post(
                "/scm/containers/create/",
                {"container_number": NEW_ONE, "start_tracking": "on"},
            )
        self.assertEqual(spy.containers, [])


@override_settings(CACHES=_LOCMEM)
class ImportFailureIsolationTest(TestCase):
    """A provider failure costs the tracking and nothing else."""

    def setUp(self):
        self.team = Team.objects.create(name="MCR", slug="mcr-isolation")
        self.user = CustomUser.objects.create(username="admin@isolation.test", email="admin@isolation.test")
        self.team.members.add(self.user, through_defaults={"role": ROLE_ADMIN})
        # An intake needs a default equipment type to fall back on, or every row is
        # rejected before it reaches the writes and there is nothing to isolate.
        _equipment_type()
        self.client = Client()
        self.client.force_login(self.user)

    def _confirm_with(self, behaviour):
        import json

        spy = _SpyStart(behaviour=behaviour)
        with _patch_start(spy):
            response = self.client.post(
                "/scm/containers/import/confirm/",
                {
                    "entries": json.dumps([[NEW_ONE, ""], [NEW_TWO, ""]]),
                    "tab": "paste",
                    "start_tracking": "1",
                },
            )
        return response

    def test_the_containers_are_imported_even_though_tracking_failed(self):
        self._confirm_with(_unreachable)

        self.assertTrue(Container.objects.filter(team=self.team, owner_code="ONE").exists())
        self.assertTrue(Container.objects.filter(team=self.team, owner_code="MSC").exists())

    def test_the_import_still_reports_success(self):
        response = self._confirm_with(_unreachable)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Import finished.")
        self.assertEqual(response.context["result"].created_count, 2)

    def test_tracking_is_not_falsely_marked_active(self):
        self._confirm_with(_unreachable)

        self.assertFalse(TrackingSubscription.objects.filter(team=self.team).exists())

    def test_the_failure_is_visible_on_the_summary(self):
        response = self._confirm_with(_unreachable)

        self.assertContains(response, "tracking could not be started")
        self.assertContains(response, NEW_ONE)
        self.assertContains(response, NEW_TWO)

    def test_a_carrier_that_answered_nobody_is_reported_the_same_way(self):
        def _no_carrier(container):  # noqa: ARG001
            return RefreshResult(
                level="info",
                state=NOT_CONFIGURED,
                message="No carrier could be established for this container.",
                tracked=False,
            )

        response = self._confirm_with(_no_carrier)

        self.assertContains(response, "tracking could not be started")
        self.assertEqual(response.context["result"].created_count, 2)
        self.assertFalse(TrackingSubscription.objects.filter(team=self.team).exists())

    def test_an_unexpected_error_still_leaves_the_import_successful(self):
        def _explodes(container):  # noqa: ARG001
            raise RuntimeError("bug in the lifecycle")

        with self.assertLogs("apps.scm.tracking.lifecycle", level="ERROR"):
            response = self._confirm_with(_explodes)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["result"].created_count, 2)
        self.assertContains(response, "tracking could not be started")
