"""Tests for planned-container discovery.

Container numbers here are real ISO 6346 numbers with valid check digits, because
registration now validates them. Carrier clients are always injected fakes.
"""

from datetime import timedelta

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone

from apps.scm.containers.discovery import (
    DEFAULT_MAX_ATTEMPTS,
    add_planned_container,
    cancel_planned_container,
    check_planned_container,
    expire_exhausted_planned_containers,
    get_planned_containers,
    get_planned_containers_for_discovery,
    is_exhausted,
    mark_planned_container_arrived,
    mark_planned_container_detected,
    mark_planned_container_in_transit,
    run_discovery_for_team,
)
from apps.scm.containers.models import (
    Container,
    EquipmentType,
    PlannedContainer,
    PlannedContainerResult,
    PlannedContainerStatus,
)
from apps.scm.integrations.carriers.base import BaseCarrierClient, CarrierCapability
from apps.scm.integrations.carriers.dcsa.schemas import NormalisedTrackingEvent
from apps.scm.integrations.carriers.exceptions import (
    CarrierNoDataError,
    CarrierNotImplementedError,
    CarrierTimeoutError,
)
from apps.scm.integrations.models import Integration
from apps.scm.shipments.models import Shipment, ShipmentContainer
from apps.scm.tracking.models import TrackingSubscription
from apps.teams.models import Team

# Valid ISO 6346 numbers (Maersk-owned prefix MRKU, so the carrier can be suggested).
MRKU_1 = "MRKU1234563"
MRKU_2 = "MRKU2345685"
MCUU_1 = "MCUU1000000"
MCUU_2 = "MCUU2000004"
MCUU_3 = "MCUU3000009"
MCUU_4 = "MCUU4000003"
MCUU_5 = "MCUU5000008"
MCUU_6 = "MCUU1000015"


def _team(slug):
    return Team.objects.get_or_create(slug=slug, defaults={"name": slug})[0]


def _equipment_type():
    return EquipmentType.objects.get_or_create(
        iso_code="22G1",
        defaults={"category": "GP", "length_ft": 20, "high_cube": False, "description": "20' GP"},
    )[0]


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
    """Carrier client that reports a container as known, unknown, or broken."""

    capabilities = CarrierCapability(supports_pull=True, supports_tracking_by_container=True)

    def __init__(self, provider_code="maersk", *, payload=None, error=None):
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
    provider_code = "maersk"

    def __init__(self, events=None):
        self.events = events or []

    def parse_tracking_events(self, raw_payload):
        return list(self.events)


def _patch_parser(events):
    from unittest import mock

    return mock.patch(
        "apps.scm.integrations.carriers.factory.build_carrier_parser",
        return_value=FakeParser(events),
    )


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


