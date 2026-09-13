"""Tests for the two aggregator discovery wrappers, and the SCAC table behind them.

What these pin down is the four-valued classification, because that is what the whole
cost policy rests on. A resolution chain that read a Traqo timeout as "no carrier" would
go on to buy a Vizion reference on the strength of our own outage, and one that read a
Vizion 401 as "container unknown" would report a configuration fault as a fact about the
box. Both are asserted here rather than left to the chain to notice.

No live calls: every client is a fake, and the wrappers are exercised through the real
readers and the real registry.
"""

from django.test import SimpleTestCase, TestCase, override_settings

from apps.scm.integrations.carriers.exceptions import (
    CarrierAuthenticationError,
    CarrierConfigurationError,
    CarrierTimeoutError,
)
from apps.scm.integrations.carriers.registry import (
    carrier_scac,
    get_carrier_definition,
    list_carriers,
    resolve_carrier_code_from_scac,
)
from apps.scm.integrations.traqo import discovery as traqo_discovery
from apps.scm.integrations.traqo.discovery import lookup_carrier_for_container
from apps.scm.integrations.vizion import discovery as vizion_discovery
from apps.scm.integrations.vizion.discovery import identify_carrier

CONTAINER_NUMBER = "BBCU3273070"


class ScacRegistryTest(SimpleTestCase):
    """One SCAC table, in the registry, so no aggregator keeps a second copy."""

    def test_oney_resolves_to_the_canonical_code_for_one(self):
        """The canonical code is the registry's own, not the SCAC."""
        self.assertEqual(resolve_carrier_code_from_scac("ONEY"), "one")

    def test_the_common_scacs_all_resolve(self):
        for scac, expected in (
            ("MAEU", "maersk"),
            ("MSCU", "msc"),
            ("CMDU", "cma_cgm"),
            ("HLCU", "hapag_lloyd"),
            ("COSU", "cosco"),
            ("EGLV", "evergreen"),
            ("HDMU", "hmm"),
            ("ZIMU", "zim"),
        ):
            with self.subTest(scac=scac):
                self.assertEqual(resolve_carrier_code_from_scac(scac), expected)

    def test_a_carrier_whose_scac_changed_resolves_from_either(self):
        """Yang Ming moved from YMLU to YMJA; providers still report both."""
        self.assertEqual(resolve_carrier_code_from_scac("YMLU"), "yang_ming")
        self.assertEqual(resolve_carrier_code_from_scac("YMJA"), "yang_ming")

    def test_lowercase_and_padding_are_tolerated(self):
        self.assertEqual(resolve_carrier_code_from_scac(" oney "), "one")

    def test_an_unclaimed_scac_is_none_rather_than_the_nearest_carrier(self):
        """OOLU is OOCL. Nothing in this system carries it, and guessing would be worse."""
        self.assertIsNone(resolve_carrier_code_from_scac("OOLU"))
        self.assertIsNone(resolve_carrier_code_from_scac(""))

    def test_every_registered_carrier_has_a_scac(self):
        """A carrier with none can never be recognised from an aggregator's answer."""
        for definition in list_carriers():
            with self.subTest(carrier=definition.provider_code):
                self.assertTrue(definition.scac_codes, f"{definition.provider_code} has no SCAC")

    def test_no_scac_is_claimed_by_two_carriers(self):
        seen: dict[str, str] = {}
        for definition in list_carriers():
            for scac in definition.scac_codes:
                self.assertNotIn(scac, seen, f"{scac} claimed by both {seen.get(scac)} and {definition.provider_code}")
                seen[scac] = definition.provider_code

    def test_the_canonical_scac_round_trips(self):
        for definition in list_carriers():
            with self.subTest(carrier=definition.provider_code):
                scac = carrier_scac(definition.provider_code)
                self.assertEqual(resolve_carrier_code_from_scac(scac), definition.provider_code)

    def test_traqos_own_sealine_table_still_answers_only_for_traqo(self):
        """Extending the registry must not silently widen a Traqo-specific claim."""
        from apps.scm.integrations.traqo.sealines import carrier_code_for_sealine

        # Traqo publishes no sealine for Evergreen, even though the registry knows EGLV.
        self.assertIsNone(carrier_code_for_sealine("EGLV"))
        self.assertEqual(carrier_code_for_sealine("ONEY"), "one")

    def test_the_registry_name_for_one_is_what_gets_reported(self):
        self.assertIn("ONE", get_carrier_definition("one").name)


