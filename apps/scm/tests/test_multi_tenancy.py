"""Multi-tenancy tests for the SCM system.

Verifies that data never leaks between Pegasus teams across four levels:
  1. Queryset / selector level
  2. List views
  3. Detail pages
  4. HTMX endpoints
  5. Destructive actions (POST/DELETE)
  6. Dashboard counts
  7. Superuser / admin behaviour

Team slugs use the prefix "mt-" to avoid collisions with other test modules.
"""

from django.test import Client, TestCase
from django.urls import reverse

from apps.scm.containers.models import Container, EquipmentType
from apps.scm.containers.selectors import get_team_containers
from apps.scm.containers.utils import calculate_check_digit
from apps.scm.procurement.models import PurchaseOrder
from apps.scm.procurement.selectors import get_team_purchase_orders
from apps.scm.shipments.models import Shipment
from apps.scm.shipments.selectors import get_team_shipments
from apps.scm.supplier_deliveries.models import SupplierDelivery, SupplierDeliveryStatus
from apps.scm.supplier_deliveries.selectors import (
    get_supplier_deliveries_for_team,
    get_supplier_delivery_dashboard,
)
from apps.scm.tracking.models import TrackingEvent, TrackingProvider, TrackingSubscription
from apps.scm.tracking.selectors import (
    get_team_tracking_subscriptions,
    get_tracking_events_for_team,
)
from apps.teams.models import Membership, Team
from apps.users.models import CustomUser

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


def _user(email: str, *, is_superuser: bool = False) -> CustomUser:
    user = CustomUser.objects.get_or_create(
        username=email,
        defaults={"email": email, "is_superuser": is_superuser, "is_staff": is_superuser},
    )[0]
    user.set_password("testpass")
    user.save()
    return user


def _member(team: Team, user: CustomUser, role: str = "member") -> None:
    Membership.objects.get_or_create(team=team, user=user, defaults={"role": role})


def _container(team: Team, owner: str, serial: str) -> Container:
    check = calculate_check_digit(owner, "U", serial)
    return Container.objects.create(
        team=team,
        owner_code=owner,
        category_id="U",
        serial_number=serial,
        check_digit=check,
        equipment_type=_equipment_type(),
    )


def _shipment(team: Team, number: str) -> Shipment:
    return Shipment.objects.create(team=team, shipment_number=number)


def _purchase_order(team: Team, po_number: str, ext_id: str) -> PurchaseOrder:
    return PurchaseOrder.objects.create(
        team=team,
        external_id=ext_id,
        po_number=po_number,
        supplier_no="SUP001",
        supplier_name="Test Supplier",
    )


def _supplier_delivery(team: Team, po: PurchaseOrder, ref: str) -> SupplierDelivery:
    return SupplierDelivery.objects.create(team=team, purchase_order=po, delivery_reference=ref)


def _tracking_provider() -> TrackingProvider:
    return TrackingProvider.objects.get_or_create(
        code="mt-test-provider",
        defaults={"name": "MT Test Provider"},
    )[0]


def _tracking_subscription(team: Team, ref: str) -> TrackingSubscription:
    return TrackingSubscription.objects.create(
        team=team,
        provider=_tracking_provider(),
        tracking_reference=ref,
        reference_type=TrackingSubscription.ReferenceType.CONTAINER_NUMBER,
    )


def _tracking_event(team: Team, ref: str) -> TrackingEvent:
    return TrackingEvent.objects.create(
        team=team,
        provider=_tracking_provider(),
        event_type=TrackingEvent.EventType.GATE_IN if hasattr(TrackingEvent, "EventType") else "GATE_IN",
        source_event_id=ref,
    )


def _client_for(user: CustomUser, team: Team) -> Client:
    """Return a Django test client with user logged in and team set as default."""
    c = Client()
    c.force_login(user)
    session = c.session
    session["team"] = team.pk  # key expected by get_default_team_from_request
    session.save()
    return c


# ---------------------------------------------------------------------------
# 1. Selector / queryset level
# ---------------------------------------------------------------------------


class ContainerSelectorIsolationTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        _equipment_type()
        cls.team_a = _team("mt-sel-c-a")
        cls.team_b = _team("mt-sel-c-b")
        cls.container_a = _container(cls.team_a, "CSQ", "100001")
        cls.container_b = _container(cls.team_b, "CSQ", "100002")

    def test_selector_returns_only_team_a_containers(self):
        qs = get_team_containers(self.team_a)
        self.assertIn(self.container_a, qs)
        self.assertNotIn(self.container_b, qs)

    def test_selector_returns_only_team_b_containers(self):
        qs = get_team_containers(self.team_b)
        self.assertIn(self.container_b, qs)
        self.assertNotIn(self.container_a, qs)


class ShipmentSelectorIsolationTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team_a = _team("mt-sel-s-a")
        cls.team_b = _team("mt-sel-s-b")
        cls.shipment_a = _shipment(cls.team_a, "TEAM-A-SHIPMENT")
        cls.shipment_b = _shipment(cls.team_b, "TEAM-B-SHIPMENT")

    def test_selector_excludes_other_team_shipments(self):
        qs_a = get_team_shipments(self.team_a)
        self.assertIn(self.shipment_a, qs_a)
        self.assertNotIn(self.shipment_b, qs_a)


class PurchaseOrderSelectorIsolationTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team_a = _team("mt-sel-po-a")
        cls.team_b = _team("mt-sel-po-b")
        cls.po_a = _purchase_order(cls.team_a, "TEAM-A-PO", "EXT-A-001")
        cls.po_b = _purchase_order(cls.team_b, "TEAM-B-PO", "EXT-B-001")

    def test_selector_excludes_other_team_purchase_orders(self):
        qs_a = get_team_purchase_orders(self.team_a)
        po_numbers = list(qs_a.values_list("po_number", flat=True))
        self.assertIn("TEAM-A-PO", po_numbers)
        self.assertNotIn("TEAM-B-PO", po_numbers)


class SupplierDeliverySelectorIsolationTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team_a = _team("mt-sel-sd-a")
        cls.team_b = _team("mt-sel-sd-b")
        po_a = _purchase_order(cls.team_a, "PO-SD-A", "EXT-SD-A")
        po_b = _purchase_order(cls.team_b, "PO-SD-B", "EXT-SD-B")
        cls.delivery_a = _supplier_delivery(cls.team_a, po_a, "TEAM-A-DEL")
        cls.delivery_b = _supplier_delivery(cls.team_b, po_b, "TEAM-B-DEL")

    def test_selector_excludes_other_team_deliveries(self):
        qs_a = get_supplier_deliveries_for_team(self.team_a)
        refs = list(qs_a.values_list("delivery_reference", flat=True))
        self.assertIn("TEAM-A-DEL", refs)
        self.assertNotIn("TEAM-B-DEL", refs)


class TrackingSubscriptionSelectorIsolationTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team_a = _team("mt-sel-ts-a")
        cls.team_b = _team("mt-sel-ts-b")
        cls.sub_a = _tracking_subscription(cls.team_a, "TEAM-A-CONTAINER")
        cls.sub_b = _tracking_subscription(cls.team_b, "TEAM-B-CONTAINER")

    def test_selector_excludes_other_team_subscriptions(self):
        qs_a = get_team_tracking_subscriptions(self.team_a)
        self.assertIn(self.sub_a, qs_a)
        self.assertNotIn(self.sub_b, qs_a)


class TrackingEventSelectorIsolationTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team_a = _team("mt-sel-te-a")
        cls.team_b = _team("mt-sel-te-b")
        cls.event_a = _tracking_event(cls.team_a, "EVT-TEAM-A")
        cls.event_b = _tracking_event(cls.team_b, "EVT-TEAM-B")

    def test_selector_excludes_other_team_events(self):
        qs_a = get_tracking_events_for_team(self.team_a)
        self.assertIn(self.event_a, qs_a)
        self.assertNotIn(self.event_b, qs_a)


# ---------------------------------------------------------------------------
# 2. List views
# ---------------------------------------------------------------------------


