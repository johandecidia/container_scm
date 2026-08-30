"""Tests for discovering which carrier knows a container number.

Two things are defended here. First, that the candidate list only ever contains
carriers it is worth spending an API call on — a sweep that asks carriers which
cannot answer is a rate limit burnt for nothing. Second, that one carrier's silence,
outage or missing configuration never ends the sweep, because the box may well be
moving with the next one.

Every carrier call is an injected fake; nothing here touches the network.
"""

from unittest import mock

from django.test import TestCase
from django.utils import timezone

from apps.scm.integrations.carriers.base import BaseCarrierClient, CarrierCapability
from apps.scm.integrations.carriers.carrier_discovery import (
    SKIP_NOT_CONFIGURED,
    SKIP_UNSUPPORTED,
    SOURCE_CONFIGURED,
    SOURCE_OWNER_PREFIX,
    SOURCE_PREFERRED,
    build_carrier_candidates,
    discover_carrier_for_container,
)
from apps.scm.integrations.carriers.dcsa.schemas import NormalisedTrackingEvent
from apps.scm.integrations.carriers.exceptions import (
    CarrierConfigurationError,
    CarrierNoDataError,
    CarrierTimeoutError,
)
from apps.scm.integrations.carriers.probe import ProbeOutcome
from apps.scm.integrations.carriers.registry import get_carrier_definition
from apps.scm.integrations.models import Integration
from apps.teams.models import Team

# A leasing company's prefix: no carrier can be guessed from it.
TRDU = "TRDU9258963"
# Maersk's own prefix, so the owner-prefix hint has something to say.
MRKU = "MRKU1234563"


def _team(slug: str) -> Team:
    return Team.objects.get_or_create(slug=slug, defaults={"name": slug})[0]


def _carrier_integration(team, provider_code, *, is_active=True, family=None):
    return Integration.objects.create(
        team=team,
        name=provider_code,
        provider_code=provider_code,
        provider_family=family or Integration.ProviderFamily.CARRIER,
        is_active=is_active,
    )


def _event(container_number: str) -> NormalisedTrackingEvent:
    return NormalisedTrackingEvent(
        event_type="EQUIPMENT",
        event_classifier="ACT",
        event_code="GTIN",
        event_datetime=timezone.now(),
        location_unlocode="CNSHA",
        container_number=container_number,
        raw_event_id=f"EVT-{container_number}",
    )


class FakeClient(BaseCarrierClient):
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
    """Turns a payload into however many events the test wants."""

    def __init__(self, events):
        self.events = events

    def parse_tracking_events(self, raw_payload):
        return list(self.events)


def _patch_parsers(events_by_code: dict[str, list]):
    """Give each carrier its own parser result, keyed by provider code."""
    return mock.patch(
        "apps.scm.integrations.carriers.factory.build_carrier_parser",
        side_effect=lambda code: FakeParser(events_by_code.get(code, [])),
    )


def _codes(candidates) -> list[str]:
    return [candidate.carrier_code for candidate in candidates]


