"""Canonical locations on tracking events: wiring, and what it must not cost.

The resolver is wired into ``ingestion``, the single path every provider's events
take, so these tests are about the seam rather than about the rules — the rules
themselves live in ``apps/scm/containers/tests/test_location_resolver.py``.

Two properties matter more than the resolution itself:

* **Evidence survives.** The carrier's own ``location_name``, UN/LOCODE and
  coordinates are never rewritten to match a canonical location, and an event whose
  place cannot be resolved is still stored in full. Tracking data is the thing that
  cannot be recovered; a missing canonical link is repaired on the next refresh.

* **Ingestion cannot be broken by master data.** A resolver fault costs the link,
  not the event.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest import mock

from django.test import TestCase

from apps.scm.containers.choices import LocationResolutionMethod, LocationResolutionStatus, LocationType
from apps.scm.containers.services import create_location, create_location_alias
from apps.scm.integrations.carriers.dcsa.schemas import NormalisedTrackingEvent
from apps.scm.tracking.ingestion import persist_normalised_event, persist_normalised_events
from apps.scm.tracking.models import TrackingEvent, TrackingProvider
from apps.teams.models import Team

_EVENT_TIME = datetime(2024, 3, 10, 8, 0, tzinfo=UTC)


def _provider(code: str) -> TrackingProvider:
    return TrackingProvider.objects.get_or_create(code=code, defaults={"name": code})[0]


def _normalised(**kwargs) -> NormalisedTrackingEvent:
    defaults: dict[str, Any] = {
        "event_type": "EQUIPMENT",
        "event_classifier": "ACT",
        "event_code": "DISC",
        "description": "Discharged",
        "event_datetime": _EVENT_TIME,
        "container_number": "MRKU1234567",
        "raw_event_id": "EVT-1",
    }
    defaults.update(kwargs)
    return NormalisedTrackingEvent(**defaults)


class ResolutionOnIngestTest(TestCase):
    """One wiring covers every provider, because they all arrive through here."""

    team: Team

    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="Ingest", slug="loc-ingest")

    def setUp(self):
        self.port = create_location(
            self.team,
            {"name": "Göteborg", "location_type": LocationType.PORT, "unlocode": "SEGOT", "country_code": "SE"},
        )
        self.terminal = create_location(
            self.team,
            {"name": "Oceanterminalen", "location_type": LocationType.DEPOT, "parent_location": self.port},
        )

    def _persist(self, provider_code: str, **normalised_kwargs) -> TrackingEvent:
        event, _created = persist_normalised_event(
            team=self.team,
            provider=_provider(provider_code),
            normalised=_normalised(**normalised_kwargs),
        )
        return event

    def test_an_alias_configured_for_traqo_resolves_a_traqo_event(self):
        create_location_alias(self.team, self.port, {"source": "traqo", "external_name": "GOTHENBURG"})
        event = self._persist("traqo", location_name="GOTHENBURG")
        self.assertEqual(event.location, self.port)
        self.assertEqual(event.location_resolution_status, LocationResolutionStatus.RESOLVED)
        self.assertEqual(event.location_resolution_method, LocationResolutionMethod.ALIAS)

    def test_the_same_alias_does_not_resolve_another_providers_event(self):
        """Which is why each provider gets its own alias and no adapter gets a rule."""
        create_location_alias(self.team, self.port, {"source": "traqo", "external_name": "GOTHENBURG"})
        event = self._persist("maersk", location_name="GOTHENBURG")
        self.assertIsNone(event.location_id)

    def test_a_carrier_unlocode_resolves_without_any_alias(self):
        event = self._persist("maersk", location_unlocode="SEGOT")
        self.assertEqual(event.location, self.port)
        self.assertEqual(event.location_resolution_method, LocationResolutionMethod.UNLOCODE)

    def test_a_facility_name_is_resolved_when_there_is_no_location_name(self):
        """The string resolved is the string stored, so the two cannot disagree."""
        create_location_alias(self.team, self.terminal, {"source": "maersk", "external_name": "APM GOT"})
        event = self._persist("maersk", location_name="", facility_name="APM GOT")
        self.assertEqual(event.location, self.terminal)
        self.assertEqual(event.location_name, "APM GOT")

    def test_an_ambiguous_place_is_recorded_as_ambiguous_and_left_unlinked(self):
        create_location(self.team, {"name": "Gothenburg Free Port", "unlocode": "SEGOT"})
        event = self._persist("maersk", location_unlocode="SEGOT")
        self.assertIsNone(event.location_id)
        self.assertEqual(event.location_resolution_status, LocationResolutionStatus.AMBIGUOUS)
        self.assertEqual(event.location_resolution_method, LocationResolutionMethod.UNLOCODE)

    def test_an_event_naming_no_place_is_unresolved_by_no_method(self):
        event = self._persist("maersk", location_name="", location_unlocode="", latitude="", longitude="")
        self.assertIsNone(event.location_id)
        self.assertEqual(event.location_resolution_status, LocationResolutionStatus.UNRESOLVED)
        self.assertEqual(event.location_resolution_method, LocationResolutionMethod.NONE)


class EvidenceSurvivesTest(TestCase):
    """A canonical link is added beside the carrier's words, never over them."""

    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="Evidence", slug="loc-evidence")

    def test_an_unresolved_event_is_still_stored_in_full(self):
        """Unresolved location data is valid tracking evidence."""
        event, created = persist_normalised_event(
            team=self.team,
            provider=_provider("maersk"),
            normalised=_normalised(
                location_name="SOME FACILITY NOBODY RECORDED",
                location_unlocode="XXFOO",
                latitude="1.234567",
                longitude="2.345678",
            ),
        )
        self.assertTrue(created)
        self.assertIsNone(event.location_id)
        self.assertEqual(event.location_name, "SOME FACILITY NOBODY RECORDED")
        self.assertEqual(event.location_unlocode, "XXFOO")
        self.assertEqual(str(event.location_latitude), "1.234567")
        self.assertEqual(event.event_datetime, _EVENT_TIME)

    def test_resolving_does_not_rewrite_the_carriers_own_wording(self):
        """The event says "GOTEBORG" because that is what Maersk said."""
        port = create_location(self.team, {"name": "Göteborg", "unlocode": "SEGOT"})
        create_location_alias(self.team, port, {"source": "maersk", "external_name": "GOTEBORG"})
        event, _created = persist_normalised_event(
            team=self.team,
            provider=_provider("maersk"),
            normalised=_normalised(location_name="GOTEBORG", location_unlocode="SEGOT"),
        )
        self.assertEqual(event.location, port)
        self.assertEqual(event.location_name, "GOTEBORG")
        self.assertNotEqual(event.location_name, port.name)

    def test_a_resolver_fault_costs_the_link_and_not_the_event(self):
        with mock.patch("apps.scm.tracking.ingestion.resolve_location", side_effect=RuntimeError("resolver exploded")):
            event, created = persist_normalised_event(
                team=self.team,
                provider=_provider("maersk"),
                normalised=_normalised(location_name="GOTEBORG"),
            )
        self.assertTrue(created)
        self.assertEqual(event.location_name, "GOTEBORG")
        self.assertIsNone(event.location_id)

    def test_a_batch_resolves_every_event_it_stores(self):
        port = create_location(self.team, {"name": "Göteborg", "unlocode": "SEGOT"})
        result = persist_normalised_events(
            team=self.team,
            provider=_provider("maersk"),
            events=[
                _normalised(raw_event_id="A", location_unlocode="SEGOT"),
                _normalised(raw_event_id="B", location_unlocode="XXFOO", location_name="Elsewhere"),
            ],
        )
        self.assertEqual(result["created"], 2)
        self.assertEqual(TrackingEvent.objects.filter(team=self.team, location=port).count(), 1)
        self.assertEqual(
            TrackingEvent.objects.filter(
                team=self.team, location__isnull=True, location_resolution_status=LocationResolutionStatus.UNRESOLVED
            ).count(),
            1,
        )


