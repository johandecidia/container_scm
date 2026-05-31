"""Tests for tracking models."""

from django.test import TestCase

from apps.scm.containers.models import Container, EquipmentType
from apps.scm.containers.utils import calculate_check_digit
from apps.scm.shipments.models import Shipment
from apps.scm.tracking.models import (
    TrackingEvent,
    TrackingProvider,
    TrackingRawPayload,
    TrackingSubscription,
    TrackingSyncRun,
)
from apps.teams.models import Team


def _team(slug="model-test-team"):
    return Team.objects.create(name=slug, slug=slug)


def _provider():
    return TrackingProvider.objects.create(
        code="TEST_PROVIDER",
        name="Test Provider",
        provider_type=TrackingProvider.ProviderType.MANUAL,
    )


def _et():
    return EquipmentType.objects.get_or_create(
        iso_code="20GP",
        defaults={"category": "GP", "length_ft": 20, "high_cube": False, "description": "20' GP"},
    )[0]


def _container(team):
    check = calculate_check_digit("MSC", "U", "111111")
    return Container.objects.create(
        team=team,
        owner_code="MSC",
        category_id="U",
        serial_number="111111",
        check_digit=check,
        equipment_type=_et(),
    )


class TrackingProviderModelTest(TestCase):
    def test_create_provider(self):
        p = _provider()
        self.assertIsNotNone(p.pk)

    def test_str(self):
        p = _provider()
        self.assertEqual(str(p), p.name)

    def test_is_active_default(self):
        p = _provider()
        self.assertTrue(p.is_active)

    def test_code_unique(self):
        _provider()
        from django.db import IntegrityError

        with self.assertRaises(IntegrityError):
            TrackingProvider.objects.create(
                code="TEST_PROVIDER",
                name="Duplicate",
                provider_type=TrackingProvider.ProviderType.MANUAL,
            )


class TrackingSubscriptionModelTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = _team("sub-model-team")
        cls.provider = _provider()
        cls.shipment = Shipment.objects.create(team=cls.team, shipment_number="SHP-MODEL-1")
        cls.container = _container(cls.team)

    def test_create_subscription_with_shipment(self):
        sub = TrackingSubscription.objects.create(
            team=self.team,
            provider=self.provider,
            tracking_reference="MSKU1234567",
            shipment=self.shipment,
        )
        self.assertIsNotNone(sub.pk)
        self.assertEqual(sub.shipment, self.shipment)

    def test_create_subscription_with_container(self):
        sub = TrackingSubscription.objects.create(
            team=self.team,
            provider=self.provider,
            tracking_reference="MSKU1234568",
            container=self.container,
        )
        self.assertIsNotNone(sub.pk)
        self.assertEqual(sub.container, self.container)

    def test_default_status_active(self):
        sub = TrackingSubscription.objects.create(
            team=self.team,
            provider=self.provider,
            tracking_reference="MSKU1234569",
        )
        self.assertEqual(sub.status, TrackingSubscription.Status.ACTIVE)

    def test_str(self):
        sub = TrackingSubscription.objects.create(
            team=self.team,
            provider=self.provider,
            tracking_reference="MSKU9999999",
        )
        self.assertIn("MSKU9999999", str(sub))


class TrackingEventModelTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = _team("event-model-team")
        cls.provider = TrackingProvider.objects.create(
            code="EVENT_PROVIDER",
            name="Event Provider",
            provider_type=TrackingProvider.ProviderType.MANUAL,
        )
        cls.subscription = TrackingSubscription.objects.create(
            team=cls.team,
            provider=cls.provider,
            tracking_reference="EVT-REF",
        )

    def test_create_event(self):
        event = TrackingEvent.objects.create(
            team=self.team,
            provider=self.provider,
            subscription=self.subscription,
            event_type=TrackingEvent.EventType.GATE_IN,
        )
        self.assertIsNotNone(event.pk)

    def test_str(self):
        from django.utils import timezone

        event = TrackingEvent.objects.create(
            team=self.team,
            provider=self.provider,
            event_type=TrackingEvent.EventType.DISCHARGED,
            event_datetime=timezone.now(),
        )
        self.assertIn("Discharged", str(event))


class TrackingRawPayloadModelTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = _team("raw-model-team")
        cls.provider = TrackingProvider.objects.create(
            code="RAW_PROVIDER",
            name="Raw Provider",
            provider_type=TrackingProvider.ProviderType.API,
        )

    def test_create_raw_payload(self):
        payload = TrackingRawPayload.objects.create(
            team=self.team,
            provider=self.provider,
            payload_json={"data": "test"},
        )
        self.assertIsNotNone(payload.pk)


class TrackingSyncRunModelTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = _team("sync-model-team")
        cls.provider = TrackingProvider.objects.create(
            code="SYNC_PROVIDER",
            name="Sync Provider",
            provider_type=TrackingProvider.ProviderType.API,
        )
        cls.subscription = TrackingSubscription.objects.create(
            team=cls.team,
            provider=cls.provider,
            tracking_reference="SYNC-REF",
        )

    def test_create_sync_run(self):
        run = TrackingSyncRun.objects.create(
            team=self.team,
            provider=self.provider,
            subscription=self.subscription,
        )
        self.assertIsNotNone(run.pk)
