# Analytics models — lightweight aggregation snapshots and user preferences.
# Heavy computation belongs in services.py or Celery tasks.

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.teams.models import BaseTeamModel


class AnalyticsSnapshot(BaseTeamModel):
    """Daily KPI snapshot for a team, computed from Shipments and Containers."""

    date = models.DateField(_("date"))

    total_shipments = models.PositiveIntegerField(_("total shipments"), default=0)
    active_shipments = models.PositiveIntegerField(_("active shipments"), default=0)
    completed_shipments = models.PositiveIntegerField(_("completed shipments"), default=0)

    containers_in_transit = models.PositiveIntegerField(_("containers in transit"), default=0)
    containers_delivered = models.PositiveIntegerField(_("containers delivered"), default=0)

    avg_transit_days = models.DecimalField(
        _("average transit days"),
        max_digits=7,
        decimal_places=2,
        null=True,
        blank=True,
    )

    class Meta:
        ordering = ["-date"]
        constraints = [
            models.UniqueConstraint(fields=["team", "date"], name="unique_analytics_snapshot_per_team_date"),
        ]
        indexes = [
            models.Index(fields=["team", "-date"]),
        ]
        verbose_name = _("Analytics Snapshot")
        verbose_name_plural = _("Analytics Snapshots")

    def __str__(self) -> str:
        return f"Snapshot {self.date} — {self.team}"


class SavedFilter(BaseTeamModel):
    """Persisted filter state for a SCM list view, per user."""

    class ViewKey(models.TextChoices):
        PURCHASE_ORDERS = "purchase_orders", _("Purchase Orders")
        SUPPLIER_DELIVERIES = "supplier_deliveries", _("Supplier Deliveries")
        SHIPMENTS = "shipments", _("Shipments")
        CONTAINERS = "containers", _("Containers")
        TRACKING = "tracking", _("Tracking")
        ANALYTICS = "analytics", _("Analytics")

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="scm_saved_filters",
        verbose_name=_("user"),
    )
    name = models.CharField(_("name"), max_length=100)
    view_key = models.CharField(_("view"), max_length=50, choices=ViewKey.choices)
    params = models.JSONField(_("filter parameters"), default=dict, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["team", "user", "view_key"]),
        ]
        verbose_name = _("Saved Filter")
        verbose_name_plural = _("Saved Filters")

    def __str__(self) -> str:
        return f"{self.name} ({self.get_view_key_display()})"
