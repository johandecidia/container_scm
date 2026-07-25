"""Backfill event_fingerprint for tracking events created before fingerprinting.

The hashing logic is deliberately inlined rather than imported from
``apps.scm.tracking.ingestion``: a migration must keep producing the same values
even if the application's algorithm is later revised.

Rows whose computed fingerprint collides with one already taken are left blank —
those are pre-existing duplicates admitted by the old, weaker deduplication, and
the unique constraint only applies to non-blank fingerprints. Leaving them blank
keeps the history intact instead of deleting audit data.
"""

import hashlib

from django.db import migrations

_FINGERPRINT_VERSION = "v1"


def _fingerprint(event, provider_code: str) -> str:
    if event.source_event_id:
        parts = [_FINGERPRINT_VERSION, "id", str(event.team_id), provider_code, event.source_event_id]
    else:
        parts = [
            _FINGERPRINT_VERSION,
            "fields",
            str(event.team_id),
            provider_code,
            event.equipment_reference or "",
            event.carrier_event_type or "",
            event.event_code or "",
            event.event_time_type or "",
            event.event_datetime.isoformat() if event.event_datetime else "",
            event.location_unlocode or event.location_name or "",
            event.vessel_imo or event.vessel_name or "",
            event.voyage_number or "",
        ]
    return hashlib.sha256("|".join(part.strip().upper() for part in parts).encode()).hexdigest()


def backfill_fingerprints(apps, schema_editor):
    TrackingEvent = apps.get_model("scm_tracking", "TrackingEvent")

    taken: set[tuple[int, int, str]] = set()
    updated = []
    queryset = TrackingEvent.objects.filter(event_fingerprint="").select_related("provider").order_by("pk")
    for event in queryset.iterator(chunk_size=500):
        fingerprint = _fingerprint(event, event.provider.code)
        key = (event.team_id, event.provider_id, fingerprint)
        if key in taken:
            continue
        taken.add(key)
        event.event_fingerprint = fingerprint
        updated.append(event)
        if len(updated) >= 500:
            TrackingEvent.objects.bulk_update(updated, ["event_fingerprint"])
            updated = []
    if updated:
        TrackingEvent.objects.bulk_update(updated, ["event_fingerprint"])


def clear_fingerprints(apps, schema_editor):
    TrackingEvent = apps.get_model("scm_tracking", "TrackingEvent")
    TrackingEvent.objects.update(event_fingerprint="")


class Migration(migrations.Migration):
    dependencies = [
        ("scm_tracking", "0003_remove_trackingevent_unique_tracking_event_source_id_and_more"),
    ]

    operations = [
        migrations.RunPython(backfill_fingerprints, clear_fingerprints),
    ]
