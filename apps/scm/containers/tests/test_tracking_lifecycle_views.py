"""Start and Stop tracking, from the container list and from the Container Workspace.

Two surfaces, one endpoint pair, one service. What is asserted here is the boundary and
the wiring, not the lifecycle rules themselves — those are
``tracking/tests/test_tracking_lifecycle.py``:

    a member cannot start or stop, however the page was rendered for them
    another team's container is not reachable at all
    GET never changes tracking state
    both surfaces reach the same service and get their own HTML back

No provider is ever called: every test either starts a container that already has a
watch, or asserts that a refused request changed nothing.
"""

from unittest import mock

from django.test import Client, TestCase, override_settings

from apps.scm.containers.models import Container, EquipmentType
from apps.scm.containers.utils import calculate_check_digit
from apps.scm.tracking.manual_refresh import get_or_create_container_subscription
from apps.scm.tracking.models import CarrierSource, TrackingSubscription
from apps.teams.models import Team
from apps.teams.roles import ROLE_ADMIN, ROLE_MEMBER
from apps.users.models import CustomUser

_LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache", "LOCATION": "lifecycle-views"}}


def _user(email: str) -> CustomUser:
    return CustomUser.objects.create(username=email, email=email)


def _equipment_type() -> EquipmentType:
    return EquipmentType.objects.get_or_create(
        iso_code="22G1",
        defaults={"category": "GP", "length_ft": 20, "high_cube": False, "description": "20' GP"},
    )[0]


def _container(team, serial="123456") -> Container:
    return Container.objects.create(
        team=team,
        owner_code="MRK",
        category_id="U",
        serial_number=serial,
        check_digit=calculate_check_digit("MRK", "U", serial),
        equipment_type=_equipment_type(),
    )


def _watch(team, container, *, status=TrackingSubscription.Status.ACTIVE) -> TrackingSubscription:
    """A verified Maersk watch, which is what a started container looks like."""
    subscription = get_or_create_container_subscription(
        team=team,
        container=container,
        provider_code="maersk",
        provider_name="Maersk",
        carrier_code="maersk",
        carrier_name="Maersk",
        carrier_source=CarrierSource.DIRECT_API,
    )
    if subscription.status != status:
        subscription.status = status
        subscription.save(update_fields=["status"])
    return subscription


@override_settings(CACHES=_LOCMEM)
class LifecycleViewBase(TestCase):
    def setUp(self):
        self.team = Team.objects.create(name="MCR", slug="mcr-lifecycle")
        self.admin = _user("admin@lifecycle-views.test")
        self.member = _user("member@lifecycle-views.test")
        self.team.members.add(self.admin, through_defaults={"role": ROLE_ADMIN})
        self.team.members.add(self.member, through_defaults={"role": ROLE_MEMBER})
        self.container = _container(self.team)
        self.start_url = f"/scm/containers/{self.container.pk}/tracking/start/"
        self.stop_url = f"/scm/containers/{self.container.pk}/tracking/stop/"
        self.client = Client()
        self.client.force_login(self.admin)

    def as_member(self) -> Client:
        client = Client()
        client.force_login(self.member)
        return client


