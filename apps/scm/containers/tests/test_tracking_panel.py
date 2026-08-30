"""Tests for the container tracking panel as an HTMX component.

The carrier pipeline itself is covered in apps.scm.tracking; what is tested here is
the part the user touches: that one button posts, that the response is the panel and
nothing else, that every outcome is rendered inside the panel rather than lost, and
that nothing technical leaks into it.
"""

from unittest import mock

import requests
from django.db import connection
from django.test import Client, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from apps.scm.containers.models import Container, EquipmentType
from apps.scm.containers.utils import calculate_check_digit
from apps.scm.integrations.carriers.maersk.client import PUBLIC_TRACK_AND_TRACE_CONFIG, MaerskClient
from apps.scm.integrations.credentials import set_integration_credentials
from apps.scm.integrations.models import Integration, IntegrationCredential
from apps.scm.shipments.models import Shipment, ShipmentContainer
from apps.scm.tracking.models import TrackingEvent, TrackingSubscription
from apps.teams.models import Team
from apps.teams.roles import ROLE_MEMBER
from apps.users.models import CustomUser

_LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache", "LOCATION": "tracking-panel"}}
_TEST_STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}

API_KEY = "panel-secret-key"
PANEL_TEMPLATE = "scm/containers/partials/container_tracking_panel.html"

PAYLOAD = {
    "events": [
        {
            "eventID": "PANEL-EVT-001",
            "eventType": "EQUIPMENT",
            "eventClassifierCode": "ACT",
            "equipmentEventTypeCode": "LOAD",
            "eventDateTime": "2026-03-10T08:00:00Z",
            "equipmentReference": "TRDU9258963",
            "location": {"locationName": "Port of Felixstowe", "UNLocationCode": "GBFXT"},
            "vessel": {"vesselName": "MAERSK EINDHOVEN", "vesselIMONumber": "9778791"},
            "exportVoyageNumber": "213E",
            "modeOfTransport": "VESSEL",
        }
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

    def get(self, url, headers=None, params=None, timeout=None):
        if self.error is not None:
            raise self.error
        return self.responses.pop(0) if self.responses else FakeResponse(200, {"events": []})


def _equipment_type() -> EquipmentType:
    return EquipmentType.objects.get_or_create(
        iso_code="22G1",
        defaults={"category": "GP", "length_ft": 20, "high_cube": False, "description": "20' GP"},
    )[0]


def _container(team, owner_code="TRD", serial="925896", check_digit=3) -> Container:
    return Container.objects.create(
        team=team,
        owner_code=owner_code,
        category_id="U",
        serial_number=serial,
        check_digit=check_digit,
        equipment_type=_equipment_type(),
    )


def _maersk_integration(team, config=None) -> Integration:
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


@override_settings(CACHES=_LOCMEM, STORAGES=_TEST_STORAGES)
class TrackingPanelTestBase(TestCase):
    team_slug = "panel-team"

    def setUp(self):
        self.team = Team.objects.create(name=self.team_slug, slug=self.team_slug)
        self.user = CustomUser.objects.create_user(username=f"{self.team_slug}@example.com", password="pass")
        self.team.members.add(self.user, through_defaults={"role": ROLE_MEMBER})
        self.container = _container(self.team)
        self.client_ = Client()
        self.client_.force_login(self.user)

    def _url(self, container=None) -> str:
        return reverse("containers:refresh_tracking", args=[(container or self.container).pk])

    def _detail(self, container=None):
        return self.client_.get(reverse("containers:detail", args=[(container or self.container).pk]))

    def _refresh(self, session=None, *, integration=None, htmx=True, follow=False):
        """POST the refresh button with a fake carrier session behind it."""
        headers = {"HTTP_HX_REQUEST": "true"} if htmx else {}
        if integration is None:
            return self.client_.post(self._url(), follow=follow, **headers)
        client = MaerskClient(integration, session=session or FakeSession())
        with mock.patch(
            "apps.scm.integrations.carriers.factory.build_carrier_client",
            return_value=client,
        ):
            return self.client_.post(self._url(), follow=follow, **headers)


class TrackingPanelOnDetailPageTest(TrackingPanelTestBase):
    """The detail page ships the panel as an addressable component."""

    team_slug = "panel-detail"

    def test_the_panel_has_a_stable_root_id(self):
        response = self._detail()
        self.assertContains(response, 'id="container-tracking-panel"')

    def test_the_refresh_button_posts_to_the_refresh_endpoint(self):
        response = self._detail()
        self.assertContains(response, f'hx-post="{self._url()}"')
        self.assertContains(response, 'hx-target="#container-tracking-panel"')

    def test_the_button_cannot_fire_twice_at_once(self):
        self.assertContains(self._detail(), 'hx-disabled-elt="this"')

    def test_the_button_has_a_busy_label(self):
        response = self._detail()
        self.assertContains(response, "Refreshing")
        self.assertContains(response, "htmx-request-show")

    def test_the_refresh_form_is_not_nested_in_another_form(self):
        """The button is a plain button, so it cannot end up inside the edit form."""
        body = self._detail().content.decode()
        panel = body.split('id="container-tracking-panel"', 1)[1]
        self.assertNotIn("<form", panel.split("</div>")[0])

    def test_an_untracked_container_says_so(self):
        response = self._detail()
        self.assertContains(response, "Not tracked")
        self.assertContains(response, "No tracking events yet")
        self.assertContains(response, "Refresh tracking to check the carriers")


class RefreshRequestHandlingTest(TrackingPanelTestBase):
    """How the endpoint answers, by request type and by team."""

    team_slug = "panel-request"

    def test_get_is_rejected(self):
        self.assertEqual(self.client_.get(self._url()).status_code, 405)

    def test_htmx_post_returns_only_the_tracking_panel(self):
        response = self._refresh()
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, PANEL_TEMPLATE)
        self.assertTemplateNotUsed(response, "scm/containers/pages/container_detail.html")
        self.assertContains(response, 'id="container-tracking-panel"')

    def test_the_refresh_button_survives_the_swap(self):
        response = self._refresh()
        self.assertContains(response, f'hx-post="{self._url()}"')
        self.assertContains(response, "Refresh tracking")

    def test_a_plain_post_redirects_back_to_the_detail_page(self):
        response = self._refresh(htmx=False)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("containers:detail", args=[self.container.pk]))

    def test_a_plain_post_still_reports_through_messages(self):
        response = self._refresh(htmx=False, follow=True)
        text = " ".join(str(message) for message in response.context["messages"])
        self.assertTrue(text)

    def test_another_teams_container_cannot_be_refreshed(self):
        other_team = Team.objects.create(name="panel-other", slug="panel-other")
        other_container = _container(other_team, serial="925897", check_digit=9)
        response = self.client_.post(
            reverse("containers:refresh_tracking", args=[other_container.pk]), HTTP_HX_REQUEST="true"
        )
        self.assertEqual(response.status_code, 404)
        self.assertFalse(TrackingSubscription.objects.filter(container=other_container).exists())

    def test_anonymous_users_are_redirected(self):
        response = Client().post(self._url())
        self.assertIn(response.status_code, (302, 403))


