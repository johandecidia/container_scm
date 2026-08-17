"""Tests for continuation discovery — finding the carrier that covers a journey gap.

A container that already has a verified source, whose journey does not account for
where the box has physically been seen, is swept across the team's *other* carriers.
What is tested here is that this adds sources rather than replacing them, that it
only ever runs off a real gap, and that it cannot be made to spend carrier calls
twice on the same question.

Every carrier is an injected fake. No live call is made.
"""

from datetime import UTC, datetime, timedelta
from unittest import mock

from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.scm.containers.choices import LocationSource, LocationType
from apps.scm.containers.models import Container, ContainerLocation, EquipmentType
from apps.scm.containers.utils import calculate_check_digit
from apps.scm.integrations.carriers.dcsa.schemas import NormalisedTrackingEvent
from apps.scm.integrations.carriers.exceptions import (
    CarrierNoDataError,
    CarrierServerError,
)
from apps.scm.integrations.locks import resource_lock
from apps.scm.integrations.models import Integration
from apps.scm.shipments.models import Shipment, ShipmentContainer
from apps.scm.tracking.continuation import (
    COOLDOWN,
    FOUND,
    IN_PROGRESS,
    NO_GAP,
    NOT_FOUND,
    NOTHING_TO_ASK,
    RECENTLY_CHECKED_MINUTES,
    discover_journey_continuation,
    get_recently_checked_carrier_codes,
)
from apps.scm.tracking.manual_refresh import (
    CONTAINER_DISCOVERY_LOCK_PREFIX,
    CONTAINER_DISCOVERY_LOCK_TTL_SECONDS,
    refresh_container_tracking,
)
from apps.scm.tracking.models import TrackingEvent, TrackingProvider, TrackingSubscription, TrackingSyncRun

# Reused rather than re-invented: these fakes are the carrier test doubles the
# discovery sweep tests already drive the same factory with.
from apps.scm.tracking.tests.test_manual_refresh import _fake_client, _patch_carriers
from apps.teams.models import Team

_LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache", "LOCATION": "continuation"}}

DISCHARGED_AT_BORN = datetime(2026, 8, 12, 8, 0, tzinfo=UTC)
AT_GOTHENBURG = datetime(2026, 8, 16, 9, 30, tzinfo=UTC)

CONTAINER_PREFIX = "MCUU200930"
CARRIERS = ("cma_cgm", "cosco", "maersk")


@override_settings(CACHES=_LOCMEM)
class ContinuationTestBase(TestCase):
    """MCUU2009300: tracked by CMA CGM to Born, physically received in Gothenburg."""

    team_slug: str

    def setUp(self):
        cache.clear()
        self.team = Team.objects.create(name=self.team_slug, slug=self.team_slug)
        self.container = _container(self.team, CONTAINER_PREFIX)
        for code in CARRIERS:
            Integration.objects.create(
                team=self.team,
                name=code,
                provider_code=code,
                provider_family=Integration.ProviderFamily.CARRIER,
                is_active=True,
            )
        self.cma = _provider("cma_cgm", "CMA CGM")

    # -- fixtures ----------------------------------------------------------

    def cma_source(self, *, last_synced_at=None) -> TrackingSubscription:
        """The container's first verified source, which got it as far as Born."""
        subscription = TrackingSubscription.objects.create(
            team=self.team,
            provider=self.cma,
            container=self.container,
            tracking_reference=self.container.container_id,
            status=TrackingSubscription.Status.ACTIVE,
            tracking_status=TrackingSubscription.TrackingStatus.TRACKING,
            last_synced_at=last_synced_at,
        )
        TrackingEvent.objects.create(
            team=self.team,
            provider=self.cma,
            container=self.container,
            subscription=subscription,
            event_type=TrackingEvent.EventType.DISCHARGED,
            event_time_type=TrackingEvent.EventTimeType.ACTUAL,
            event_datetime=DISCHARGED_AT_BORN,
            location_name="Born",
            location_unlocode="NLBON",
            event_fingerprint="cma-discharged-born",
        )
        return subscription

    def seen_in_gothenburg(self, when=AT_GOTHENBURG):
        location = ContainerLocation.objects.create(
            team=self.team,
            name="Oceanterminalen",
            location_type=LocationType.DEPOT,
            city="Gothenburg",
            country="Sweden",
        )
        self.container.current_location = location
        self.container.last_location_update = when
        self.container.location_source = LocationSource.MANUAL
        self.container.save()
        return location

    def a_gap(self, *, last_synced_at=None):
        """The whole situation: one source, and a move it does not explain."""
        subscription = self.cma_source(last_synced_at=last_synced_at)
        self.seen_in_gothenburg()
        return subscription

    def sweep(self, behaviour, *, events_by_code=None, **kwargs):
        """Run a continuation sweep against fake carriers, and return the outcome."""
        clients = {code: _fake_client(code, behaviour.get(code, CarrierNoDataError("404"))) for code in CARRIERS}
        with _patch_carriers(clients, events_by_code or {}):
            outcome = discover_journey_continuation(team=self.team, container=self.container, **kwargs)
        return outcome, clients

    def onward_events(self, *, unlocode="CNSHA", name="", event_id="COSCO-ONWARD", when=None):
        """Carrier events from inside the ocean leg.

        Enough to prove a carrier knows the box — which is what makes it a source —
        and dated before the depot receipt, so they do not themselves explain the
        segment the sweep was looking for. Explicit timestamps rather than "now":
        whether a gap is open must not depend on when the suite runs.
        """
        return [
            _event_at(
                self.container.container_id,
                unlocode=unlocode,
                name=name,
                event_id=event_id,
                when=when or DISCHARGED_AT_BORN - timedelta(days=10),
            )
        ]

    def sources(self):
        return TrackingSubscription.objects.filter(team=self.team, container=self.container)

    def source_codes(self):
        return sorted(self.sources().values_list("provider__code", flat=True))


