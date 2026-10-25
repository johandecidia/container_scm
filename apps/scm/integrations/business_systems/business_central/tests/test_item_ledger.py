"""Serial classification, mapping and the item ledger entry model. No Business Central calls."""

from decimal import Decimal

from django.db import IntegrityError, transaction
from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from apps.scm.containers.models import Container
from apps.scm.integrations.business_systems.business_central.item_ledger import (
    classify_serial,
    compute_sync_hash,
    map_item_ledger_entry,
)
from apps.scm.integrations.models import BusinessCentralItemLedgerEntry, Integration
from apps.teams.models import Team

SerialStatus = BusinessCentralItemLedgerEntry.SerialStatus

_RAW = {
    "entryNo": 92058,
    "itemNo": "CON22G1",
    "serialNo": " MCUU2901785 ",
    "entryType": "Positive Adjmt.",
    "documentType": " ",
    "documentNo": "IB L005",
    "postingDate": "2026-07-28",
    "quantity": 1,
    "remainingQuantity": 1,
    "open": True,
    "lotNo": "",
    "locationCode": "MALMO",
    "sourceType": "",
    "sourceNo": "",
    "expirationDate": "0001-01-01",
}


class ClassifySerialTest(SimpleTestCase):
    def test_valid_iso(self):
        self.assertEqual(classify_serial("MCUU2901785"), ("MCUU2901785", SerialStatus.VALID_ISO))

    def test_leading_and_trailing_whitespace_and_case(self):
        self.assertEqual(classify_serial(" mcuu2901785 "), ("MCUU2901785", SerialStatus.VALID_ISO))

    def test_wrong_check_digit_is_not_corrected(self):
        self.assertEqual(classify_serial("MCUU2901786"), ("MCUU2901786", SerialStatus.INVALID_ISO))

    def test_invalid_format(self):
        for raw in ("CONX0001", "12345678901", "TEST1", "MCUU290178"):
            with self.subTest(raw=raw):
                self.assertEqual(classify_serial(raw)[1], SerialStatus.INVALID_ISO)

    def test_missing_serial(self):
        for raw in ("", "   ", None):
            with self.subTest(raw=raw):
                self.assertEqual(classify_serial(raw), ("", SerialStatus.MISSING_SERIAL))


class MapItemLedgerEntryTest(SimpleTestCase):
    def test_maps_fields_and_keeps_raw(self):
        mapped = map_item_ledger_entry(_RAW)
        self.assertEqual(mapped["entry_no"], 92058)
        self.assertEqual(mapped["serial_no_raw"], " MCUU2901785 ")
        self.assertEqual(mapped["serial_no"], "MCUU2901785")
        self.assertEqual(mapped["serial_status"], SerialStatus.VALID_ISO)
        self.assertEqual(mapped["document_type"], "")  # BC's blank option is a single space
        self.assertEqual(mapped["quantity"], Decimal("1.00000"))
        self.assertEqual(str(mapped["posting_date"]), "2026-07-28")
        self.assertEqual(mapped["raw_payload"], _RAW)
        self.assertNotIn("open", mapped)  # fields BC updates later stay in raw_payload only

    def test_entry_no_is_required(self):
        for value in (None, "92058", True, 1.5):
            with self.subTest(value=value), self.assertRaises(ValueError):
                map_item_ledger_entry({**_RAW, "entryNo": value})

    def test_uninterpretable_values_are_kept_raw(self):
        raw = {**_RAW, "postingDate": "not-a-date", "quantity": "lots"}
        mapped = map_item_ledger_entry(raw)
        self.assertIsNone(mapped["posting_date"])
        self.assertIsNone(mapped["quantity"])
        self.assertEqual(mapped["raw_payload"]["quantity"], "lots")

    def test_out_of_range_quantity_is_kept_raw(self):
        self.assertIsNone(map_item_ledger_entry({**_RAW, "quantity": 1e30})["quantity"])

    def test_blank_bc_date_is_none(self):
        self.assertIsNone(map_item_ledger_entry({**_RAW, "postingDate": "0001-01-01"})["posting_date"])

    def test_sync_hash_changes_with_mutable_fields_and_ignores_odata_keys(self):
        self.assertNotEqual(compute_sync_hash(_RAW), compute_sync_hash({**_RAW, "remainingQuantity": 0}))
        self.assertEqual(compute_sync_hash(_RAW), compute_sync_hash({**_RAW, "@odata.etag": "x"}))


class ItemLedgerEntryModelTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="ile-model", slug="ile-model")
        cls.integration = Integration.objects.create(
            team=cls.team,
            name="BC",
            provider_code="business_central",
            provider_family=Integration.ProviderFamily.BUSINESS_SYSTEM,
        )

    def _entry(self, integration=None, company_id="company-a", entry_no=1, **extra):
        integration = integration or self.integration
        return BusinessCentralItemLedgerEntry.objects.create(
            team=integration.team,
            integration=integration,
            company_id=company_id,
            entry_no=entry_no,
            serial_status=SerialStatus.MISSING_SERIAL,
            sync_hash="h",
            synced_at=timezone.now(),
            **extra,
        )

    def test_defaults_to_unclassified(self):
        self.assertEqual(
            self._entry().item_classification, BusinessCentralItemLedgerEntry.ItemClassification.UNCLASSIFIED
        )

    def test_same_entry_twice_is_rejected(self):
        self._entry()
        with self.assertRaises(IntegrityError), transaction.atomic():
            self._entry()

    def test_same_entry_no_in_another_company_is_a_different_entry(self):
        self._entry(company_id="company-a")
        self._entry(company_id="company-b")
        self.assertEqual(BusinessCentralItemLedgerEntry.objects.filter(entry_no=1).count(), 2)

    def test_same_entry_no_and_serial_in_another_team(self):
        other_team = Team.objects.create(name="ile-other", slug="ile-other")
        other = Integration.objects.create(
            team=other_team,
            name="BC",
            provider_code="business_central",
            provider_family=Integration.ProviderFamily.BUSINESS_SYSTEM,
        )
        self._entry(serial_no="MCUU2901785")
        self._entry(integration=other, serial_no="MCUU2901785")
        self.assertEqual(BusinessCentralItemLedgerEntry.objects.filter(team=self.team).count(), 1)
        self.assertEqual(BusinessCentralItemLedgerEntry.objects.filter(team=other_team).count(), 1)

    def test_creating_entries_never_creates_containers(self):
        self._entry(serial_no="MCUU2901785")
        self.assertFalse(Container.objects.exists())