class RefreshResultStatesTest(TrackingPanelTestBase):
    """Every outcome is rendered inside the panel, in words a user can act on."""

    team_slug = "panel-states"

    def test_nothing_to_ask_asks_for_a_carrier_to_be_connected(self):
        """The user is told to connect an integration, never to identify the carrier."""
        response = self._refresh()
        self.assertContains(response, "No carrier integration is connected")
        self.assertNotContains(response, "Carrier could not be determined")

    def test_a_known_carrier_without_an_integration_reports_configuration(self):
        shipment = Shipment.objects.create(team=self.team, shipment_number="SHP-P1", carrier="Maersk")
        ShipmentContainer.objects.create(shipment=shipment, container=self.container)
        response = self._refresh()
        self.assertContains(response, "Tracking is not configured for this container")

    def test_a_successful_refresh_summarises_what_arrived(self):
        integration = _maersk_integration(self.team)
        response = self._refresh(FakeSession([FakeResponse(200, PAYLOAD)]), integration=integration)
        # The carrier was discovered by this refresh, so the panel names it.
        self.assertContains(response, "Tracking found via Maersk")
        self.assertContains(response, "1 tracking events retrieved")
        self.assertEqual(TrackingEvent.objects.filter(team=self.team).count(), 1)

    def test_a_successful_refresh_shows_the_event_in_the_panel(self):
        integration = _maersk_integration(self.team)
        response = self._refresh(FakeSession([FakeResponse(200, PAYLOAD)]), integration=integration)
        self.assertContains(response, "Port of Felixstowe")
        self.assertContains(response, "MAERSK EINDHOVEN")
        # Freshness now comes from the shared visibility component.
        self.assertContains(response, "Last checked")

    def test_no_data_is_reported_without_alarm(self):
        integration = _maersk_integration(self.team)
        response = self._refresh(FakeSession([FakeResponse(404)]), integration=integration)
        self.assertContains(response, "No tracking data found at Maersk")

    def test_a_carrier_with_no_data_is_not_shown_as_an_active_tracking_source(self):
        """The panel may say we asked Maersk; it may not say Maersk tracks the box."""
        integration = _maersk_integration(self.team)
        shipment = Shipment.objects.create(team=self.team, shipment_number="SHP-P2", carrier="Maersk")
        ShipmentContainer.objects.create(shipment=shipment, container=self.container)

        response = self._refresh(FakeSession([FakeResponse(404)]), integration=integration)

        self.assertContains(response, "Not tracked")
        self.assertContains(response, "has not been assigned as a tracking source")
        self.assertNotContains(response, "● Active")
        self.assertFalse(TrackingSubscription.objects.filter(team=self.team, container=self.container).exists())

    def test_a_shipment_carrier_is_shown_as_context_not_as_tracking(self):
        shipment = Shipment.objects.create(team=self.team, shipment_number="SHP-P3", carrier="Maersk")
        ShipmentContainer.objects.create(shipment=shipment, container=self.container)
        response = self._detail()
        self.assertContains(response, "Shipment carrier: Maersk")
        self.assertContains(response, "No tracking data available yet")
        self.assertContains(response, "Not tracked")

    def test_a_verified_carrier_is_shown_as_active(self):
        integration = _maersk_integration(self.team)
        self._refresh(FakeSession([FakeResponse(200, PAYLOAD)]), integration=integration)
        response = self._detail()
        self.assertContains(response, "Maersk")
        self.assertContains(response, "● Tracking")
        self.assertNotContains(response, "Not tracked")

    def test_an_api_error_is_shown_as_temporarily_unavailable(self):
        integration = _maersk_integration(self.team)
        response = self._refresh(FakeSession([FakeResponse(500), FakeResponse(500)]), integration=integration)
        self.assertContains(response, "Maersk tracking is temporarily unavailable")

    def test_a_timeout_is_shown_as_temporarily_unavailable(self):
        integration = _maersk_integration(self.team)
        response = self._refresh(FakeSession(error=requests.Timeout("timed out")), integration=integration)
        self.assertContains(response, "temporarily unavailable")

    def test_an_authentication_failure_leaks_nothing(self):
        integration = _maersk_integration(self.team)
        response = self._refresh(FakeSession([FakeResponse(401), FakeResponse(401)]), integration=integration)
        body = response.content.decode()
        self.assertIn("temporarily unavailable", body)
        for forbidden in (API_KEY, "consumer-key", "Traceback", "401"):
            self.assertNotIn(forbidden, body)

    def test_refreshing_twice_reports_no_new_events_and_creates_no_duplicates(self):
        integration = _maersk_integration(self.team)
        self._refresh(FakeSession([FakeResponse(200, PAYLOAD)]), integration=integration)
        response = self._refresh(FakeSession([FakeResponse(200, PAYLOAD)]), integration=integration)
        self.assertContains(response, "0 new")
        self.assertContains(response, "1 unchanged")
        self.assertEqual(TrackingEvent.objects.filter(team=self.team).count(), 1)