class ContinuationRunsOnlyOnAGapTest(ContinuationTestBase):
    team_slug = "continuation-trigger"

    def test_a_gap_sends_the_container_to_the_other_carriers(self):
        self.a_gap()

        outcome, clients = self.sweep(
            {"cosco": {"events": [{"id": 1}]}},
            events_by_code={"cosco": self.onward_events()},
        )

        self.assertEqual(outcome.reason, FOUND)
        self.assertEqual(outcome.carrier_code, "cosco")
        self.assertEqual(clients["cosco"].calls, [self.container.container_id])
        # The sweep stops at the first carrier with data, so the rest are not called.
        self.assertEqual(clients["maersk"].calls, [])

    def test_no_gap_asks_nobody(self):
        """The journey is accounted for; there is nothing to look for."""
        self.cma_source()

        outcome, clients = self.sweep({code: {"events": [{"id": 1}]} for code in CARRIERS})

        self.assertEqual(outcome.reason, NO_GAP)
        self.assertIsNone(outcome.gap)
        for code in CARRIERS:
            self.assertEqual(clients[code].calls, [])

    def test_carrier_silence_alone_asks_nobody(self):
        """No physical observation to contradict the carrier means no gap."""
        self.cma_source(last_synced_at=timezone.now() - timedelta(days=9))

        outcome, clients = self.sweep({code: {"events": [{"id": 1}]} for code in CARRIERS})

        self.assertEqual(outcome.reason, NO_GAP)
        self.assertEqual(clients["cosco"].calls, [])

    def test_an_untracked_container_is_left_to_ordinary_discovery(self):
        self.seen_in_gothenburg()

        outcome, clients = self.sweep({code: {"events": [{"id": 1}]} for code in CARRIERS})

        self.assertEqual(outcome.reason, NO_GAP)
        self.assertEqual(clients["cosco"].calls, [])

    def test_the_outcome_carries_the_gap_that_prompted_it(self):
        self.a_gap()

        outcome, _ = self.sweep({})

        self.assertEqual(outcome.reason, NOT_FOUND)
        self.assertEqual(outcome.gap.from_location, "Born")
        self.assertEqual(outcome.gap.to_location, "Oceanterminalen")


