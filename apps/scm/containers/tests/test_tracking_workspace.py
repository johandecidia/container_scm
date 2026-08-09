"""Tests for the container tracking workspace and position classification.

Two things matter most here: the workspace derives carrier, ETA, vessel and status
from the shipment and its events rather than duplicating them onto Container, and a
position always carries how it was obtained — a port coordinate is never presented
as a live GPS fix.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from apps.scm.containers.models import Container, EquipmentType
from apps.scm.containers.selectors import get_container_workspace
from apps.scm.procurement.models import PurchaseOrder, PurchaseOrderLine
from apps.scm.shipments.models import Shipment, ShipmentContainer
from apps.scm.supplier_deliveries.models import SupplierDelivery, SupplierDeliveryLine
from apps.scm.tracking.models import TrackingEvent, TrackingProvider, TrackingSubscription, TrackingSyncRun
from apps.scm.tracking.positions import PositionType, classify_position, get_latest_container_position
from apps.teams.models import Team

LOADED_AT = datetime(2024, 3, 10, 8, 0, tzinfo=UTC)
ARRIVED_AT = datetime(2024, 3, 25, 14, 0, tzinfo=UTC)


def _team(slug: str) -> Team:
    return Team.objects.get_or_create(slug=slug, defaults={"name": slug})[0]


class WorkspaceTestBase(TestCase):
    def setUp(self):
        self.team = _team(self.team_slug)
        self.provider = TrackingProvider.objects.get_or_create(code="maersk", defaults={"name": "Maersk"})[0]
        equipment_type = EquipmentType.objects.get_or_create(
            iso_code="22G1",
            defaults={"category": "GP", "length_ft": 20, "high_cube": False, "description": "20' GP"},
        )[0]
        self.container = Container.objects.create(
            team=self.team,
            owner_code="MRK",
            category_id="U",
            serial_number="123456",
            check_digit=3,
            equipment_type=equipment_type,
        )

    def _shipment(self, **kwargs) -> Shipment:
        defaults = {"shipment_number": f"SHP-{self.team_slug}", "carrier": "Maersk"}
        defaults.update(kwargs)
        shipment = Shipment.objects.create(team=self.team, **defaults)
        ShipmentContainer.objects.create(shipment=shipment, container=self.container)
        return shipment

    def _subscription(self, **kwargs) -> TrackingSubscription:
        defaults = {
            "tracking_reference": self.container.container_id,
            "container": self.container,
        }
        defaults.update(kwargs)
        return TrackingSubscription.objects.create(team=self.team, provider=self.provider, **defaults)

    def _event(self, event_type, when, **kwargs):
        defaults = {
            "team": self.team,
            "provider": self.provider,
            "container": self.container,
            "event_type": event_type,
            "event_time_type": TrackingEvent.EventTimeType.ACTUAL,
            "event_datetime": when,
            "event_fingerprint": f"{event_type}-{when.isoformat()}-{kwargs.get('location_unlocode', '')}",
        }
        defaults.update(kwargs)
        return TrackingEvent.objects.create(**defaults)


class WorkspaceDerivesFromRelationsTest(WorkspaceTestBase):
    """Carrier, ETA, vessel and status are read through relations, not duplicated."""

    team_slug = "workspace-derive"

    def test_carrier_comes_from_the_tracking_subscription(self):
        self._shipment()
        self._subscription()
        workspace = get_container_workspace(self.team, self.container)
        self.assertEqual(workspace.carrier_name, "Maersk")

    def test_carrier_falls_back_to_the_shipment(self):
        self._shipment(carrier="Hapag-Lloyd")
        workspace = get_container_workspace(self.team, self.container)
        self.assertEqual(workspace.carrier_name, "Hapag-Lloyd")

    def test_transport_status_comes_from_the_shipment(self):
        self._shipment(status=Shipment.Status.IN_TRANSIT)
        workspace = get_container_workspace(self.team, self.container)
        self.assertEqual(workspace.transport_status, "In Transit")

    def test_business_status_stays_on_the_container(self):
        """The container's own status answers a different question and is separate."""
        from apps.scm.containers.choices import ContainerStatus

        self._shipment(status=Shipment.Status.IN_TRANSIT)
        workspace = get_container_workspace(self.team, self.container)
        self.assertEqual(workspace.container.status, ContainerStatus.AVAILABLE)
        self.assertNotEqual(workspace.container.get_status_display(), workspace.transport_status)

    def test_tracking_status_comes_from_the_subscription(self):
        self._subscription(tracking_status=TrackingSubscription.TrackingStatus.NO_DATA)
        workspace = get_container_workspace(self.team, self.container)
        self.assertEqual(workspace.tracking_status, "No data at carrier yet")

    def test_vessel_and_voyage_come_from_the_latest_actual_event(self):
        self._event(
            TrackingEvent.EventType.LOADED_ON_VESSEL,
            LOADED_AT,
            vessel_name="MAERSK EINDHOVEN",
            voyage_number="213E",
        )
        workspace = get_container_workspace(self.team, self.container)
        self.assertEqual(workspace.vessel_name, "MAERSK EINDHOVEN")
        self.assertEqual(workspace.voyage_number, "213E")

    def test_eta_comes_from_the_shipment(self):
        shipment = self._shipment()
        shipment.eta = ARRIVED_AT.date()
        shipment.original_eta = (ARRIVED_AT - timedelta(days=3)).date()
        shipment.save()
        workspace = get_container_workspace(self.team, self.container)
        self.assertEqual(workspace.current_eta, ARRIVED_AT.date())
        self.assertEqual(workspace.eta_delay_days, 3)

    def test_no_shipment_leaves_derived_values_empty(self):
        workspace = get_container_workspace(self.team, self.container)
        self.assertIsNone(workspace.active_shipment)
        self.assertEqual(workspace.transport_status, "")
        self.assertIsNone(workspace.current_eta)
        self.assertIsNone(workspace.eta_delay_days)

    def test_active_subscription_prefers_an_active_one(self):
        self._subscription(status=TrackingSubscription.Status.CANCELLED, tracking_reference="OLD")
        active = self._subscription(status=TrackingSubscription.Status.ACTIVE, tracking_reference="NEW")
        workspace = get_container_workspace(self.team, self.container)
        self.assertEqual(workspace.active_subscription.pk, active.pk)


