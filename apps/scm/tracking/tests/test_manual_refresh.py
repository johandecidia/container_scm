"""Tests for "Refresh tracking" on container detail.

The refresh goes through the same sync engine as the scheduled poller, so what is
tested here is the part that is new: choosing the carrier from a container, refusing
to invent one, and reporting the outcome honestly. The carrier itself is an injected
fake session — no live call is made.
"""

from unittest import mock

import requests
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from apps.scm.containers.models import Container, EquipmentType
from apps.scm.integrations.carriers.maersk.client import PUBLIC_TRACK_AND_TRACE_CONFIG, MaerskClient
from apps.scm.integrations.credentials import set_integration_credentials
from apps.scm.integrations.models import Integration, IntegrationCredential
from apps.scm.shipments.models import Shipment, ShipmentContainer
from apps.scm.tracking.manual_refresh import (
    CARRIER_UNKNOWN,
    ERROR,
    INFO,
    NO_DATA,
    NOT_CONFIGURED,
    SUCCESS,
    UNAVAILABLE,
    UPDATED,
    WARNING,
    refresh_container_tracking,
    resolve_carrier_code_for_container,
)
from apps.scm.tracking.models import TrackingEvent, TrackingRawPayload, TrackingSubscription
from apps.teams.models import Team
from apps.teams.roles import ROLE_MEMBER
from apps.users.models import CustomUser

_LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache", "LOCATION": "manual-refresh"}}
_TEST_STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}

API_KEY = "refresh-secret-key"

PAYLOAD = {
    "events": [
        {
            "eventID": "REFRESH-EVT-001",
            "eventType": "EQUIPMENT",
            "eventClassifierCode": "ACT",
            "equipmentEventTypeCode": "LOAD",
            "eventDateTime": "2026-03-10T08:00:00Z",
            "equipmentReference": "TRDU9258963",
            "location": {"locationName": "Port of Felixstowe", "UNLocationCode": "GBFXT"},
            "vessel": {"vesselName": "MAERSK EINDHOVEN", "vesselIMONumber": "9778791"},
            "exportVoyageNumber": "213E",
            "modeOfTransport": "VESSEL",
        },
        {
            "eventID": "REFRESH-EVT-002",
            "eventType": "TRANSPORT",
            "eventClassifierCode": "EST",
            "transportEventTypeCode": "ARRI",
            "eventDateTime": "2026-03-25T14:00:00Z",
            "equipmentReference": "TRDU9258963",
            "location": {"locationName": "Port of Rotterdam", "UNLocationCode": "NLRTM"},
        },
    ]
}


class FakeResponse:
    def __init__(self, status_code=200, payload=None, headers=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.headers = headers or {}

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, responses=None, error=None):
        self.responses = list(responses or [])
        self.error = error
        self.requests = []

    def get(self, url, headers=None, params=None, timeout=None):
        self.requests.append({"url": url, "headers": headers or {}, "params": params or {}})
        if self.error is not None:
            raise self.error
        return self.responses.pop(0) if self.responses else FakeResponse(200, {"events": []})


def _equipment_type():
    return EquipmentType.objects.get_or_create(
        iso_code="22G1",
        defaults={"category": "GP", "length_ft": 20, "high_cube": False, "description": "20' GP"},
    )[0]


def _container(team, owner_code="TRD", serial="925896", check_digit=3):
    """A container whose owner prefix is a leasing company, not a carrier."""
    return Container.objects.create(
        team=team,
        owner_code=owner_code,
        category_id="U",
        serial_number=serial,
        check_digit=check_digit,
        equipment_type=_equipment_type(),
    )


def _maersk_integration(team, config=None):
    integration = Integration.objects.create(
        team=team,
        name="Maersk",
        provider_code="maersk",
        provider_family=Integration.ProviderFamily.CARRIER,
        api_style=Integration.ApiStyle.DCSA,
        config=dict(config or PUBLIC_TRACK_AND_TRACE_CONFIG) | {"max_retries": 0, "retry_backoff_seconds": 0},
        is_active=True,
    )
    set_integration_credentials(integration, IntegrationCredential.AuthType.API_KEY, {"api_key": API_KEY})
    return integration


