"""Dashboard metrics tests — verifies exact counts and team isolation.

Tests cover:
  1. Deterministic test data setup (Team A and Team B)
  2. Exact counts via selectors
  3. Exact counts via live dashboard stats
  4. Team isolation at service/queryset level
  5. Team isolation at view/context level
  6. Zero state (no data → counts are 0, not blank/crash)
"""

import datetime

from django.test import Client, TestCase, override_settings
from django.urls import reverse

from apps.scm.analytics.selectors import get_live_dashboard_stats
from apps.scm.analytics.services import (
    get_active_shipments,
    get_container_analytics,
    get_containers_in_transit,
    get_supplier_analytics,
    get_total_shipments,
)
from apps.scm.containers.choices import ContainerStatus
from apps.scm.containers.models import Container, EquipmentType, PlannedContainer, PlannedContainerStatus
from apps.scm.containers.utils import calculate_check_digit
from apps.scm.procurement.models import PurchaseOrder, PurchaseOrderStatus
from apps.scm.shipments.models import Shipment
from apps.scm.supplier_deliveries.models import SupplierDelivery, SupplierDeliveryStatus
from apps.scm.supplier_deliveries.selectors import get_supplier_delivery_dashboard
from apps.teams.models import Team
from apps.teams.roles import ROLE_MEMBER
from apps.users.models import CustomUser

_TEST_STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_EQ_ISO = "22G1"


def _equipment_type() -> EquipmentType:
    return EquipmentType.objects.get_or_create(
        iso_code=_EQ_ISO,
        defaults={"category": "GP", "length_ft": 20, "high_cube": False, "description": "20GP"},
    )[0]


def _team(slug: str, name: str | None = None) -> Team:
    return Team.objects.get_or_create(slug=slug, defaults={"name": name or slug})[0]


def _user(email: str) -> CustomUser:
    user = CustomUser.objects.get_or_create(
        username=email,
        defaults={"email": email},
    )[0]
    user.set_password("testpass")
    user.save()
    return user


def _member(team: Team, user: CustomUser) -> None:
    from apps.teams.models import Membership

    Membership.objects.get_or_create(team=team, user=user, defaults={"role": ROLE_MEMBER})


def _purchase_order(
    team: Team,
    po_number: str,
    ext_id: str,
    status: str = PurchaseOrderStatus.OPEN,
    supplier_no: str = "SUP-METRICS",
) -> PurchaseOrder:
    return PurchaseOrder.objects.create(
        team=team,
        external_id=ext_id,
        po_number=po_number,
        supplier_no=supplier_no,
        supplier_name="Metrics Supplier",
        status=status,
    )


def _supplier_delivery(team: Team, po: PurchaseOrder, ref: str, status: str = SupplierDeliveryStatus.PLANNED):
    return SupplierDelivery.objects.create(team=team, purchase_order=po, delivery_reference=ref, status=status)


def _shipment(team: Team, number: str, status: str = Shipment.Status.DRAFT) -> Shipment:
    s = Shipment.objects.create(team=team, shipment_number=number)
    if status != Shipment.Status.DRAFT:
        s.status = status
        s.save()
    return s


def _container(team: Team, owner: str, serial: str, status: str = ContainerStatus.AVAILABLE) -> Container:
    _equipment_type()
    check = calculate_check_digit(owner, "U", serial)
    c = Container.objects.create(
        team=team,
        owner_code=owner,
        category_id="U",
        serial_number=serial,
        check_digit=check,
        equipment_type=_equipment_type(),
        status=status,
    )
    return c


def _client_for(user: CustomUser, team: Team) -> Client:
    c = Client()
    c.force_login(user)
    session = c.session
    session["team"] = team.pk
    session.save()
    return c


# ---------------------------------------------------------------------------
# 9.1 Deterministic test data
# ---------------------------------------------------------------------------