class ListViewIsolationTest(TestCase):
    team_a: Team
    user_a: CustomUser

    @classmethod
    def setUpTestData(cls):
        _equipment_type()
        cls.team_a = _team("mt-lv-a", "MT List Team A")
        cls.team_b = _team("mt-lv-b", "MT List Team B")
        cls.user_a = _user("mt-lv-a@example.com")
        cls.user_b = _user("mt-lv-b@example.com")
        _member(cls.team_a, cls.user_a)
        _member(cls.team_b, cls.user_b)

        # Containers
        cls.container_a = _container(cls.team_a, "AAA", "100001")
        cls.container_b = _container(cls.team_b, "BBB", "200001")

        # Shipments
        cls.shipment_a = _shipment(cls.team_a, "TEAM-A-SHIPMENT")
        cls.shipment_b = _shipment(cls.team_b, "TEAM-B-SHIPMENT")

        # Purchase orders
        cls.po_a = _purchase_order(cls.team_a, "TEAM-A-PO", "EXT-LV-A")
        cls.po_b = _purchase_order(cls.team_b, "TEAM-B-PO", "EXT-LV-B")

        # Supplier deliveries
        cls.delivery_a = _supplier_delivery(cls.team_a, cls.po_a, "TEAM-A-DEL")
        cls.delivery_b = _supplier_delivery(cls.team_b, cls.po_b, "TEAM-B-DEL")

        # Tracking subscriptions
        cls.sub_a = _tracking_subscription(cls.team_a, "TRACK-A")
        cls.sub_b = _tracking_subscription(cls.team_b, "TRACK-B")

    def _client_a(self) -> Client:
        return _client_for(self.user_a, self.team_a)

    def test_container_list_shows_only_team_a_data(self):
        c = self._client_a()
        resp = c.get(reverse("containers:list"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "AAA")
        self.assertNotContains(resp, "BBB")

    def test_shipment_list_shows_only_team_a_data(self):
        c = self._client_a()
        resp = c.get(reverse("shipments:list"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "TEAM-A-SHIPMENT")
        self.assertNotContains(resp, "TEAM-B-SHIPMENT")

    def test_purchase_order_list_shows_only_team_a_data(self):
        c = self._client_a()
        resp = c.get(reverse("procurement:purchase_order_list"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "TEAM-A-PO")
        self.assertNotContains(resp, "TEAM-B-PO")

    def test_supplier_delivery_list_shows_only_team_a_data(self):
        c = self._client_a()
        resp = c.get(reverse("supplier_deliveries:list"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "TEAM-A-DEL")
        self.assertNotContains(resp, "TEAM-B-DEL")

    def test_tracking_list_shows_only_team_a_data(self):
        c = self._client_a()
        resp = c.get(reverse("tracking:list"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "TRACK-A")
        self.assertNotContains(resp, "TRACK-B")


# ---------------------------------------------------------------------------
# 3. Detail pages
# ---------------------------------------------------------------------------


class DetailPageIsolationTest(TestCase):
    team_a: Team
    user_a: CustomUser

    @classmethod
    def setUpTestData(cls):
        _equipment_type()
        cls.team_a = _team("mt-dp-a", "MT Detail Team A")
        cls.team_b = _team("mt-dp-b", "MT Detail Team B")
        cls.user_a = _user("mt-dp-a@example.com")
        _member(cls.team_a, cls.user_a)

        cls.container_b = _container(cls.team_b, "CCC", "300001")
        cls.shipment_b = _shipment(cls.team_b, "TEAM-B-DETAIL-SHIP")
        cls.po_b = _purchase_order(cls.team_b, "TEAM-B-DETAIL-PO", "EXT-DP-B")
        cls.delivery_b = _supplier_delivery(cls.team_b, cls.po_b, "TEAM-B-DETAIL-DEL")
        cls.sub_b = _tracking_subscription(cls.team_b, "TRACK-DP-B")

    def _client_a(self) -> Client:
        return _client_for(self.user_a, self.team_a)

    def test_cross_team_container_detail_returns_404(self):
        resp = self._client_a().get(reverse("containers:detail", kwargs={"container_id": self.container_b.pk}))
        self.assertEqual(resp.status_code, 404)

    def test_cross_team_shipment_detail_returns_404(self):
        resp = self._client_a().get(reverse("shipments:detail", kwargs={"pk": self.shipment_b.pk}))
        self.assertEqual(resp.status_code, 404)

    def test_cross_team_purchase_order_detail_returns_404(self):
        resp = self._client_a().get(
            reverse("procurement:purchase_order_detail", kwargs={"purchase_order_id": self.po_b.pk})
        )
        self.assertEqual(resp.status_code, 404)

    def test_cross_team_supplier_delivery_detail_returns_404(self):
        resp = self._client_a().get(reverse("supplier_deliveries:detail", kwargs={"delivery_id": self.delivery_b.pk}))
        self.assertEqual(resp.status_code, 404)

    def test_cross_team_tracking_detail_returns_404(self):
        resp = self._client_a().get(reverse("tracking:detail", kwargs={"pk": self.sub_b.pk}))
        self.assertEqual(resp.status_code, 404)


# ---------------------------------------------------------------------------
# 4. HTMX endpoints
# ---------------------------------------------------------------------------


class HtmxEndpointIsolationTest(TestCase):
    team_a: Team
    user_a: CustomUser

    @classmethod
    def setUpTestData(cls):
        _equipment_type()
        cls.team_a = _team("mt-htmx-a", "MT HTMX Team A")
        cls.team_b = _team("mt-htmx-b", "MT HTMX Team B")
        cls.user_a = _user("mt-htmx-a@example.com")
        _member(cls.team_a, cls.user_a)

        # Container list HTMX
        _container(cls.team_a, "DDD", "400001")
        _container(cls.team_b, "EEE", "500001")

        # Shipment list HTMX + timeline
        cls.shipment_a = _shipment(cls.team_a, "HTMX-A-SHIP")
        _shipment(cls.team_b, "HTMX-B-SHIP")

        # Tracking list HTMX + timeline
        cls.sub_a = _tracking_subscription(cls.team_a, "HTMX-TRACK-A")
        _tracking_subscription(cls.team_b, "HTMX-TRACK-B")

    def _client_a(self) -> Client:
        return _client_for(self.user_a, self.team_a)

    def test_htmx_container_list_partial_excludes_other_team(self):
        resp = self._client_a().get(reverse("containers:list"), HTTP_HX_REQUEST="true")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "DDD")
        self.assertNotContains(resp, "EEE")

    def test_htmx_shipment_list_partial_excludes_other_team(self):
        resp = self._client_a().get(reverse("shipments:list"), HTTP_HX_REQUEST="true")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "HTMX-A-SHIP")
        self.assertNotContains(resp, "HTMX-B-SHIP")

    def test_htmx_tracking_list_partial_excludes_other_team(self):
        resp = self._client_a().get(reverse("tracking:list"), HTTP_HX_REQUEST="true")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "HTMX-TRACK-A")
        self.assertNotContains(resp, "HTMX-TRACK-B")

    def test_htmx_shipment_timeline_cross_team_returns_404(self):
        """Team A cannot fetch timeline partial for Team B's shipment."""
        team_b_ship = _shipment(self.team_b, "HTMX-B-TIMELINE-SHIP")
        resp = self._client_a().get(
            reverse("shipments:timeline", kwargs={"pk": team_b_ship.pk}),
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(resp.status_code, 404)

    def test_htmx_tracking_timeline_cross_team_returns_404(self):
        """Team A cannot fetch tracking timeline for Team B's subscription."""
        team_b_sub = _tracking_subscription(self.team_b, "HTMX-B-TIMELINE-TRACK")
        resp = self._client_a().get(
            reverse("tracking:timeline", kwargs={"pk": team_b_sub.pk}),
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(resp.status_code, 404)

    def test_htmx_supplier_delivery_mark_received_cross_team_returns_404(self):
        """Team A cannot mark Team B's delivery as received."""
        po_b = _purchase_order(self.team_b, "HTMX-B-PO", "EXT-HTMX-B")
        delivery_b = _supplier_delivery(self.team_b, po_b, "HTMX-B-DEL")
        resp = self._client_a().post(
            reverse("supplier_deliveries:mark_received", kwargs={"delivery_id": delivery_b.pk}),
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(resp.status_code, 404)


# ---------------------------------------------------------------------------
# 5. Destructive actions
# ---------------------------------------------------------------------------


class DestructiveActionIsolationTest(TestCase):
    team_a: Team
    user_a: CustomUser

    @classmethod
    def setUpTestData(cls):
        _equipment_type()
        cls.team_a = _team("mt-dest-a", "MT Dest Team A")
        cls.team_b = _team("mt-dest-b", "MT Dest Team B")
        cls.user_a = _user("mt-dest-a@example.com")
        _member(cls.team_a, cls.user_a)

    def _client_a(self) -> Client:
        return _client_for(self.user_a, self.team_a)

    def test_cannot_delete_other_team_container(self):
        container_b = _container(self.team_b, "FFF", "600001")
        resp = self._client_a().post(reverse("containers:delete", kwargs={"container_id": container_b.pk}))
        self.assertEqual(resp.status_code, 404)
        self.assertTrue(Container.objects.filter(pk=container_b.pk).exists())

    def test_cannot_cancel_other_team_shipment(self):
        shipment_b = _shipment(self.team_b, "DEST-B-SHIP")
        resp = self._client_a().post(reverse("shipments:cancel", kwargs={"pk": shipment_b.pk}))
        self.assertEqual(resp.status_code, 404)
        shipment_b.refresh_from_db()
        self.assertNotEqual(shipment_b.status, Shipment.Status.CANCELLED)

    def test_cannot_update_other_team_supplier_delivery(self):
        po_b = _purchase_order(self.team_b, "DEST-B-PO", "EXT-DEST-B")
        delivery_b = _supplier_delivery(self.team_b, po_b, "DEST-B-DEL")
        resp = self._client_a().post(
            reverse("supplier_deliveries:update", kwargs={"delivery_id": delivery_b.pk}),
            data={"delivery_reference": "HACKED"},
        )
        self.assertEqual(resp.status_code, 404)
        delivery_b.refresh_from_db()
        self.assertEqual(delivery_b.delivery_reference, "DEST-B-DEL")

    def test_cannot_cancel_other_team_tracking_subscription(self):
        sub_b = _tracking_subscription(self.team_b, "DEST-TRACK-B")
        resp = self._client_a().post(reverse("tracking:cancel", kwargs={"pk": sub_b.pk}))
        self.assertEqual(resp.status_code, 404)
        sub_b.refresh_from_db()
        self.assertNotEqual(sub_b.status, TrackingSubscription.Status.CANCELLED)


# ---------------------------------------------------------------------------
# 6. Dashboard count isolation
# ---------------------------------------------------------------------------


class DashboardCountIsolationTest(TestCase):
    """Dashboard counts must not include data from other teams."""

    @classmethod
    def setUpTestData(cls):
        cls.team_a = _team("mt-dash-a", "MT Dash Team A")
        cls.team_b = _team("mt-dash-b", "MT Dash Team B")
        _purchase_order(cls.team_a, "DASH-A-PO", "EXT-DASH-A")
        po_b = _purchase_order(cls.team_b, "DASH-B-PO", "EXT-DASH-B")
        # Team B gets 3 IN_TRANSIT deliveries; Team A gets 0
        for i in range(3):
            d = _supplier_delivery(cls.team_b, po_b, f"DASH-B-DEL-{i}")
            d.status = SupplierDeliveryStatus.IN_TRANSIT
            d.save()

    def test_team_a_dashboard_in_transit_count_is_zero(self):
        stats = get_supplier_delivery_dashboard(self.team_a)
        self.assertEqual(stats["in_transit_count"], 0)

    def test_team_b_dashboard_in_transit_count_is_three(self):
        stats = get_supplier_delivery_dashboard(self.team_b)
        self.assertEqual(stats["in_transit_count"], 3)

    def test_live_stats_shipments_not_leaked(self):
        from apps.scm.analytics.selectors import get_live_dashboard_stats

        # Give team B 2 active shipments
        for i in range(2):
            s = _shipment(self.team_b, f"DASH-ACTIVE-{i}")
            s.status = Shipment.Status.IN_TRANSIT
            s.save()

        stats_a = get_live_dashboard_stats(self.team_a)
        self.assertEqual(stats_a["active_shipments"], 0, "Team A must not see Team B's active shipments")


# ---------------------------------------------------------------------------
# 7. Superuser / admin behaviour
# ---------------------------------------------------------------------------


class SuperuserBehaviorTest(TestCase):
    """Superusers follow the same team-scoped rules in SCM views.

    The SCM decorator @scm_login_required uses request.default_team which
    depends on team membership, not superuser status. A superuser who is
    a member of team_a will see team_a data; they cannot freely access
    team_b objects via URL because views use get_object_or_404(team=team).
    """

    team_a: Team
    superuser: CustomUser

    @classmethod
    def setUpTestData(cls):
        _equipment_type()
        cls.team_a = _team("mt-su-a", "MT SU Team A")
        cls.team_b = _team("mt-su-b", "MT SU Team B")
        cls.superuser = _user("mt-su@example.com", is_superuser=True)
        # Superuser is a member of team_a only
        _member(cls.team_a, cls.superuser, role="admin")

        cls.container_b = _container(cls.team_b, "GGG", "700001")
        cls.shipment_b = _shipment(cls.team_b, "SU-B-SHIP")

    def _su_client(self) -> Client:
        return _client_for(self.superuser, self.team_a)

    def test_superuser_can_access_own_team_container_list(self):
        resp = self._su_client().get(reverse("containers:list"))
        self.assertEqual(resp.status_code, 200)

    def test_superuser_cannot_access_other_team_container_detail(self):
        """Even superusers are blocked from cross-team object access in SCM views."""
        resp = self._su_client().get(reverse("containers:detail", kwargs={"container_id": self.container_b.pk}))
        self.assertEqual(resp.status_code, 404)

    def test_superuser_cannot_access_other_team_shipment_detail(self):
        resp = self._su_client().get(reverse("shipments:detail", kwargs={"pk": self.shipment_b.pk}))
        self.assertEqual(resp.status_code, 404)

    def test_superuser_without_team_gets_404_on_scm_views(self):
        """A superuser who belongs to no team cannot access SCM views."""
        su_no_team = _user("mt-su-noteam@example.com", is_superuser=True)
        c = Client()
        c.force_login(su_no_team)
        resp = c.get(reverse("containers:list"))
        self.assertEqual(resp.status_code, 404)
