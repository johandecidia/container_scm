"""Provider ETA observations reaching the canonical ETA history.

Everything here is provider-independent on purpose: the sources are named "alpha" and
"beta" so that nothing can pass because of something Traqo-shaped. What is being tested
is that Container SCM's own ETA concept can accept an observation from any source, keep
it attributable, and refuse the ones that would put a wrong ETA on the screen.

Traqo's own reading of its response is tested separately, in
``apps/scm/integrations/tests/test_traqo_eta.py``.
"""

import datetime

from django.test import TestCase
from django.utils import timezone

from apps.scm.containers.models import Container, EquipmentType
from apps.scm.shipments.models import Shipment, ShipmentEvent
from apps.scm.tracking.eta_observations import (
    ETA_TARGET_PROVIDER_DEFINED,
    ETA_TARGET_VESSEL_ARRIVAL_POD,
    ProviderEtaObservation,
    record_eta_change,
    record_provider_eta_observation,
)
from apps.scm.tracking.models import ETAHistory, TrackingEvent, TrackingProvider
from apps.teams.models import Team

UTC = datetime.UTC

# The drift the brief asks for: a forecast of 14 September that becomes 17 September.
FIRST_ETA = datetime.datetime(2026, 9, 14, 6, 0, tzinfo=UTC)
SECOND_ETA = datetime.datetime(2026, 9, 17, 6, 0, tzinfo=UTC)


def _observation(source="alpha", eta_at=FIRST_ETA, *, observed_at=None, **overrides):
    fields = {
        "provider_code": source,
        "observed_at": observed_at or timezone.now(),
        "eta_at": eta_at,
        "eta_date": eta_at.date() if eta_at else None,
        "target": ETA_TARGET_PROVIDER_DEFINED,
        "target_name": "Gothenburg",
        "target_unlocode": "SEGOT",
        "provider_updated_at": "2026-08-19 19:55:37.211575",
        "reliable": True,
    }
    fields.update(overrides)
    return ProviderEtaObservation(**fields)


