from django import forms

from .models import Container


class ContainerForm(forms.ModelForm):
    class Meta:
        model = Container
        fields = ["container_number", "carrier", "status", "etd", "eta"]
        widgets = {
            "etd": forms.DateInput(attrs={"type": "date"}),
            "eta": forms.DateInput(attrs={"type": "date"}),
        }
