"""Tests for SCM Audit Log (8.4)."""

from django.test import TestCase

from apps.scm.audit_log.models import SCMAuditLog
from apps.scm.audit_log.services import log_scm_action
from apps.teams.models import Team
from apps.users.models import CustomUser


def make_team(slug="audit-test") -> Team:
    return Team.objects.get_or_create(slug=slug, defaults={"name": "Audit Test"})[0]


def make_user() -> CustomUser:
    return CustomUser.objects.get_or_create(username="auditor@example.com", defaults={"email": "auditor@example.com"})[
        0
    ]


class AuditLogCreationTests(TestCase):
    def setUp(self):
        self.team = make_team()
        self.user = make_user()

    def test_log_scm_action_creates_record(self):
        log_scm_action(
            team=self.team,
            action=SCMAuditLog.Action.SHIPMENT_CREATED,
            object_type="Shipment",
            object_id="42",
            object_repr="SHP-001",
            actor=self.user,
        )
        self.assertEqual(SCMAuditLog.objects.filter(team=self.team).count(), 1)
        entry = SCMAuditLog.objects.get(team=self.team)
        self.assertEqual(entry.action, SCMAuditLog.Action.SHIPMENT_CREATED)
        self.assertEqual(entry.object_type, "Shipment")
        self.assertEqual(entry.object_id, "42")
        self.assertEqual(entry.actor, self.user)

    def test_system_action_has_null_actor(self):
        log_scm_action(
            team=self.team,
            action=SCMAuditLog.Action.TRACKING_SYNC_COMPLETED,
            object_type="TrackingSubscription",
            object_id="1",
            actor=None,
        )
        entry = SCMAuditLog.objects.filter(team=self.team).latest("created_at")
        self.assertIsNone(entry.actor)

    def test_metadata_is_stored(self):
        log_scm_action(
            team=self.team,
            action=SCMAuditLog.Action.IMPORT_COMPLETED,
            object_type="ImportJob",
            object_id="5",
            metadata={"import_type": "containers", "processed": 10},
            actor=self.user,
        )
        entry = SCMAuditLog.objects.filter(team=self.team).latest("created_at")
        self.assertEqual(entry.metadata["import_type"], "containers")
        self.assertEqual(entry.metadata["processed"], 10)

    def test_log_action_never_raises_on_bad_action(self):
        """Service must swallow errors so it never blocks the primary operation."""
        log_scm_action(
            team=self.team,
            action="nonexistent_action",  # not in choices
            object_type="Test",
        )
        # If we get here, the service swallowed the error (or stored it anyway)

    def test_no_secrets_in_audit_metadata(self):
        """Audit metadata must not contain credential-like keys."""
        log_scm_action(
            team=self.team,
            action=SCMAuditLog.Action.INTEGRATION_CREDENTIAL_UPDATED,
            object_type="IntegrationCredential",
            object_id="1",
            metadata={"auth_type": "api_key", "updated_by": "admin@example.com"},
            actor=self.user,
        )
        entry = SCMAuditLog.objects.filter(team=self.team).latest("created_at")
        metadata_str = str(entry.metadata).lower()
        for secret_key in ("password", "token", "secret", "encrypted_data", "api_key_value"):
            self.assertNotIn(secret_key, metadata_str)


class AuditLogTenantIsolationTests(TestCase):
    def test_audit_log_is_team_scoped(self):
        team_a = make_team("audit-team-a")
        team_b = make_team("audit-team-b")
        user = make_user()

        log_scm_action(team=team_a, action=SCMAuditLog.Action.SHIPMENT_CREATED, object_type="S", actor=user)
        log_scm_action(team=team_b, action=SCMAuditLog.Action.SHIPMENT_CREATED, object_type="S", actor=user)

        self.assertEqual(SCMAuditLog.objects.filter(team=team_a).count(), 1)
        self.assertEqual(SCMAuditLog.objects.filter(team=team_b).count(), 1)

    def test_cross_team_query_returns_empty(self):
        team_a = make_team("audit-iso-a")
        team_b = make_team("audit-iso-b")
        user = make_user()

        log_scm_action(team=team_a, action=SCMAuditLog.Action.SHIPMENT_CREATED, object_type="S", actor=user)

        self.assertFalse(SCMAuditLog.objects.filter(team=team_b).exists())


class AuditLogReadOnlyTests(TestCase):
    def test_admin_has_no_add_permission(self):
        from unittest.mock import MagicMock

        from django.contrib.admin.sites import AdminSite

        from apps.scm.audit_log.admin import SCMAuditLogAdmin

        admin_instance = SCMAuditLogAdmin(SCMAuditLog, AdminSite())
        request = MagicMock()
        self.assertFalse(admin_instance.has_add_permission(request))

    def test_admin_has_no_change_permission(self):
        from unittest.mock import MagicMock

        from django.contrib.admin.sites import AdminSite

        from apps.scm.audit_log.admin import SCMAuditLogAdmin

        admin_instance = SCMAuditLogAdmin(SCMAuditLog, AdminSite())
        request = MagicMock()
        self.assertFalse(admin_instance.has_change_permission(request))

    def test_admin_has_no_delete_permission(self):
        from unittest.mock import MagicMock

        from django.contrib.admin.sites import AdminSite

        from apps.scm.audit_log.admin import SCMAuditLogAdmin

        admin_instance = SCMAuditLogAdmin(SCMAuditLog, AdminSite())
        request = MagicMock()
        self.assertFalse(admin_instance.has_delete_permission(request))


class ShipmentServiceAuditTests(TestCase):
    def setUp(self):
        self.team = make_team("audit-shipment")
        self.user = make_user()

    def test_create_shipment_logs_audit_event(self):
        from apps.scm.shipments.services import create_shipment

        create_shipment(self.team, self.user, {"shipment_number": "SHP-AUDIT-001"})
        entry = SCMAuditLog.objects.filter(team=self.team, action=SCMAuditLog.Action.SHIPMENT_CREATED).first()
        self.assertIsNotNone(entry)
        self.assertEqual(entry.actor, self.user)

    def test_change_shipment_status_logs_audit_event(self):
        from apps.scm.shipments.models import Shipment
        from apps.scm.shipments.services import change_shipment_status, create_shipment

        shipment = create_shipment(self.team, self.user, {"shipment_number": "SHP-AUDIT-002"})
        SCMAuditLog.objects.filter(team=self.team).delete()

        change_shipment_status(shipment, self.user, Shipment.Status.BOOKED)
        entry = SCMAuditLog.objects.filter(team=self.team, action=SCMAuditLog.Action.SHIPMENT_STATUS_CHANGED).first()
        self.assertIsNotNone(entry)
        self.assertEqual(entry.metadata["new_status"], Shipment.Status.BOOKED)
