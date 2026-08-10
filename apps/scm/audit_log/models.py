"""SCM Audit Log model.

Records important user and system actions for compliance, debugging, and ops visibility.
Secrets must never be stored in metadata.
"""

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.teams.models import BaseTeamModel


class SCMAuditLog(BaseTeamModel):
    """Append-only audit trail for important SCM events.

    actor is null for system-initiated jobs (Celery tasks, scheduled jobs).
    Metadata must not contain credentials, tokens, or other secrets.
    """

    class Action(models.TextChoices):
        # Import events
        IMPORT_STARTED = "import_started", _("Import Started")
        IMPORT_COMPLETED = "import_completed", _("Import Completed")
        IMPORT_FAILED = "import_failed", _("Import Failed")
        # Shipment events
        SHIPMENT_CREATED = "shipment_created", _("Shipment Created")
        SHIPMENT_UPDATED = "shipment_updated", _("Shipment Updated")
        SHIPMENT_STATUS_CHANGED = "shipment_status_changed", _("Shipment Status Changed")
        # Supplier delivery events
        DELIVERY_CREATED = "delivery_created", _("Delivery Created")
        DELIVERY_UPDATED = "delivery_updated", _("Delivery Updated")
        DELIVERY_STATUS_CHANGED = "delivery_status_changed", _("Delivery Status Changed")
        # Container events
        CONTAINER_CREATED = "container_created", _("Container Created")
        CONTAINER_UPDATED = "container_updated", _("Container Updated")
        CONTAINER_LINKED = "container_linked", _("Container Linked to Shipment")
        CONTAINER_UNLINKED = "container_unlinked", _("Container Unlinked from Shipment")
        CONTAINER_DISCOVERED = "container_discovered", _("Container Discovered")
        # Tracking events
        TRACKING_SYNC_COMPLETED = "tracking_sync_completed", _("Tracking Sync Completed")
        TRACKING_SYNC_FAILED = "tracking_sync_failed", _("Tracking Sync Failed")
        # Filter events
        SAVED_FILTER_CREATED = "saved_filter_created", _("Saved Filter Created")
        SAVED_FILTER_DELETED = "saved_filter_deleted", _("Saved Filter Deleted")
        # Integration events
        INTEGRATION_CREDENTIAL_UPDATED = "integration_credential_updated", _("Integration Credential Updated")
        # Manual overrides
        MANUAL_OVERRIDE = "manual_override", _("Manual Override")

    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="scm_audit_logs",
        verbose_name=_("actor"),
        help_text=_("Null for system-initiated actions (Celery tasks, scheduled jobs)."),
    )
    action = models.CharField(_("action"), max_length=60, choices=Action.choices)
    object_type = models.CharField(_("object type"), max_length=100, blank=True)
    object_id = models.CharField(_("object ID"), max_length=100, blank=True)
    object_repr = models.CharField(_("object representation"), max_length=255, blank=True)
    # Metadata: contextual data — must NOT contain secrets, tokens, or credentials.
    metadata = models.JSONField(_("metadata"), default=dict, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["team", "-created_at"]),
            models.Index(fields=["team", "action"]),
            models.Index(fields=["team", "object_type", "object_id"]),
        ]
        verbose_name = _("SCM Audit Log")
        verbose_name_plural = _("SCM Audit Logs")

    def __str__(self) -> str:
        actor_label = str(self.actor) if self.actor_id else "system"
        return f"[{self.get_action_display()}] {actor_label} — {self.object_repr or self.object_id}"
