"""Business Central location mappings: safe defaults, validation, activation boundary, admin-only UI."""

from django.db import IntegrityError, transaction
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.scm.containers.models import ContainerLocation
from apps.scm.integrations.location_forms import LocationMappingForm
from apps.scm.integrations.models import BusinessCentralLocationMapping, IntegrationSyncRun
from apps.scm.integrations.tests.test_item_rules import bc_integration
from apps.teams.models import Membership
from apps.teams.roles import ROLE_ADMIN, ROLE_MEMBER
from apps.users.models import CustomUser

Mapping = BusinessCentralLocationMapping
PHYSICAL = Mapping.LocationType.PHYSICAL
COMMERCIAL = Mapping.LocationType.COMMERCIAL
PPR = Mapping.ReceiveTrigger.POSTED_PURCHASE_RECEIPT


def pegasus_location(team, name="Oceanterminalen") -> ContainerLocation:
    return ContainerLocation.objects.create(team=team, name=name, location_type="terminal")


def ledger_synced(integration, cursor: int, *, has_more: bool = False) -> IntegrationSyncRun:
    """A completed item ledger sync run that read up to ``cursor``."""
    return IntegrationSyncRun.objects.create(
        team=integration.team,
        integration=integration,
        resource_type=IntegrationSyncRun.ResourceType.ITEM_LEDGER_ENTRIES,
        status=IntegrationSyncRun.Status.COMPLETED,
        started_at=timezone.now(),
        finished_at=timezone.now(),
        metadata={"source": {"mode": "dummy"}, "cursor_to": cursor, "has_more": has_more},
    )


def mapping_data(location=None, **overrides) -> dict:
    data = {
        "bc_location_code": "L005",
        "name": "Oceanterminalen",
        "location_type": PHYSICAL,
        "pegasus_location": location.pk if location else "",
        "receive_trigger": PPR,
        "is_active": "on",
        "auto_receive_enabled": "on",
    }
    data.update(overrides)
    return {k: v for k, v in data.items() if v is not None}


class MappingModelTest(TestCase):
    def test_defaults_never_receive(self):
        integration = bc_integration("map-defaults")
        mapping = Mapping.objects.create(team=integration.team, integration=integration, bc_location_code="L001")
        self.assertEqual(mapping.receive_trigger, Mapping.ReceiveTrigger.NONE)
        self.assertFalse(mapping.auto_receive_enabled)
        self.assertFalse(mapping.auto_create_containers)
        self.assertTrue(mapping.is_active)
        self.assertEqual(mapping.location_type, COMMERCIAL)
        self.assertFalse(mapping.receives_automatically)

    def test_database_refuses_auto_receive_without_physical_location(self):
        integration = bc_integration("map-constraint")
        with self.assertRaises(IntegrityError), transaction.atomic():
            Mapping.objects.create(
                team=integration.team,
                integration=integration,
                bc_location_code="L005",
                location_type=PHYSICAL,
                receive_trigger=PPR,
                auto_receive_enabled=True,
                auto_receive_after_entry_no=1,
            )
        with self.assertRaises(IntegrityError), transaction.atomic():
            Mapping.objects.create(
                team=integration.team, integration=integration, bc_location_code="L006", auto_create_containers=True
            )

    def test_code_is_unique_per_integration_only(self):
        first, second = bc_integration("map-uniq-a"), bc_integration("map-uniq-b")
        Mapping.objects.create(team=first.team, integration=first, bc_location_code="L005")
        Mapping.objects.create(team=second.team, integration=second, bc_location_code="L005")
        with self.assertRaises(IntegrityError), transaction.atomic():
            Mapping.objects.create(team=first.team, integration=first, bc_location_code="L005")