class ContinuationAddsWithoutReplacingTest(ContinuationTestBase):
    team_slug = "continuation-adds"

    def test_a_second_source_is_created_beside_the_first(self):
        first = self.a_gap()

        outcome, _ = self.sweep(
            {"cosco": {"events": [{"id": 1}]}},
            events_by_code={"cosco": self.onward_events()},
        )

        self.assertEqual(self.source_codes(), ["cma_cgm", "cosco"])
        first.refresh_from_db()
        self.assertEqual(first.status, TrackingSubscription.Status.ACTIVE)
        self.assertEqual(outcome.subscription.provider.code, "cosco")

    def test_the_first_sources_events_survive(self):
        self.a_gap()

        self.sweep(
            {"cosco": {"events": [{"id": 1}]}},
            events_by_code={"cosco": self.onward_events()},
        )

        by_provider = sorted(
            TrackingEvent.objects.filter(team=self.team, container=self.container).values_list(
                "provider__code", flat=True
            )
        )
        self.assertEqual(by_provider, ["cma_cgm", "cosco"])

    def test_the_shipments_carrier_is_not_touched(self):
        shipment = Shipment.objects.create(team=self.team, shipment_number="SHP-CONT", carrier="CMA CGM")
        ShipmentContainer.objects.create(shipment=shipment, container=self.container)
        self.a_gap()

        self.sweep(
            {"cosco": {"events": [{"id": 1}]}},
            events_by_code={"cosco": self.onward_events()},
        )

        shipment.refresh_from_db()
        self.assertEqual(shipment.carrier, "CMA CGM")

    def test_the_new_source_gets_its_own_sync_run(self):
        self.a_gap()

        outcome, _ = self.sweep(
            {"cosco": {"events": [{"id": 1}]}},
            events_by_code={"cosco": self.onward_events()},
        )

        run = TrackingSyncRun.objects.get(subscription=outcome.subscription)
        self.assertEqual(run.status, TrackingSyncRun.Status.SUCCESS)
        self.assertEqual(outcome.events_created, 1)

    def test_the_journey_holds_both_sources_afterwards(self):
        from apps.scm.tracking.journey import get_container_journey

        self.a_gap()
        self.sweep(
            {"cosco": {"events": [{"id": 1}]}},
            events_by_code={"cosco": self.onward_events()},
        )

        journey = get_container_journey(self.team, self.container)

        self.assertEqual([source.code for source in journey.carrier_sources], ["cma_cgm", "cosco"])

    def test_a_third_leg_can_still_be_discovered_later(self):
        """Found once is not found forever: a new gap may be swept again."""
        self.a_gap()
        self.sweep(
            {"cosco": {"events": [{"id": 1}]}},
            events_by_code={"cosco": self.onward_events()},
        )
        # The box turns up somewhere else again, with nothing explaining the move.
        self.container.current_location.name = "Inland depot, Jönköping"
        self.container.current_location.city = "Jönköping"
        self.container.current_location.save()
        self.container.last_location_update = AT_GOTHENBURG + timedelta(hours=6)
        self.container.save()

        outcome, _ = self.sweep(
            {"maersk": {"events": [{"id": 2}]}},
            events_by_code={"maersk": self.onward_events(event_id="MAERSK-ONWARD")},
            ignore_cooldown=True,
        )

        self.assertEqual(outcome.reason, FOUND)
        self.assertEqual(self.source_codes(), ["cma_cgm", "cosco", "maersk"])


class ContinuationSurvivesCarrierFailuresTest(ContinuationTestBase):
    team_slug = "continuation-failures"

    def test_not_found_does_not_end_the_sweep(self):
        self.a_gap()

        outcome, clients = self.sweep(
            {"cosco": CarrierNoDataError("404"), "maersk": {"events": [{"id": 1}]}},
            events_by_code={"maersk": self.onward_events()},
        )

        self.assertEqual(outcome.reason, FOUND)
        self.assertEqual(outcome.carrier_code, "maersk")
        self.assertEqual(clients["cosco"].calls, [self.container.container_id])

    def test_an_error_does_not_end_the_sweep(self):
        self.a_gap()

        outcome, _ = self.sweep(
            {"cosco": CarrierServerError("500 <html>stack trace</html>"), "maersk": {"events": [{"id": 1}]}},
            events_by_code={"maersk": self.onward_events()},
        )

        self.assertEqual(outcome.reason, FOUND)
        self.assertEqual(outcome.carrier_code, "maersk")

    def test_nobody_with_data_leaves_the_gap_and_the_sources_alone(self):
        first = self.a_gap()

        outcome, _ = self.sweep({code: CarrierNoDataError("404") for code in CARRIERS})

        self.assertEqual(outcome.reason, NOT_FOUND)
        self.assertIsNotNone(outcome.gap)
        self.assertEqual(self.source_codes(), ["cma_cgm"])
        first.refresh_from_db()
        self.assertEqual(first.status, TrackingSubscription.Status.ACTIVE)
        self.assertEqual(TrackingEvent.objects.filter(team=self.team, container=self.container).count(), 1)