@override_settings(CACHES=_LOCMEM)
class CarrierResolutionTest(TestCase):
    """Which carrier to ask is decided from evidence, never guessed."""

    def setUp(self):
        self.team = Team.objects.create(name="resolve-team", slug="resolve-team")
        self.container = _container(self.team)

    def test_no_evidence_means_no_carrier(self):
        self.assertEqual(resolve_carrier_code_for_container(self.team, self.container), "")

    def test_the_shipments_carrier_is_used(self):
        shipment = Shipment.objects.create(team=self.team, shipment_number="SHP-1", carrier="Maersk")
        ShipmentContainer.objects.create(shipment=shipment, container=self.container)
        self.assertEqual(resolve_carrier_code_for_container(self.team, self.container), "maersk")

    def test_an_unrecognised_shipment_carrier_is_not_substituted(self):
        shipment = Shipment.objects.create(team=self.team, shipment_number="SHP-2", carrier="Regional Feeder Line")
        ShipmentContainer.objects.create(shipment=shipment, container=self.container)
        self.assertEqual(resolve_carrier_code_for_container(self.team, self.container), "")

    def test_the_owner_prefix_is_never_treated_as_the_carrier(self):
        """MRKU is an owner code. Who owns the box does not say who is moving it."""
        maersk_box = _container(self.team, owner_code="MRK", serial="123456", check_digit=3)
        self.assertEqual(resolve_carrier_code_for_container(self.team, maersk_box), "")

    def test_a_carrier_recorded_for_the_planned_container_is_used(self):
        from apps.scm.containers.models import PlannedContainer

        PlannedContainer.objects.create(
            team=self.team,
            container_number=self.container.container_id,
            carrier="maersk",
        )
        self.assertEqual(resolve_carrier_code_for_container(self.team, self.container), "maersk")

    def test_another_teams_planned_container_does_not_count(self):
        from apps.scm.containers.models import PlannedContainer

        other = Team.objects.create(name="resolve-planned-other", slug="resolve-planned-other")
        PlannedContainer.objects.create(
            team=other,
            container_number=self.container.container_id,
            carrier="maersk",
        )
        self.assertEqual(resolve_carrier_code_for_container(self.team, self.container), "")

    def test_the_shipment_carrier_outranks_the_planned_container(self):
        from apps.scm.containers.models import PlannedContainer

        PlannedContainer.objects.create(
            team=self.team,
            container_number=self.container.container_id,
            carrier="msc",
        )
        shipment = Shipment.objects.create(team=self.team, shipment_number="SHP-3", carrier="Maersk")
        ShipmentContainer.objects.create(shipment=shipment, container=self.container)
        self.assertEqual(resolve_carrier_code_for_container(self.team, self.container), "maersk")

    def test_a_single_configured_carrier_is_the_last_resort(self):
        _maersk_integration(self.team)
        self.assertEqual(resolve_carrier_code_for_container(self.team, self.container), "maersk")

    def test_two_configured_carriers_are_not_chosen_between(self):
        _maersk_integration(self.team)
        Integration.objects.create(
            team=self.team,
            name="Hapag-Lloyd",
            provider_code="hapag_lloyd",
            provider_family=Integration.ProviderFamily.CARRIER,
            is_active=True,
        )
        self.assertEqual(resolve_carrier_code_for_container(self.team, self.container), "")

    def test_an_inactive_integration_does_not_count(self):
        integration = _maersk_integration(self.team)
        integration.is_active = False
        integration.save(update_fields=["is_active"])
        self.assertEqual(resolve_carrier_code_for_container(self.team, self.container), "")

    def test_another_teams_integration_does_not_count(self):
        other = Team.objects.create(name="resolve-other", slug="resolve-other")
        _maersk_integration(other)
        self.assertEqual(resolve_carrier_code_for_container(self.team, self.container), "")

    def test_an_existing_subscription_wins_over_everything_else(self):
        from apps.scm.integrations.carriers.auto_link import get_or_create_tracking_provider

        maersk_box = _container(self.team, owner_code="MRK", serial="123456", check_digit=3)
        provider = get_or_create_tracking_provider(carrier_code="msc", carrier_name="MSC")
        TrackingSubscription.objects.create(
            team=self.team,
            provider=provider,
            container=maersk_box,
            tracking_reference=maersk_box.container_id,
            reference_type=TrackingSubscription.ReferenceType.CONTAINER_NUMBER,
        )
        self.assertEqual(resolve_carrier_code_for_container(self.team, maersk_box), "msc")


