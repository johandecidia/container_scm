"""Tests for carrier identity on a subscription, and the places that used to conflate it.

Three groups, each covering a way the old single-code model went wrong once an
aggregator could carry a carrier:

*Recording.* Carrier identity is added but never overwritten, so a later lookup cannot
quietly replace a carrier that has already proved itself.

*Reading.* A direct watch's provider legitimately answers "who is the carrier"; an
aggregator's does not, and must read as unknown rather than as the aggregator's name.

*Continuation exclusion.* The sweep excludes carriers. A Traqo watch carrying ONE has to
contribute ``one`` to that exclusion set — contributing ``traqo`` would exclude nothing
and let the sweep probe ONE directly moments after Traqo answered about it.
"""

from datetime import UTC, datetime

from django.test import TestCase, override_settings
from django.utils import timezone

from apps.scm.containers.models import Container, EquipmentType
from apps.scm.integrations.traqo import PROVIDER_CODE as TRAQO_PROVIDER_CODE
from apps.scm.tracking.continuation import get_recently_checked_carrier_codes
from apps.scm.tracking.manual_refresh import (
    describe_subscription_carrier,
    get_or_create_container_subscription,
    record_subscription_carrier,
)
from apps.scm.tracking.models import CarrierSource, TrackingSubscription
from apps.scm.tracking.selectors import TrackingProvenance, get_container_tracking_provenance
from apps.teams.models import Team

_LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache", "LOCATION": "carrier-identity"}}


def _equipment_type():
    return EquipmentType.objects.get_or_create(
        iso_code="22G1",
        defaults={"category": "GP", "length_ft": 20, "high_cube": False, "description": "20' GP"},
    )[0]


@override_settings(CACHES=_LOCMEM)
class IdentityTestBase(TestCase):
    def setUp(self):
        self.team = Team.objects.create(name="identity", slug=f"identity-{self.__class__.__name__.lower()}")
        self.container = Container.objects.create(
            team=self.team,
            owner_code="BBC",
            category_id="U",
            serial_number="327307",
            check_digit=0,
            equipment_type=_equipment_type(),
        )

    def traqo_watch(self, **carrier):
        return get_or_create_container_subscription(
            team=self.team,
            container=self.container,
            provider_code=TRAQO_PROVIDER_CODE,
            provider_name="Traqo Ocean",
            **carrier,
        )

    def direct_watch(self, provider_code="maersk", provider_name="Maersk", **carrier):
        return get_or_create_container_subscription(
            team=self.team,
            container=self.container,
            provider_code=provider_code,
            provider_name=provider_name,
            **carrier,
        )


class RecordingCarrierIdentityTest(IdentityTestBase):
    def test_a_new_watch_stores_the_carrier_beside_the_provider(self):
        subscription = self.traqo_watch(
            carrier_code="one",
            carrier_name="ONE (Ocean Network Express)",
            carrier_source=CarrierSource.VIZION_ACI,
            provider_reference="ONEY",
        )

        self.assertEqual(subscription.provider.code, TRAQO_PROVIDER_CODE)
        self.assertEqual(subscription.carrier_code, "one")
        self.assertEqual(subscription.carrier_source, CarrierSource.VIZION_ACI)
        self.assertEqual(subscription.provider_reference, "ONEY")

    def test_carrier_identity_is_not_part_of_the_natural_key(self):
        """A provider learning the carrier must enrich the watch, not create a second."""
        first = self.traqo_watch()
        second = self.traqo_watch(carrier_code="one", carrier_source=CarrierSource.VIZION_ACI)

        self.assertEqual(first.pk, second.pk)
        self.assertEqual(TrackingSubscription.objects.filter(team=self.team, container=self.container).count(), 1)

    def test_a_later_call_fills_in_a_carrier_that_was_missing(self):
        subscription = self.traqo_watch()
        self.assertEqual(subscription.carrier_code, "")

        changed = record_subscription_carrier(
            subscription,
            carrier_code="one",
            carrier_name="ONE (Ocean Network Express)",
            carrier_source=CarrierSource.VIZION_ACI,
        )

        subscription.refresh_from_db()
        self.assertTrue(changed)
        self.assertEqual(subscription.carrier_code, "one")

    def test_an_established_carrier_is_never_overwritten(self):
        """A disagreement about who is moving a box is for a person to settle."""
        subscription = self.traqo_watch(
            carrier_code="one",
            carrier_name="ONE (Ocean Network Express)",
            carrier_source=CarrierSource.DIRECT_API,
        )

        changed = record_subscription_carrier(
            subscription,
            carrier_code="maersk",
            carrier_name="Maersk",
            carrier_source=CarrierSource.TRAQO_LOOKUP,
        )

        subscription.refresh_from_db()
        self.assertFalse(changed)
        self.assertEqual(subscription.carrier_code, "one")
        self.assertEqual(subscription.carrier_source, CarrierSource.DIRECT_API)

    def test_the_provider_reference_is_refreshed_rather_than_protected(self):
        """It is the provider's handle for the same watch; a newer one is more current."""
        subscription = self.traqo_watch(carrier_code="one", provider_reference="ONEY")

        changed = record_subscription_carrier(subscription, provider_reference="ONEX")

        subscription.refresh_from_db()
        self.assertTrue(changed)
        self.assertEqual(subscription.provider_reference, "ONEX")
        self.assertEqual(subscription.carrier_code, "one", "the carrier was not disturbed")

    def test_recording_nothing_new_writes_nothing(self):
        subscription = self.traqo_watch(carrier_code="one", provider_reference="ONEY")

        self.assertFalse(record_subscription_carrier(subscription, carrier_code="one", provider_reference="ONEY"))


