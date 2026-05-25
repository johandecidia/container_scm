from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.teams.models import BaseTeamModel


class Integration(BaseTeamModel):
    """An external system integration configured for a team."""

    class Status(models.TextChoices):
        ACTIVE = "active", _("Active")
        INACTIVE = "inactive", _("Inactive")
        ERROR = "error", _("Error")

    name = models.CharField(_("name"), max_length=100)
    provider = models.CharField(_("provider"), max_length=100)
    status = models.CharField(_("status"), max_length=20, choices=Status.choices, default=Status.INACTIVE)
    config = models.JSONField(_("config"), default=dict, blank=True)

    class Meta:
        ordering = ["name"]
        unique_together = [["team", "provider"]]

    def __str__(self):
        return f"{self.name} ({self.provider})"
