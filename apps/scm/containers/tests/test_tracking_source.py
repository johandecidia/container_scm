"""The Container Workspace's "Tracking via" control.

What matters here is the boundary, not the rendering: a member cannot change a
container's tracking source even though they can read the workspace, another team's
container is not reachable at all, and a choice that is not on the allow-list is
refused rather than stored.

The switch itself is also pinned down: changing to an explicit provider pauses the
watch it supersedes — so the scheduled sync stops polling two providers for one
leg — while leaving that provider's events in place.
"""

from unittest import mock

from django.test import Client, TestCase, override_settings

from apps.scm.containers.models import Container, EquipmentType
from apps.scm.containers.utils import calculate_check_digit
from apps.scm.integrations.services import connect_carrier_integration
from apps.scm.integrations.traqo import PROVIDER_CODE as TRAQO_PROVIDER_CODE
from apps.scm.tracking.manual_refresh import get_or_create_container_subscription
from apps.scm.tracking.models import CarrierSource, TrackingSubscription
from apps.scm.tracking.source_switch import apply_container_tracking_source
from apps.teams.models import Team
from apps.teams.roles import ROLE_ADMIN, ROLE_MEMBER
from apps.users.models import CustomUser

TRAQO_LIVE = {"TRAQO_ENABLED": True, "TRAQO_API_KEY": "switch-key"}


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


@override_settings(**TRAQO_LIVE)
class TrackingSourceViewTest(TestCase):
    def setUp(self):
        self.team = Team.objects.create(name="MCR", slug="mcr-source")
        self.admin = _user("admin@mcr.test")
        self.member = _user("member@mcr.test")
        self.team.members.add(self.admin, through_defaults={"role": ROLE_ADMIN})
        self.team.members.add(self.member, through_defaults={"role": ROLE_MEMBER})
        self.container = _container(self.team)
        self.url = f"/scm/containers/{self.container.pk}/tracking-source/"
        self.client = Client()
        self.client.force_login(self.admin)

    def test_a_member_cannot_change_the_tracking_source(self):
        client = Client()
        client.force_login(self.member)
        response = client.post(self.url, {"provider": TRAQO_PROVIDER_CODE})
        self.assertEqual(response.status_code, 404)
        self.container.refresh_from_db()
        self.assertEqual(self.container.tracking_provider_override, "")

    def test_a_member_still_sees_the_workspace_without_the_selector(self):
        client = Client()
        client.force_login(self.member)
        response = client.get(f"/scm/containers/{self.container.pk}/")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context["can_manage_tracking_source"])
        self.assertEqual(response.context["tracking_provider_options"], [])

    def test_an_admin_sees_the_selector_with_the_team_default_first(self):
        response = self.client.get(f"/scm/containers/{self.container.pk}/")
        options = response.context["tracking_provider_options"]
        self.assertTrue(response.context["can_manage_tracking_source"])
        self.assertEqual(options[0].value, "")
        self.assertTrue(any(o.value == TRAQO_PROVIDER_CODE for o in options))

    def test_another_teams_container_is_not_reachable(self):
        other_team = Team.objects.create(name="Theirs", slug="theirs-source")
        other_container = _container(other_team, serial="654321")
        response = self.client.post(
            f"/scm/containers/{other_container.pk}/tracking-source/", {"provider": TRAQO_PROVIDER_CODE}
        )
        self.assertEqual(response.status_code, 404)
        other_container.refresh_from_db()
        self.assertEqual(other_container.tracking_provider_override, "")

    def test_choosing_traqo_stores_the_override(self):
        self.client.post(self.url, {"provider": TRAQO_PROVIDER_CODE})
        self.container.refresh_from_db()
        self.assertEqual(self.container.tracking_provider_override, TRAQO_PROVIDER_CODE)

    def test_a_provider_that_is_not_on_the_allow_list_is_refused(self):
        """Maersk is connected but is not this container's carrier, so it is not offered."""
        connect_carrier_integration(self.team, "maersk", {"api_key": "k"})
        self.client.post(self.url, {"provider": "maersk"})
        self.container.refresh_from_db()
        self.assertEqual(self.container.tracking_provider_override, "")

    def test_an_invalid_choice_is_reported_in_the_swapped_panel(self):
        connect_carrier_integration(self.team, "maersk", {"api_key": "k"})
        response = self.client.post(self.url, {"provider": "maersk"}, headers={"hx-request": "true"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "not an available tracking source")

    def test_clearing_the_override_returns_the_container_to_the_team_default(self):
        self.client.post(self.url, {"provider": TRAQO_PROVIDER_CODE})
        self.client.post(self.url, {"provider": ""})
        self.container.refresh_from_db()
        self.assertEqual(self.container.tracking_provider_override, "")


@override_settings(**TRAQO_LIVE)
class SourceSwitchTest(TestCase):
    """What changing the preference actually does to the container's watches."""

    def setUp(self):
        self.team = Team.objects.create(name="MCR", slug="mcr-switch")
        self.container = _container(self.team)

    def _subscription(self, provider_code, carrier_code) -> TrackingSubscription:
        return get_or_create_container_subscription(
            team=self.team,
            container=self.container,
            provider_code=provider_code,
            provider_name=provider_code,
            carrier_code=carrier_code,
            carrier_name=carrier_code,
            carrier_source=CarrierSource.DIRECT_API,
        )

    def test_a_container_with_no_known_carrier_keeps_the_preference_and_says_so(self):
        """Nothing is fetched to work out the carrier — that would spend a request."""
        result = apply_container_tracking_source(team=self.team, container=self.container)
        self.assertEqual(result.state, "not_configured")
        self.assertIn("carrier", str(result.message))

    def test_switching_providers_pauses_the_watch_it_supersedes(self):
        traqo_watch = self._subscription(TRAQO_PROVIDER_CODE, "maersk")
        connect_carrier_integration(self.team, "maersk", {"api_key": "k"})
        self.container.tracking_provider_override = "maersk"
        self.container.save(update_fields=["tracking_provider_override"])

        # The activation's own fetch is not what is under test; the subscription
        # bookkeeping around it is.
        with mock.patch("apps.scm.tracking.sync.sync_tracking_subscription", return_value=None):
            apply_container_tracking_source(team=self.team, container=self.container)

        traqo_watch.refresh_from_db()
        maersk_watch = TrackingSubscription.objects.get(
            team=self.team, container=self.container, provider__code="maersk"
        )
        self.assertEqual(traqo_watch.status, TrackingSubscription.Status.PAUSED)
        self.assertEqual(maersk_watch.status, TrackingSubscription.Status.ACTIVE)

    def test_a_paused_watch_is_no_longer_polled_but_is_still_a_verified_source(self):
        from apps.scm.tracking.selectors import (
            get_due_tracking_subscriptions,
            get_verified_container_subscriptions,
        )

        watch = self._subscription(TRAQO_PROVIDER_CODE, "maersk")
        watch.status = TrackingSubscription.Status.PAUSED
        watch.save(update_fields=["status"])

        self.assertNotIn(watch, list(get_due_tracking_subscriptions(team=self.team)))
        self.assertIn(watch, get_verified_container_subscriptions(self.team, self.container))

    def test_staying_on_the_team_default_pauses_nothing(self):
        watch = self._subscription(TRAQO_PROVIDER_CODE, "maersk")
        with mock.patch("apps.scm.tracking.sync.sync_tracking_subscription", return_value=None):
            apply_container_tracking_source(team=self.team, container=self.container)
        watch.refresh_from_db()
        self.assertEqual(watch.status, TrackingSubscription.Status.ACTIVE)
