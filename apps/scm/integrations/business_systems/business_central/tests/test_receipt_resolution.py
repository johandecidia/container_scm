"""Receipt → serial number resolution (stage 2c): exact link through the receipt's item ledger entries.

No Business Central calls; receipts and ledger entries are built in BC's API shape.
"""

from datetime import date, timedelta

from django.core.cache import cache
from django.test import TestCase, override_settings

from apps.scm.containers.intake import create_or_get_container
from apps.scm.containers.models import Container, ContainerMovement
from apps.scm.integrations.business_systems.business_central.item_ledger_sync import upsert_item_ledger_entries
from apps.scm.integrations.business_systems.business_central.receipt_resolution import resolve_purchase_receipts
from apps.scm.integrations.business_systems.business_central.receipt_sync import upsert_purchase_receipts
from apps.scm.integrations.models import (
    BusinessCentralPurchaseReceipt,
    BusinessCentralReceiptContainer,
    ReceiptResolution,
)
from apps.scm.integrations.tests.test_item_rules import NOT_CONTAINER, PREFIX, bc_integration, equipment_type, rule
from apps.users.models import CustomUser

_LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache", "LOCATION": "bc-rcpt-resolve"}}
COMPANY = "782d7133-5033-4c4b-9a29-83f7f44f062d"
# Posting dates relative to today, so the receive's age limit never makes a test drift into failure.
RECENT = date.today() - timedelta(days=2)
R = ReceiptResolution
Receipt = BusinessCentralPurchaseReceipt
Line = BusinessCentralReceiptContainer


def ile(entry_no, serial, *, item="CON22G1", doc="IN1", qty=1, loc="L005", doc_type="Purchase Receipt", posted=None):
    """One raw item ledger entry as the Analytics API returns it."""
    return {
        "entryNo": entry_no,
        "itemNo": item,
        "serialNo": serial,
        "entryType": "Purchase" if doc_type == "Purchase Receipt" else "Sale",
        "documentType": doc_type,
        "documentNo": doc,
        "postingDate": (posted or RECENT).isoformat(),
        "quantity": qty,
        "locationCode": loc,
        "sourceType": "Vendor",
        "sourceNo": "1203",
    }


def bc_receipt(number, lines, *, posted=None):
    """One raw posted purchase receipt with ``lines`` = [(item, quantity), ...]."""
    return {
        "id": f"00000000-0000-0000-0000-{abs(hash(number)) % 10**12:012d}",
        "number": number,
        "postingDate": (posted or RECENT).isoformat(),
        "vendorNumber": "1203",
        "orderNumber": "100017",
        "lastModifiedDateTime": f"{(posted or RECENT).isoformat()}T10:00:00Z",
        "purchaseReceiptLines": [
            {"id": f"{number}-{i}", "sequence": i * 10000, "lineType": "Item", "lineObjectNumber": item, "quantity": q}
            for i, (item, q) in enumerate(lines, start=1)
        ],
    }


def receipt_setup(slug: str):
    """An integration with container rules: CON* → 22G1, CONNOTYPE container without type, CONHYRA not."""
    integration = bc_integration(slug)
    t20 = equipment_type("22G1")
    rule(integration, "CON", match_type=PREFIX, eq=t20)
    rule(integration, "CONNOTYPE")
    rule(integration, "CONHYRA", NOT_CONTAINER)
    user = CustomUser.objects.create_user(username=f"{slug}@example.com", email=f"{slug}@example.com", password="pw")
    return integration, user, t20


def load(integration, receipts, entries, company=COMPANY):
    upsert_purchase_receipts(integration, company, receipts)
    upsert_item_ledger_entries(integration, company, entries)