class AddPlannedContainerTest(TestCase):
    def test_creates_planned_container(self):
        team = _team("disc-add")
        planned = add_planned_container(team, MCUU_1)
        self.assertEqual(planned.container_number, MCUU_1)
        self.assertEqual(planned.status, PlannedContainerStatus.PLANNED)
        self.assertEqual(planned.last_result, PlannedContainerResult.PENDING)
        self.assertEqual(planned.team, team)

    def test_uppercases_and_strips_container_number(self):
        team = _team("disc-upper")
        planned = add_planned_container(team, f"  {MCUU_2.lower()} ")
        self.assertEqual(planned.container_number, MCUU_2)

    def test_rejects_malformed_container_number(self):
        team = _team("disc-malformed")
        with self.assertRaises(ValidationError):
            add_planned_container(team, "NOT-A-CONTAINER")

    def test_rejects_invalid_check_digit(self):
        """A typo must be caught at registration, not after days of polling."""
        team = _team("disc-checkdigit")
        with self.assertRaises(ValidationError):
            add_planned_container(team, "MCUU1000001")  # correct check digit is 0

    def test_idempotent_add(self):
        team = _team("disc-idempotent")
        first = add_planned_container(team, MCUU_3)
        second = add_planned_container(team, MCUU_3)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(PlannedContainer.objects.filter(team=team, container_number=MCUU_3).count(), 1)

    def test_team_isolation(self):
        team1 = _team("disc-iso-1")
        team2 = _team("disc-iso-2")
        add_planned_container(team1, MCUU_4)
        add_planned_container(team2, MCUU_4)
        self.assertEqual(PlannedContainer.objects.filter(container_number=MCUU_4).count(), 2)
        self.assertEqual(PlannedContainer.objects.filter(team=team1, container_number=MCUU_4).count(), 1)

    def test_owner_prefix_suggests_a_carrier(self):
        team = _team("disc-prefix")
        planned = add_planned_container(team, MRKU_1)
        self.assertEqual(planned.carrier, "maersk")

    def test_explicit_carrier_wins_over_prefix_suggestion(self):
        """The prefix is a hint; a leased box may be moving with another carrier."""
        team = _team("disc-explicit-carrier")
        planned = add_planned_container(team, MRKU_2, carrier="Hapag-Lloyd")
        self.assertEqual(planned.carrier, "hapag_lloyd")

    def test_unknown_owner_prefix_leaves_carrier_empty(self):
        team = _team("disc-unknown-prefix")
        planned = add_planned_container(team, MCUU_5)
        self.assertEqual(planned.carrier, "")

    def test_max_attempts_and_expiry_are_configurable(self):
        team = _team("disc-limits")
        planned = add_planned_container(team, MCUU_6, max_attempts=3, expires_in_days=5)
        self.assertEqual(planned.max_attempts, 3)
        self.assertIsNotNone(planned.expires_at)


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


class StatusTransitionTest(TestCase):
    def setUp(self):
        self.team = _team("disc-transitions")

    def test_mark_detected(self):
        planned = add_planned_container(self.team, MCUU_1)
        updated = mark_planned_container_detected(planned)
        self.assertEqual(updated.status, PlannedContainerStatus.DETECTED)
        self.assertEqual(updated.last_result, PlannedContainerResult.DETECTED)
        self.assertIsNotNone(updated.detected_at)
        self.assertIsNone(updated.next_check_at)

    def test_mark_in_transit(self):
        planned = add_planned_container(self.team, MCUU_2)
        mark_planned_container_detected(planned)
        self.assertEqual(mark_planned_container_in_transit(planned).status, PlannedContainerStatus.IN_TRANSIT)

    def test_mark_arrived(self):
        planned = add_planned_container(self.team, MCUU_3)
        self.assertEqual(mark_planned_container_arrived(planned).status, PlannedContainerStatus.ARRIVED)

    def test_cancel(self):
        planned = add_planned_container(self.team, MCUU_4)
        self.assertEqual(cancel_planned_container(planned).status, PlannedContainerStatus.CANCELLED)


class ExhaustionTest(TestCase):
    def setUp(self):
        self.team = _team("disc-exhaust")

    def test_fresh_container_is_not_exhausted(self):
        self.assertFalse(is_exhausted(add_planned_container(self.team, MCUU_1)))

    def test_attempt_limit_exhausts(self):
        planned = add_planned_container(self.team, MCUU_2, max_attempts=2)
        planned.attempts = 2
        self.assertTrue(is_exhausted(planned))

    def test_default_attempt_limit_applies(self):
        planned = add_planned_container(self.team, MCUU_3)
        planned.attempts = DEFAULT_MAX_ATTEMPTS
        self.assertTrue(is_exhausted(planned))

    def test_expiry_time_exhausts(self):
        planned = add_planned_container(self.team, MCUU_4)
        planned.expires_at = timezone.now() - timedelta(hours=1)
        self.assertTrue(is_exhausted(planned))

    def test_expired_containers_are_taken_out_of_the_queue(self):
        planned = add_planned_container(self.team, MCUU_5, max_attempts=1)
        PlannedContainer.objects.filter(pk=planned.pk).update(attempts=1)
        self.assertEqual(expire_exhausted_planned_containers(self.team), 1)
        planned.refresh_from_db()
        self.assertEqual(planned.status, PlannedContainerStatus.EXPIRED)
        self.assertNotIn(planned, get_planned_containers_for_discovery(self.team))


# ---------------------------------------------------------------------------
# Queue selection
# ---------------------------------------------------------------------------