class ContainerListTrackingColumnTest(TrackingPanelTestBase):
    """Tracking visibility in the list, without a query per row."""

    team_slug = "panel-list"

    def test_an_untracked_container_reads_as_not_tracked(self):
        response = self.client_.get(reverse("containers:list"))
        self.assertContains(response, "Not tracked")

    def test_a_tracked_container_shows_its_carrier_and_state(self):
        from apps.scm.integrations.carriers.auto_link import get_or_create_tracking_provider

        provider = get_or_create_tracking_provider(carrier_code="maersk", carrier_name="Maersk")
        TrackingSubscription.objects.create(
            team=self.team,
            provider=provider,
            container=self.container,
            tracking_reference=self.container.container_id,
            status=TrackingSubscription.Status.ACTIVE,
        )
        response = self.client_.get(reverse("containers:list"))
        self.assertContains(response, "Maersk")
        self.assertContains(response, "Active")

    def test_a_failed_subscription_reads_as_an_error(self):
        from apps.scm.integrations.carriers.auto_link import get_or_create_tracking_provider

        provider = get_or_create_tracking_provider(carrier_code="maersk", carrier_name="Maersk")
        TrackingSubscription.objects.create(
            team=self.team,
            provider=provider,
            container=self.container,
            tracking_reference=self.container.container_id,
            status=TrackingSubscription.Status.FAILED,
        )
        self.assertContains(self.client_.get(reverse("containers:list")), "Error")

    def test_another_teams_subscription_is_not_shown(self):
        from apps.scm.integrations.carriers.auto_link import get_or_create_tracking_provider

        other_team = Team.objects.create(name="panel-list-other", slug="panel-list-other")
        provider = get_or_create_tracking_provider(carrier_code="msc", carrier_name="MSC")
        TrackingSubscription.objects.create(
            team=other_team,
            provider=provider,
            container=self.container,
            tracking_reference=self.container.container_id,
        )
        response = self.client_.get(reverse("containers:list"))
        self.assertNotContains(response, "MSC")

    def test_more_tracked_containers_do_not_mean_more_queries(self):
        """The carrier and tracking columns are annotated, not followed per row."""
        self._track(self.container)
        baseline = self._count_list_queries()

        for index in range(5):
            serial = f"10000{index}"
            self._track(
                _container(
                    self.team,
                    owner_code="ABC",
                    serial=serial,
                    check_digit=calculate_check_digit("ABC", "U", serial),
                )
            )

        self.assertEqual(self._count_list_queries(), baseline)

    def _track(self, container) -> None:
        from apps.scm.integrations.carriers.auto_link import get_or_create_tracking_provider

        TrackingSubscription.objects.create(
            team=self.team,
            provider=get_or_create_tracking_provider(carrier_code="maersk", carrier_name="Maersk"),
            container=container,
            tracking_reference=container.container_id,
        )

    def _count_list_queries(self) -> int:
        with CaptureQueriesContext(connection) as queries:
            self.client_.get(reverse("containers:list"))
        return len(queries.captured_queries)