class ContinuationRationsCarrierCallsTest(ContinuationTestBase):
    team_slug = "continuation-rationing"

    def test_a_source_polled_moments_ago_is_not_asked_again(self):
        self.a_gap(last_synced_at=timezone.now())

        outcome, clients = self.sweep(
            {"cosco": {"events": [{"id": 1}]}},
            events_by_code={"cosco": self.onward_events()},
        )

        self.assertEqual(outcome.reason, FOUND)
        self.assertEqual(clients["cma_cgm"].calls, [])

    def test_a_source_not_polled_for_a_while_is_asked_again(self):
        """It may know now what it did not know last week."""
        stale = timezone.now() - timedelta(minutes=RECENTLY_CHECKED_MINUTES + 5)
        self.a_gap(last_synced_at=stale)

        _, clients = self.sweep({code: CarrierNoDataError("404") for code in CARRIERS})

        self.assertEqual(clients["cma_cgm"].calls, [self.container.container_id])

    def test_recently_checked_codes_are_reported_for_the_sweep_to_skip(self):
        self.a_gap(last_synced_at=timezone.now())

        self.assertEqual(get_recently_checked_carrier_codes(self.team, self.container), frozenset({"cma_cgm"}))

    def test_nothing_left_to_ask_is_not_reported_as_no_data(self):
        """Every candidate was one we had just polled. That is not an answer."""
        self.a_gap(last_synced_at=timezone.now())
        Integration.objects.filter(team=self.team).exclude(provider_code="cma_cgm").delete()

        outcome, clients = self.sweep({})

        self.assertEqual(outcome.reason, NOTHING_TO_ASK)
        for code in CARRIERS:
            self.assertEqual(clients[code].calls, [])

    def test_a_second_sweep_inside_the_cooldown_asks_nobody(self):
        """A gap stays open until something explains it; the cooldown is the brake."""
        self.a_gap()
        self.sweep({code: CarrierNoDataError("404") for code in CARRIERS})

        outcome, clients = self.sweep({code: {"events": [{"id": 1}]} for code in CARRIERS})

        self.assertEqual(outcome.reason, COOLDOWN)
        for code in CARRIERS:
            self.assertEqual(clients[code].calls, [])

    def test_a_sweep_already_running_for_the_container_is_not_joined(self):
        self.a_gap()

        with resource_lock(
            f"container:{self.container.pk}",
            ttl=CONTAINER_DISCOVERY_LOCK_TTL_SECONDS,
            prefix=CONTAINER_DISCOVERY_LOCK_PREFIX,
        ):
            outcome, clients = self.sweep({code: {"events": [{"id": 1}]} for code in CARRIERS})

        self.assertEqual(outcome.reason, IN_PROGRESS)
        self.assertEqual(self.source_codes(), ["cma_cgm"])
        for code in CARRIERS:
            self.assertEqual(clients[code].calls, [])

    def test_a_repeated_sweep_creates_no_duplicate_source_or_events(self):
        """Belt and braces: if two sweeps did interleave, the writes are idempotent."""
        self.a_gap()
        events = self.onward_events()
        self.sweep({"cosco": {"events": [{"id": 1}]}}, events_by_code={"cosco": events})
        # Age both sources so the second sweep asks them again rather than skipping
        # them as just-checked — the situation a re-run days later would be in.
        self.sources().update(last_synced_at=timezone.now() - timedelta(hours=2))

        outcome, _ = self.sweep(
            {"cosco": {"events": [{"id": 1}]}},
            events_by_code={"cosco": events},
            ignore_cooldown=True,
        )

        self.assertEqual(self.source_codes(), ["cma_cgm", "cosco"])
        self.assertEqual(TrackingEvent.objects.filter(team=self.team, container=self.container).count(), 2)
        self.assertEqual(outcome.events_created, 0)
        self.assertEqual(outcome.events_updated, 1)

    def test_another_teams_carriers_are_never_swept(self):
        other = Team.objects.create(name="continuation-other", slug="continuation-other")
        other_container = _container(other, "MSCU393930")

        clients = {code: _fake_client(code, {"events": [{"id": 1}]}) for code in CARRIERS}
        with _patch_carriers(clients, {}):
            outcome = discover_journey_continuation(team=other, container=other_container)

        self.assertEqual(outcome.reason, NO_GAP)
        for code in CARRIERS:
            self.assertEqual(clients[code].calls, [])


