"""Settings → Locations: stop-on-receive per location, admin-only, team-scoped."""

from django.test import Client, TestCase
from django.urls import reverse

from apps.scm.containers.forms import ContainerLocationForm
from apps.scm.containers.models import ContainerLocation
from apps.teams.models import Team
from apps.teams.roles import ROLE_ADMIN, ROLE_MEMBER
from apps.users.models import CustomUser


def _user(email: str) -> CustomUser:
    return CustomUser.objects.create_user(username=email, email=email, password="x")


def _location(team, name, **kwargs) -> ContainerLocation:
    return ContainerLocation.objects.create(team=team, name=name, location_type="terminal", **kwargs)


class LocationSettingsTest(TestCase):
    def setUp(self):
        self.team = Team.objects.create(name="MCR", slug="mcr-location-settings")
        self.admin = _user("admin@location-settings.test")
        self.member = _user("member@location-settings.test")
        self.team.members.add(self.admin, through_defaults={"role": ROLE_ADMIN})
        self.team.members.add(self.member, through_defaults={"role": ROLE_MEMBER})
        self.terminal = _location(self.team, "Oceanterminalen")
        self.depot = _location(self.team, "Conspect")
        self.client = Client()
        self.client.force_login(self.admin)

    def _toggle(self, location, value, client=None):
        url = reverse("team_settings:location_auto_stop", args=[location.pk])
        return (client or self.client).post(url, {"auto_stop_tracking_on_receive": value})

    def test_new_locations_start_with_it_off(self):
        self.assertFalse(self.terminal.auto_stop_tracking_on_receive)

    def test_admin_sees_every_location_with_its_status(self):
        response = self.client.get(reverse("team_settings:locations"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual({loc.name for loc in response.context["locations"]}, {"Oceanterminalen", "Conspect"})
        self.assertContains(response, reverse("team_settings:location_auto_stop", args=[self.terminal.pk]))

    def test_admin_switches_one_location_without_touching_the_other(self):
        self._toggle(self.terminal, "1")
        self.terminal.refresh_from_db()
        self.depot.refresh_from_db()
        self.assertTrue(self.terminal.auto_stop_tracking_on_receive)
        self.assertFalse(self.depot.auto_stop_tracking_on_receive)

        self._toggle(self.terminal, "0")
        self.terminal.refresh_from_db()
        self.assertFalse(self.terminal.auto_stop_tracking_on_receive)

    def test_member_cannot_open_or_change_it(self):
        client = Client()
        client.force_login(self.member)
        self.assertEqual(client.get(reverse("team_settings:locations")).status_code, 404)
        self.assertEqual(self._toggle(self.terminal, "1", client).status_code, 404)
        self.terminal.refresh_from_db()
        self.assertFalse(self.terminal.auto_stop_tracking_on_receive)

    def test_another_teams_location_is_not_reachable(self):
        other = Team.objects.create(name="Theirs", slug="theirs-location-settings")
        foreign = _location(other, "Their depot")
        self.assertEqual(self._toggle(foreign, "1").status_code, 404)
        foreign.refresh_from_db()
        self.assertFalse(foreign.auto_stop_tracking_on_receive)
        response = self.client.get(reverse("team_settings:locations"))
        self.assertNotIn(foreign, list(response.context["locations"]))

    def test_tracking_page_points_to_locations(self):
        response = self.client.get(reverse("team_settings:tracking"))
        self.assertContains(response, reverse("team_settings:locations"))
        self.assertNotIn("stop_tracking_on_receive", response.context)


class LocationFormAutoStopTest(TestCase):
    def setUp(self):
        self.team = Team.objects.create(name="MCR", slug="mcr-location-form")
        self.admin = _user("admin@location-form.test")
        self.member = _user("member@location-form.test")
        self.team.members.add(self.admin, through_defaults={"role": ROLE_ADMIN})
        self.team.members.add(self.member, through_defaults={"role": ROLE_MEMBER})

    def _data(self, **overrides):
        return {"name": "New depot", "location_type": "depot", "is_active": "on", **overrides}

    def test_admin_creates_a_location_with_auto_stop_on(self):
        client = Client()
        client.force_login(self.admin)
        client.post(reverse("containers:location_create"), self._data(auto_stop_tracking_on_receive="on"))
        self.assertTrue(ContainerLocation.objects.get(team=self.team, name="New depot").auto_stop_tracking_on_receive)

    def test_member_can_edit_a_location_but_not_its_auto_stop(self):
        location = _location(self.team, "Oceanterminalen", auto_stop_tracking_on_receive=True)
        client = Client()
        client.force_login(self.member)
        client.post(
            reverse("containers:location_update", args=[location.pk]),
            self._data(name="Oceanterminalen Nord", location_type="terminal"),  # checkbox absent = "off"
        )
        location.refresh_from_db()
        self.assertEqual((location.name, location.auto_stop_tracking_on_receive), ("Oceanterminalen Nord", True))

    def test_field_is_disabled_unless_the_form_may_manage_tracking(self):
        self.assertTrue(ContainerLocationForm(team=self.team).fields["auto_stop_tracking_on_receive"].disabled)
        form = ContainerLocationForm(team=self.team, can_manage_tracking=True)
        self.assertFalse(form.fields["auto_stop_tracking_on_receive"].disabled)