class ProviderEtaObservationTest(TestCase):
    """A tracked container with no shipment — the shape this project actually has."""

    def setUp(self):
        self.team = Team.objects.create(name="eta-obs", slug="eta-obs")
        equipment_type = EquipmentType.objects.get_or_create(
            iso_code="22G1",
            defaults={"category": "GP", "length_ft": 20, "high_cube": False, "description": "20' GP"},
        )[0]
        self.container = Container.objects.create(
            team=self.team,
            owner_code="CPW",
            category_id="U",
            serial_number="258829",
            check_digit=7,
            equipment_type=equipment_type,
        )

    def _record(self, observation):
        return record_provider_eta_observation(team=self.team, observation=observation, container=self.container)

    def _arrive(self):
        """Give the container the actual arrival that answers any forecast."""
        provider = TrackingProvider.objects.get_or_create(
            code="ETA_OBS_PROVIDER",
            defaults={"name": "ETA Obs Provider", "provider_type": TrackingProvider.ProviderType.MANUAL},
        )[0]
        TrackingEvent.objects.create(
            team=self.team,
            container=self.container,
            provider=provider,
            event_type=TrackingEvent.EventType.DISCHARGED,
            event_time_type=TrackingEvent.EventTimeType.ACTUAL,
            event_datetime=datetime.datetime(2026, 7, 1, 20, 0, tzinfo=UTC),
        )

    # A — the observation becomes a canonical ETA history entry
    def test_a_provider_observation_creates_a_canonical_history_entry(self):
        row = self._record(_observation())

        self.assertIsNotNone(row)
        self.assertEqual(ETAHistory.objects.filter(team=self.team).count(), 1)
        self.assertEqual(row.container, self.container)
        self.assertIsNone(row.shipment)
        self.assertEqual(row.new_eta, datetime.date(2026, 9, 14))
        self.assertEqual(row.new_eta_at, FIRST_ETA)
        self.assertIsNone(row.previous_eta_at)
        # First forecast, so there is nothing to have drifted from.
        self.assertIsNone(row.delta_minutes)

    # B — a changed observation from the same provider updates the history
    def test_b_a_changed_observation_appends_a_second_entry(self):
        self._record(_observation())
        second = self._record(_observation(eta_at=SECOND_ETA))

        self.assertIsNotNone(second)
        self.assertEqual(ETAHistory.objects.filter(team=self.team).count(), 2)
        self.assertEqual(second.previous_eta_at, FIRST_ETA)
        self.assertEqual(second.new_eta_at, SECOND_ETA)

    # C — no TrackingEvent is invented to carry the forecast
    def test_c_no_tracking_event_is_synthesised_for_the_forecast(self):
        row = self._record(_observation())

        self.assertEqual(TrackingEvent.objects.filter(team=self.team).count(), 0)
        self.assertIsNone(row.tracking_event)

    # D — repeating the same forecast is not news
    def test_d_repeating_the_same_forecast_writes_nothing(self):
        self._record(_observation())

        for _ in range(5):
            self.assertIsNone(self._record(_observation()))

        self.assertEqual(ETAHistory.objects.filter(team=self.team).count(), 1)

    # E — the drift itself is recorded, in minutes
    def test_e_the_drift_from_14_to_17_september_is_measured(self):
        self._record(_observation())
        second = self._record(_observation(eta_at=SECOND_ETA))

        self.assertEqual(second.previous_eta, datetime.date(2026, 9, 14))
        self.assertEqual(second.new_eta, datetime.date(2026, 9, 17))
        self.assertEqual(second.delta_minutes, 3 * 24 * 60)
        self.assertTrue(second.is_delay)

    # F — which provider said it is recoverable from the row alone
    def test_f_the_source_of_the_forecast_is_identifiable(self):
        row = self._record(_observation(source="alpha"))

        self.assertEqual(row.source, "alpha")
        self.assertEqual(row.raw_payload["provider"], "alpha")
        self.assertEqual(row.location_name, "Gothenburg")
        self.assertEqual(row.location_unlocode, "SEGOT")

    # G — the provider's own caveats survive
    def test_g_provider_confidence_and_target_are_preserved(self):
        row = self._record(_observation(reliable=False, target=ETA_TARGET_VESSEL_ARRIVAL_POD))

        self.assertIs(row.raw_payload["eta_reliable"], False)
        self.assertEqual(row.raw_payload["eta_target"], ETA_TARGET_VESSEL_ARRIVAL_POD)
        self.assertEqual(row.raw_payload["provider_updated_at"], "2026-08-19 19:55:37.211575")

    def test_g_the_observation_time_is_kept_apart_from_the_forecast(self):
        observed_at = datetime.datetime(2026, 8, 19, 19, 55, tzinfo=UTC)
        row = self._record(_observation(observed_at=observed_at))

        self.assertEqual(row.received_at, observed_at)
        self.assertEqual(row.changed_at, observed_at)
        self.assertEqual(row.new_eta_at, FIRST_ETA)
        self.assertEqual(row.raw_payload["observed_at"], observed_at.isoformat())
        # The provider's own clock is kept verbatim and never used as either of the above.
        self.assertEqual(row.raw_payload["provider_updated_at"], "2026-08-19 19:55:37.211575")

    # H / J — an arrived journey has no arrival left to forecast
    def test_h_a_completed_journey_records_no_further_eta(self):
        self._arrive()

        self.assertIsNone(self._record(_observation()))
        self.assertEqual(ETAHistory.objects.filter(team=self.team).count(), 0)

    def test_j_an_actual_arrival_supersedes_a_later_forecast(self):
        self._record(_observation())
        self._arrive()

        self.assertIsNone(self._record(_observation(eta_at=SECOND_ETA)))
        # The forecast already recorded stays: it was true when it was made.
        self.assertEqual(ETAHistory.objects.filter(team=self.team).count(), 1)

    # I — two providers observing one journey do not merge
    def test_i_two_providers_leave_two_attributable_trails(self):
        self._record(_observation(source="alpha", eta_at=FIRST_ETA))
        self._record(_observation(source="beta", eta_at=SECOND_ETA))

        rows = ETAHistory.objects.filter(team=self.team).order_by("source")
        self.assertEqual([row.source for row in rows], ["alpha", "beta"])
        self.assertEqual([row.new_eta_at for row in rows], [FIRST_ETA, SECOND_ETA])
        # Neither is measured against the other: beta's first forecast is not a delay.
        self.assertIsNone(rows[1].delta_minutes)

    def test_i_a_provider_only_drifts_against_its_own_previous_forecast(self):
        self._record(_observation(source="alpha", eta_at=FIRST_ETA))
        self._record(_observation(source="beta", eta_at=SECOND_ETA))
        second_alpha = self._record(_observation(source="alpha", eta_at=SECOND_ETA))

        self.assertEqual(second_alpha.previous_eta_at, FIRST_ETA)

    # K — nothing usable is harmless, and cannot corrupt what is there
    def test_k_an_observation_with_no_eta_is_ignored(self):
        self.assertIsNone(self._record(_observation(eta_at=None, eta_date=None)))
        self.assertEqual(ETAHistory.objects.filter(team=self.team).count(), 0)

    def test_k_an_unusable_eta_leaves_an_existing_forecast_alone(self):
        first = self._record(_observation())

        self.assertIsNone(self._record(_observation(eta_at=None, eta_date=None)))

        first.refresh_from_db()
        self.assertEqual(first.new_eta_at, FIRST_ETA)
        self.assertEqual(ETAHistory.objects.filter(team=self.team).count(), 1)

    def test_a_change_needs_something_to_be_about(self):
        with self.assertRaises(ValueError):
            record_eta_change(team=self.team, changed_at=timezone.now(), received_at=timezone.now())


