"""Container Pool and Discovery Event models.

ContainerPool tracks planned container numbers the system should search for at carriers.
ContainerDiscoveryEvent logs the outcome of each discovery attempt.
"""

from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.teams.models import BaseTeamModel


class ContainerPoolStatus(models.TextChoices):
    PLANNED = "planned", _("Planned")
    DETECTED = "detected", _("Detected")
    RETIRED = "retired", _("Retired")


class ContainerPool(BaseTeamModel):
    """A planned container number that the system should search for at carriers."""

    container_number = models.CharField(_("Container Number"), max_length=20)
    status = models.CharField(
        _("Status"),
        max_length=20,
        choices=ContainerPoolStatus.choices,
        default=ContainerPoolStatus.PLANNED,
    )
    notes = models.TextField(_("Notes"), blank=True)

    class Meta:
        verbose_name = _("Container Pool Entry")
        verbose_name_plural = _("Container Pool Entries")
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["team", "container_number"],
                name="unique_team_container_number",
            )
        ]

    def __str__(self) -> str:
        return f"{self.container_number} ({self.get_status_display()})"


class ContainerDiscoveryEvent(BaseTeamModel):
    """Records the result of a discovery attempt for a planned container."""

    class EventType(models.TextChoices):
        SEARCH_STARTED = "search_started", _("Search Started")
        CONTAINER_DETECTED = "container_detected", _("Container Detected")
        SEARCH_FAILED = "search_failed", _("Search Failed")

    container_pool = models.ForeignKey(
        ContainerPool,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="discovery_events",
        verbose_name=_("Container Pool Entry"),
    )
    container_number = models.CharField(_("Container Number"), max_length=20)
    carrier_code = models.CharField(_("Carrier Code"), max_length=50, blank=True)
    carrier_name = models.CharField(_("Carrier Name"), max_length=200, blank=True)
    event_type = models.CharField(
        _("Event Type"),
        max_length=30,
        choices=EventType.choices,
    )
    detected_at = models.DateTimeField(_("Detected At"), null=True, blank=True)
    payload = models.JSONField(_("Payload"), default=dict)

    class Meta:
        verbose_name = _("Container Discovery Event")
        verbose_name_plural = _("Container Discovery Events")
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["team", "container_number"]),
            models.Index(fields=["team", "event_type"]),
        ]

    def __str__(self) -> str:
        return f"{self.container_number} — {self.get_event_type_display()}"
