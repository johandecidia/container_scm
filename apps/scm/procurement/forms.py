from django import forms
from django.utils.translation import gettext_lazy as _

from .models import PurchaseOrder


class PurchaseOrderForm(forms.ModelForm):
    class Meta:
        model = PurchaseOrder
        fields = [
            "po_number",
            "supplier_no",
            "supplier_name",
            "status",
            "order_date",
            "expected_receipt_date",
            "currency",
        ]
        widgets = {
            "order_date": forms.DateInput(attrs={"type": "date"}),
            "expected_receipt_date": forms.DateInput(attrs={"type": "date"}),
        }
        labels = {
            "po_number": _("PO Number"),
            "supplier_no": _("Supplier No"),
            "supplier_name": _("Supplier Name"),
            "status": _("Status"),
            "order_date": _("Order Date"),
            "expected_receipt_date": _("Expected Receipt Date"),
            "currency": _("Currency"),
        }
