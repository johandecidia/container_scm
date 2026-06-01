from django import forms
from django.utils.translation import gettext_lazy as _

from .models import TrackingProvider, TrackingSubscription


class TrackingSubscriptionForm(forms.Form):
    """Form for creating a new tracking subscription."""

    provider = forms.ModelChoiceField(
        queryset=TrackingProvider.objects.filter(is_active=True),
        label=_("Provider"),
        empty_label=_("Select provider"),
    )
    tracking_reference = forms.CharField(
        label=_("Tracking reference"),
        max_length=200,
        help_text=_("Container number, booking number, bill of lading, etc."),
    )
    reference_type = forms.ChoiceField(
        label=_("Reference type"),
        choices=TrackingSubscription.ReferenceType.choices,
        initial=TrackingSubscription.ReferenceType.CONTAINER_NUMBER,
    )