@override_settings(CACHES=_LOCMEM)
class RefreshContainerTrackingTest(TestCase):
    """The refresh runs the real pipeline and reports what actually happened."""

    def setUp(self):
        self.team = Team.objects.create(name="refresh-team", slug="refresh-team")
        self.container = _container(self.team)
        self.integration = _maersk_integration(self.team)

    def _refresh(self, session):
        client = MaerskClient(self.integration, session=session)
        with mock.patch(
            "apps.scm.integrations.carriers.factory.build_carrier_client",
            return_value=client,
        ):
            return refresh_container_tracking(team=self.team, container=self.container)

    def test_events_are_created_and_reported(self):
        result = self._refresh(FakeSession([FakeResponse(200, PAYLOAD)]))
        self.assertEqual(result.level, SUCCESS)
        self.assertEqual(result.events_created, 2)
        self.assertEqual(TrackingEvent.objects.filter(team=self.team).count(), 2)

    def test_the_carrier_is_asked_by_equipment_reference_with_the_consumer_key(self):
        session = FakeSession([FakeResponse(200, PAYLOAD)])
        self._refresh(session)
        self.assertEqual(session.requests[0]["params"], {"equipmentReference": self.container.container_id})
        self.assertEqual(session.requests[0]["headers"]["consumer-key"], API_KEY)
        self.assertEqual(session.requests[0]["headers"]["API-Version"], "1")

    def test_the_raw_response_is_stored_before_it_is_trusted(self):
        self._refresh(FakeSession([FakeResponse(200, PAYLOAD)]))
        stored = TrackingRawPayload.objects.get(team=self.team)
        self.assertEqual(stored.payload_json, PAYLOAD)
        self.assertTrue(stored.parsed_successfully)

    def test_events_are_linked_to_the_container(self):
        self._refresh(FakeSession([FakeResponse(200, PAYLOAD)]))
        for event in TrackingEvent.objects.filter(team=self.team):
            self.assertEqual(event.container_id, self.container.pk)

    def test_a_subscription_is_created_once_and_reused(self):
        self._refresh(FakeSession([FakeResponse(200, PAYLOAD)]))
        self._refresh(FakeSession([FakeResponse(200, PAYLOAD)]))
        self.assertEqual(TrackingSubscription.objects.filter(team=self.team, container=self.container).count(), 1)

    def test_refreshing_twice_creates_no_duplicate_events(self):
        self._refresh(FakeSession([FakeResponse(200, PAYLOAD)]))
        result = self._refresh(FakeSession([FakeResponse(200, PAYLOAD)]))
        self.assertEqual(result.events_created, 0)
        self.assertEqual(result.events_updated, 2)
        self.assertEqual(TrackingEvent.objects.filter(team=self.team).count(), 2)

    def test_the_summary_counts_new_and_unchanged_events(self):
        self._refresh(FakeSession([FakeResponse(200, PAYLOAD)]))
        result = self._refresh(FakeSession([FakeResponse(200, PAYLOAD)]))
        self.assertEqual(result.state, UPDATED)
        self.assertIn("2 events received", str(result.message))
        self.assertIn("0 new", str(result.message))
        self.assertIn("2 unchanged", str(result.message))

    def test_no_data_is_reported_as_no_data_not_as_a_failure(self):
        result = self._refresh(FakeSession([FakeResponse(404)]))
        self.assertEqual(result.level, INFO)
        self.assertEqual(result.state, NO_DATA)
        self.assertIn("no tracking data", str(result.message).lower())
        self.assertEqual(TrackingEvent.objects.filter(team=self.team).count(), 0)

    def test_authentication_failure_is_reported_as_a_plain_unavailable(self):
        """The user is told it does not work, not which credential is wrong."""
        result = self._refresh(FakeSession([FakeResponse(401), FakeResponse(401)]))
        self.assertEqual(result.level, ERROR)
        self.assertEqual(result.state, UNAVAILABLE)
        self.assertIn("temporarily unavailable", str(result.message))
        self.assertNotIn("auth", str(result.message).lower())
        self.assertEqual(TrackingEvent.objects.filter(team=self.team).count(), 0)

    def test_rate_limiting_is_reported_as_an_error(self):
        result = self._refresh(FakeSession([FakeResponse(429, headers={"Retry-After": "600"})]))
        self.assertEqual(result.level, ERROR)
        self.assertEqual(result.state, UNAVAILABLE)

    def test_a_timeout_is_reported_as_an_error(self):
        result = self._refresh(FakeSession(error=requests.Timeout("timed out")))
        self.assertEqual(result.level, ERROR)
        self.assertEqual(result.state, UNAVAILABLE)

    def test_a_server_error_does_not_leak_the_carrier_response(self):
        leaky = {"detail": "internal stack trace here"}
        result = self._refresh(FakeSession([FakeResponse(500, leaky), FakeResponse(500, leaky)]))
        self.assertEqual(result.level, ERROR)
        self.assertNotIn("stack trace", str(result.message))

    def test_an_unconfigured_integration_is_a_warning_not_a_silent_success(self):
        self.integration.config = {}
        self.integration.save(update_fields=["config"])
        result = self._refresh(FakeSession([FakeResponse(200, PAYLOAD)]))
        self.assertEqual(result.level, WARNING)
        self.assertEqual(result.state, NOT_CONFIGURED)
        self.assertIn("not configured", str(result.message).lower())
        self.assertEqual(TrackingEvent.objects.filter(team=self.team).count(), 0)

    def test_no_secret_appears_in_the_message_shown_to_the_user(self):
        for session in (
            FakeSession([FakeResponse(200, PAYLOAD)]),
            FakeSession([FakeResponse(401), FakeResponse(401)]),
            FakeSession([FakeResponse(500), FakeResponse(500)]),
        ):
            result = self._refresh(session)
            self.assertNotIn(API_KEY, str(result.message))

    def test_the_carrier_call_is_bounded_while_someone_is_waiting(self):
        """A manual refresh must not be able to hold the request for minutes."""
        from apps.scm.integrations.carriers.http import (
            INTERACTIVE_MAX_RETRIES,
            INTERACTIVE_TIMEOUT_SECONDS,
            HttpConfig,
        )

        seen = {}
        session = FakeSession([FakeResponse(200, PAYLOAD)])

        original = HttpConfig.from_config

        def _record(config):
            resolved = original(config)
            seen["config"] = resolved
            return resolved

        with mock.patch.object(HttpConfig, "from_config", staticmethod(_record)):
            self._refresh(session)

        self.assertLessEqual(seen["config"].timeout_seconds, INTERACTIVE_TIMEOUT_SECONDS)
        self.assertLessEqual(seen["config"].max_retries, INTERACTIVE_MAX_RETRIES)