class DashboardMetricsTeamDataSetup(TestCase):
    """Base class that creates Team A and Team B with deterministic data.

    Team A data:
        - 2 open POs
        - 1 released PO
        - 1 completed (fully_received) PO
        - 4 open supplier deliveries (planned/booked)
        - 2 in-transit supplier deliveries
        - 1 received supplier delivery
        - 3 booked/in-transit shipments
        - 2 delayed shipments (IN_TRANSIT with past ETA)
        - 5 planned containers
        - 2 in-transit containers
        - 2 detected planned containers

    Team B data (should never affect Team A counts):
        - 3 open POs
        - 5 in-transit supplier deliveries
        - 4 in-transit shipments
        - 3 in-transit containers
    """

    @classmethod
    def setUpTestData(cls):
        _equipment_type()

        cls.team_a = _team("dm-team-a", "Dashboard Metrics Team A")
        cls.team_b = _team("dm-team-b", "Dashboard Metrics Team B")
        cls.user_a = _user("dm-user-a@example.com")
        _member(cls.team_a, cls.user_a)

        # ---- Team A POs ----
        cls.po_a_open_1 = _purchase_order(cls.team_a, "DM-A-PO-OPEN-1", "dm-a-ext-open-1", PurchaseOrderStatus.OPEN)
        cls.po_a_open_2 = _purchase_order(cls.team_a, "DM-A-PO-OPEN-2", "dm-a-ext-open-2", PurchaseOrderStatus.OPEN)
        cls.po_a_released = _purchase_order(
            cls.team_a, "DM-A-PO-RELEASED", "dm-a-ext-released", PurchaseOrderStatus.RELEASED
        )
        cls.po_a_completed = _purchase_order(
            cls.team_a, "DM-A-PO-DONE", "dm-a-ext-done", PurchaseOrderStatus.FULLY_RECEIVED
        )

        # ---- Team A Supplier Deliveries ----
        cls.sd_a_planned_1 = _supplier_delivery(
            cls.team_a, cls.po_a_open_1, "DM-A-DEL-P1", SupplierDeliveryStatus.PLANNED
        )
        cls.sd_a_planned_2 = _supplier_delivery(
            cls.team_a, cls.po_a_open_1, "DM-A-DEL-P2", SupplierDeliveryStatus.PLANNED
        )
        cls.sd_a_booked_1 = _supplier_delivery(
            cls.team_a, cls.po_a_open_2, "DM-A-DEL-B1", SupplierDeliveryStatus.BOOKED
        )
        cls.sd_a_booked_2 = _supplier_delivery(
            cls.team_a, cls.po_a_open_2, "DM-A-DEL-B2", SupplierDeliveryStatus.BOOKED
        )
        cls.sd_a_transit_1 = _supplier_delivery(
            cls.team_a, cls.po_a_open_1, "DM-A-DEL-T1", SupplierDeliveryStatus.IN_TRANSIT
        )
        cls.sd_a_transit_2 = _supplier_delivery(
            cls.team_a, cls.po_a_open_2, "DM-A-DEL-T2", SupplierDeliveryStatus.IN_TRANSIT
        )
        cls.sd_a_received = _supplier_delivery(
            cls.team_a, cls.po_a_completed, "DM-A-DEL-REC", SupplierDeliveryStatus.RECEIVED
        )

        # ---- Team A Shipments ----
        cls.ship_a_booked = _shipment(cls.team_a, "DM-A-SHP-BOOKED", Shipment.Status.BOOKED)
        cls.ship_a_transit = _shipment(cls.team_a, "DM-A-SHP-TRANSIT", Shipment.Status.IN_TRANSIT)
        cls.ship_a_arrived = _shipment(cls.team_a, "DM-A-SHP-ARRIVED", Shipment.Status.ARRIVED)
        # Delayed: IN_TRANSIT with past ETA
        yesterday = datetime.date.today() - datetime.timedelta(days=1)
        cls.ship_a_delayed_1 = _shipment(cls.team_a, "DM-A-SHP-DELAY1", Shipment.Status.IN_TRANSIT)
        cls.ship_a_delayed_1.eta = yesterday
        cls.ship_a_delayed_1.save()
        cls.ship_a_delayed_2 = _shipment(cls.team_a, "DM-A-SHP-DELAY2", Shipment.Status.BOOKED)
        cls.ship_a_delayed_2.eta = yesterday
        cls.ship_a_delayed_2.save()

        # ---- Team A Containers ----
        cls.ctr_a_available = _container(cls.team_a, "DMA", "100001", ContainerStatus.AVAILABLE)
        cls.ctr_a_transit_1 = _container(cls.team_a, "DMA", "200001", ContainerStatus.IN_TRANSIT)
        cls.ctr_a_transit_2 = _container(cls.team_a, "DMA", "300001", ContainerStatus.IN_TRANSIT)

        # Planned containers (ISO 6346 format: 4 letters + 7 digits = 11 chars)
        for i in range(5):
            PlannedContainer.objects.create(
                team=cls.team_a,
                container_number=f"DMAU{100000 + i:07d}"[:11],
                status=PlannedContainerStatus.PLANNED,
            )
        for i in range(2):
            PlannedContainer.objects.create(
                team=cls.team_a,
                container_number=f"DMAU{200000 + i:07d}"[:11],
                status=PlannedContainerStatus.DETECTED,
            )

        # ---- Team B data (should not affect Team A counts) ----
        cls.po_b_1 = _purchase_order(cls.team_b, "DM-B-PO-1", "dm-b-ext-1", PurchaseOrderStatus.OPEN, "SUP-B")
        cls.po_b_2 = _purchase_order(cls.team_b, "DM-B-PO-2", "dm-b-ext-2", PurchaseOrderStatus.OPEN, "SUP-B")
        cls.po_b_3 = _purchase_order(cls.team_b, "DM-B-PO-3", "dm-b-ext-3", PurchaseOrderStatus.OPEN, "SUP-B")
        for i in range(5):
            _supplier_delivery(cls.team_b, cls.po_b_1, f"DM-B-DEL-T{i}", SupplierDeliveryStatus.IN_TRANSIT)
        for i in range(4):
            _shipment(cls.team_b, f"DM-B-SHP-TRANSIT-{i}", Shipment.Status.IN_TRANSIT)
        for i in range(3):
            _container(cls.team_b, "DMB", f"{i + 1}00001", ContainerStatus.IN_TRANSIT)