class FakeTraqoClient:
    """Returns a lookup envelope, or raises, without any transport."""

    def __init__(self, payload=None, error=None):
        self.payload = payload
        self.error = error
        self.calls: list[str] = []

    def lookup_carrier(self, reference):
        self.calls.append(reference)
        if self.error is not None:
            raise self.error
        return self.payload


def _lookup_envelope(scac, *, confidence="high", reason=""):
    return {
        "success": True,
        "data": {
            "carrier": {"scac": scac, "confidence": confidence, "reason": reason},
            "slot_consumed": False,
        },
    }


class TraqoCarrierLookupOutcomeTest(TestCase):
    """Four outcomes, kept apart. Only one of them is about the container."""

    def test_a_named_carrier_is_found_and_translated(self):
        client = FakeTraqoClient(_lookup_envelope("ONEY"))

        result = lookup_carrier_for_container(CONTAINER_NUMBER, client=client)

        self.assertEqual(result.status, traqo_discovery.FOUND)
        self.assertTrue(result.found)
        self.assertEqual(result.carrier_code, "one")
        self.assertEqual(result.scac, "ONEY")
        self.assertIn("ONE", result.carrier_name)

    def test_no_carrier_named_is_not_found(self):
        client = FakeTraqoClient({"success": True, "data": {"message": "nothing matched"}})

        result = lookup_carrier_for_container(CONTAINER_NUMBER, client=client)

        self.assertEqual(result.status, traqo_discovery.NOT_FOUND)
        self.assertFalse(result.found)
        self.assertTrue(result.answered, "Traqo was reached and said no; that is an answer")

    def test_a_carrier_with_no_adapter_is_reported_but_not_actionable(self):
        client = FakeTraqoClient(_lookup_envelope("OOLU"))

        result = lookup_carrier_for_container(CONTAINER_NUMBER, client=client)

        self.assertEqual(result.scac, "OOLU", "what Traqo said is kept")
        self.assertEqual(result.carrier_code, "")
        self.assertFalse(result.found, "nothing could be routed to a carrier with no adapter")

    def test_a_timeout_is_an_error_and_not_a_missing_carrier(self):
        client = FakeTraqoClient(error=CarrierTimeoutError("read timed out", provider_code="traqo"))

        result = lookup_carrier_for_container(CONTAINER_NUMBER, client=client)

        self.assertEqual(result.status, traqo_discovery.ERROR)
        self.assertFalse(result.answered)
        self.assertEqual(result.carrier_code, "")

    def test_a_missing_credential_is_not_configured_and_not_an_error(self):
        client = FakeTraqoClient(error=CarrierConfigurationError("no key", provider_code="traqo"))

        result = lookup_carrier_for_container(CONTAINER_NUMBER, client=client)

        self.assertEqual(result.status, traqo_discovery.NOT_CONFIGURED)
        self.assertFalse(result.answered)

    def test_an_empty_container_number_asks_nobody(self):
        client = FakeTraqoClient(_lookup_envelope("ONEY"))

        result = lookup_carrier_for_container("", client=client)

        self.assertEqual(result.status, traqo_discovery.NOT_FOUND)
        self.assertEqual(client.calls, [])

    def test_the_lookup_writes_nothing(self):
        from apps.scm.tracking.models import TrackingEvent, TrackingSubscription

        lookup_carrier_for_container(CONTAINER_NUMBER, client=FakeTraqoClient(_lookup_envelope("ONEY")))

        self.assertEqual(TrackingSubscription.objects.count(), 0)
        self.assertEqual(TrackingEvent.objects.count(), 0)

    @override_settings(TRAQO_ENABLED=True, TRAQO_API_KEY="k")
    def test_configured_is_read_from_settings_rather_than_by_calling(self):
        self.assertTrue(traqo_discovery.is_traqo_configured())

    @override_settings(TRAQO_ENABLED=True, TRAQO_API_KEY="")
    def test_enabled_without_a_key_is_not_configured(self):
        self.assertFalse(traqo_discovery.is_traqo_configured())


class FakeVizionClient:
    """Serves a create response and then reference reads, or raises."""

    def __init__(self, create=None, reference=None, error=None):
        self.create = create or {}
        self.reference = reference
        self.error = error
        self.created: list[str] = []

    def create_reference(self, container_number, **kwargs):
        self.created.append(container_number)
        if self.error is not None:
            raise self.error
        return self.create

    def get_reference(self, reference_id):
        return self.reference if self.reference is not None else self.create


