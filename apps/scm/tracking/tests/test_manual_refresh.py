"""Tests for "Refresh tracking" on container detail.

The refresh goes through the same sync engine as the scheduled poller, so what is
tested here is the part that is new: finding out which carrier knows a container
when nothing tracks it yet, refusing to assign one that has not proved itself, and
reporting the outcome honestly. Every carrier is an injected fake — no live call is
made.
"""

from unittest import mock

import requests
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.scm.containers.models import Container, EquipmentType
from apps.scm.integrations.carriers.base import BaseCarrierClient, CarrierCapability
from apps.scm.integrations.carriers.dcsa.schemas import NormalisedTrackingEvent
from apps.scm.integrations.carriers.exceptions import (
    CarrierConfigurationError,
    CarrierNoDataError,
    CarrierTimeoutError,
)
from apps.scm.integrations.carriers.maersk.client import PUBLIC_TRACK_AND_TRACE_CONFIG, MaerskClient
from apps.scm.integrations.credentials import set_integration_credentials
from apps.scm.integrations.models import Integration, IntegrationCredential
from apps.scm.shipments.models import Shipment, ShipmentContainer
from apps.scm.tracking.manual_refresh import (
    CARRIER_UNKNOWN,
    ERROR,
    IN_PROGRESS,
    INFO,
    NO_DATA,
    NOT_CONFIGURED,
    SUCCESS,
    UNAVAILABLE,
    UPDATED,
    WARNING,
    get_preferred_carrier_codes_for_container,
    refresh_container_tracking,
)
from apps.scm.tracking.models import TrackingEvent, TrackingRawPayload, TrackingSubscription, TrackingSyncRun
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


def _normalised_event(container_number: str) -> NormalisedTrackingEvent:
    return NormalisedTrackingEvent(
        event_type="EQUIPMENT",
        event_classifier="ACT",
        event_code="GTIN",
        event_datetime=timezone.now(),
        location_unlocode="CNSHA",
        container_number=container_number,
        raw_event_id=f"EVT-{container_number}",
    )


class FakeCarrierClient(BaseCarrierClient):
    """A carrier that knows the box, does not know it, or cannot answer."""

    capabilities = CarrierCapability(supports_pull=True, supports_tracking_by_container=True)

    def __init__(self, provider_code, *, payload=None, error=None):
        super().__init__(None)
        self.provider_code = provider_code
        self.payload = payload if payload is not None else {"events": []}
        self.error = error
        self.calls: list[str] = []

    def fetch_tracking(self, *, container_number=None, **kwargs):
        self.calls.append(container_number)
        if self.error is not None:
            raise self.error
        return self.payload


class FakeParser:
    def __init__(self, events):
        self.events = events

    def parse_tracking_events(self, raw_payload):
        return list(self.events)


def _fake_client(provider_code, behaviour):
    """A client for one carrier: ``behaviour`` is a payload, an exception, or None."""
    if isinstance(behaviour, Exception):
        return FakeCarrierClient(provider_code, error=behaviour)
    return FakeCarrierClient(provider_code, payload=behaviour)


def _patch_carriers(clients: dict, events_by_code: dict):
    """Route the factory to the fakes, so a sweep can hit several carriers in turn."""

    def _client(provider_code, **kwargs):
        assert provider_code in clients, f"unexpected carrier asked: {provider_code}"
        return clients[provider_code]

    return mock.patch.multiple(
        "apps.scm.integrations.carriers.factory",
        build_carrier_client=mock.Mock(side_effect=_client),
        build_carrier_parser=mock.Mock(side_effect=lambda code: FakeParser(events_by_code.get(code, []))),
    )