@override_settings(CACHES=_LOCMEM)
class RefreshWithoutAnIntegrationTest(TestCase):
    def setUp(self):
        self.team = Team.objects.create(name="refresh-none", slug="refresh-none")

    def test_an_unknown_carrier_is_reported_rather_than_guessed(self):
        container = _container(self.team)
        result = refresh_container_tracking(team=self.team, container=container)
        self.assertEqual(result.level, ERROR)
        self.assertEqual(result.state, CARRIER_UNKNOWN)
        self.assertIn("carrier could not be determined", str(result.message).lower())
        self.assertEqual(TrackingSubscription.objects.filter(team=self.team).count(), 0)

    def test_a_known_carrier_without_an_integration_creates_no_noise(self):
        """Nothing to call means no subscription and no sync run to explain later."""
        container = _container(self.team)
        shipment = Shipment.objects.create(team=self.team, shipment_number="SHP-NOINT", carrier="Maersk")
        ShipmentContainer.objects.create(shipment=shipment, container=container)
        result = refresh_container_tracking(team=self.team, container=container)
        self.assertEqual(result.level, ERROR)
        self.assertEqual(result.state, NOT_CONFIGURED)
        self.assertIn("not configured", str(result.message).lower())
        self.assertEqual(TrackingSubscription.objects.filter(team=self.team).count(), 0)


@override_settings(CACHES=_LOCMEM, STORAGES=_TEST_STORAGES)
class RefreshTrackingViewTest(TestCase):
    """The container detail button is POST-only and strictly team-scoped."""

    def setUp(self):
        self.team = Team.objects.create(name="refresh-view", slug="refresh-view")
        self.user = CustomUser.objects.create_user(username="refresh@example.com", password="pass")
        self.team.members.add(self.user, through_defaults={"role": ROLE_MEMBER})
        self.container = _container(self.team)
        self.integration = _maersk_integration(self.team)
        self.client_ = Client()
        self.client_.force_login(self.user)

    def _url(self, container=None):
        return reverse("containers:refresh_tracking", args=[(container or self.container).pk])

    def _post(self, session):
        client = MaerskClient(self.integration, session=session)
        with mock.patch(
            "apps.scm.integrations.carriers.factory.build_carrier_client",
            return_value=client,
        ):
            return self.client_.post(self._url(), follow=True)

    def test_get_is_not_allowed(self):
        response = self.client_.get(self._url())
        self.assertEqual(response.status_code, 405)

    def test_anonymous_users_are_redirected_to_login(self):
        response = Client().post(self._url())
        self.assertIn(response.status_code, (302, 403))

    def test_a_successful_refresh_reports_the_events(self):
        response = self._post(FakeSession([FakeResponse(200, PAYLOAD)]))
        self.assertEqual(response.status_code, 200)
        text = " ".join(str(message) for message in response.context["messages"])
        self.assertIn("2 events received", text)
        self.assertEqual(TrackingEvent.objects.filter(team=self.team).count(), 2)

    def test_a_failure_is_shown_without_leaking_the_key(self):
        response = self._post(FakeSession([FakeResponse(401), FakeResponse(401)]))
        text = " ".join(str(message) for message in response.context["messages"])
        self.assertTrue(text)
        self.assertNotIn(API_KEY, text)

    def test_another_teams_container_is_not_found(self):
        other_team = Team.objects.create(name="refresh-view-other", slug="refresh-view-other")
        other_container = _container(other_team, serial="925897", check_digit=9)
        response = self.client_.post(self._url(other_container))
        self.assertEqual(response.status_code, 404)

    def test_the_detail_page_offers_the_button(self):
        response = self.client_.get(reverse("containers:detail", args=[self.container.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self._url())