def _reference(*, status, carrier_code="", reference_id="vz-1"):
    return {
        "reference": {
            "id": reference_id,
            "container_id": CONTAINER_NUMBER,
            "last_update_status": status,
            "auto_carrier": True,
            "active": True,
            "carrier_code": carrier_code,
            "carrier": {"code": carrier_code, "name": "ONE"} if carrier_code else None,
        }
    }


class VizionAciOutcomeTest(TestCase):
    """Five outcomes, because "still looking" is not "no"."""

    def identify(self, client):
        # No waiting in tests: the poll interval is what the service sleeps for.
        return identify_carrier(CONTAINER_NUMBER, client=client, poll_attempts=2, poll_interval_seconds=0)

    def test_an_identified_carrier_is_found_and_translated(self):
        client = FakeVizionClient(
            create=_reference(status=""),
            reference=_reference(status="auto_carrier_completed", carrier_code="ONEY"),
        )

        result = self.identify(client)

        self.assertEqual(result.status, vizion_discovery.FOUND)
        self.assertTrue(result.found)
        self.assertEqual(result.carrier_code, "one")
        self.assertEqual(result.scac, "ONEY")

    def test_the_billable_reference_is_reported(self):
        client = FakeVizionClient(
            create=_reference(status="", reference_id="vz-99"),
            reference=_reference(status="auto_carrier_completed", carrier_code="ONEY", reference_id="vz-99"),
        )

        result = self.identify(client)

        self.assertEqual(result.reference_id, "vz-99")
        self.assertTrue(result.reference_created)

    def test_not_found_is_reported_with_its_reference_still_alive(self):
        """Vizion retries daily for seven days; the reference has to survive the answer."""
        client = FakeVizionClient(
            create=_reference(status=""),
            reference=_reference(status="auto_carrier_not_found"),
        )

        result = self.identify(client)

        self.assertEqual(result.status, vizion_discovery.NOT_FOUND)
        self.assertTrue(result.answered)
        self.assertTrue(result.reference_created)

    def test_still_looking_is_pending_and_not_a_denial(self):
        client = FakeVizionClient(create=_reference(status=""), reference=_reference(status="no_data"))

        result = self.identify(client)

        self.assertEqual(result.status, vizion_discovery.PENDING)
        self.assertFalse(result.answered)
        self.assertFalse(result.found)

    def test_a_failed_identification_is_an_error(self):
        client = FakeVizionClient(
            create=_reference(status=""),
            reference=_reference(status="auto_carrier_failed"),
        )

        result = self.identify(client)

        self.assertEqual(result.status, vizion_discovery.ERROR)

    def test_a_401_is_an_error_and_not_an_unknown_container(self):
        client = FakeVizionClient(error=CarrierAuthenticationError("401", provider_code="vizion"))

        result = self.identify(client)

        self.assertEqual(result.status, vizion_discovery.ERROR)
        self.assertFalse(result.answered)
        self.assertEqual(result.carrier_code, "")

    def test_a_missing_credential_is_not_configured(self):
        client = FakeVizionClient(error=CarrierConfigurationError("no key", provider_code="vizion"))

        result = self.identify(client)

        self.assertEqual(result.status, vizion_discovery.NOT_CONFIGURED)

    def test_identification_starts_no_tracking(self):
        from apps.scm.tracking.models import TrackingEvent, TrackingSubscription

        client = FakeVizionClient(
            create=_reference(status=""),
            reference=_reference(status="auto_carrier_completed", carrier_code="ONEY"),
        )
        self.identify(client)

        self.assertEqual(TrackingSubscription.objects.count(), 0)
        self.assertEqual(TrackingEvent.objects.count(), 0)

    def test_no_carrier_hint_is_ever_sent(self):
        """ACI is invoked by the container number alone; a hint would corrupt the answer."""
        client = FakeVizionClient(
            create=_reference(status=""),
            reference=_reference(status="auto_carrier_completed", carrier_code="ONEY"),
        )
        self.identify(client)

        self.assertEqual(client.created, [CONTAINER_NUMBER])

    @override_settings(VIZION_ENABLED=True, VIZION_API_KEY="k")
    def test_configured_is_read_from_settings(self):
        self.assertTrue(vizion_discovery.is_vizion_configured())

    @override_settings(VIZION_ENABLED=False, VIZION_API_KEY="k")
    def test_disabled_is_not_configured(self):
        self.assertFalse(vizion_discovery.is_vizion_configured())