class MappingFormTest(TestCase):
    def setUp(self):
        self.integration = bc_integration("map-form")
        self.location = pegasus_location(self.integration.team)

    def _form(self, data, instance=None):
        return LocationMappingForm(data, instance=instance, integration=self.integration)

    def test_enabling_records_the_activation_boundary(self):
        ledger_synced(self.integration, 95108)
        form = self._form(mapping_data(self.location, bc_location_code=" l005 "))
        self.assertTrue(form.is_valid(), form.errors)
        mapping = form.save()
        self.assertEqual(mapping.bc_location_code, "L005")
        self.assertEqual(mapping.auto_receive_after_entry_no, 95108)
        self.assertIsNotNone(mapping.auto_receive_enabled_at)
        self.assertTrue(mapping.receives_automatically)

    def test_enabling_is_refused_until_the_ledger_has_caught_up(self):
        form = self._form(mapping_data(self.location))
        self.assertIn("auto_receive_enabled", form.errors)  # never synced
        ledger_synced(self.integration, 5000, has_more=True)
        form = self._form(mapping_data(self.location))
        self.assertIn("auto_receive_enabled", form.errors)
        self.assertFalse(Mapping.objects.exists())

    def test_auto_receive_requires_physical_location_trigger_and_active(self):
        ledger_synced(self.integration, 1)
        cases = {
            "commercial": mapping_data(self.location, location_type=COMMERCIAL),
            "no location": mapping_data(None),
            "manual trigger": mapping_data(self.location, receive_trigger=Mapping.ReceiveTrigger.MANUAL),
            "inactive": mapping_data(self.location, is_active=None),
        }
        for label, data in cases.items():
            with self.subTest(label):
                self.assertIn("auto_receive_enabled", self._form(data).errors)

    def test_commercial_location_cannot_map_to_a_pegasus_location(self):
        data = mapping_data(self.location, location_type=Mapping.LocationType.CUSTOMER, auto_receive_enabled=None)
        self.assertIn("pegasus_location", self._form(data).errors)

    def test_commercial_location_without_receive_is_valid(self):
        data = mapping_data(
            None,
            bc_location_code="L005UTHYRT",
            location_type=Mapping.LocationType.CUSTOMER,
            receive_trigger=Mapping.ReceiveTrigger.NONE,
            auto_receive_enabled=None,
        )
        form = self._form(data)
        self.assertTrue(form.is_valid(), form.errors)
        self.assertFalse(form.save().receives_automatically)

    def test_create_containers_requires_auto_receive(self):
        data = mapping_data(self.location, auto_receive_enabled=None, auto_create_containers="on")
        self.assertIn("auto_create_containers", self._form(data).errors)

    def test_another_teams_location_is_refused(self):
        foreign = pegasus_location(bc_integration("map-form-other").team, "Foreign")
        ledger_synced(self.integration, 1)
        self.assertIn("pegasus_location", self._form(mapping_data(foreign)).errors)

    def test_duplicate_code_is_refused(self):
        Mapping.objects.create(team=self.integration.team, integration=self.integration, bc_location_code="L005")
        self.assertIn("bc_location_code", self._form(mapping_data(None, auto_receive_enabled=None)).errors)

    def test_disabling_clears_and_re_enabling_moves_the_boundary(self):
        ledger_synced(self.integration, 100)
        mapping = self._form(mapping_data(self.location, auto_create_containers="on")).save()
        self.assertEqual(mapping.auto_receive_after_entry_no, 100)

        form = self._form(mapping_data(self.location, auto_receive_enabled=None), instance=mapping)
        mapping = form.save()
        self.assertEqual((mapping.auto_receive_after_entry_no, mapping.auto_receive_enabled_at), (None, None))
        self.assertFalse(mapping.auto_create_containers)

        ledger_synced(self.integration, 250)
        mapping = self._form(mapping_data(self.location), instance=mapping).save()
        self.assertEqual(mapping.auto_receive_after_entry_no, 250)

    def test_saving_while_enabled_keeps_the_boundary(self):
        ledger_synced(self.integration, 100)
        mapping = self._form(mapping_data(self.location)).save()
        ledger_synced(self.integration, 300)
        mapping = self._form(mapping_data(self.location, name="Renamed"), instance=mapping).save()
        self.assertEqual(mapping.auto_receive_after_entry_no, 100)


class LocationMappingsViewTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.integration = bc_integration("map-ui")
        cls.admin = cls._user("map-admin@example.com", ROLE_ADMIN)
        cls.member = cls._user("map-member@example.com", ROLE_MEMBER)

    @classmethod
    def _user(cls, email, role):
        user = CustomUser.objects.create_user(username=email, email=email, password="pw")
        Membership.objects.create(team=cls.integration.team, user=user, role=role)
        return user

    def _login(self, user, team=None):
        self.client.force_login(user)
        session = self.client.session
        session["team"] = (team or self.integration.team).pk
        session.save()

    def _url(self, name="integrations:location_mappings", *args):
        return reverse(name, args=[self.integration.pk, *args])

    def test_admin_adds_edits_and_removes_a_location(self):
        self._login(self.admin)
        data = mapping_data(None, bc_location_code="L005UTHYRT", location_type="customer", receive_trigger="none")
        data.pop("auto_receive_enabled")
        self.client.post(self._url(), data)
        saved = Mapping.objects.get(integration=self.integration)
        self.assertEqual((saved.bc_location_code, saved.team), ("L005UTHYRT", self.integration.team))

        resp = self.client.get(self._url())
        self.assertContains(resp, "L005UTHYRT")

        self.client.post(self._url("integrations:location_mapping_update", saved.pk), {**data, "name": "Uthyrd"})
        saved.refresh_from_db()
        self.assertEqual(saved.name, "Uthyrd")

        self.client.post(self._url("integrations:location_mapping_delete", saved.pk))
        self.assertFalse(Mapping.objects.exists())

    def test_member_gets_404_everywhere(self):
        existing = Mapping.objects.create(
            team=self.integration.team, integration=self.integration, bc_location_code="L1"
        )
        self._login(self.member)
        for method, url in (
            ("get", self._url()),
            ("post", self._url()),
            ("get", self._url("integrations:location_mapping_update", existing.pk)),
            ("post", self._url("integrations:location_mapping_update", existing.pk)),
            ("post", self._url("integrations:location_mapping_delete", existing.pk)),
        ):
            with self.subTest(method=method, url=url):
                self.assertEqual(getattr(self.client, method)(url).status_code, 404)
        self.assertTrue(Mapping.objects.filter(pk=existing.pk).exists())

    def test_admin_of_another_team_cannot_reach_this_integration(self):
        other = bc_integration("map-ui-other")
        outsider = CustomUser.objects.create_user(username="mo@example.com", email="mo@example.com", password="pw")
        Membership.objects.create(team=other.team, user=outsider, role=ROLE_ADMIN)
        existing = Mapping.objects.create(
            team=self.integration.team, integration=self.integration, bc_location_code="L1"
        )
        self._login(outsider, other.team)
        self.assertEqual(self.client.get(self._url()).status_code, 404)
        self.assertEqual(
            self.client.post(self._url("integrations:location_mapping_delete", existing.pk)).status_code, 404
        )
        self.assertTrue(Mapping.objects.filter(pk=existing.pk).exists())