class DiscoveryQueueTest(TestCase):
    def setUp(self):
        self.team = _team("disc-queue")

    def test_filters_by_status(self):
        add_planned_container(self.team, MCUU_1)
        detected = add_planned_container(self.team, MCUU_2)
        mark_planned_container_detected(detected)
        planned = list(get_planned_containers(self.team, status="planned"))
        self.assertEqual([p.container_number for p in planned], [MCUU_1])

    def test_returns_all_without_filter(self):
        add_planned_container(self.team, MCUU_1)
        add_planned_container(self.team, MCUU_2)
        self.assertEqual(get_planned_containers(self.team).count(), 2)

    def test_queue_contains_only_planned(self):
        add_planned_container(self.team, MCUU_3)
        detected = add_planned_container(self.team, MCUU_4)
        mark_planned_container_detected(detected)
        queue = list(get_planned_containers_for_discovery(self.team))
        self.assertEqual([p.container_number for p in queue], [MCUU_3])

    def test_future_next_check_is_not_due(self):
        planned = add_planned_container(self.team, MCUU_5)
        PlannedContainer.objects.filter(pk=planned.pk).update(next_check_at=timezone.now() + timedelta(hours=2))
        self.assertNotIn(planned, get_planned_containers_for_discovery(self.team))

    def test_team_scoped(self):
        other = _team("disc-queue-other")
        mine = add_planned_container(self.team, MCUU_1)
        theirs = add_planned_container(other, MCUU_1)
        queue = list(get_planned_containers_for_discovery(self.team))
        self.assertIn(mine, queue)
        self.assertNotIn(theirs, queue)


# ---------------------------------------------------------------------------
# Discovery run
# ---------------------------------------------------------------------------