def _carrier_integration(team, provider_code):
    return Integration.objects.create(
        team=team,
        name=provider_code,
        provider_code=provider_code,
        provider_family=Integration.ProviderFamily.CARRIER,
        is_active=True,
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
class PreferredCarrierSignalsTest(TestCase):
    """Which carrier to try *first* comes from evidence; the rest is discovery's job.

    These signals order the sweep. None of them is proof, so none of them may be the
    only carrier considered — that was the old behaviour, and it left a container
    with an unknown carrier permanently untrackable.
    """

    def setUp(self):
        self.team = Team.objects.create(name="resolve-team", slug="resolve-team")
        self.container = _container(self.team)

    def test_no_evidence_means_no_preference(self):
        self.assertEqual(get_preferred_carrier_codes_for_container(self.team, self.container), [])

    def test_the_shipments_carrier_is_preferred(self):
        shipment = Shipment.objects.create(team=self.team, shipment_number="SHP-1", carrier="Maersk")
        ShipmentContainer.objects.create(shipment=shipment, container=self.container)
        self.assertEqual(get_preferred_carrier_codes_for_container(self.team, self.container), ["maersk"])

    def test_an_unrecognised_shipment_carrier_is_not_substituted(self):
        shipment = Shipment.objects.create(team=self.team, shipment_number="SHP-2", carrier="Regional Feeder Line")
        ShipmentContainer.objects.create(shipment=shipment, container=self.container)
        self.assertEqual(get_preferred_carrier_codes_for_container(self.team, self.container), [])

    def test_the_owner_prefix_is_not_one_of_these_signals(self):
        """MRKU is an owner code. Discovery may use it to order candidates; this may not."""
        maersk_box = _container(self.team, owner_code="MRK", serial="123456", check_digit=3)
        self.assertEqual(get_preferred_carrier_codes_for_container(self.team, maersk_box), [])

    def test_a_carrier_recorded_for_the_planned_container_is_preferred(self):
        from apps.scm.containers.models import PlannedContainer

        PlannedContainer.objects.create(
            team=self.team,
            container_number=self.container.container_id,
            carrier="maersk",
        )
        self.assertEqual(get_preferred_carrier_codes_for_container(self.team, self.container), ["maersk"])

    def test_another_teams_planned_container_does_not_count(self):
        from apps.scm.containers.models import PlannedContainer

        other = Team.objects.create(name="resolve-planned-other", slug="resolve-planned-other")
        PlannedContainer.objects.create(
            team=other,
            container_number=self.container.container_id,
            carrier="maersk",
        )
        self.assertEqual(get_preferred_carrier_codes_for_container(self.team, self.container), [])

    def test_the_shipment_carrier_outranks_the_planned_container(self):
        from apps.scm.containers.models import PlannedContainer

        PlannedContainer.objects.create(
            team=self.team,
            container_number=self.container.container_id,
            carrier="msc",
        )
        shipment = Shipment.objects.create(team=self.team, shipment_number="SHP-3", carrier="Maersk")
        ShipmentContainer.objects.create(shipment=shipment, container=self.container)
        self.assertEqual(get_preferred_carrier_codes_for_container(self.team, self.container), ["maersk", "msc"])

    def test_the_same_carrier_is_not_listed_twice(self):
        from apps.scm.containers.models import PlannedContainer

        PlannedContainer.objects.create(
            team=self.team,
            container_number=self.container.container_id,
            carrier="maersk",
        )
        shipment = Shipment.objects.create(team=self.team, shipment_number="SHP-4", carrier="Maersk")
        ShipmentContainer.objects.create(shipment=shipment, container=self.container)
        self.assertEqual(get_preferred_carrier_codes_for_container(self.team, self.container), ["maersk"])


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
class SubscriptionFollowsVerifiedDataTest(TestCase):
    """A carrier becomes a container's tracking source by answering, not by being asked.

    The distinction these tests defend: resolving Maersk as the carrier to *try* is a
    transient decision inside one request, while a TrackingSubscription is a lasting
    claim that Maersk tracks this box. Only carrier data may turn one into the other —
    otherwise a container is silently locked to the first carrier anyone probed, and
    Hapag-Lloyd or MSC never get asked.
    """

    def setUp(self):
        self.team = Team.objects.create(name="verified-team", slug="verified-team")
        self.container = _container(self.team)
        self.integration = _maersk_integration(self.team)

    def _refresh(self, session, *, container=None, team=None):
        client = MaerskClient(self.integration, session=session)
        with mock.patch(
            "apps.scm.integrations.carriers.factory.build_carrier_client",
            return_value=client,
        ):
            return refresh_container_tracking(team=team or self.team, container=container or self.container)

    def _subscriptions(self):
        return TrackingSubscription.objects.filter(team=self.team, container=self.container)

    # -- Nothing tracks the container yet ---------------------------------

    def test_data_found_creates_the_subscription_and_the_events(self):
        result = self._refresh(FakeSession([FakeResponse(200, PAYLOAD)]))

        self.assertEqual(result.level, SUCCESS)
        self.assertTrue(result.tracked)
        subscription = self._subscriptions().get()
        self.assertEqual(subscription.provider.code, "maersk")
        self.assertEqual(subscription.status, TrackingSubscription.Status.ACTIVE)
        self.assertEqual(subscription.tracking_status, TrackingSubscription.TrackingStatus.TRACKING)
        self.assertEqual(TrackingEvent.objects.filter(team=self.team, container=self.container).count(), 2)

    def test_a_carrier_with_no_data_is_not_assigned(self):
        result = self._refresh(FakeSession([FakeResponse(404)]))

        self.assertEqual(result.state, NO_DATA)
        self.assertFalse(result.tracked)
        self.assertFalse(self._subscriptions().exists())
        self.assertEqual(TrackingEvent.objects.filter(team=self.team).count(), 0)

    def test_an_empty_but_successful_response_is_not_data(self):
        """HTTP 200 is not the test — a normalised event is."""
        result = self._refresh(FakeSession([FakeResponse(200, {"events": []})]))

        self.assertEqual(result.state, NO_DATA)
        self.assertFalse(self._subscriptions().exists())

    def test_an_authentication_failure_does_not_assign_the_carrier(self):
        result = self._refresh(FakeSession([FakeResponse(401), FakeResponse(401)]))

        self.assertEqual(result.state, UNAVAILABLE)
        self.assertFalse(result.tracked)
        self.assertFalse(self._subscriptions().exists())

    def test_a_timeout_does_not_assign_the_carrier(self):
        result = self._refresh(FakeSession(error=requests.Timeout("timed out")))

        self.assertEqual(result.state, UNAVAILABLE)
        self.assertFalse(self._subscriptions().exists())

    def test_a_server_error_does_not_assign_the_carrier(self):
        result = self._refresh(FakeSession([FakeResponse(500), FakeResponse(500)]))

        self.assertEqual(result.state, UNAVAILABLE)
        self.assertFalse(self._subscriptions().exists())

    def test_an_unverified_probe_leaves_no_sync_run_to_explain_later(self):
        """A sync run is a subscription's history; a probe has no subscription."""
        self._refresh(FakeSession([FakeResponse(404)]))
        self.assertEqual(TrackingSyncRun.objects.filter(team=self.team).count(), 0)

    def test_an_unverified_probe_is_still_recorded_as_a_request(self):
        """The call itself is not lost: request history keeps it, tracking does not."""
        from apps.scm.integrations.models import IntegrationRequestLog

        self._refresh(FakeSession([FakeResponse(404)]))
        log = IntegrationRequestLog.objects.filter(team=self.team, provider_code="maersk").latest("created_at")
        self.assertEqual(log.status_code, 404)

    def test_a_verified_refresh_records_a_sync_run(self):
        self._refresh(FakeSession([FakeResponse(200, PAYLOAD)]))
        sync_run = TrackingSyncRun.objects.get(team=self.team)
        self.assertEqual(sync_run.status, TrackingSyncRun.Status.SUCCESS)
        self.assertEqual(sync_run.events_created, 2)

    def test_the_raw_payload_is_kept_for_a_verified_result(self):
        self._refresh(FakeSession([FakeResponse(200, PAYLOAD)]))
        stored = TrackingRawPayload.objects.get(team=self.team)
        self.assertEqual(stored.payload_json, PAYLOAD)
        self.assertTrue(stored.parsed_successfully)
        self.assertEqual(stored.subscription, self._subscriptions().get())

    # -- Another carrier must stay reachable -------------------------------

    def test_a_carrier_that_had_nothing_can_be_replaced_by_one_that_does(self):
        """Maersk drawing a blank must not stop Hapag-Lloyd from being tried and winning."""
        from apps.scm.containers.models import PlannedContainer

        self._refresh(FakeSession([FakeResponse(404)]))
        self.assertFalse(self._subscriptions().exists())

        PlannedContainer.objects.create(
            team=self.team,
            container_number=self.container.container_id,
            carrier="hapag_lloyd",
        )
        Integration.objects.filter(pk=self.integration.pk).update(provider_code="hapag_lloyd")
        self.integration.refresh_from_db()
        self._refresh(FakeSession([FakeResponse(200, PAYLOAD)]))

        self.assertEqual(self._subscriptions().get().provider.code, "hapag_lloyd")

    # -- Once verified, a watch survives a bad day -------------------------

    def _track(self):
        """Establish a real subscription the way the product does: with carrier data."""
        self._refresh(FakeSession([FakeResponse(200, PAYLOAD)]))
        return self._subscriptions().get()

    def test_an_established_subscription_survives_a_later_empty_answer(self):
        subscription = self._track()
        result = self._refresh(FakeSession([FakeResponse(404)]))

        self.assertEqual(result.state, NO_DATA)
        self.assertTrue(result.tracked)
        self.assertEqual(self._subscriptions().count(), 1)
        subscription.refresh_from_db()
        self.assertEqual(subscription.status, TrackingSubscription.Status.ACTIVE)

    def test_an_established_subscription_survives_a_temporary_api_error(self):
        self._track()
        result = self._refresh(FakeSession([FakeResponse(500), FakeResponse(500)]))

        self.assertEqual(result.state, UNAVAILABLE)
        self.assertEqual(self._subscriptions().count(), 1)

    def test_an_established_subscription_keeps_its_events_through_an_empty_answer(self):
        self._track()
        self._refresh(FakeSession([FakeResponse(404)]))
        self.assertEqual(TrackingEvent.objects.filter(team=self.team, container=self.container).count(), 2)

    def test_a_second_successful_refresh_does_not_add_a_second_subscription(self):
        self._track()
        self._refresh(FakeSession([FakeResponse(200, PAYLOAD)]))
        self.assertEqual(self._subscriptions().count(), 1)

    # -- The shipment's carrier is nobody else's business ------------------

    def test_a_shipment_carrier_is_untouched_by_an_empty_answer(self):
        shipment = Shipment.objects.create(team=self.team, shipment_number="SHP-KEEP", carrier="Maersk")
        ShipmentContainer.objects.create(shipment=shipment, container=self.container)

        self._refresh(FakeSession([FakeResponse(404)]))

        shipment.refresh_from_db()
        self.assertEqual(shipment.carrier, "Maersk")
        self.assertFalse(self._subscriptions().exists())

    def test_a_shipment_carrier_is_untouched_by_a_carrier_error(self):
        shipment = Shipment.objects.create(team=self.team, shipment_number="SHP-KEEP-2", carrier="Maersk")
        ShipmentContainer.objects.create(shipment=shipment, container=self.container)

        self._refresh(FakeSession([FakeResponse(500), FakeResponse(500)]))

        shipment.refresh_from_db()
        self.assertEqual(shipment.carrier, "Maersk")
        self.assertFalse(self._subscriptions().exists())

    # -- Tenancy ------------------------------------------------------------

    def test_a_verified_subscription_belongs_to_one_team_only(self):
        other = Team.objects.create(name="verified-other", slug="verified-other")
        self._track()
        self.assertFalse(TrackingSubscription.objects.filter(team=other).exists())
        self.assertFalse(TrackingEvent.objects.filter(team=other).exists())

    def test_another_teams_subscription_does_not_make_this_container_tracked(self):
        """A neighbour's watch on the same box must not stand in for our own probe."""
        from apps.scm.integrations.carriers.auto_link import get_or_create_tracking_provider

        other = Team.objects.create(name="verified-neighbour", slug="verified-neighbour")
        TrackingSubscription.objects.create(
            team=other,
            provider=get_or_create_tracking_provider(carrier_code="maersk", carrier_name="Maersk"),
            container=self.container,
            tracking_reference=self.container.container_id,
        )

        self._refresh(FakeSession([FakeResponse(404)]))

        self.assertFalse(self._subscriptions().exists())
        self.assertEqual(TrackingSubscription.objects.filter(team=other).count(), 1)


@override_settings(CACHES=_LOCMEM)
class RefreshWithoutAnIntegrationTest(TestCase):
    """ "Carrier unknown" now means "nothing to ask", not "you did not tell us"."""

    def setUp(self):
        self.team = Team.objects.create(name="refresh-none", slug="refresh-none")

    def test_with_nothing_connected_the_user_is_told_to_connect_a_carrier(self):
        container = _container(self.team)
        result = refresh_container_tracking(team=self.team, container=container)
        self.assertEqual(result.level, ERROR)
        self.assertEqual(result.state, CARRIER_UNKNOWN)
        self.assertIn("no carrier integration is connected", str(result.message).lower())
        # Never the old advice: the user is not asked to name a carrier any more.
        self.assertNotIn("assign a carrier", str(result.message).lower())
        self.assertEqual(TrackingSubscription.objects.filter(team=self.team).count(), 0)

    def test_a_known_carrier_without_an_integration_creates_no_noise(self):
        """Nothing to call means no subscription and no sync run to explain later."""
        container = _container(self.team)
        shipment = Shipment.objects.create(team=self.team, shipment_number="SHP-NOINT", carrier="Maersk")
        ShipmentContainer.objects.create(shipment=shipment, container=container)
        result = refresh_container_tracking(team=self.team, container=container)
        self.assertEqual(result.level, WARNING)
        self.assertEqual(result.state, NOT_CONFIGURED)
        self.assertIn("not configured", str(result.message).lower())
        self.assertIn("Maersk", str(result.message))
        self.assertEqual(TrackingSubscription.objects.filter(team=self.team).count(), 0)
        self.assertEqual(TrackingSyncRun.objects.filter(team=self.team).count(), 0)


@override_settings(CACHES=_LOCMEM)
class MultiCarrierRefreshTest(TestCase):
    """A container with no verified tracking source is swept across the team's carriers.

    The user never names the carrier. What matters is that the sweep stops at the
    first carrier with real data, that nothing is written before then, and that a
    carrier's silence or outage does not stop the ones behind it in the queue.
    """

    CARRIERS = ("maersk", "cma_cgm", "cosco")

    def setUp(self):
        self.team = Team.objects.create(name="sweep-refresh", slug="sweep-refresh")
        self.container = _container(self.team)
        for code in self.CARRIERS:
            _carrier_integration(self.team, code)

    def _refresh(self, behaviour, *, container=None, events_by_code=None):
        """Run a refresh where each carrier behaves as ``behaviour`` says.

        ``behaviour`` maps a provider code to a payload or a carrier exception; any
        carrier not named answers with nothing.
        """
        clients = {code: _fake_client(code, behaviour.get(code)) for code in self.CARRIERS}
        with _patch_carriers(clients, events_by_code or {}):
            result = refresh_container_tracking(team=self.team, container=container or self.container)
        return result, clients

    def _subscriptions(self):
        return TrackingSubscription.objects.filter(team=self.team, container=self.container)

    # -- A carrier is found -------------------------------------------------

    def test_the_first_candidate_can_answer_immediately(self):
        shipment = Shipment.objects.create(team=self.team, shipment_number="SHP-SWEEP", carrier="CMA CGM")
        ShipmentContainer.objects.create(shipment=shipment, container=self.container)

        result, clients = self._refresh(
            {"cma_cgm": {"events": [{"id": 1}]}},
            events_by_code={"cma_cgm": [_normalised_event(self.container.container_id)]},
        )

        self.assertEqual(result.level, SUCCESS)
        self.assertEqual(result.carrier_code, "cma_cgm")
        self.assertEqual(clients["maersk"].calls, [], "the shipment's carrier answered, so nobody else is asked")
        self.assertEqual(self._subscriptions().get().provider.code, "cma_cgm")

    def test_a_later_carrier_wins_when_the_shipments_carrier_has_nothing(self):
        """A shipment's carrier field can be stale — NOT_FOUND must not end the sweep."""
        shipment = Shipment.objects.create(team=self.team, shipment_number="SHP-STALE", carrier="Maersk")
        ShipmentContainer.objects.create(shipment=shipment, container=self.container)

        result, clients = self._refresh(
            {"maersk": CarrierNoDataError("404"), "cosco": {"events": [{"id": 1}]}},
            events_by_code={"cosco": [_normalised_event(self.container.container_id)]},
        )

        self.assertEqual(result.level, SUCCESS)
        self.assertEqual(result.carrier_code, "cosco")
        self.assertEqual(clients["maersk"].calls, [self.container.container_id])
        self.assertEqual(self._subscriptions().get().provider.code, "cosco")

    def test_a_broken_carrier_does_not_stop_the_one_behind_it(self):
        result, _ = self._refresh(
            {"cma_cgm": CarrierTimeoutError("timed out"), "cosco": {"events": [{"id": 1}]}},
            events_by_code={"cosco": [_normalised_event(self.container.container_id)]},
        )

        self.assertEqual(result.level, SUCCESS)
        self.assertEqual(result.carrier_code, "cosco")

    def test_the_message_names_the_carrier_that_was_found(self):
        result, _ = self._refresh(
            {"cosco": {"events": [{"id": 1}]}},
            events_by_code={"cosco": [_normalised_event(self.container.container_id)]},
        )
        self.assertIn("COSCO Shipping", str(result.message))
        self.assertIn("1 tracking events retrieved", str(result.message))

    def test_the_events_go_through_the_normal_tracking_write_path(self):
        result, _ = self._refresh(
            {"cosco": {"events": [{"id": 1}]}},
            events_by_code={"cosco": [_normalised_event(self.container.container_id)]},
        )

        subscription = self._subscriptions().get()
        self.assertEqual(TrackingEvent.objects.filter(team=self.team, container=self.container).count(), 1)
        self.assertEqual(TrackingRawPayload.objects.get(team=self.team).subscription, subscription)
        run = TrackingSyncRun.objects.get(team=self.team)
        self.assertEqual(run.subscription, subscription)
        self.assertEqual(run.status, TrackingSyncRun.Status.SUCCESS)
        self.assertEqual(result.sync_run, run)

    def test_finding_tracking_does_not_reassign_the_shipments_carrier(self):
        """Tracking source and booked carrier stay separate concepts."""
        shipment = Shipment.objects.create(team=self.team, shipment_number="SHP-KEEPS", carrier="Maersk")
        ShipmentContainer.objects.create(shipment=shipment, container=self.container)

        self._refresh(
            {"maersk": CarrierNoDataError("404"), "cosco": {"events": [{"id": 1}]}},
            events_by_code={"cosco": [_normalised_event(self.container.container_id)]},
        )

        shipment.refresh_from_db()
        self.assertEqual(shipment.carrier, "Maersk")
        self.assertEqual(self._subscriptions().get().provider.code, "cosco")

    def test_a_verified_container_is_not_swept_again(self):
        self._refresh(
            {"cosco": {"events": [{"id": 1}]}},
            events_by_code={"cosco": [_normalised_event(self.container.container_id)]},
        )
        _, clients = self._refresh(
            {"cosco": {"events": [{"id": 1}]}},
            events_by_code={"cosco": [_normalised_event(self.container.container_id)]},
        )

        self.assertEqual(clients["cosco"].calls, [self.container.container_id])
        self.assertEqual(clients["maersk"].calls, [])
        self.assertEqual(clients["cma_cgm"].calls, [])
        self.assertEqual(self._subscriptions().count(), 1)

    # -- Nobody has the container -------------------------------------------

    def test_all_carriers_without_data_reports_how_many_were_checked(self):
        result, clients = self._refresh({code: CarrierNoDataError("404") for code in self.CARRIERS})

        self.assertEqual(result.level, INFO)
        self.assertEqual(result.state, NO_DATA)
        self.assertIn("Checked 3 carriers", str(result.message))
        self.assertIn("COSCO Shipping", str(result.message))
        self.assertEqual(sorted(result.carriers_checked), ["CMA CGM", "COSCO Shipping", "Maersk"])
        for code in self.CARRIERS:
            self.assertEqual(clients[code].calls, [self.container.container_id])

    def test_no_data_creates_no_subscription_and_no_sync_run(self):
        self._refresh({code: CarrierNoDataError("404") for code in self.CARRIERS})

        self.assertFalse(self._subscriptions().exists())
        self.assertEqual(TrackingSyncRun.objects.filter(team=self.team).count(), 0)
        self.assertEqual(TrackingEvent.objects.filter(team=self.team).count(), 0)

    def test_all_carriers_unusable_is_a_configuration_problem(self):
        result, _ = self._refresh({code: CarrierConfigurationError("no credentials") for code in self.CARRIERS})

        self.assertEqual(result.level, WARNING)
        self.assertEqual(result.state, NOT_CONFIGURED)
        self.assertIn("not configured", str(result.message).lower())
        self.assertFalse(self._subscriptions().exists())

    def test_all_carriers_failing_is_an_outage_not_an_empty_result(self):
        result, _ = self._refresh({code: CarrierTimeoutError("timed out") for code in self.CARRIERS})

        self.assertEqual(result.level, ERROR)
        self.assertEqual(result.state, UNAVAILABLE)
        self.assertIn("temporarily unavailable", str(result.message).lower())
        self.assertNotIn("timed out", str(result.message))
        self.assertFalse(self._subscriptions().exists())

    def test_a_partial_sweep_is_flagged_rather_than_reported_as_a_clean_miss(self):
        result, _ = self._refresh(
            {
                "maersk": CarrierNoDataError("404"),
                "cma_cgm": CarrierTimeoutError("timed out"),
                "cosco": CarrierConfigurationError("no credentials"),
            }
        )

        self.assertEqual(result.level, WARNING)
        self.assertEqual(result.state, NO_DATA)
        self.assertIn("2 further carrier(s) could not be checked", str(result.message))
        self.assertFalse(self._subscriptions().exists())

    def test_a_carrier_error_is_never_quoted_back_to_the_user(self):
        result, _ = self._refresh({code: CarrierTimeoutError(f"secret-{code}") for code in self.CARRIERS})
        for code in self.CARRIERS:
            self.assertNotIn(f"secret-{code}", str(result.message))

    # -- An explicitly named carrier that cannot be called --------------------

    def test_an_unconfigured_shipment_carrier_falls_back_to_the_connected_ones(self):
        """The shipment names Hapag-Lloyd, which is not connected. Keep going."""
        shipment = Shipment.objects.create(team=self.team, shipment_number="SHP-GAP", carrier="Hapag-Lloyd")
        ShipmentContainer.objects.create(shipment=shipment, container=self.container)

        result, clients = self._refresh(
            {"cosco": {"events": [{"id": 1}]}},
            events_by_code={"cosco": [_normalised_event(self.container.container_id)]},
        )

        self.assertEqual(result.level, SUCCESS)
        self.assertEqual(result.carrier_code, "cosco")
        self.assertEqual(self._subscriptions().get().provider.code, "cosco")
        # Hapag-Lloyd was never called: there is no integration to call it with.
        self.assertNotIn("Hapag-Lloyd", result.carriers_checked)

    def test_an_unconfigured_planned_carrier_falls_back_to_the_connected_ones(self):
        from apps.scm.containers.models import PlannedContainer

        PlannedContainer.objects.create(
            team=self.team,
            container_number=self.container.container_id,
            carrier="hapag_lloyd",
        )

        result, _ = self._refresh(
            {"cosco": {"events": [{"id": 1}]}},
            events_by_code={"cosco": [_normalised_event(self.container.container_id)]},
        )

        self.assertEqual(result.level, SUCCESS)
        self.assertEqual(self._subscriptions().get().provider.code, "cosco")

    def test_an_unconfigured_named_carrier_alone_is_still_a_configuration_problem(self):
        """With nothing else to fall back to, the gap is what the user needs to hear."""
        team = Team.objects.create(name="sweep-gap-only", slug="sweep-gap-only")
        container = _container(team, serial="925897", check_digit=9)
        shipment = Shipment.objects.create(team=team, shipment_number="SHP-ONLY", carrier="Hapag-Lloyd")
        ShipmentContainer.objects.create(shipment=shipment, container=container)

        result = refresh_container_tracking(team=team, container=container)

        self.assertEqual(result.state, NOT_CONFIGURED)
        self.assertIn("Hapag-Lloyd", str(result.message))

    # -- Every kind of technical failure is survivable ------------------------

    def test_an_authentication_failure_does_not_end_the_sweep(self):
        from apps.scm.integrations.carriers.exceptions import CarrierAuthenticationError

        result, clients = self._refresh(
            {"cma_cgm": CarrierAuthenticationError("bad key sk-live-123"), "cosco": {"events": [{"id": 1}]}},
            events_by_code={"cosco": [_normalised_event(self.container.container_id)]},
        )

        self.assertEqual(result.level, SUCCESS)
        self.assertEqual(result.carrier_code, "cosco")
        self.assertEqual(clients["cma_cgm"].calls, [self.container.container_id])
        self.assertNotIn("sk-live-123", str(result.message))

    def test_a_server_error_does_not_end_the_sweep(self):
        from apps.scm.integrations.carriers.exceptions import CarrierServerError

        result, _ = self._refresh(
            {"cma_cgm": CarrierServerError("500 <html>stack trace</html>"), "cosco": {"events": [{"id": 1}]}},
            events_by_code={"cosco": [_normalised_event(self.container.container_id)]},
        )

        self.assertEqual(result.level, SUCCESS)
        self.assertEqual(result.carrier_code, "cosco")
        self.assertNotIn("stack trace", str(result.message))

    def test_no_carrier_response_body_survives_into_a_failure_message(self):
        from apps.scm.integrations.carriers.exceptions import (
            CarrierAuthenticationError,
            CarrierServerError,
        )

        result, _ = self._refresh(
            {
                "maersk": CarrierAuthenticationError("consumer-key refresh-secret-key rejected"),
                "cma_cgm": CarrierServerError("<html>stack trace</html>"),
                "cosco": CarrierTimeoutError("read timeout to api.cosco.example"),
            }
        )

        rendered = str(result.message)
        for leak in ("refresh-secret-key", "stack trace", "api.cosco.example", "consumer-key"):
            self.assertNotIn(leak, rendered)

    # -- Exactly one subscription, for the carrier that answered --------------

    def test_only_the_carrier_that_answered_gets_a_subscription(self):
        result, _ = self._refresh(
            {
                "maersk": CarrierNoDataError("404"),
                "cma_cgm": CarrierTimeoutError("timed out"),
                "cosco": {"events": [{"id": 1}]},
            },
            events_by_code={"cosco": [_normalised_event(self.container.container_id)]},
        )

        self.assertEqual(result.carrier_code, "cosco")
        self.assertEqual(self._subscriptions().count(), 1)
        self.assertEqual(
            list(TrackingSubscription.objects.filter(team=self.team).values_list("provider__code", flat=True)),
            ["cosco"],
        )

    # -- Two refreshes at once ------------------------------------------------

    def test_a_second_refresh_while_one_is_sweeping_is_told_to_wait(self):
        """The lock is what stops a double click costing two full sweeps."""
        from apps.scm.integrations.locks import resource_lock
        from apps.scm.tracking.manual_refresh import (
            CONTAINER_DISCOVERY_LOCK_PREFIX,
            CONTAINER_DISCOVERY_LOCK_TTL_SECONDS,
        )

        clients = {code: _fake_client(code, {"events": [{"id": 1}]}) for code in self.CARRIERS}
        with (
            resource_lock(
                f"container:{self.container.pk}",
                ttl=CONTAINER_DISCOVERY_LOCK_TTL_SECONDS,
                prefix=CONTAINER_DISCOVERY_LOCK_PREFIX,
            ),
            _patch_carriers(
                clients, {code: [_normalised_event(self.container.container_id)] for code in self.CARRIERS}
            ),
        ):
            result = refresh_container_tracking(team=self.team, container=self.container)

        self.assertEqual(result.state, IN_PROGRESS)
        self.assertFalse(self._subscriptions().exists())
        for code in self.CARRIERS:
            self.assertEqual(clients[code].calls, [], "no carrier is asked while another sweep holds the lock")

    def test_a_lost_race_still_leaves_one_subscription_and_no_duplicate_events(self):
        """Belt and braces: if two sweeps did interleave, the writes are idempotent."""
        events = [_normalised_event(self.container.container_id)]
        behaviour = {"cosco": {"events": [{"id": 1}]}}

        self._refresh(behaviour, events_by_code={"cosco": events})
        # Force the second refresh down the discovery path as if it had never seen
        # the subscription the first one created — the race, without the threads.
        with mock.patch("apps.scm.tracking.manual_refresh.get_verified_container_subscriptions", return_value=[]):
            second, _ = self._refresh(behaviour, events_by_code={"cosco": events})

        self.assertEqual(self._subscriptions().count(), 1)
        self.assertEqual(TrackingEvent.objects.filter(team=self.team, container=self.container).count(), 1)
        self.assertEqual(second.events_created, 0)

    # -- Tenancy -------------------------------------------------------------

    def test_another_teams_carriers_are_never_swept(self):
        other = Team.objects.create(name="sweep-other", slug="sweep-other")
        other_container = _container(other, serial="925897", check_digit=9)

        clients = {code: _fake_client(code, {"events": [{"id": 1}]}) for code in self.CARRIERS}
        with _patch_carriers(
            clients, {code: [_normalised_event(other_container.container_id)] for code in self.CARRIERS}
        ):
            result = refresh_container_tracking(team=other, container=other_container)

        self.assertEqual(result.state, CARRIER_UNKNOWN)
        for code in self.CARRIERS:
            self.assertEqual(clients[code].calls, [])
        self.assertFalse(TrackingSubscription.objects.filter(team=other).exists())


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
        self.assertIn("Tracking found via Maersk", text)
        self.assertIn("2 tracking events retrieved", text)
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
