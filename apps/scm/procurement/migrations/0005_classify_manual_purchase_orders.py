"""Classify pre-existing purchase orders by source, using only reliable signals.

Manually created POs use an ``external_id`` of the form ``manual-<uuid>`` — a
known, unambiguous signal — so those are set to source_system='manual'. All other
existing rows keep the field default ('business_central'); Business Central GUIDs
and document-import PO numbers cannot be reliably told apart after the fact, so no
unsafe guess is made here (a management command can reclassify if needed).
"""

from django.db import migrations


def classify_manual(apps, schema_editor):
    PurchaseOrder = apps.get_model("scm_procurement", "PurchaseOrder")
    PurchaseOrder.objects.filter(external_id__startswith="manual-").update(source_system="manual")


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("scm_procurement", "0004_purchaseorder_last_synced_at_and_more"),
    ]

    operations = [
        migrations.RunPython(classify_manual, noop),
    ]