class StandaloneContainerTrackingTest(WorkspaceTestBase):
    """A container tracked without a shipment still has a status, an ETA and a vessel.

    Everything here used to come only from the shipment, so a container tracked on
    its own showed "—" no matter how much the carrier had told us about it.
    """

    team_slug = "workspace-standalone"

    def test_current_status_is_the_latest_observed_event(self):
        self._event(TrackingEvent.EventType.LOADED_ON_VESSEL, LOADED_AT)
        self._event(TrackingEvent.EventType.GATE_IN, ARRIVED_AT)
        workspace = get_container_workspace(self.team, self.container)
        self.assertEqual(workspace.tracking_current_status, "Gate In")
        self.assertEqual(workspace.current_status, "Gate In")

    def test_a_forecast_never_becomes_the_current_status(self):
        """Otherwise a box would read as arrived days before it was."""
        self._event(TrackingEvent.EventType.LOADED_ON_VESSEL, LOADED_AT)
        self._event(
            TrackingEvent.EventType.VESSEL_ARRIVED,
            ARRIVED_AT,
            event_time_type=TrackingEvent.EventTimeType.ESTIMATED,
        )
        workspace = get_container_workspace(self.team, self.container)
        self.assertEqual(workspace.tracking_current_status, "Loaded on Vessel")

    def test_an_unclassified_event_does_not_blank_the_status(self):
        """A gap in our mapping tables must not erase what we do know."""
        self._event(TrackingEvent.EventType.GATE_IN, LOADED_AT)
        self._event(TrackingEvent.EventType.UNKNOWN, ARRIVED_AT, event_code="RELS")
        workspace = get_container_workspace(self.team, self.container)
        self.assertEqual(workspace.tracking_current_status, "Gate In")

    def test_tracking_leads_over_the_shipments_transport_status(self):
        self._shipment(status=Shipment.Status.IN_TRANSIT)
        self._event(TrackingEvent.EventType.DISCHARGED, ARRIVED_AT)
        workspace = get_container_workspace(self.team, self.container)
        self.assertEqual(workspace.current_status, "Discharged")

    def test_the_shipment_status_stands_in_when_nothing_is_tracked(self):
        self._shipment(status=Shipment.Status.IN_TRANSIT)
        workspace = get_container_workspace(self.team, self.container)
        self.assertEqual(workspace.tracking_current_status, "")
        self.assertEqual(workspace.current_status, "In Transit")

    def test_eta_comes_from_an_estimated_arrival_event(self):
        self._event(
            TrackingEvent.EventType.VESSEL_ARRIVED,
            ARRIVED_AT,
            event_time_type=TrackingEvent.EventTimeType.ESTIMATED,
        )
        workspace = get_container_workspace(self.team, self.container)
        self.assertEqual(workspace.tracking_eta_at, ARRIVED_AT)
        self.assertEqual(workspace.current_eta, timezone.localtime(ARRIVED_AT).date())
        self.assertEqual(workspace.eta_source, "tracking")

    def test_eta_uses_full_precision_not_just_the_date(self):
        """A slip from 06:00 to 22:00 is a working day lost; a date would hide it."""
        self._event(
            TrackingEvent.EventType.ETA_UPDATED,
            ARRIVED_AT,
            event_time_type=TrackingEvent.EventTimeType.ESTIMATED,
        )
        workspace = get_container_workspace(self.team, self.container)
        self.assertEqual(workspace.tracking_eta_at.hour, ARRIVED_AT.hour)

    def test_the_latest_forecast_wins(self):
        later = ARRIVED_AT + timedelta(days=4)
        self._event(
            TrackingEvent.EventType.VESSEL_ARRIVED,
            ARRIVED_AT,
            event_time_type=TrackingEvent.EventTimeType.ESTIMATED,
        )
        self._event(
            TrackingEvent.EventType.ETA_UPDATED,
            later,
            event_time_type=TrackingEvent.EventTimeType.ESTIMATED,
        )
        workspace = get_container_workspace(self.team, self.container)
        self.assertEqual(workspace.tracking_eta_at, later)

    def test_an_actual_arrival_retires_the_forecast(self):
        """Once it has arrived, an ETA is not an estimate — it is a contradiction."""
        self._event(
            TrackingEvent.EventType.VESSEL_ARRIVED,
            ARRIVED_AT + timedelta(days=2),
            event_time_type=TrackingEvent.EventTimeType.ESTIMATED,
        )
        self._event(TrackingEvent.EventType.VESSEL_ARRIVED, ARRIVED_AT)
        workspace = get_container_workspace(self.team, self.container)
        self.assertIsNone(workspace.tracking_eta)
        self.assertIsNone(workspace.current_eta)

    def test_a_forecast_departure_is_not_an_eta(self):
        self._event(
            TrackingEvent.EventType.VESSEL_DEPARTED,
            LOADED_AT,
            event_time_type=TrackingEvent.EventTimeType.ESTIMATED,
        )
        workspace = get_container_workspace(self.team, self.container)
        self.assertIsNone(workspace.tracking_eta)

    def test_the_shipments_eta_still_wins_when_there_is_one(self):
        shipment = self._shipment()
        shipment.eta = ARRIVED_AT.date()
        shipment.save(update_fields=["eta"])
        self._event(
            TrackingEvent.EventType.ETA_UPDATED,
            ARRIVED_AT + timedelta(days=9),
            event_time_type=TrackingEvent.EventTimeType.ESTIMATED,
        )
        workspace = get_container_workspace(self.team, self.container)
        self.assertEqual(workspace.current_eta, ARRIVED_AT.date())
        self.assertEqual(workspace.eta_source, "shipment")

    def test_vessel_survives_a_later_truck_movement(self):
        """The last thing to happen to a box is often a truck, which names no vessel."""
        self._event(
            TrackingEvent.EventType.VESSEL_ARRIVED,
            LOADED_AT,
            vessel_name="JEBEL ALI",
            voyage_number="623W",
        )
        self._event(TrackingEvent.EventType.GATE_IN, ARRIVED_AT, transport_mode=TrackingEvent.TransportMode.TRUCK)
        workspace = get_container_workspace(self.team, self.container)
        self.assertEqual(workspace.vessel_name, "JEBEL ALI")
        self.assertEqual(workspace.voyage_number, "623W")

    def test_next_check_is_shown_only_for_a_live_watch(self):
        when = timezone.now() + timedelta(hours=1)
        subscription = self._subscription(next_sync_at=when)
        self.assertEqual(get_container_workspace(self.team, self.container).next_check_at, when)

        subscription.status = TrackingSubscription.Status.COMPLETED
        subscription.save(update_fields=["status"])
        self.assertIsNone(get_container_workspace(self.team, self.container).next_check_at)

    def test_derived_values_stay_inside_the_team(self):
        other_team = _team("workspace-standalone-other")
        self._event(
            TrackingEvent.EventType.VESSEL_ARRIVED,
            ARRIVED_AT,
            event_time_type=TrackingEvent.EventTimeType.ESTIMATED,
        )
        workspace = get_container_workspace(other_team, self.container)
        self.assertIsNone(workspace.tracking_eta)
        self.assertEqual(workspace.tracking_current_status, "")


