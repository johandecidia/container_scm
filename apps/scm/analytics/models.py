# Analytics models — lightweight aggregation snapshots.
# Heavy computation belongs in services.py or Celery tasks.

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