class ReResolutionTest(TestCase):
    """The link tracks the alias table, because that is what it is derived from."""

    team: Team

    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="Rerun", slug="loc-rerun")

    def setUp(self):
        self.provider = _provider("traqo")
        self.port = create_location(self.team, {"name": "Göteborg", "unlocode": "SEGOT"})

    def _sync(self) -> TrackingEvent:
        event, _created = persist_normalised_event(
            team=self.team,
            provider=self.provider,
            normalised=_normalised(location_name="GOTHENBURG"),
        )
        return event

    def test_recording_an_alias_takes_effect_on_the_next_refresh(self):
        first = self._sync()
        self.assertIsNone(first.location_id)

        create_location_alias(self.team, self.port, {"source": "traqo", "external_name": "GOTHENBURG"})
        second = self._sync()

        self.assertEqual(second.pk, first.pk, "the same carrier event must not become a second row")
        self.assertEqual(second.location, self.port)
        self.assertEqual(second.location_resolution_status, LocationResolutionStatus.RESOLVED)

    def test_removing_a_wrong_alias_removes_the_wrong_link(self):
        """The one link allowed to be cleared. Recording it was explicit; so is undoing it."""
        alias = create_location_alias(self.team, self.port, {"source": "traqo", "external_name": "GOTHENBURG"})
        resolved = self._sync()
        self.assertEqual(resolved.location, self.port)

        alias.delete()
        after = self._sync()

        self.assertEqual(after.pk, resolved.pk)
        self.assertIsNone(after.location_id)
        self.assertEqual(after.location_resolution_status, LocationResolutionStatus.UNRESOLVED)