class WorkspaceTimelineAndSyncTest(WorkspaceTestBase):
    team_slug = "workspace-timeline"

    def test_timeline_is_newest_first(self):
        self._event(TrackingEvent.EventType.LOADED_ON_VESSEL, LOADED_AT)
        self._event(TrackingEvent.EventType.VESSEL_ARRIVED, ARRIVED_AT)
        workspace = get_container_workspace(self.team, self.container)
        self.assertEqual(
            [event.event_type for event in workspace.timeline],
            [TrackingEvent.EventType.VESSEL_ARRIVED, TrackingEvent.EventType.LOADED_ON_VESSEL],
        )

    def test_latest_actual_event_ignores_forecasts(self):
        self._event(TrackingEvent.EventType.LOADED_ON_VESSEL, LOADED_AT)
        self._event(
            TrackingEvent.EventType.VESSEL_ARRIVED,
            ARRIVED_AT,
            event_time_type=TrackingEvent.EventTimeType.ESTIMATED,
        )
        workspace = get_container_workspace(self.team, self.container)
        self.assertEqual(workspace.latest_actual_event.event_type, TrackingEvent.EventType.LOADED_ON_VESSEL)
        self.assertEqual(workspace.latest_tracking_event.event_type, TrackingEvent.EventType.VESSEL_ARRIVED)

    def test_recent_sync_runs_are_exposed(self):
        subscription = self._subscription()
        TrackingSyncRun.objects.create(
            team=self.team,
            subscription=subscription,
            provider=self.provider,
            status=TrackingSyncRun.Status.SUCCESS,
            started_at=timezone.now(),
        )
        workspace = get_container_workspace(self.team, self.container)
        self.assertEqual(len(workspace.recent_sync_runs), 1)
        self.assertEqual(workspace.last_sync_run.status, TrackingSyncRun.Status.SUCCESS)

    def test_not_configured_reports_a_readable_problem(self):
        self._subscription(tracking_status=TrackingSubscription.TrackingStatus.NOT_CONFIGURED)
        workspace = get_container_workspace(self.team, self.container)
        self.assertIn("not configured", workspace.sync_problem)

    def test_failed_subscription_reports_its_error(self):
        self._subscription(
            status=TrackingSubscription.Status.FAILED,
            last_error_message="maersk rejected the credentials (HTTP 401)",
        )
        workspace = get_container_workspace(self.team, self.container)
        self.assertIn("401", workspace.sync_problem)

    def test_no_data_is_not_a_problem(self):
        """Waiting for the carrier is a status, not an error to alarm anyone with."""
        self._subscription(tracking_status=TrackingSubscription.TrackingStatus.NO_DATA)
        workspace = get_container_workspace(self.team, self.container)
        self.assertEqual(workspace.sync_problem, "")

    def test_healthy_subscription_reports_no_problem(self):
        self._subscription(tracking_status=TrackingSubscription.TrackingStatus.TRACKING)
        self.assertEqual(get_container_workspace(self.team, self.container).sync_problem, "")


