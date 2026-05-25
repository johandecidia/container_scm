from django import forms

from .models import Rate


class RateForm(forms.ModelForm):
    class Meta:
        model = Rate
        fields = ["origin", "destination", "carrier", "amount", "currency", "valid_from", "valid_to"]
