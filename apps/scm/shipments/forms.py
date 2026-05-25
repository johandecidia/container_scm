from django import forms

from .models import Shipment


class ShipmentForm(forms.ModelForm):
    class Meta:
        model = Shipment
        fields = ["reference", "status", "origin", "destination", "estimated_arrival"]