class WorkspaceProcurementLinksTest(WorkspaceTestBase):
    team_slug = "workspace-procurement"

    def _delivery_line(self):
        order = PurchaseOrder.objects.create(
            team=self.team,
            po_number="PO-1001",
            supplier_name="Acme Industries",
            external_id="ext-1001",
        )
        line = PurchaseOrderLine.objects.create(
            team=self.team,
            purchase_order=order,
            external_id="ext-1001-1",
            line_no="10",
            item_no="ITEM-1",
            description="Steel brackets",
            ordered_qty=100,
        )
        delivery = SupplierDelivery.objects.create(
            team=self.team,
            purchase_order=order,
            delivery_reference="DEL-1",
            supplier="Acme Industries",
        )
        return SupplierDeliveryLine.objects.create(
            team=self.team,
            delivery=delivery,
            purchase_order_line=line,
            article="ITEM-1",
            delivery_qty=100,
            container=self.container,
        )

    def test_purchase_order_lines_are_linked_through_delivery_lines(self):
        self._delivery_line()
        workspace = get_container_workspace(self.team, self.container)
        self.assertEqual(len(workspace.purchase_order_lines), 1)
        self.assertEqual(workspace.purchase_order_lines[0].item_no, "ITEM-1")

    def test_purchase_orders_are_deduplicated(self):
        self._delivery_line()
        workspace = get_container_workspace(self.team, self.container)
        self.assertEqual([order.po_number for order in workspace.purchase_orders], ["PO-1001"])

    def test_suppliers_are_listed(self):
        self._delivery_line()
        self.assertEqual(get_container_workspace(self.team, self.container).suppliers, ["Acme Industries"])

    def test_no_procurement_links_is_empty_not_an_error(self):
        workspace = get_container_workspace(self.team, self.container)
        self.assertEqual(workspace.purchase_order_lines, [])
        self.assertEqual(workspace.purchase_orders, [])


