"""Tests for the auto-link service."""

from django.test import TestCase

from apps.scm.container_discovery.auto_link import MatchConfidence, auto_link_detected_container
from apps.scm.integrations.carriers.schemas import ContainerDiscoveryResult
from apps.scm.shipments.models import Shipment
from apps.teams.models import Team


def _team(slug="cd-al-team") -> Team:
    return Team.objects.get_or_create(slug=slug, defaults={"name": slug})[0]


def _shipment(team, booking_ref="", bl_number="", reference="") -> Shipment:
    return Shipment.objects.create(
        team=team,
        carrier_booking_reference=booking_ref,
        bill_of_lading_number=bl_number,
        reference=reference,
    )


class AutoLinkHighConfidenceTest(TestCase):
    def test_exact_booking_number_gives_high_confidence(self):
        team = _team()
        shipment = _shipment(team, booking_ref="BKG-EXACT-001")

        result = ContainerDiscoveryResult(
            container_number="MCUU1234567",
            carrier_code="dummy",
            carrier_name="Dummy",
            booking_number="BKG-EXACT-001",
            raw_payload={},
        )
        match = auto_link_detected_container(result)

        self.assertEqual(match.confidence, MatchConfidence.HIGH)
        self.assertEqual(match.shipment_id, shipment.pk)
        self.assertEqual(match.matched_on, "booking_number")

    def test_no_match_gives_none_confidence(self):
        result = ContainerDiscoveryResult(
            container_number="MCUU9999999",
            carrier_code="dummy",
            carrier_name="Dummy",
            booking_number="BKG-NONEXISTENT",
            raw_payload={},
        )
        match = auto_link_detected_container(result)
        self.assertEqual(match.confidence, MatchConfidence.NONE)

    def test_no_references_gives_none_confidence(self):
        result = ContainerDiscoveryResult(
            container_number="MCUU8888888",
            carrier_code="dummy",
            carrier_name="Dummy",
            raw_payload={},
        )
        match = auto_link_detected_container(result)
        self.assertEqual(match.confidence, MatchConfidence.NONE)


class AutoLinkLowConfidenceTest(TestCase):
    def test_bl_number_match_gives_low_confidence(self):
        team = _team()
        shipment = _shipment(team, bl_number="BL-LOWCONF-001")

        result = ContainerDiscoveryResult(
            container_number="MCUU7777777",
            carrier_code="dummy",
            carrier_name="Dummy",
            bl_number="BL-LOWCONF-001",
            raw_payload={},
        )
        match = auto_link_detected_container(result)

        self.assertEqual(match.confidence, MatchConfidence.LOW)
        self.assertEqual(match.shipment_id, shipment.pk)

    def test_shipment_reference_match_gives_low_confidence(self):
        team = _team()
        shipment = _shipment(team, reference="REF-LOWCONF-001")

        result = ContainerDiscoveryResult(
            container_number="MCUU6666666",
            carrier_code="dummy",
            carrier_name="Dummy",
            shipment_reference="REF-LOWCONF-001",
            raw_payload={},
        )
        match = auto_link_detected_container(result)

        self.assertEqual(match.confidence, MatchConfidence.LOW)
        self.assertEqual(match.shipment_id, shipment.pk)