class StopFromTheContainerListTest(LifecycleViewBase):
    """The list's Stop action, which re-renders the row it was pressed in."""

    def setUp(self):
        super().setUp()
        self.subscription = _watch(self.team, self.container)

    def test_an_admin_can_stop_tracking_from_the_list(self):
        response = self.client.post(self.stop_url, {"origin": "row"}, headers={"hx-request": "true"})

        self.assertEqual(response.status_code, 200)
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.status, TrackingSubscription.Status.CANCELLED)

    def test_the_row_comes_back_offering_start(self):
        response = self.client.post(self.stop_url, {"origin": "row"}, headers={"hx-request": "true"})

        self.assertContains(response, f"container-row-{self.container.pk}")
        self.assertContains(response, "Not tracking")
        self.assertContains(response, self.start_url)
        self.assertNotContains(response, self.stop_url)

    def test_the_row_offers_stop_while_the_container_is_tracked(self):
        response = self.client.post(self.start_url, {"origin": "row"}, headers={"hx-request": "true"})

        self.assertContains(response, "Tracking")
        self.assertContains(response, self.stop_url)

    def test_a_member_cannot_stop_from_the_list(self):
        response = self.as_member().post(self.stop_url, {"origin": "row"}, headers={"hx-request": "true"})

        self.assertEqual(response.status_code, 404)
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.status, TrackingSubscription.Status.ACTIVE)

    def test_the_list_shows_the_action_to_an_admin_only(self):
        admin_page = self.client.get("/scm/containers/")
        member_page = self.as_member().get("/scm/containers/")

        self.assertTrue(admin_page.context["can_manage_tracking"])
        self.assertContains(admin_page, self.stop_url)
        self.assertFalse(member_page.context["can_manage_tracking"])
        self.assertNotContains(member_page, self.stop_url)
        self.assertNotContains(member_page, self.start_url)

    def test_the_list_reads_tracked_from_the_live_watch_rule(self):
        """A cancelled watch reads as not tracking; a failing one still reads as tracked."""
        response = self.client.get("/scm/containers/")
        row = next(container for container in response.context["containers"] if container.pk == self.container.pk)
        self.assertTrue(row.tracking_live)

        self.subscription.status = TrackingSubscription.Status.CANCELLED
        self.subscription.save(update_fields=["status"])
        response = self.client.get("/scm/containers/")
        row = next(container for container in response.context["containers"] if container.pk == self.container.pk)
        self.assertFalse(row.tracking_live)

        self.subscription.status = TrackingSubscription.Status.FAILED
        self.subscription.save(update_fields=["status"])
        response = self.client.get("/scm/containers/")
        row = next(container for container in response.context["containers"] if container.pk == self.container.pk)
        self.assertTrue(row.tracking_live)


class StopFromTheWorkspaceTest(LifecycleViewBase):
    """The workspace's Stop action, which re-renders the tracking panel."""

    def setUp(self):
        super().setUp()
        self.subscription = _watch(self.team, self.container)

    def test_an_admin_can_stop_tracking_from_the_workspace(self):
        response = self.client.post(self.stop_url, headers={"hx-request": "true"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "container-tracking-panel")
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.status, TrackingSubscription.Status.CANCELLED)

    def test_the_panel_comes_back_with_the_outcome_and_the_start_action(self):
        response = self.client.post(self.stop_url, headers={"hx-request": "true"})

        self.assertContains(response, "Tracking stopped")
        self.assertContains(response, self.start_url)

    def test_the_workspace_offers_stop_to_an_admin_and_nothing_to_a_member(self):
        admin_page = self.client.get(f"/scm/containers/{self.container.pk}/")
        member_page = self.as_member().get(f"/scm/containers/{self.container.pk}/")

        self.assertContains(admin_page, self.stop_url)
        self.assertNotContains(member_page, self.stop_url)
        self.assertNotContains(member_page, self.start_url)

    def test_a_member_cannot_stop_from_the_workspace(self):
        response = self.as_member().post(self.stop_url, headers={"hx-request": "true"})

        self.assertEqual(response.status_code, 404)
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.status, TrackingSubscription.Status.ACTIVE)

    def test_a_non_htmx_stop_redirects_with_a_message(self):
        response = self.client.post(self.stop_url)

        self.assertRedirects(response, f"/scm/containers/{self.container.pk}/", fetch_redirect_response=False)
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.status, TrackingSubscription.Status.CANCELLED)

    def test_a_stopped_container_reports_when_it_was_stopped(self):
        self.client.post(self.stop_url)

        response = self.client.get(f"/scm/containers/{self.container.pk}/")

        self.assertIsNotNone(response.context["workspace"].tracking_stopped_at)
        self.assertContains(response, "Tracking stopped")