class CheckPlannedContainerTest(TestCase):
    def setUp(self):
        self.team = _team("disc-check")
        _equipment_type()

    def test_carrier_with_events_promotes_the_container(self):
        planned = add_planned_container(self.team, MRKU_1)
        client = FakeCarrierClient(payload={"events": [{"eventID": "E1"}]})
        with _patch_parser([_normalised_event(MRKU_1)]):
            result = check_planned_container(planned, client=client)

        self.assertEqual(result, PlannedContainerResult.DETECTED)
        planned.refresh_from_db()
        self.assertEqual(planned.status, PlannedContainerStatus.DETECTED)
        self.assertIsNotNone(planned.container_id)

    def test_detection_creates_container_and_subscription_together(self):
        planned = add_planned_container(self.team, MRKU_1)
        client = FakeCarrierClient(payload={"events": [{"eventID": "E1"}]})
        with _patch_parser([_normalised_event(MRKU_1)]):
            check_planned_container(planned, client=client)

        container = Container.objects.get(team=self.team, owner_code="MRK", serial_number="123456")
        self.assertTrue(
            TrackingSubscription.objects.filter(team=self.team, container=container).exists(),
            "A detected container must get tracking in the same transaction",
        )

    def test_detection_links_to_the_planned_shipment(self):
        shipment = Shipment.objects.create(team=self.team, shipment_number="SHP-DISC-1", reference="REF-1")
        planned = add_planned_container(self.team, MRKU_1, shipment=shipment)
        client = FakeCarrierClient(payload={"events": [{"eventID": "E1"}]})
        with _patch_parser([_normalised_event(MRKU_1)]):
            check_planned_container(planned, client=client)

        container = Container.objects.get(team=self.team, owner_code="MRK", serial_number="123456")
        self.assertTrue(ShipmentContainer.objects.filter(shipment=shipment, container=container).exists())

    def test_carrier_without_data_creates_nothing(self):
        planned = add_planned_container(self.team, MRKU_1)
        client = FakeCarrierClient(error=CarrierNoDataError("404"))
        result = check_planned_container(planned, client=client)

        self.assertEqual(result, PlannedContainerResult.NOT_FOUND)
        planned.refresh_from_db()
        self.assertEqual(planned.status, PlannedContainerStatus.PLANNED)
        self.assertEqual(planned.attempts, 1)
        self.assertEqual(Container.objects.filter(team=self.team).count(), 0)
        self.assertEqual(Shipment.objects.filter(team=self.team).count(), 0)
        self.assertEqual(TrackingSubscription.objects.filter(team=self.team).count(), 0)

    def test_empty_event_list_is_not_found_not_detected(self):
        planned = add_planned_container(self.team, MRKU_1)
        with _patch_parser([]):
            result = check_planned_container(planned, client=FakeCarrierClient(payload={"events": []}))
        self.assertEqual(result, PlannedContainerResult.NOT_FOUND)

    def test_not_found_schedules_the_next_check(self):
        planned = add_planned_container(self.team, MRKU_1)
        check_planned_container(planned, client=FakeCarrierClient(error=CarrierNoDataError("404")))
        planned.refresh_from_db()
        self.assertIsNotNone(planned.next_check_at)
        self.assertGreater(planned.next_check_at, timezone.now())

    def test_stub_adapter_is_skipped_and_does_not_consume_an_attempt(self):
        """An unimplemented carrier must not burn the attempt budget."""
        planned = add_planned_container(self.team, MRKU_1)
        result = check_planned_container(planned, client=FakeCarrierClient(error=CarrierNotImplementedError("stub")))
        self.assertEqual(result, PlannedContainerResult.SKIPPED)
        planned.refresh_from_db()
        self.assertEqual(planned.attempts, 0)
        self.assertEqual(planned.status, PlannedContainerStatus.PLANNED)

    def test_carrier_error_is_recorded_as_an_error(self):
        planned = add_planned_container(self.team, MRKU_1)
        result = check_planned_container(planned, client=FakeCarrierClient(error=CarrierTimeoutError("timed out")))
        self.assertEqual(result, PlannedContainerResult.ERROR)
        planned.refresh_from_db()
        self.assertEqual(planned.last_result, PlannedContainerResult.ERROR)
        self.assertIn("timed out", planned.last_error_message)

    def test_no_carrier_means_nothing_is_asked(self):
        """Without a carrier we do not query every carrier in the registry."""
        planned = add_planned_container(self.team, MCUU_5)  # unknown prefix → no carrier
        result = check_planned_container(planned)
        self.assertEqual(result, PlannedContainerResult.SKIPPED)
        planned.refresh_from_db()
        self.assertEqual(planned.attempts, 0)

    def test_attempt_exhaustion_expires_the_container(self):
        planned = add_planned_container(self.team, MRKU_1, max_attempts=1)
        check_planned_container(planned, client=FakeCarrierClient(error=CarrierNoDataError("404")))
        planned.refresh_from_db()
        self.assertEqual(planned.status, PlannedContainerStatus.EXPIRED)

    def test_repeated_detection_is_idempotent(self):
        planned = add_planned_container(self.team, MRKU_1)
        client = FakeCarrierClient(payload={"events": [{"eventID": "E1"}]})
        with _patch_parser([_normalised_event(MRKU_1)]):
            check_planned_container(planned, client=client)
            planned.status = PlannedContainerStatus.PLANNED
            planned.save(update_fields=["status"])
            check_planned_container(planned, client=client)

        self.assertEqual(Container.objects.filter(team=self.team, owner_code="MRK").count(), 1)
        self.assertEqual(TrackingSubscription.objects.filter(team=self.team).count(), 1)


