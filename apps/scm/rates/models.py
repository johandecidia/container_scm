from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.teams.models import BaseTeamModel


class Rate(BaseTeamModel):
    """A freight rate associated with a team."""

    origin = models.CharField(_("origin"), max_length=200)
    destination = models.CharField(_("destination"), max_length=200)
    carrier = models.CharField(_("carrier"), max_length=200, blank=True)
    amount = models.DecimalField(_("amount"), max_digits=12, decimal_places=2)
    currency = models.CharField(_("currency"), max_length=3, default="USD")
    valid_from = models.DateField(_("valid from"), null=True, blank=True)
    valid_to = models.DateField(_("valid to"), null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.origin} → {self.destination} ({self.carrier})"