# ---------------------------------------------------------------------------
# 9.2 Exact counts — service level
# ---------------------------------------------------------------------------


class AnalyticsDashboardExactCountsTest(DashboardMetricsTeamDataSetup):
    """Verify service-level functions return exact counts for Team A."""

    def test_open_purchase_orders_count(self):
        # 2 OPEN + 1 RELEASED = 3 "open" POs (live stats uses OPEN and RELEASED)
        stats = get_live_dashboard_stats(self.team_a)
        self.assertEqual(stats["open_purchase_orders"], 3)

    def test_active_shipments_count(self):
        # get_active_shipments includes BOOKED + IN_TRANSIT + ARRIVED
        count = get_active_shipments(self.team_a)
        self.assertEqual(count, 5)  # booked(2) + in_transit(2) + arrived(1)

    def test_total_shipments_count(self):
        count = get_total_shipments(self.team_a)
        self.assertEqual(count, 5)

    def test_containers_in_transit_count(self):
        count = get_containers_in_transit(self.team_a)
        self.assertEqual(count, 2)

    def test_delayed_shipments_count(self):
        stats = get_live_dashboard_stats(self.team_a)
        # active + past ETA = both delayed_1 (IN_TRANSIT, past ETA) and delayed_2 (BOOKED, past ETA)
        self.assertEqual(stats["delayed_shipments"], 2)

    def test_supplier_delivery_open_count(self):
        dashboard = get_supplier_delivery_dashboard(self.team_a)
        # open = PLANNED(2) + BOOKED(2) = 4
        self.assertEqual(dashboard["open_count"], 4)

    def test_supplier_delivery_in_transit_count(self):
        dashboard = get_supplier_delivery_dashboard(self.team_a)
        self.assertEqual(dashboard["in_transit_count"], 2)

    def test_supplier_delivery_completed_count(self):
        dashboard = get_supplier_delivery_dashboard(self.team_a)
        self.assertEqual(dashboard["completed_count"], 1)

    def test_planned_containers_count(self):
        stats = get_container_analytics(self.team_a)
        self.assertEqual(stats["planned_count"], 7)  # 5 planned + 2 detected

    def test_container_in_transit_analytics(self):
        stats = get_container_analytics(self.team_a)
        self.assertEqual(stats["in_transit"], 2)

    def test_container_total(self):
        stats = get_container_analytics(self.team_a)
        # 1 available + 2 in_transit = 3
        self.assertEqual(stats["total"], 3)