class PlannedCarrierFallbackTest(TestCase):
    """The carrier a number was registered with is a starting point, not a verdict.

    A packing list often names the wrong carrier, or none at all. Rather than giving
    up, a pass falls back to the team's other connected carriers — the same sweep the
    container detail page uses — and promotes the container with whichever one
    actually answered.
    """

    def setUp(self):
        self.team = _team("disc-fallback")
        _equipment_type()
        for code in ("maersk", "cosco"):
            Integration.objects.create(
                team=self.team,
                name=code,
                provider_code=code,
                provider_family=Integration.ProviderFamily.CARRIER,
                is_active=True,
            )

    def _check(self, planned, behaviour, events_by_code=None):
        """Run one pass with each carrier behaving as ``behaviour`` says."""
        from unittest import mock

        clients = {
            code: FakeCarrierClient(code, error=value)
            if isinstance(value, Exception)
            else FakeCarrierClient(code, payload=value)
            for code, value in behaviour.items()
        }
        events_by_code = events_by_code or {}
        with mock.patch.multiple(
            "apps.scm.integrations.carriers.factory",
            build_carrier_client=mock.Mock(side_effect=lambda code, **kwargs: clients[code]),
            build_carrier_parser=mock.Mock(side_effect=lambda code: FakeParser(events_by_code.get(code, []))),
        ):
            return check_planned_container(planned), clients

    def test_the_planned_carrier_is_asked_first(self):
        planned = add_planned_container(self.team, MRKU_1, carrier="cosco")
        result, clients = self._check(
            planned,
            {"cosco": {"events": [{"eventID": "E1"}]}, "maersk": {"events": []}},
            events_by_code={"cosco": [_normalised_event(MRKU_1)]},
        )

        self.assertEqual(result, PlannedContainerResult.DETECTED)
        self.assertEqual(clients["cosco"].calls, [MRKU_1])
        self.assertEqual(clients["maersk"].calls, [], "the planned carrier answered, so nobody else is asked")

    def test_a_fallback_carrier_can_find_the_container(self):
        planned = add_planned_container(self.team, MRKU_1, carrier="maersk")
        result, clients = self._check(
            planned,
            {"maersk": CarrierNoDataError("404"), "cosco": {"events": [{"eventID": "E1"}]}},
            events_by_code={"cosco": [_normalised_event(MRKU_1)]},
        )

        self.assertEqual(result, PlannedContainerResult.DETECTED)
        self.assertEqual(clients["maersk"].calls, [MRKU_1])
        self.assertEqual(clients["cosco"].calls, [MRKU_1])

    def test_promotion_uses_the_carrier_that_returned_the_data(self):
        planned = add_planned_container(self.team, MRKU_1, carrier="maersk")
        self._check(
            planned,
            {"maersk": CarrierNoDataError("404"), "cosco": {"events": [{"eventID": "E1"}]}},
            events_by_code={"cosco": [_normalised_event(MRKU_1)]},
        )

        container = Container.objects.get(team=self.team, owner_code="MRK", serial_number="123456")
        subscription = TrackingSubscription.objects.get(team=self.team, container=container)
        self.assertEqual(subscription.provider.code, "cosco")
        planned.refresh_from_db()
        self.assertEqual(planned.carrier, "cosco")

    def test_a_number_without_a_carrier_still_gets_swept(self):
        """Registering a number from a packing list should not require naming a carrier."""
        planned = add_planned_container(self.team, MCUU_5)  # unknown owner prefix
        self.assertEqual(planned.carrier, "")

        result, clients = self._check(
            planned,
            # Both connected carriers are asked, in a stable order, until one answers.
            {"cosco": CarrierNoDataError("404"), "maersk": {"events": [{"eventID": "E1"}]}},
            events_by_code={"maersk": [_normalised_event(MCUU_5)]},
        )

        self.assertEqual(result, PlannedContainerResult.DETECTED)
        self.assertEqual(clients["cosco"].calls, [MCUU_5])
        self.assertEqual(clients["maersk"].calls, [MCUU_5])

    def test_an_unconfigured_planned_carrier_falls_back_to_the_connected_ones(self):
        """Hapag-Lloyd is named but not connected, so it is skipped rather than fatal."""
        planned = add_planned_container(self.team, MRKU_1, carrier="hapag_lloyd")
        self.assertEqual(planned.carrier, "hapag_lloyd")

        result, clients = self._check(
            planned,
            {"cosco": CarrierNoDataError("404"), "maersk": {"events": [{"eventID": "E1"}]}},
            events_by_code={"maersk": [_normalised_event(MRKU_1)]},
        )

        self.assertEqual(result, PlannedContainerResult.DETECTED)
        self.assertNotIn("hapag_lloyd", clients, "an unconnected carrier is never built or called")
        planned.refresh_from_db()
        self.assertEqual(planned.carrier, "maersk")

    def test_one_pass_over_many_carriers_is_one_attempt(self):
        """The attempt budget limits how often a number is chased, not how widely."""
        planned = add_planned_container(self.team, MRKU_1, carrier="maersk")
        result, _ = self._check(planned, {code: CarrierNoDataError("404") for code in ("maersk", "cosco")})

        self.assertEqual(result, PlannedContainerResult.NOT_FOUND)
        planned.refresh_from_db()
        self.assertEqual(planned.attempts, 1)

    def test_a_sweep_that_finds_nothing_writes_no_tracking_records(self):
        planned = add_planned_container(self.team, MRKU_1, carrier="maersk")
        self._check(planned, {code: CarrierNoDataError("404") for code in ("maersk", "cosco")})

        self.assertEqual(Container.objects.filter(team=self.team).count(), 0)
        self.assertEqual(TrackingSubscription.objects.filter(team=self.team).count(), 0)
        planned.refresh_from_db()
        self.assertEqual(planned.status, PlannedContainerStatus.PLANNED)

    def test_a_broken_carrier_does_not_stop_the_next_one(self):
        planned = add_planned_container(self.team, MRKU_1, carrier="maersk")
        result, _ = self._check(
            planned,
            {"maersk": CarrierTimeoutError("timed out"), "cosco": {"events": [{"eventID": "E1"}]}},
            events_by_code={"cosco": [_normalised_event(MRKU_1)]},
        )
        self.assertEqual(result, PlannedContainerResult.DETECTED)

    def test_carriers_from_another_team_are_never_swept(self):
        other = _team("disc-fallback-other")
        planned = add_planned_container(other, MRKU_1)
        result, clients = self._check(planned, {"maersk": {"events": [{"eventID": "E1"}]}})

        self.assertEqual(result, PlannedContainerResult.SKIPPED)
        self.assertEqual(clients["maersk"].calls, [])
        self.assertEqual(planned.attempts, 0)


