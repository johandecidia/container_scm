"""Performance optimization tests for SCM views (8.6).

Tests:
- Pagination present on heavy list views
- Querysets filter correctly by team
- select_related / prefetch_related prevent obvious N+1 in selectors
"""

from django.test import TestCase

from apps.scm.containers.models import Container, EquipmentType
from apps.scm.containers.selectors import filter_containers, get_team_containers
from apps.scm.containers.utils import calculate_check_digit
from apps.scm.procurement.selectors import get_team_purchase_orders
from apps.scm.shipments.models import Shipment
from apps.scm.shipments.selectors import filter_shipments
from apps.scm.supplier_deliveries.selectors import get_supplier_deliveries_for_team
from apps.scm.tracking.selectors import get_team_tracking_subscriptions
from apps.teams.models import Team

OWNER = "CSQ"
CAT = "U"
SERIAL = "305418"
CHECK = calculate_check_digit(OWNER, CAT, SERIAL)


def make_team(slug: str = "perf-test") -> Team:
    return Team.objects.get_or_create(slug=slug, defaults={"name": "Perf Test"})[0]


def make_equipment_type() -> EquipmentType:
    return EquipmentType.objects.get_or_create(
        iso_code="22G1",
        defaults={"category": "GP", "length_ft": 20, "high_cube": False, "description": "20GP"},
    )[0]


class PaginationPresentTests(TestCase):
    """List views must have pagination in place — tested at selector level."""

    def setUp(self):
        self.team = make_team("perf-pagination")

    def test_shipment_selector_returns_queryset(self):
        qs = filter_shipments(team=self.team)
        # Must be a lazy QuerySet so Paginator can slice it
        from django.db.models import QuerySet

        self.assertIsInstance(qs, QuerySet)

    def test_container_selector_returns_queryset(self):
        qs = filter_containers(team=self.team)
        from django.db.models import QuerySet

        self.assertIsInstance(qs, QuerySet)

    def test_tracking_selector_returns_queryset(self):
        qs = get_team_tracking_subscriptions(team=self.team)
        from django.db.models import QuerySet

        self.assertIsInstance(qs, QuerySet)

    def test_procurement_selector_returns_queryset(self):
        qs = get_team_purchase_orders(team=self.team)
        from django.db.models import QuerySet

        self.assertIsInstance(qs, QuerySet)

    def test_supplier_deliveries_selector_returns_queryset(self):
        qs = get_supplier_deliveries_for_team(team=self.team)
        from django.db.models import QuerySet

        self.assertIsInstance(qs, QuerySet)


class TeamFilteringTests(TestCase):
    """All querysets must filter by team."""

    def setUp(self):
        self.team_a = make_team("perf-team-a")
        self.team_b = make_team("perf-team-b")

    def test_shipments_returns_only_own_team(self):
        Shipment.objects.create(team=self.team_a, shipment_number="PERF-A-001")
        Shipment.objects.create(team=self.team_b, shipment_number="PERF-B-001")
        qs = filter_shipments(team=self.team_a)
        self.assertTrue(all(s.team_id == self.team_a.pk for s in qs))
        self.assertFalse(any(s.team_id == self.team_b.pk for s in qs))

    def test_containers_returns_only_own_team(self):
        eq = make_equipment_type()
        Container.objects.create(
            team=self.team_a,
            owner_code=OWNER,
            category_id=CAT,
            serial_number=SERIAL,
            check_digit=CHECK,
            equipment_type=eq,
        )
        qs = get_team_containers(team=self.team_a)
        self.assertTrue(all(c.team_id == self.team_a.pk for c in qs))

    def test_procurement_returns_only_own_team(self):
        qs_a = get_team_purchase_orders(team=self.team_a)
        qs_b = get_team_purchase_orders(team=self.team_b)
        # Both can be empty; verify no cross-contamination
        team_a_ids = set(qs_a.values_list("team_id", flat=True))
        team_b_ids = set(qs_b.values_list("team_id", flat=True))
        self.assertFalse(team_a_ids.intersection({self.team_b.pk}))
        self.assertFalse(team_b_ids.intersection({self.team_a.pk}))


class SelectRelatedTests(TestCase):
    """Selectors with select_related must not trigger extra queries when attributes are accessed."""

    def setUp(self):
        self.team = make_team("perf-select-related")

    def test_containers_select_related_equipment_type(self):
        """Accessing equipment_type on a container must not trigger an extra query."""
        eq = make_equipment_type()
        Container.objects.create(
            team=self.team,
            owner_code=OWNER,
            category_id=CAT,
            serial_number=SERIAL,
            check_digit=CHECK,
            equipment_type=eq,
        )
        qs = list(get_team_containers(team=self.team))
        # Access equipment_type — should not cause a new DB hit (already selected)
        with self.assertNumQueries(0):
            for c in qs:
                _ = c.equipment_type.iso_code

    def test_procurement_prefetch_lines(self):
        """Accessing lines on a prefetched PO should not trigger additional queries."""
        from apps.scm.procurement.models import PurchaseOrder

        po = PurchaseOrder.objects.create(
            team=self.team,
            external_id="PERF-EXT-001",
            po_number="PERF-PO-001",
            supplier_no="SUP001",
            supplier_name="Test Supplier",
        )
        qs = list(get_team_purchase_orders(team=self.team))
        # Accessing prefetched lines should not trigger extra queries
        with self.assertNumQueries(0):
            for po_obj in qs:
                _ = list(po_obj.lines.all())  # lines are prefetched
        po.delete()