class StartFromBothSurfacesTest(LifecycleViewBase):
    """Start reaches the same service from both surfaces, and is admin-only on both."""

    def setUp(self):
        super().setUp()
        self.subscription = _watch(self.team, self.container, status=TrackingSubscription.Status.CANCELLED)

    def test_an_admin_can_start_from_the_list(self):
        response = self.client.post(self.start_url, {"origin": "row"}, headers={"hx-request": "true"})

        self.assertEqual(response.status_code, 200)
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.status, TrackingSubscription.Status.ACTIVE)

    def test_an_admin_can_start_from_the_workspace(self):
        response = self.client.post(self.start_url, headers={"hx-request": "true"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "container-tracking-panel")
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.status, TrackingSubscription.Status.ACTIVE)

    def test_a_member_cannot_start(self):
        for url_kwargs in ({"origin": "row"}, {}):
            with self.subTest(origin=url_kwargs):
                response = self.as_member().post(self.start_url, url_kwargs, headers={"hx-request": "true"})
                self.assertEqual(response.status_code, 404)
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.status, TrackingSubscription.Status.CANCELLED)

    def test_both_surfaces_call_the_one_lifecycle_service(self):
        """No second Start implementation: the two views are one call each."""
        with mock.patch("apps.scm.containers.tracking_lifecycle_views.start_container_tracking") as start:
            start.return_value = mock.Mock(level="info", message="ok", tracked=False, state="x")
            self.client.post(self.start_url, {"origin": "row"}, headers={"hx-request": "true"})
            self.client.post(self.start_url, headers={"hx-request": "true"})

        self.assertEqual(start.call_count, 2)
        for call in start.call_args_list:
            self.assertEqual(call.kwargs["container"], self.container)
            self.assertEqual(call.kwargs["team"], self.team)
            self.assertEqual(call.kwargs["actor"], self.admin)


class LifecycleEndpointSafetyTest(LifecycleViewBase):
    """The two rules a state-changing endpoint has to keep whatever the page did."""

    def setUp(self):
        super().setUp()
        self.subscription = _watch(self.team, self.container)

    def test_a_get_never_changes_tracking_state(self):
        for url in (self.start_url, self.stop_url):
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 405)
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.status, TrackingSubscription.Status.ACTIVE)

    def test_an_anonymous_request_is_sent_to_the_login_page(self):
        response = Client().post(self.stop_url)

        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response["Location"])
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.status, TrackingSubscription.Status.ACTIVE)


class LifecycleTenantIsolationTest(LifecycleViewBase):
    """A container id from another tenant is not a way in, even for an administrator."""

    def setUp(self):
        super().setUp()
        self.subscription = _watch(self.team, self.container)
        self.other_team = Team.objects.create(name="Theirs", slug="theirs-lifecycle")
        self.other_admin = _user("admin@theirs.test")
        self.other_team.members.add(self.other_admin, through_defaults={"role": ROLE_ADMIN})
        self.other_container = _container(self.other_team, serial="654321")
        self.other_subscription = _watch(self.other_team, self.other_container)

    def test_stopping_another_teams_container_is_a_404(self):
        response = self.client.post(f"/scm/containers/{self.other_container.pk}/tracking/stop/")

        self.assertEqual(response.status_code, 404)
        self.other_subscription.refresh_from_db()
        self.assertEqual(self.other_subscription.status, TrackingSubscription.Status.ACTIVE)

    def test_starting_another_teams_container_is_a_404(self):
        self.other_subscription.status = TrackingSubscription.Status.CANCELLED
        self.other_subscription.save(update_fields=["status"])

        response = self.client.post(f"/scm/containers/{self.other_container.pk}/tracking/start/")

        self.assertEqual(response.status_code, 404)
        self.other_subscription.refresh_from_db()
        self.assertEqual(self.other_subscription.status, TrackingSubscription.Status.CANCELLED)

    def test_the_other_teams_admin_is_unaffected_by_ours(self):
        """The isolation is mutual, and it is the container's team that decides."""
        client = Client()
        client.force_login(self.other_admin)

        response = client.post(self.stop_url)

        self.assertEqual(response.status_code, 404)
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.status, TrackingSubscription.Status.ACTIVE)
