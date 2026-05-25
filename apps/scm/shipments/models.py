from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.teams.models import BaseTeamModel


class Shipment(BaseTeamModel):
    """A shipment belonging to a team."""

    class Status(models.TextChoices):
        DRAFT = "draft", _("Draft")
        BOOKED = "booked", _("Booked")
        IN_TRANSIT = "in_transit", _("In Transit")
        ARRIVED = "arrived", _("Arrived")
        DELIVERED = "delivered", _("Delivered")
        CANCELLED = "cancelled", _("Cancelled")

    reference = models.CharField(_("reference"), max_length=100, unique=True)
    status = models.CharField(_("status"), max_length=20, choices=Status.choices, default=Status.DRAFT)
    origin = models.CharField(_("origin"), max_length=200, blank=True)
    destination = models.CharField(_("destination"), max_length=200, blank=True)
    estimated_arrival = models.DateField(_("estimated arrival"), null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.reference