# ---------------------------------------------------------------------------
# 9.3 Team isolation — counts scoped to current team
# ---------------------------------------------------------------------------


class AnalyticsDashboardTeamIsolationTest(DashboardMetricsTeamDataSetup):
    """Verify Team A counts are not contaminated by Team B data."""

    def test_live_stats_open_pos_not_leaked(self):
        stats = get_live_dashboard_stats(self.team_a)
        # Team B has 3 more open POs — must not show up in Team A
        self.assertEqual(stats["open_purchase_orders"], 3)

    def test_live_stats_active_shipments_not_leaked(self):
        stats = get_live_dashboard_stats(self.team_a)
        # Team B has 4 IN_TRANSIT shipments — must not be counted for Team A.
        # live_stats counts BOOKED + IN_TRANSIT (not ARRIVED), so Team A = 4
        self.assertEqual(stats["active_shipments"], 4)

    def test_live_stats_delayed_shipments_not_leaked(self):
        stats = get_live_dashboard_stats(self.team_a)
        # Team B has no delayed; Team A has 2
        self.assertEqual(stats["delayed_shipments"], 2)

    def test_supplier_delivery_in_transit_not_leaked(self):
        dashboard = get_supplier_delivery_dashboard(self.team_a)
        # Team B has 5 IN_TRANSIT deliveries — must not affect Team A's count (2)
        self.assertEqual(dashboard["in_transit_count"], 2)

    def test_container_analytics_not_leaked(self):
        stats = get_container_analytics(self.team_a)
        # Team B has 3 IN_TRANSIT containers; Team A has 2
        self.assertEqual(stats["in_transit"], 2)

    def test_team_b_does_not_see_team_a_stats(self):
        stats_b = get_live_dashboard_stats(self.team_b)
        # Team B has 3 open POs — Team A's POs should not be counted
        self.assertEqual(stats_b["open_purchase_orders"], 3)

    def test_supplier_analytics_scoped_to_team(self):
        # Team A supplier analytics should only count Team A POs
        supplier_data = get_supplier_analytics(self.team_a)
        # All Team A POs use "SUP-METRICS" supplier_no
        supplier_nos = [s["supplier_no"] for s in supplier_data]
        self.assertIn("SUP-METRICS", supplier_nos)
        # Team B uses "SUP-B" — should not appear in Team A analytics
        self.assertNotIn("SUP-B", supplier_nos)


# ---------------------------------------------------------------------------
# 9.4 View / context level — dashboard renders correct numbers
# ---------------------------------------------------------------------------


@override_settings(STORAGES=_TEST_STORAGES)
class AnalyticsDashboardRenderingTest(DashboardMetricsTeamDataSetup):
    """Verify the analytics dashboard view renders the correct counts in context."""

    def _client_a(self):
        return _client_for(self.user_a, self.team_a)

    def test_dashboard_view_has_correct_open_po_count(self):
        response = self._client_a().get(reverse("analytics:dashboard"))
        self.assertEqual(response.status_code, 200)
        live_stats = response.context["live_stats"]
        self.assertEqual(live_stats["open_purchase_orders"], 3)

    def test_dashboard_view_has_correct_active_shipments(self):
        response = self._client_a().get(reverse("analytics:dashboard"))
        live_stats = response.context["live_stats"]
        # live_stats uses BOOKED + IN_TRANSIT only (not ARRIVED), so = 4
        self.assertEqual(live_stats["active_shipments"], 4)

    def test_dashboard_view_has_correct_delayed_shipments(self):
        response = self._client_a().get(reverse("analytics:dashboard"))
        live_stats = response.context["live_stats"]
        self.assertEqual(live_stats["delayed_shipments"], 2)

    def test_dashboard_view_does_not_include_other_team_counts(self):
        # Create a Team B user and check their dashboard shows Team B counts only
        user_b = _user("dm-user-b@example.com")
        _member(self.team_b, user_b)
        client_b = _client_for(user_b, self.team_b)
        response = client_b.get(reverse("analytics:dashboard"))
        self.assertEqual(response.status_code, 200)
        live_stats = response.context["live_stats"]
        # Team B has 3 open POs and 4 active shipments (IN_TRANSIT)
        self.assertEqual(live_stats["open_purchase_orders"], 3)
        self.assertEqual(live_stats["active_shipments"], 4)

    def test_supplier_delivery_dashboard_renders_correct_open_count(self):
        response = self._client_a().get(reverse("supplier_deliveries:dashboard"))
        self.assertEqual(response.status_code, 200)
        dashboard = response.context["dashboard"]
        self.assertEqual(dashboard["open_count"], 4)

    def test_supplier_delivery_dashboard_renders_correct_in_transit_count(self):
        response = self._client_a().get(reverse("supplier_deliveries:dashboard"))
        dashboard = response.context["dashboard"]
        self.assertEqual(dashboard["in_transit_count"], 2)

    def test_container_discovery_dashboard_renders_planned_count(self):
        response = self._client_a().get(reverse("containers:discovery_dashboard"))
        self.assertEqual(response.status_code, 200)
        counts = response.context["counts"]
        self.assertEqual(counts["planned"], 5)
        self.assertEqual(counts["detected"], 2)