class RunDiscoveryForTeamTest(TestCase):
    def setUp(self):
        self.team = _team("disc-run")
        _equipment_type()

    def test_run_without_carrier_reports_skipped(self):
        add_planned_container(self.team, MCUU_1)
        add_planned_container(self.team, MCUU_2)
        summary = run_discovery_for_team(team=self.team, providers=[])
        self.assertEqual(summary["checked"], 2)
        self.assertEqual(summary["detected"], 0)
        self.assertEqual(summary["skipped"], 2)
        self.assertEqual(summary["errors"], [])

    def test_run_updates_last_checked_at(self):
        planned = add_planned_container(self.team, MCUU_3)
        self.assertIsNone(planned.last_checked_at)
        run_discovery_for_team(team=self.team, providers=[])
        planned.refresh_from_db()
        self.assertIsNotNone(planned.last_checked_at)

    def test_run_detects_with_an_injected_client(self):
        add_planned_container(self.team, MRKU_1)
        client = FakeCarrierClient(payload={"events": [{"eventID": "E1"}]})
        with _patch_parser([_normalised_event(MRKU_1)]):
            summary = run_discovery_for_team(team=self.team, providers=[client])

        self.assertEqual(summary["detected"], 1)
        planned = PlannedContainer.objects.get(team=self.team, container_number=MRKU_1)
        self.assertEqual(planned.status, PlannedContainerStatus.DETECTED)

    def test_run_reports_not_found_separately_from_errors(self):
        add_planned_container(self.team, MRKU_1)
        client = FakeCarrierClient(error=CarrierNoDataError("404"))
        summary = run_discovery_for_team(team=self.team, providers=[client])
        self.assertEqual(summary["not_found"], 1)
        self.assertEqual(summary["errors"], [])

    def test_run_expires_exhausted_containers_before_checking(self):
        planned = add_planned_container(self.team, MRKU_1, max_attempts=1)
        PlannedContainer.objects.filter(pk=planned.pk).update(attempts=1)
        summary = run_discovery_for_team(team=self.team, providers=[])
        self.assertEqual(summary["expired"], 1)
        self.assertEqual(summary["checked"], 0)

    def test_run_is_team_scoped(self):
        other = _team("disc-run-other")
        add_planned_container(other, MRKU_1)
        summary = run_discovery_for_team(team=self.team, providers=[])
        self.assertEqual(summary["checked"], 0)
