"""End-to-end flow tests for the Business Central PO XML import.

Verifies the full pipeline:
  1. Parse the anonymized BC XML fixture with ``parse_bc_po_xml``.
  2. Pass the parsed data to ``import_purchase_orders_from_bc``.
  3. Assert that ``PurchaseOrder`` and ``PurchaseOrderLine`` records are
     created in the database and scoped to the correct team.

Multi-tenancy: a second team is created to confirm that records belonging to
team A are never visible to team B.
"""

import io
from decimal import Decimal
from pathlib import Path

from django.test import TestCase

from apps.scm.imports.bc_xml_parser import parse_bc_po_xml
from apps.scm.procurement.models import PurchaseOrder
from apps.scm.procurement.selectors import get_team_purchase_orders
from apps.scm.procurement.services import import_purchase_orders_from_bc
from apps.teams.models import Team
from apps.users.models import CustomUser

FIXTURE_PATH = (
    Path(__file__).parent.parent / "imports" / "tests" / "fixtures" / "business_central_purchase_order_sample.xml"
)


def _make_team(slug: str) -> Team:
    return Team.objects.get_or_create(slug=slug, defaults={"name": slug})[0]


def _make_user(email: str) -> CustomUser:
    return CustomUser.objects.get_or_create(username=email, defaults={"email": email})[0]


class BCXmlEndToEndImportTests(TestCase):
    """Full round-trip: fixture XML → parse → import → DB assertions."""

    def setUp(self):
        self.team = _make_team("e2e-team-alpha")
        self.user = _make_user("e2e@example.com")

    def _parse_fixture(self):
        return parse_bc_po_xml(io.BytesIO(FIXTURE_PATH.read_bytes()))

    def test_fixture_parses_without_error(self):
        pos = self._parse_fixture()
        self.assertEqual(len(pos), 1)

    def test_import_creates_purchase_order(self):
        pos = self._parse_fixture()
        import_purchase_orders_from_bc(self.team, pos)
        self.assertEqual(PurchaseOrder.objects.filter(team=self.team).count(), 1)

    def test_import_creates_correct_po_number(self):
        pos = self._parse_fixture()
        import_purchase_orders_from_bc(self.team, pos)
        po = PurchaseOrder.objects.get(team=self.team)
        self.assertEqual(po.po_number, "PO-2026-001")

    def test_import_creates_correct_supplier(self):
        pos = self._parse_fixture()
        import_purchase_orders_from_bc(self.team, pos)
        po = PurchaseOrder.objects.get(team=self.team)
        self.assertEqual(po.supplier_no, "SUPP-0001")
        self.assertEqual(po.supplier_name, "Anon Supplier Ltd")

    def test_import_creates_correct_order_date(self):
        from datetime import date

        pos = self._parse_fixture()
        import_purchase_orders_from_bc(self.team, pos)
        po = PurchaseOrder.objects.get(team=self.team)
        self.assertEqual(po.order_date, date(2026, 3, 15))

    def test_import_creates_correct_expected_receipt_date(self):
        from datetime import date

        pos = self._parse_fixture()
        import_purchase_orders_from_bc(self.team, pos)
        po = PurchaseOrder.objects.get(team=self.team)
        self.assertEqual(po.expected_receipt_date, date(2026, 6, 1))

    def test_import_creates_correct_currency(self):
        pos = self._parse_fixture()
        import_purchase_orders_from_bc(self.team, pos)
        po = PurchaseOrder.objects.get(team=self.team)
        self.assertEqual(po.currency, "USD")

    def test_import_creates_three_lines(self):
        pos = self._parse_fixture()
        import_purchase_orders_from_bc(self.team, pos)
        po = PurchaseOrder.objects.get(team=self.team)
        self.assertEqual(po.lines.count(), 3)

    def test_lines_have_correct_item_nos(self):
        pos = self._parse_fixture()
        import_purchase_orders_from_bc(self.team, pos)
        po = PurchaseOrder.objects.get(team=self.team)
        item_nos = list(po.lines.values_list("item_no", flat=True))
        self.assertIn("ITM-4210", item_nos)
        self.assertIn("ITM-7830", item_nos)
        self.assertIn("ITM-9010", item_nos)

    def test_lines_have_correct_quantities(self):
        pos = self._parse_fixture()
        import_purchase_orders_from_bc(self.team, pos)
        po = PurchaseOrder.objects.get(team=self.team)
        qtys = {line.item_no: line.ordered_qty for line in po.lines.all()}
        self.assertEqual(qtys["ITM-4210"], Decimal("50"))
        self.assertEqual(qtys["ITM-7830"], Decimal("100"))
        self.assertEqual(qtys["ITM-9010"], Decimal("15.000"))

    def test_idempotent_import_does_not_duplicate(self):
        """Importing the same XML twice must not create duplicate records."""
        pos = self._parse_fixture()
        import_purchase_orders_from_bc(self.team, pos)
        import_purchase_orders_from_bc(self.team, pos)
        self.assertEqual(PurchaseOrder.objects.filter(team=self.team).count(), 1)
        po = PurchaseOrder.objects.get(team=self.team)
        self.assertEqual(po.lines.count(), 3)

    def test_lines_scoped_to_team(self):
        pos = self._parse_fixture()
        import_purchase_orders_from_bc(self.team, pos)
        po = PurchaseOrder.objects.get(team=self.team)
        for line in po.lines.all():
            self.assertEqual(line.team, self.team)


class BCXmlMultiTenancyTests(TestCase):
    """Confirm that PO records are isolated between teams."""

    def setUp(self):
        self.team_a = _make_team("e2e-team-beta-a")
        self.team_b = _make_team("e2e-team-beta-b")

    def _parse_fixture(self):
        return parse_bc_po_xml(io.BytesIO(FIXTURE_PATH.read_bytes()))

    def test_team_b_cannot_see_team_a_orders(self):
        pos = self._parse_fixture()
        import_purchase_orders_from_bc(self.team_a, pos)

        team_b_orders = get_team_purchase_orders(self.team_b)
        self.assertEqual(team_b_orders.count(), 0)

    def test_team_a_selector_returns_its_own_orders(self):
        pos = self._parse_fixture()
        import_purchase_orders_from_bc(self.team_a, pos)

        team_a_orders = get_team_purchase_orders(self.team_a)
        self.assertEqual(team_a_orders.count(), 1)