class ProviderEtaObservationOnAShipmentTest(TestCase):
    """The same intake where the journey is on a shipment."""

    def setUp(self):
        self.team = Team.objects.create(name="eta-obs-shipment", slug="eta-obs-shipment")

    def _shipment(self, **overrides):
        return Shipment.objects.create(team=self.team, **overrides)

    def _record(self, shipment, observation):
        return record_provider_eta_observation(team=self.team, observation=observation, shipment=shipment)

    def test_an_unforecast_shipment_adopts_the_observation_through_the_existing_writer(self):
        shipment = self._shipment()

        row = self._record(shipment, _observation())

        shipment.refresh_from_db()
        self.assertEqual(shipment.eta, datetime.date(2026, 9, 14))
        self.assertEqual(shipment.original_eta, datetime.date(2026, 9, 14))
        self.assertEqual(shipment.eta_source, "alpha")
        self.assertEqual(row.shipment, shipment)
        self.assertEqual(row.new_eta_at, FIRST_ETA)
        self.assertEqual(row.raw_payload["provider"], "alpha")
        self.assertTrue(
            ShipmentEvent.objects.filter(shipment=shipment, event_type=ShipmentEvent.EventType.ETA_UPDATED).exists()
        )

    def test_a_forecast_shipment_keeps_its_own_eta_and_still_records_the_observation(self):
        shipment = self._shipment(eta=datetime.date(2026, 9, 14), eta_source="maersk")

        row = self._record(shipment, _observation(source="beta", eta_at=SECOND_ETA))

        shipment.refresh_from_db()
        # Choosing between two providers' forecasts is precedence, which is not decided
        # here — so the cached ETA and its owner are untouched.
        self.assertEqual(shipment.eta, datetime.date(2026, 9, 14))
        self.assertEqual(shipment.eta_source, "maersk")
        self.assertEqual(row.source, "beta")
        self.assertEqual(row.new_eta_at, SECOND_ETA)

    def test_a_closed_shipment_records_no_further_eta(self):
        for status in (
            Shipment.Status.ARRIVED,
            Shipment.Status.DELIVERED,
            Shipment.Status.CANCELLED,
            Shipment.Status.PARTIALLY_RECEIVED,
        ):
            with self.subTest(status=status):
                shipment = self._shipment(status=status)

                self.assertIsNone(self._record(shipment, _observation()))
                shipment.refresh_from_db()
                self.assertIsNone(shipment.eta)

    def test_an_arrived_shipment_records_no_further_eta(self):
        shipment = self._shipment(
            status=Shipment.Status.IN_TRANSIT,
            actual_arrival_at=datetime.datetime(2026, 7, 1, 20, 0, tzinfo=UTC),
        )

        self.assertIsNone(self._record(shipment, _observation()))
        self.assertEqual(ETAHistory.objects.filter(team=self.team).count(), 0)