# ---------------------------------------------------------------------------
# 9.5 Zero state — no data → counts are 0, not blank/crash
# ---------------------------------------------------------------------------


@override_settings(STORAGES=_TEST_STORAGES)
class AnalyticsDashboardZeroCountsTest(TestCase):
    """Verify that an empty team's dashboard shows 0 everywhere and does not crash."""

    @classmethod
    def setUpTestData(cls):
        cls.team = _team("dm-zero-team", "Dashboard Zero Team")
        cls.user = _user("dm-zero@example.com")
        _member(cls.team, cls.user)

    def _client(self):
        return _client_for(self.user, self.team)

    def test_live_stats_all_zero_when_no_data(self):
        stats = get_live_dashboard_stats(self.team)
        self.assertEqual(stats["active_shipments"], 0)
        self.assertEqual(stats["delayed_shipments"], 0)
        self.assertEqual(stats["containers_in_transit"], 0)
        self.assertEqual(stats["containers_available"], 0)
        self.assertEqual(stats["open_purchase_orders"], 0)
        self.assertEqual(stats["partial_deliveries"], 0)
        self.assertEqual(stats["tracking_issues"], 0)

    def test_supplier_delivery_dashboard_all_zero(self):
        dashboard = get_supplier_delivery_dashboard(self.team)
        self.assertEqual(dashboard["open_count"], 0)
        self.assertEqual(dashboard["partial_count"], 0)
        self.assertEqual(dashboard["completed_count"], 0)
        self.assertEqual(dashboard["in_transit_count"], 0)
        self.assertEqual(dashboard["delayed_count"], 0)

    def test_container_analytics_all_zero(self):
        stats = get_container_analytics(self.team)
        self.assertEqual(stats["total"], 0)
        self.assertEqual(stats["in_transit"], 0)
        self.assertEqual(stats["available"], 0)
        self.assertEqual(stats["planned_count"], 0)

    def test_analytics_dashboard_view_zero_state(self):
        response = self._client().get(reverse("analytics:dashboard"))
        self.assertEqual(response.status_code, 200)
        live_stats = response.context["live_stats"]
        self.assertEqual(live_stats["active_shipments"], 0)
        self.assertEqual(live_stats["open_purchase_orders"], 0)

    def test_supplier_delivery_dashboard_view_zero_state(self):
        response = self._client().get(reverse("supplier_deliveries:dashboard"))
        self.assertEqual(response.status_code, 200)
        dashboard = response.context["dashboard"]
        self.assertEqual(dashboard["open_count"], 0)
        self.assertEqual(dashboard["in_transit_count"], 0)

    def test_container_discovery_dashboard_view_zero_state(self):
        response = self._client().get(reverse("containers:discovery_dashboard"))
        self.assertEqual(response.status_code, 200)
        counts = response.context["counts"]
        self.assertEqual(counts["planned"], 0)
        self.assertEqual(counts["detected"], 0)
        self.assertEqual(counts["in_transit"], 0)
        self.assertEqual(counts["arrived"], 0)
