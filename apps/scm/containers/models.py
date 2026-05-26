from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.teams.models import BaseTeamModel


class Container(BaseTeamModel):
    """A shipping container tracked within a team."""

    container_number = models.CharField(_("container number"), max_length=20, unique=True)
    carrier = models.CharField(_("carrier"), max_length=100, blank=True)
    status = models.CharField(_("status"), max_length=50, blank=True)
    etd = models.DateField(_("estimated time of departure"), null=True, blank=True)
    eta = models.DateField(_("estimated time of arrival"), null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.container_number