class CandidateSelectionTest(TestCase):
    """Only carriers that could plausibly answer make the list."""

    def setUp(self):
        self.team = _team("cand-team")

    def test_a_team_without_carriers_has_no_candidates(self):
        self.assertEqual(build_carrier_candidates(team=self.team, container_number=TRDU), [])

    def test_an_active_carrier_integration_is_a_candidate(self):
        _carrier_integration(self.team, "maersk")
        candidates = build_carrier_candidates(team=self.team, container_number=TRDU)
        self.assertEqual(_codes(candidates), ["maersk"])
        self.assertEqual(candidates[0].source, SOURCE_CONFIGURED)
        self.assertEqual(candidates[0].carrier_name, "Maersk")

    def test_an_inactive_integration_is_not_a_candidate(self):
        _carrier_integration(self.team, "maersk", is_active=False)
        self.assertEqual(build_carrier_candidates(team=self.team, container_number=TRDU), [])

    def test_a_business_system_sharing_the_code_is_not_a_carrier(self):
        _carrier_integration(self.team, "maersk", family=Integration.ProviderFamily.BUSINESS_SYSTEM)
        self.assertEqual(build_carrier_candidates(team=self.team, container_number=TRDU), [])

    def test_another_teams_integration_is_not_a_candidate(self):
        _carrier_integration(_team("cand-other"), "maersk")
        self.assertEqual(build_carrier_candidates(team=self.team, container_number=TRDU), [])

    def test_a_carrier_without_pull_support_is_never_asked(self):
        """Evergreen has no pull API — probing it can only ever fail."""
        _carrier_integration(self.team, "evergreen")
        _carrier_integration(self.team, "maersk")
        self.assertEqual(_codes(build_carrier_candidates(team=self.team, container_number=TRDU)), ["maersk"])

    def test_a_carrier_that_cannot_answer_by_container_number_is_never_asked(self):
        _carrier_integration(self.team, "maersk")
        definition = get_carrier_definition("maersk")
        with mock.patch.object(definition.capabilities, "supports_tracking_by_container", False):
            self.assertEqual(build_carrier_candidates(team=self.team, container_number=TRDU), [])

    def test_a_preferred_carrier_goes_first(self):
        _carrier_integration(self.team, "cosco")
        _carrier_integration(self.team, "maersk")
        candidates = build_carrier_candidates(
            team=self.team,
            container_number=TRDU,
            preferred_carrier_codes=["maersk"],
        )
        self.assertEqual(_codes(candidates)[0], "maersk")
        self.assertEqual(candidates[0].source, SOURCE_PREFERRED)
        self.assertIn("cosco", _codes(candidates))

    def test_preferred_carriers_keep_the_order_they_were_given(self):
        for code in ("maersk", "cosco", "msc"):
            _carrier_integration(self.team, code)
        candidates = build_carrier_candidates(
            team=self.team,
            container_number=TRDU,
            preferred_carrier_codes=["cosco", "msc"],
        )
        self.assertEqual(_codes(candidates)[:2], ["cosco", "msc"])

    def test_a_free_text_carrier_name_is_resolved(self):
        _carrier_integration(self.team, "hapag_lloyd")
        candidates = build_carrier_candidates(
            team=self.team,
            container_number=TRDU,
            preferred_carrier_codes=["Hapag-Lloyd"],
        )
        self.assertEqual(_codes(candidates), ["hapag_lloyd"])

    def test_an_unresolvable_preferred_carrier_is_ignored(self):
        _carrier_integration(self.team, "maersk")
        candidates = build_carrier_candidates(
            team=self.team,
            container_number=TRDU,
            preferred_carrier_codes=["Regional Feeder Line"],
        )
        self.assertEqual(_codes(candidates), ["maersk"])

    def test_the_owner_prefix_only_reorders_the_list(self):
        """MRKU is Maersk's prefix — worth trying first, never proof."""
        _carrier_integration(self.team, "cosco")
        _carrier_integration(self.team, "maersk")
        candidates = build_carrier_candidates(team=self.team, container_number=MRKU)
        self.assertEqual(_codes(candidates)[0], "maersk")
        self.assertEqual(candidates[0].source, SOURCE_OWNER_PREFIX)
        self.assertIn("cosco", _codes(candidates))

    def test_the_owner_prefix_never_adds_an_unconfigured_carrier(self):
        _carrier_integration(self.team, "cosco")
        self.assertEqual(_codes(build_carrier_candidates(team=self.team, container_number=MRKU)), ["cosco"])

    def test_an_explicit_carrier_outranks_the_owner_prefix(self):
        _carrier_integration(self.team, "cosco")
        _carrier_integration(self.team, "maersk")
        candidates = build_carrier_candidates(
            team=self.team,
            container_number=MRKU,
            preferred_carrier_codes=["cosco"],
        )
        self.assertEqual(_codes(candidates), ["cosco", "maersk"])

    def test_carriers_with_nothing_to_separate_them_are_ordered_stably(self):
        """Two identical sweeps must ask in the same order, or results become luck."""
        for code in ("msc", "maersk", "cosco"):
            _carrier_integration(self.team, code)
        first = _codes(build_carrier_candidates(team=self.team, container_number=TRDU))
        second = _codes(build_carrier_candidates(team=self.team, container_number=TRDU))
        self.assertEqual(first, second)
        self.assertEqual(first, sorted(first))

    def test_a_carrier_appears_only_once(self):
        _carrier_integration(self.team, "maersk")
        candidates = build_carrier_candidates(
            team=self.team,
            container_number=MRKU,  # owner prefix also points at Maersk
            preferred_carrier_codes=["maersk", "Maersk"],
        )
        self.assertEqual(_codes(candidates), ["maersk"])

    def test_an_unconfigured_preferred_carrier_is_kept_but_marked_unusable(self):
        """The user named it, so the gap is reported rather than silently ignored."""
        candidates = build_carrier_candidates(
            team=self.team,
            container_number=TRDU,
            preferred_carrier_codes=["maersk"],
        )
        self.assertEqual(_codes(candidates), ["maersk"])
        self.assertFalse(candidates[0].usable)
        self.assertEqual(candidates[0].skip_reason, SKIP_NOT_CONFIGURED)

    def test_a_preferred_carrier_that_cannot_be_pulled_is_marked_unusable(self):
        _carrier_integration(self.team, "evergreen")
        candidates = build_carrier_candidates(
            team=self.team,
            container_number=TRDU,
            preferred_carrier_codes=["evergreen"],
        )
        self.assertEqual(candidates[0].skip_reason, SKIP_UNSUPPORTED)
        self.assertFalse(candidates[0].usable)