class ReadingCarrierIdentityTest(IdentityTestBase):
    def test_an_aggregator_watch_with_a_carrier_names_the_carrier(self):
        subscription = self.traqo_watch(
            carrier_code="one",
            carrier_name="ONE (Ocean Network Express)",
            carrier_source=CarrierSource.VIZION_ACI,
        )
        provenance = TrackingProvenance(subscription)

        self.assertIn("ONE", provenance.carrier_name)
        self.assertEqual(provenance.provider_label, "Traqo Ocean")
        self.assertFalse(provenance.is_direct)

    def test_an_aggregator_watch_without_a_carrier_reads_as_unknown(self):
        """Never "Traqo" — that is the exact conflation these fields exist to end."""
        provenance = TrackingProvenance(self.traqo_watch())

        self.assertEqual(provenance.carrier_name, "")
        self.assertEqual(provenance.carrier_code, "")
        self.assertFalse(provenance.carrier_known)
        self.assertEqual(provenance.provider_label, "Traqo Ocean")

    def test_a_direct_watch_needs_no_recorded_carrier_to_name_one(self):
        """Maersk supplying the data and Maersk carrying the box are the same fact."""
        provenance = TrackingProvenance(self.direct_watch())

        self.assertEqual(provenance.carrier_code, "maersk")
        self.assertEqual(provenance.carrier_name, "Maersk")
        self.assertTrue(provenance.is_direct)
        self.assertFalse(provenance.carrier_recorded, "derived here, not stored")

    def test_a_direct_watch_reports_its_data_source_as_the_direct_api(self):
        self.assertEqual(TrackingProvenance(self.direct_watch()).provider_label, "Direct API")

    def test_the_source_label_is_only_offered_for_a_recorded_carrier(self):
        self.assertEqual(TrackingProvenance(self.direct_watch()).carrier_source_label, "")
        recorded = self.traqo_watch(carrier_code="one", carrier_source=CarrierSource.VIZION_ACI)
        self.assertEqual(TrackingProvenance(recorded).carrier_source_label, "Vizion Auto Carrier Identification")

    def test_provenance_is_listed_per_source_oldest_first(self):
        self.direct_watch(provider_code="cma_cgm", provider_name="CMA CGM")
        self.traqo_watch(carrier_code="one", carrier_source=CarrierSource.VIZION_ACI)

        entries = get_container_tracking_provenance(self.team, self.container)

        self.assertEqual([entry.provider_code for entry in entries], ["cma_cgm", TRAQO_PROVIDER_CODE])
        self.assertEqual([entry.carrier_code for entry in entries], ["cma_cgm", "one"])

    def test_describing_a_watch_for_a_message_agrees_with_the_read_model(self):
        subscription = self.traqo_watch(
            carrier_code="one",
            carrier_name="ONE (Ocean Network Express)",
            carrier_source=CarrierSource.VIZION_ACI,
        )

        self.assertEqual(describe_subscription_carrier(subscription), ("one", "ONE (Ocean Network Express)"))

    def test_describing_a_carrierless_aggregator_watch_names_who_was_asked(self):
        """ "No data found at Traqo" is about the act, so the provider is the right name."""
        code, name = describe_subscription_carrier(self.traqo_watch())

        self.assertEqual(code, "", "nothing downstream may mistake this for a carrier")
        self.assertEqual(name, "Traqo Ocean")


