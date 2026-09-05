"""Shared setup for the LOC-4 map surface tests.

One inbound container with a canonical destination and nowhere to be drawn, used by
the Control Tower and the workspace modules so both are tested against the same
shape of data.
"""

from __future__ import annotations

from datetime import timedelta

from django.test import Client, TestCase, override_settings
from django.utils import timezone

from apps.scm.shipments.models import Shipment, ShipmentContainer

from .factories import (
    TEST_STORAGES,
    ingest_maersk_events,
    make_container,
    make_location,
    make_user_and_team,
)

OCEANTERMINALEN = ("57.696629", "11.858448")
ROTTERDAM = ("51.949760", "4.144830")


@override_settings(STORAGES=TEST_STORAGES, MAPBOX_PUBLIC_TOKEN="pk.test-token")
class MapSurfaceTestCase(TestCase):
    """One inbound container with a canonical destination and nowhere to be drawn.

    The carrier events are ingested *before* the canonical locations exist, and the
    ordering is load-bearing. Ingestion resolves each event's place against the
    locations a team has at that moment, and LOC-2 then records a physical movement
    from a resolved gate-in — so creating Oceanterminalen first would leave every
    test starting from a container already accepted into it, and the states LOC-4
    has to handle honestly (nothing plottable, a destination and no position) would
    be unreachable.

    Tests that want a position therefore establish one explicitly, which is also
    how they say what they are testing.
    """

    @classmethod
    def setUpTestData(cls):
        cls.user, cls.team = make_user_and_team("surface@example.com", "surface-team")
        cls.container = make_container(cls.team)
        cls.shipment = Shipment.objects.create(
            team=cls.team,
            shipment_number="SHP-SURF",
            carrier="Maersk",
            status=Shipment.Status.IN_TRANSIT,
            eta=timezone.localdate() + timedelta(days=5),
        )
        ShipmentContainer.objects.create(shipment=cls.shipment, container=cls.container)
        ingest_maersk_events(cls.team, cls.container, shipment=cls.shipment)

        cls.terminal = make_location(
            cls.team, "Oceanterminalen", unlocode="SEGOT", latitude=OCEANTERMINALEN[0], longitude=OCEANTERMINALEN[1]
        )
        cls.rotterdam = make_location(
            cls.team, "Rotterdam", unlocode="NLRTM", latitude=ROTTERDAM[0], longitude=ROTTERDAM[1]
        )
        cls.shipment.destination_location = cls.terminal
        cls.shipment.save(update_fields=["destination_location"])

    def setUp(self):
        self.client = Client()
        self.client.force_login(self.user)