class WorkspaceTeamIsolationTest(WorkspaceTestBase):
    team_slug = "workspace-isolation"

    def test_another_teams_events_are_not_included(self):
        other_team = _team("workspace-isolation-other")
        TrackingEvent.objects.create(
            team=other_team,
            provider=self.provider,
            container=self.container,
            event_type=TrackingEvent.EventType.VESSEL_ARRIVED,
            event_time_type=TrackingEvent.EventTimeType.ACTUAL,
            event_datetime=ARRIVED_AT,
            event_fingerprint="other-team-event",
        )
        workspace = get_container_workspace(self.team, self.container)
        self.assertEqual(workspace.timeline, [])
        self.assertIsNone(workspace.latest_tracking_event)

    def test_another_teams_subscriptions_are_not_included(self):
        other_team = _team("workspace-isolation-subs")
        TrackingSubscription.objects.create(
            team=other_team,
            provider=self.provider,
            container=self.container,
            tracking_reference="OTHER",
        )
        self.assertEqual(get_container_workspace(self.team, self.container).tracking_subscriptions, [])


class ContainerDetailRenderTest(WorkspaceTestBase):
    """The detail page renders the populated tracking panel, not just the empty state."""

    team_slug = "workspace-render"

    def setUp(self):
        super().setUp()
        from apps.users.models import CustomUser

        self.user = CustomUser.objects.create_user(username="workspace-render@example.com", password="pass")
        self.team.members.add(self.user, through_defaults={"role": "admin"})

    def _get_detail(self):
        from django.test import Client
        from django.urls import reverse

        client = Client()
        client.force_login(self.user)
        return client.get(reverse("containers:detail", kwargs={"container_id": self.container.pk}))

    def test_page_renders_with_tracking_data(self):
        shipment = self._shipment(status=Shipment.Status.IN_TRANSIT)
        shipment.eta = ARRIVED_AT.date()
        shipment.save()
        self._subscription(tracking_status=TrackingSubscription.TrackingStatus.TRACKING)
        self._event(
            TrackingEvent.EventType.LOADED_ON_VESSEL,
            LOADED_AT,
            location_name="Port of Felixstowe",
            location_unlocode="GBFXT",
            vessel_name="MAERSK EINDHOVEN",
            voyage_number="213E",
        )
        response = self._get_detail()
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "MAERSK EINDHOVEN")
        self.assertContains(response, "GBFXT")

    def test_position_type_is_shown_next_to_the_place(self):
        """The page must never present a terminal coordinate as a live position."""
        self._subscription()
        self._event(TrackingEvent.EventType.GATE_IN, LOADED_AT, location_unlocode="GBFXT")
        response = self._get_detail()
        self.assertContains(response, "Terminal or port")
        self.assertContains(response, "not a live position")

    def test_page_renders_without_any_tracking(self):
        response = self._get_detail()
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Not tracked")
        self.assertContains(response, "No tracking events yet")


