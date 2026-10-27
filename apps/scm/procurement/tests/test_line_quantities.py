"""Outstanding quantity, line references and their display on the purchase order page."""

from decimal import Decimal

from django.test import Client, SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from apps.scm.procurement.models import PurchaseOrder, PurchaseOrderLine, PurchaseOrderSource
from apps.scm.procurement.services import compute_line_sync_hash, create_purchase_order, upsert_purchase_orders
from apps.teams.models import Team
from apps.teams.roles import ROLE_MEMBER
from apps.users.models import CustomUser

_TEST_STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}

_LINE = {"external_id": "l1", "line_no": "10000", "item_no": "CON22G1", "ordered_qty": 49}


class OutstandingQuantityTest(SimpleTestCase):
    def test_ordered_minus_received(self):
        line = PurchaseOrderLine(ordered_qty=Decimal("49"), received_qty=Decimal("3"))
        self.assertEqual(line.outstanding_qty, Decimal("46"))

    def test_decimals(self):
        line = PurchaseOrderLine(ordered_qty=Decimal("10.5"), received_qty=Decimal("2.25"))
        self.assertEqual(line.outstanding_qty, Decimal("8.25"))

    def test_over_receipt_is_not_negative(self):
        line = PurchaseOrderLine(ordered_qty=Decimal("2"), received_qty=Decimal("3"))
        self.assertEqual(line.outstanding_qty, Decimal("0"))


class LineHashTest(SimpleTestCase):
    def test_sources_without_references_keep_their_hash(self):
        """Adding the references must not re-hash every line a document import already stored."""
        self.assertEqual(
            compute_line_sync_hash(_LINE),
            compute_line_sync_hash({**_LINE, "variant_code": "", "location_code": None}),
        )

    def test_reference_change_changes_hash(self):
        self.assertNotEqual(
            compute_line_sync_hash({**_LINE, "variant_code": "RAL5013"}),
            compute_line_sync_hash({**_LINE, "variant_code": "RAL7013"}),
        )


class OtherSourcesTest(TestCase):
    def setUp(self):
        self.team = Team.objects.create(name="qty-sources", slug="qty-sources")

    def test_document_import_without_received_quantity_still_writes_zero(self):
        upsert_purchase_orders(
            self.team,
            [{"external_id": "doc-1", "po_number": "D1", "lines": [_LINE]}],
            source_system=PurchaseOrderSource.DOCUMENT_IMPORT,
        )
        line = PurchaseOrderLine.objects.get(team=self.team)
        self.assertEqual(line.received_qty, Decimal("0"))
        self.assertEqual(line.variant_code, "")
        self.assertEqual(line.location_code, "")

    def test_manual_order_line_is_unchanged(self):
        po = create_purchase_order(self.team, po_number="M1", supplier_name="Manual")
        line = PurchaseOrderLine.objects.create(
            team=self.team, purchase_order=po, external_id="m-1", line_no="1", item_no="X", ordered_qty=5
        )
        self.assertEqual(po.source_system, PurchaseOrderSource.MANUAL)
        self.assertEqual(line.received_qty, Decimal("0"))
        self.assertEqual(line.outstanding_qty, Decimal("5"))


@override_settings(STORAGES=_TEST_STORAGES)
class LineTableTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="Line table", slug="line-table")
        cls.user = CustomUser.objects.create_user(username="line-table@example.com", password="pass")
        cls.team.members.add(cls.user, through_defaults={"role": ROLE_MEMBER})
        cls.po = PurchaseOrder.objects.create(
            team=cls.team, external_id="bc-100021", po_number="100021", supplier_name="Bullbox"
        )
        cls.line = PurchaseOrderLine.objects.create(
            team=cls.team,
            purchase_order=cls.po,
            external_id="bc-100021-10000",
            line_no="10000",
            item_no="CON22G1",
            description="20' Dry Van",
            ordered_qty=49,
            received_qty=3,
            variant_code="RAL5013",
            location_code="L005",
            location_name="Lager MCR05 - Lager Oceantermialen i JohnEvans",
        )

    def _get(self, user=None, po=None):
        client = Client()
        client.force_login(user or self.user)
        return client.get(
            reverse("procurement:purchase_order_detail", kwargs={"purchase_order_id": (po or self.po).pk})
        )

    def _row(self, content: str) -> str:
        start = content.index(">CON22G1<")
        return content[start : content.index("</tr>", start)]

    def test_line_shows_quantities_variant_and_source_location(self):
        response = self._get()
        self.assertEqual(response.status_code, 200)
        row = self._row(response.content.decode())
        self.assertIn("RAL5013", row)
        self.assertIn("L005", row)
        self.assertIn("Lager MCR05 - Lager Oceantermialen i JohnEvans", row)
        for figure in (">49<", ">3<", ">46<"):
            self.assertIn(figure, row.replace(" ", "").replace("\n", ""))

    def test_missing_variant_and_location_show_a_dash(self):
        PurchaseOrderLine.objects.filter(pk=self.line.pk).update(variant_code="", location_code="", location_name="")
        row = self._row(self._get().content.decode())
        self.assertIn("—", row)
        self.assertNotIn("L005", row)

    def test_other_team_cannot_see_the_line(self):
        outsider = CustomUser.objects.create_user(username="line-outsider@example.com", password="pass")
        other = Team.objects.create(name="Other line team", slug="other-line-team")
        other.members.add(outsider, through_defaults={"role": ROLE_MEMBER})
        self.assertEqual(self._get(user=outsider).status_code, 404)