class RefreshRunsContinuationTest(ContinuationTestBase):
    """The refresh button: sources first, then the part they do not explain."""

    team_slug = "continuation-refresh"

    def _refresh(self, behaviour, events_by_code=None):
        clients = {code: _fake_client(code, behaviour.get(code, CarrierNoDataError("404"))) for code in CARRIERS}
        with _patch_carriers(clients, events_by_code or {}):
            result = refresh_container_tracking(team=self.team, container=self.container)
        return result, clients

    def test_the_known_source_is_refreshed_before_anybody_else_is_asked(self):
        self.a_gap()

        _, clients = self._refresh(
            {"cma_cgm": {"events": [{"id": 1}]}, "cosco": {"events": [{"id": 2}]}},
            events_by_code={"cosco": self.onward_events()},
        )

        self.assertEqual(clients["cma_cgm"].calls, [self.container.container_id])

    def test_a_found_continuation_is_reported_to_the_user(self):
        self.a_gap()

        result, _ = self._refresh(
            {"cma_cgm": {"events": []}, "cosco": {"events": [{"id": 2}]}},
            events_by_code={"cosco": self.onward_events()},
        )

        self.assertEqual(result.level, "success")
        self.assertIn("further source", str(result.message))
        self.assertIn("COSCO Shipping", str(result.message))
        self.assertEqual(self.source_codes(), ["cma_cgm", "cosco"])

    def test_a_refresh_without_a_gap_never_reaches_another_carrier(self):
        self.cma_source()

        _, clients = self._refresh({code: {"events": [{"id": 1}]} for code in CARRIERS})

        self.assertEqual(clients["cma_cgm"].calls, [self.container.container_id])
        self.assertEqual(clients["cosco"].calls, [])
        self.assertEqual(clients["maersk"].calls, [])

    def test_a_refresh_whose_own_events_explain_the_move_sweeps_nobody(self):
        """The sweep runs after the sync, so new events can close the gap first."""
        self.a_gap()
        explains_it = self.onward_events(
            unlocode="SEGOT",
            name="Gothenburg",
            event_id="CMA-GOT",
            when=AT_GOTHENBURG - timedelta(hours=3),
        )

        _, clients = self._refresh(
            {"cma_cgm": {"events": [{"id": 1}]}},
            events_by_code={"cma_cgm": explains_it},
        )

        self.assertEqual(clients["cosco"].calls, [])
        self.assertEqual(self.source_codes(), ["cma_cgm"])

    def test_a_fruitless_sweep_leaves_the_refresh_result_as_it_was(self):
        self.a_gap()

        result, _ = self._refresh(
            {"cma_cgm": {"events": [{"id": 1}]}},
            events_by_code={"cma_cgm": self.onward_events(event_id="CMA-ONWARD")},
        )

        self.assertEqual(result.carrier_code, "cma_cgm")
        self.assertEqual(self.source_codes(), ["cma_cgm"])

    def test_no_live_http_is_attempted_anywhere_in_this_module(self):
        """Every carrier here is a fake; a real session would be a bug in the test."""
        with mock.patch("requests.Session.request", side_effect=AssertionError("live HTTP attempted")):
            self.a_gap()
            self._refresh(
                {"cosco": {"events": [{"id": 1}]}},
                events_by_code={"cosco": self.onward_events()},
            )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _provider(code: str, name: str) -> TrackingProvider:
    return TrackingProvider.objects.get_or_create(code=code, defaults={"name": name})[0]


def _container(team: Team, prefix: str) -> Container:
    """A container from its first ten characters; the check digit is calculated."""
    equipment_type = EquipmentType.objects.get_or_create(
        iso_code="22G1",
        defaults={"category": "GP", "length_ft": 20, "high_cube": False, "description": "20' GP"},
    )[0]
    owner_code, category_id, serial_number = prefix[:3], prefix[3], prefix[4:10]
    return Container.objects.create(
        team=team,
        owner_code=owner_code,
        category_id=category_id,
        serial_number=serial_number,
        check_digit=calculate_check_digit(owner_code, category_id, serial_number),
        equipment_type=equipment_type,
    )


def _event_at(container_number: str, *, unlocode: str, event_id: str, when, name: str = "") -> NormalisedTrackingEvent:
    """One observed carrier event at a named place and time."""
    return NormalisedTrackingEvent(
        event_type="EQUIPMENT",
        event_classifier="ACT",
        event_code="GTIN",
        event_datetime=when,
        location_name=name,
        location_unlocode=unlocode,
        container_number=container_number,
        raw_event_id=event_id,
    )
