from django import forms

from .models import ImportJob


class ImportJobForm(forms.ModelForm):
    class Meta:
        model = ImportJob
        fields = ["file"]