def _backfill_migration():
    """Import the backfill migration by path — its module name starts with a digit."""
    import importlib

    return importlib.import_module("apps.scm.tracking.migrations.0012_backfill_subscription_carrier_identity")


class BackfillMigrationTest(IdentityTestBase):
    """The data migration, run against real rows.

    Driven through the migration's own function with the live app registry rather than a
    historical one — the fields it touches exist in both, and this way the test exercises
    the code that will actually run on Johan's database.
    """

    def backfill(self):
        from django.apps import apps as live_apps

        migration = _backfill_migration()
        migration.backfill(live_apps, None)

    def test_a_direct_watch_gains_the_carrier_it_always_implied(self):
        subscription = self.direct_watch(provider_code="maersk", provider_name="Maersk")
        self.assertEqual(subscription.carrier_code, "")

        self.backfill()

        subscription.refresh_from_db()
        self.assertEqual(subscription.carrier_code, "maersk")
        self.assertEqual(subscription.carrier_name, "Maersk")
        self.assertEqual(subscription.carrier_source, CarrierSource.DIRECT_API)

    def test_an_aggregator_watch_is_left_with_no_carrier(self):
        """``carrier_code = traqo`` is the precise falsehood this change exists to remove."""
        subscription = self.traqo_watch()

        self.backfill()

        subscription.refresh_from_db()
        self.assertEqual(subscription.carrier_code, "")
        self.assertEqual(subscription.carrier_source, "")

    def test_an_existing_carrier_is_not_disturbed(self):
        subscription = self.traqo_watch(
            carrier_code="one",
            carrier_name="ONE (Ocean Network Express)",
            carrier_source=CarrierSource.VIZION_ACI,
        )

        self.backfill()

        subscription.refresh_from_db()
        self.assertEqual(subscription.carrier_code, "one")
        self.assertEqual(subscription.carrier_source, CarrierSource.VIZION_ACI)

    def test_running_it_twice_changes_nothing(self):
        subscription = self.direct_watch(provider_code="cosco", provider_name="COSCO Shipping")

        self.backfill()
        subscription.refresh_from_db()
        first = (subscription.carrier_code, subscription.carrier_name, subscription.carrier_source)
        self.backfill()
        subscription.refresh_from_db()

        self.assertEqual((subscription.carrier_code, subscription.carrier_name, subscription.carrier_source), first)

    def test_the_reverse_clears_only_what_it_wrote(self):
        from django.apps import apps as live_apps

        migration = _backfill_migration()

        direct = self.direct_watch(provider_code="maersk", provider_name="Maersk")
        aggregator = self.traqo_watch(carrier_code="one", carrier_source=CarrierSource.VIZION_ACI)
        self.backfill()

        migration.unfill(live_apps, None)

        direct.refresh_from_db()
        aggregator.refresh_from_db()
        self.assertEqual(direct.carrier_code, "")
        self.assertEqual(aggregator.carrier_code, "one", "an aggregator's carrier was never the migration's to clear")


class ContinuationExclusionUsesCarrierCodesTest(IdentityTestBase):
    """The sweep excludes carriers, so this set has to be in carrier space."""

    def watch(self, subscription, *, synced_at=None):
        subscription.last_synced_at = synced_at or timezone.now()
        subscription.save(update_fields=["last_synced_at", "updated_at"])
        return subscription

    def test_a_traqo_watch_contributes_its_carrier_not_the_aggregator(self):
        self.watch(self.traqo_watch(carrier_code="one", carrier_source=CarrierSource.VIZION_ACI))

        codes = get_recently_checked_carrier_codes(self.team, self.container)

        self.assertEqual(codes, frozenset({"one"}))
        self.assertNotIn(TRAQO_PROVIDER_CODE, codes)

    def test_a_direct_watch_contributes_its_own_code(self):
        self.watch(self.direct_watch(provider_code="cma_cgm", provider_name="CMA CGM"))

        self.assertEqual(get_recently_checked_carrier_codes(self.team, self.container), frozenset({"cma_cgm"}))

    def test_an_aggregator_watch_with_no_carrier_excludes_nothing(self):
        """We do not know which carrier it covered, so we cannot skip one."""
        self.watch(self.traqo_watch())

        self.assertEqual(get_recently_checked_carrier_codes(self.team, self.container), frozenset())

    def test_a_watch_not_polled_recently_stays_in_the_sweep(self):
        self.watch(
            self.traqo_watch(carrier_code="one", carrier_source=CarrierSource.VIZION_ACI),
            synced_at=datetime(2020, 1, 1, tzinfo=UTC),
        )

        self.assertEqual(get_recently_checked_carrier_codes(self.team, self.container), frozenset())