class DiscoverySweepTest(TestCase):
    """The sweep stops at the first carrier with data, and only there."""

    def setUp(self):
        self.team = _team("sweep-team")

    def _discover(self, clients, events_by_code=None, **kwargs):
        with _patch_parsers(events_by_code or {}):
            return discover_carrier_for_container(
                team=self.team,
                container_number=TRDU,
                clients=clients,
                **kwargs,
            )

    def test_the_first_carrier_with_data_wins(self):
        clients = {
            "maersk": FakeClient("maersk", error=CarrierNoDataError("404")),
            "cosco": FakeClient("cosco", payload={"events": [{"id": 1}]}),
        }
        outcome = self._discover(
            clients,
            {"cosco": [_event(TRDU)]},
            preferred_carrier_codes=["maersk", "cosco"],
        )

        self.assertTrue(outcome.found)
        self.assertEqual(outcome.carrier_code, "cosco")
        self.assertEqual(outcome.carrier_name, "COSCO Shipping")
        self.assertEqual(len(outcome.events), 1)

    def test_carriers_after_the_winner_are_not_asked(self):
        clients = {
            "maersk": FakeClient("maersk", payload={"events": [{"id": 1}]}),
            "cosco": FakeClient("cosco", payload={"events": [{"id": 2}]}),
        }
        self._discover(clients, {"maersk": [_event(TRDU)]}, preferred_carrier_codes=["maersk", "cosco"])

        self.assertEqual(clients["maersk"].calls, [TRDU])
        self.assertEqual(clients["cosco"].calls, [])

    def test_a_timeout_does_not_end_the_sweep(self):
        clients = {
            "maersk": FakeClient("maersk", error=CarrierTimeoutError("timed out")),
            "cosco": FakeClient("cosco", payload={"events": [{"id": 1}]}),
        }
        outcome = self._discover(clients, {"cosco": [_event(TRDU)]}, preferred_carrier_codes=["maersk", "cosco"])

        self.assertEqual(outcome.carrier_code, "cosco")
        self.assertEqual([attempt.carrier_code for attempt in outcome.errored], ["maersk"])
        self.assertEqual(outcome.error_kinds, ["timeout"])

    def test_an_unconfigured_carrier_does_not_end_the_sweep(self):
        clients = {
            "maersk": FakeClient("maersk", error=CarrierConfigurationError("no credentials")),
            "cosco": FakeClient("cosco", payload={"events": [{"id": 1}]}),
        }
        outcome = self._discover(clients, {"cosco": [_event(TRDU)]}, preferred_carrier_codes=["maersk", "cosco"])

        self.assertEqual(outcome.carrier_code, "cosco")
        self.assertEqual([attempt.carrier_code for attempt in outcome.skipped], ["maersk"])

    def test_each_carrier_is_asked_at_most_once(self):
        """A sweep is not a retry loop: a rate-limited carrier is left alone."""
        clients = {
            "maersk": FakeClient("maersk", error=CarrierNoDataError("404")),
            "cosco": FakeClient("cosco", error=CarrierNoDataError("404")),
        }
        self._discover(clients, preferred_carrier_codes=["maersk", "cosco"])

        self.assertEqual(clients["maersk"].calls, [TRDU])
        self.assertEqual(clients["cosco"].calls, [TRDU])

    def test_an_empty_response_is_not_data(self):
        clients = {"maersk": FakeClient("maersk", payload={"events": []})}
        outcome = self._discover(clients, preferred_carrier_codes=["maersk"])

        self.assertFalse(outcome.found)
        self.assertEqual([attempt.outcome for attempt in outcome.attempts], [ProbeOutcome.NOT_FOUND])

    def test_every_outcome_kind_is_kept_apart(self):
        clients = {
            "maersk": FakeClient("maersk", error=CarrierNoDataError("404")),
            "cosco": FakeClient("cosco", error=CarrierTimeoutError("timed out")),
            "msc": FakeClient("msc", error=CarrierConfigurationError("no credentials")),
        }
        outcome = self._discover(clients, preferred_carrier_codes=["maersk", "cosco", "msc"])

        self.assertFalse(outcome.found)
        self.assertEqual([attempt.carrier_code for attempt in outcome.not_found], ["maersk"])
        self.assertEqual([attempt.carrier_code for attempt in outcome.errored], ["cosco"])
        self.assertEqual([attempt.carrier_code for attempt in outcome.skipped], ["msc"])
        self.assertEqual(len(outcome.answered), 2)

    def test_nothing_to_ask_produces_no_attempts(self):
        outcome = self._discover({})
        self.assertFalse(outcome.found)
        self.assertEqual(outcome.attempts, [])

    def test_the_carriers_asked_are_reported_by_name(self):
        clients = {
            "maersk": FakeClient("maersk", error=CarrierNoDataError("404")),
            "cosco": FakeClient("cosco", error=CarrierNoDataError("404")),
        }
        outcome = self._discover(clients, preferred_carrier_codes=["maersk", "cosco"])
        self.assertEqual(outcome.carrier_names(outcome.answered), ["Maersk", "COSCO Shipping"])
