"""Backfill normalized_name for locations recorded before canonical identity.

This is deliberately *not* the speculative data migration LOC-1 refuses to write.
Nothing here guesses which canonical location a text destination meant, invents a
UN/LOCODE, or creates an alias. It applies one pure function to a column that
already exists: ``normalized_name`` is a stored form of ``name``, and a row where
it is blank is a row the resolver's name rule cannot see — an existing depot would
silently stop being findable by its own name.

The normalisation is inlined rather than imported from ``location_identity``. A
migration has to keep producing the values the schema was migrated with, and
importing application code would let a later revision of the algorithm change what
this historical migration did.

Reversing clears the column. That is lossless: it is derived from ``name``, which
the reverse does not touch.
"""

import unicodedata

from django.db import migrations


def _normalize(value: str) -> str:
    if not value:
        return ""
    text = unicodedata.normalize("NFKD", str(value))
    text = "".join(char for char in text if not unicodedata.combining(char))
    return " ".join(text.split()).strip().casefold()


def backfill_normalized_names(apps, schema_editor):
    ContainerLocation = apps.get_model("scm_containers", "ContainerLocation")

    updated = []
    queryset = ContainerLocation.objects.filter(normalized_name="").exclude(name="").order_by("pk")
    for location in queryset.iterator(chunk_size=500):
        location.normalized_name = _normalize(location.name)
        updated.append(location)
        if len(updated) >= 500:
            ContainerLocation.objects.bulk_update(updated, ["normalized_name"])
            updated = []
    if updated:
        ContainerLocation.objects.bulk_update(updated, ["normalized_name"])


def clear_normalized_names(apps, schema_editor):
    ContainerLocation = apps.get_model("scm_containers", "ContainerLocation")
    ContainerLocation.objects.update(normalized_name="")


class Migration(migrations.Migration):
    dependencies = [
        ("scm_containers", "0006_locationalias_containerlocation_country_code_and_more"),
    ]

    operations = [
        migrations.RunPython(backfill_normalized_names, clear_normalized_names),
    ]
