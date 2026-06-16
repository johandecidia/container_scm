from django import forms
from django.utils.translation import gettext_lazy as _

from .models import ImportJob

ALLOWED_EXTENSIONS = (".csv", ".xlsx", ".xml")
MAX_UPLOAD_MB = 10


class ImportUploadForm(forms.Form):
    """Form for uploading an import file."""

    file = forms.FileField(
        label=_("File"),
        help_text=_("Upload a CSV or XLSX file, or an XML file for Purchase Orders (max %(size)s MB).")
        % {"size": MAX_UPLOAD_MB},
        widget=forms.FileInput(attrs={"accept": ".csv,.xlsx,.xml", "class": "file-input file-input-bordered w-full"}),
    )
    import_type = forms.ChoiceField(
        label=_("Import type"),
        choices=ImportJob.ImportType.choices,
        widget=forms.Select(attrs={"class": "select select-bordered w-full"}),
    )

    def clean_file(self):
        f = self.cleaned_data["file"]
        name = f.name.lower()
        if not any(name.endswith(ext) for ext in ALLOWED_EXTENSIONS):
            raise forms.ValidationError(_("Unsupported file type. Please upload a CSV, XLSX, or XML file."))
        if f.size > MAX_UPLOAD_MB * 1024 * 1024:
            raise forms.ValidationError(_("File too large. Maximum size is %(size)s MB.") % {"size": MAX_UPLOAD_MB})
        return f

    def clean(self):
        cleaned_data = super().clean()
        file = cleaned_data.get("file")
        import_type = cleaned_data.get("import_type")
        if file and file.name.lower().endswith(".xml") and import_type != ImportJob.ImportType.PURCHASE_ORDERS:
            raise forms.ValidationError(_("XML files are only supported for Purchase Orders imports."))
        return cleaned_data