@override_settings(CACHES=_LOCMEM)
class ReceiptResolutionTest(TestCase):
    def setUp(self):
        self.integration, self.user, self.t20 = receipt_setup("rcpt-resolve")

    def tearDown(self):
        cache.clear()

    def _resolve(self):
        return resolve_purchase_receipts(self.integration)

    def _receipt(self, number):
        return Receipt.objects.get(integration=self.integration, receipt_no=number)

    def _lines(self, number):
        return {line.serial_no: line for line in Line.objects.filter(receipt__receipt_no=number)}

    def test_one_container_one_receipt(self):
        container, _ = create_or_get_container(
            team=self.integration.team, user=self.user, number="MCUU2901785", attributes={"equipment_type": self.t20}
        )
        load(self.integration, [bc_receipt("IN1", [("CON22G1", 1)])], [ile(1, "MCUU2901785")])
        self._resolve()
        self.assertEqual(self._receipt("IN1").resolution_status, R.RESOLVED)
        line = self._lines("IN1")["MCUU2901785"]
        self.assertEqual(
            (line.resolution_status, line.container, line.location_code, line.entry_no, line.item_no),
            (R.RESOLVED, container, "L005", 1, "CON22G1"),
        )
        self.assertEqual(line.posting_date, RECENT)

    def test_several_containers_on_several_lines_of_the_same_item(self):
        serials = ["MCUU2901785", "TCLU1234568", "CRTU4101684"]
        load(
            self.integration,
            [bc_receipt("IN2", [("CON22G1", 2), ("CON22G1", 1)])],
            [ile(10 + i, s, doc="IN2") for i, s in enumerate(serials)],
        )
        self._resolve()
        self.assertEqual(self._receipt("IN2").resolution_status, R.RESOLVED)
        lines = self._lines("IN2")
        self.assertEqual(set(lines), set(serials))
        self.assertTrue(all(line.resolution_status == R.RESOLVED and line.container is None for line in lines.values()))

    def test_fewer_serials_than_lines_is_missing_serial(self):
        load(self.integration, [bc_receipt("IN3", [("CON22G1", 3)])], [ile(20, "MCUU2901785", doc="IN3")])
        self._resolve()
        receipt = self._receipt("IN3")
        self.assertEqual(receipt.resolution_status, R.MISSING_SERIAL)
        self.assertEqual(receipt.resolution_details["items"]["CON22G1"], {"lines": "3", "serials": "1.00000"})

    def test_ledger_entries_synced_later_resolve_on_the_next_pass(self):
        load(self.integration, [bc_receipt("IN4", [("CON22G1", 1)])], [])
        self._resolve()
        self.assertEqual(self._receipt("IN4").resolution_status, R.MISSING_SERIAL)
        upsert_item_ledger_entries(self.integration, COMPANY, [ile(30, "MCUU2901785", doc="IN4")])
        self._resolve()
        self.assertEqual(self._receipt("IN4").resolution_status, R.RESOLVED)

    def test_invalid_serial(self):
        load(self.integration, [bc_receipt("IN5", [("CON22G1", 1)])], [ile(40, "MCUU2901786", doc="IN5")])
        self._resolve()
        self.assertEqual(self._lines("IN5")["MCUU2901786"].resolution_status, R.INVALID_SERIAL)

    def test_unclassified_item_with_serials(self):
        load(self.integration, [bc_receipt("IN6", [("BOX1", 1)])], [ile(50, "MCUU2901785", item="BOX1", doc="IN6")])
        self._resolve()
        self.assertEqual(self._receipt("IN6").resolution_status, R.UNCLASSIFIED_ITEM)
        self.assertEqual(self._lines("IN6")["MCUU2901785"].resolution_status, R.UNCLASSIFIED_ITEM)

    def test_non_container_items_are_not_applicable(self):
        load(
            self.integration,
            [bc_receipt("IN7", [("CONHYRA", 1), ("HANDLING", 1)])],
            [ile(60, "MCUU2901785", item="CONHYRA", doc="IN7")],
        )
        self._resolve()
        self.assertEqual(self._receipt("IN7").resolution_status, R.NOT_APPLICABLE)
        self.assertEqual(self._lines("IN7")["MCUU2901785"].resolution_status, R.NOT_APPLICABLE)

    def test_missing_equipment_type_only_matters_without_a_container(self):
        load(
            self.integration,
            [bc_receipt("IN8", [("CONNOTYPE", 2)])],
            [ile(70, "MCUU2901785", item="CONNOTYPE", doc="IN8"), ile(71, "TCLU1234568", item="CONNOTYPE", doc="IN8")],
        )
        create_or_get_container(
            team=self.integration.team, user=self.user, number="MCUU2901785", attributes={"equipment_type": self.t20}
        )
        self._resolve()
        lines = self._lines("IN8")
        self.assertEqual(lines["MCUU2901785"].resolution_status, R.RESOLVED)
        self.assertEqual(lines["TCLU1234568"].resolution_status, R.MISSING_EQUIPMENT_TYPE)

    def test_undone_receipt_is_reversed(self):
        load(
            self.integration,
            [bc_receipt("IN9", [("CON22G1", 1), ("CON22G1", -1)])],
            [ile(80, "MCUU2901785", doc="IN9"), ile(81, "MCUU2901785", doc="IN9", qty=-1)],
        )
        self._resolve()
        self.assertEqual(self._receipt("IN9").resolution_status, R.REVERSED)
        line = self._lines("IN9")["MCUU2901785"]
        self.assertEqual((line.resolution_status, line.entry_no), (R.REVERSED, 80))

    def test_partly_undone_receipt_resolves_the_rest(self):
        load(
            self.integration,
            [bc_receipt("IN10", [("CON22G1", 2), ("CON22G1", -1)])],
            [
                ile(90, "MCUU2901785", doc="IN10"),
                ile(91, "TCLU1234568", doc="IN10"),
                ile(92, "TCLU1234568", doc="IN10", qty=-1),
            ],
        )
        self._resolve()
        self.assertEqual(self._receipt("IN10").resolution_status, R.RESOLVED)
        lines = self._lines("IN10")
        self.assertEqual(lines["MCUU2901785"].resolution_status, R.RESOLVED)
        self.assertEqual(lines["TCLU1234568"].resolution_status, R.REVERSED)

    def test_same_serial_twice_on_one_receipt_is_ambiguous(self):
        load(
            self.integration,
            [bc_receipt("IN11", [("CON22G1", 2)])],
            [ile(100, "MCUU2901785", doc="IN11"), ile(101, "MCUU2901785", doc="IN11")],
        )
        self._resolve()
        self.assertEqual(self._receipt("IN11").resolution_status, R.AMBIGUOUS)
        self.assertEqual(self._lines("IN11")["MCUU2901785"].resolution_status, R.AMBIGUOUS)

    def test_more_serials_than_lines_is_ambiguous(self):
        load(
            self.integration,
            [bc_receipt("IN12", [("CON22G1", 1)])],
            [ile(110, "MCUU2901785", doc="IN12"), ile(111, "TCLU1234568", doc="IN12")],
        )
        self._resolve()
        self.assertEqual(self._receipt("IN12").resolution_status, R.AMBIGUOUS)

    def test_only_entries_of_this_receipt_document_and_company_count(self):
        load(
            self.integration,
            [bc_receipt("IN13", [("CON22G1", 1)])],
            [
                ile(120, "MCUU2901785", doc="IN13"),
                ile(121, "TCLU1234568", doc="IN13", doc_type="Sales Return Receipt", qty=1),
                ile(122, "CRTU4101684", doc="IN130"),
            ],
        )
        upsert_item_ledger_entries(self.integration, "other-company", [ile(123, "MSCU7654329", doc="IN13")])
        self._resolve()
        self.assertEqual(set(self._lines("IN13")), {"MCUU2901785"})
        self.assertEqual(self._receipt("IN13").resolution_status, R.RESOLVED)

    def test_other_teams_entries_and_containers_are_never_used(self):
        other, other_user, t20 = receipt_setup("rcpt-resolve-other")
        create_or_get_container(
            team=other.team, user=other_user, number="MCUU2901785", attributes={"equipment_type": t20}
        )
        load(other, [], [ile(130, "TCLU1234568", doc="IN14")])
        load(self.integration, [bc_receipt("IN14", [("CON22G1", 1)])], [ile(130, "MCUU2901785", doc="IN14")])
        self._resolve()
        lines = self._lines("IN14")
        self.assertEqual(set(lines), {"MCUU2901785"})
        self.assertIsNone(lines["MCUU2901785"].container)  # the other team's container is invisible

    def test_repeated_passes_create_nothing_new_and_change_nothing(self):
        load(self.integration, [bc_receipt("IN15", [("CON22G1", 1)])], [ile(140, "MCUU2901785", doc="IN15")])
        self._resolve()
        first = Line.objects.get(receipt__receipt_no="IN15")
        self._resolve()
        again = Line.objects.get(receipt__receipt_no="IN15")
        self.assertEqual((Line.objects.count(), again.pk, again.updated_at), (1, first.pk, first.updated_at))

    def test_resolution_has_no_operational_side_effects(self):
        load(self.integration, [bc_receipt("IN16", [("CON22G1", 1)])], [ile(150, "MCUU2901785", doc="IN16")])
        self._resolve()
        self.assertFalse(Container.objects.exists())
        self.assertFalse(ContainerMovement.objects.exists())