class PositionClassificationTest(WorkspaceTestBase):
    """A position's quality is never inferred away."""

    team_slug = "workspace-position"

    def test_port_event_without_coordinates_is_a_facility(self):
        event = self._event(
            TrackingEvent.EventType.GATE_IN,
            LOADED_AT,
            location_name="Port of Felixstowe",
            location_unlocode="GBFXT",
        )
        self.assertEqual(classify_position(event), PositionType.FACILITY)

    def test_vessel_event_with_coordinates_is_a_vessel_position(self):
        """The ship's position is not the container's position once discharged."""
        event = self._event(
            TrackingEvent.EventType.VESSEL_DEPARTED,
            LOADED_AT,
            location_latitude=Decimal("51.955000"),
            location_longitude=Decimal("1.351000"),
            vessel_name="MAERSK EINDHOVEN",
            vessel_imo="9778791",
            transport_mode=TrackingEvent.TransportMode.VESSEL,
        )
        self.assertEqual(classify_position(event), PositionType.VESSEL)

    def test_truck_event_with_bare_coordinates_is_a_gps_fix(self):
        event = self._event(
            TrackingEvent.EventType.GATE_OUT,
            ARRIVED_AT,
            location_latitude=Decimal("51.900000"),
            location_longitude=Decimal("4.480000"),
            transport_mode=TrackingEvent.TransportMode.TRUCK,
        )
        self.assertEqual(classify_position(event), PositionType.GPS)

    def test_a_terminals_coordinates_are_a_facility_not_a_fix(self):
        """DCSA puts the terminal's coordinates on the event. Six decimals of a
        terminal are still a terminal — the box is not sitting on that pin."""
        event = self._event(
            TrackingEvent.EventType.GATE_IN,
            ARRIVED_AT,
            location_name="Gothenburg, Oceanterminalen",
            location_unlocode="SEGOT",
            location_latitude=Decimal("57.696629"),
            location_longitude=Decimal("11.858448"),
            transport_mode=TrackingEvent.TransportMode.TRUCK,
        )
        self.assertEqual(classify_position(event), PositionType.FACILITY)
        self.assertFalse(get_latest_container_position(self.team, self.container).is_realtime)

    def test_estimated_event_is_estimated_however_precise(self):
        event = self._event(
            TrackingEvent.EventType.VESSEL_ARRIVED,
            ARRIVED_AT,
            event_time_type=TrackingEvent.EventTimeType.ESTIMATED,
            location_latitude=Decimal("51.900000"),
            location_longitude=Decimal("4.480000"),
        )
        self.assertEqual(classify_position(event), PositionType.ESTIMATED)

    def test_event_without_a_place_is_unknown(self):
        event = self._event(TrackingEvent.EventType.UNKNOWN, LOADED_AT)
        self.assertEqual(classify_position(event), PositionType.UNKNOWN)

    def test_facility_position_is_not_realtime(self):
        self._event(
            TrackingEvent.EventType.GATE_IN,
            LOADED_AT,
            location_name="Port of Felixstowe",
            location_unlocode="GBFXT",
        )
        position = get_latest_container_position(self.team, self.container)
        self.assertFalse(position.is_realtime, "A port coordinate is not a live container position")
        self.assertEqual(position.get_position_type_display(), "Terminal or port")

    def test_latest_actual_event_wins_over_a_newer_forecast(self):
        self._event(
            TrackingEvent.EventType.GATE_IN,
            LOADED_AT,
            location_unlocode="GBFXT",
        )
        self._event(
            TrackingEvent.EventType.VESSEL_ARRIVED,
            ARRIVED_AT,
            event_time_type=TrackingEvent.EventTimeType.ESTIMATED,
            location_unlocode="NLRTM",
        )
        position = get_latest_container_position(self.team, self.container)
        self.assertEqual(position.location_unlocode, "GBFXT")
        self.assertEqual(position.position_type, PositionType.FACILITY)

    def test_a_forecast_stands_in_only_when_there_is_nothing_actual(self):
        self._event(
            TrackingEvent.EventType.VESSEL_ARRIVED,
            ARRIVED_AT,
            event_time_type=TrackingEvent.EventTimeType.ESTIMATED,
            location_unlocode="NLRTM",
        )
        position = get_latest_container_position(self.team, self.container)
        self.assertEqual(position.position_type, PositionType.ESTIMATED)
        self.assertFalse(position.is_realtime)

    def test_no_events_means_no_position(self):
        self.assertIsNone(get_latest_container_position(self.team, self.container))

    def test_workspace_exposes_the_position(self):
        self._event(TrackingEvent.EventType.GATE_IN, LOADED_AT, location_unlocode="GBFXT")
        workspace = get_container_workspace(self.team, self.container)
        self.assertIsNotNone(workspace.position)
        self.assertEqual(workspace.position.label, "GBFXT")
