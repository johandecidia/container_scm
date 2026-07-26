"""Retention for stored carrier responses.

A raw payload is often the only evidence of what a carrier actually said, so
retention archives rather than deletes: the body is dropped to reclaim storage
while the record — provider, subscription, timestamps, payload hash, size and
whether it parsed — is kept. The hash still lets you prove that a payload you
hold elsewhere is the one we received.

Hard deletion is available but off by default, and both windows are configurable:

    SCM_TRACKING_RAW_PAYLOAD_RETENTION_DAYS   archive bodies older than this (0 = never)
    SCM_TRACKING_RAW_PAYLOAD_DELETE_DAYS      delete records older than this (0 = never)

Deleting a payload never deletes the events parsed from it — TrackingEvent.raw_payload
is nulled, and each event keeps its own copy of the event-level payload.
"""

from __future__ import annotations

import json
import logging
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from .models import TrackingRawPayload

logger = logging.getLogger(__name__)

DEFAULT_RETENTION_DAYS = 90
DEFAULT_DELETE_DAYS = 0  # never, unless configured

# Marker left in place of an archived body so the record is self-explanatory.
ARCHIVED_MARKER = {"_archived": True}


def _days(setting_name: str, default: int) -> int:
    try:
        return int(getattr(settings, setting_name, default))
    except TypeError, ValueError:
        logger.warning("Invalid %s; falling back to %s", setting_name, default)
        return default


def get_retention_days() -> int:
    return _days("SCM_TRACKING_RAW_PAYLOAD_RETENTION_DAYS", DEFAULT_RETENTION_DAYS)


def get_delete_days() -> int:
    return _days("SCM_TRACKING_RAW_PAYLOAD_DELETE_DAYS", DEFAULT_DELETE_DAYS)


def archive_old_raw_payloads(days: int | None = None, *, team=None) -> int:
    """Drop the bodies of payloads older than the retention window.

    Returns the number archived. A payload is archived at most once. Passing
    ``days=0`` disables archiving entirely.
    """
    days = get_retention_days() if days is None else days
    if days <= 0:
        logger.info("Raw payload archiving is disabled (retention days = %s).", days)
        return 0

    cutoff = timezone.now() - timedelta(days=days)
    queryset = TrackingRawPayload.objects.filter(archived_at__isnull=True, received_at__lt=cutoff)
    if team is not None:
        queryset = queryset.filter(team=team)

    now = timezone.now()
    archived = 0
    for payload in queryset.iterator(chunk_size=200):
        payload.payload_bytes = payload.payload_bytes or _payload_size(payload.payload_json)
        payload.payload_json = dict(ARCHIVED_MARKER)
        payload.archived_at = now
        payload.save(update_fields=["payload_json", "payload_bytes", "archived_at", "updated_at"])
        archived += 1

    logger.info("Archived %d raw payload(s) older than %d days.", archived, days)
    return archived


def delete_expired_raw_payloads(days: int | None = None, *, team=None) -> int:
    """Delete payload records older than the (opt-in) deletion window.

    Returns the number deleted. Disabled by default: the audit record is usually
    worth far more than the row it occupies.
    """
    days = get_delete_days() if days is None else days
    if days <= 0:
        return 0

    cutoff = timezone.now() - timedelta(days=days)
    queryset = TrackingRawPayload.objects.filter(received_at__lt=cutoff)
    if team is not None:
        queryset = queryset.filter(team=team)

    deleted, _details = queryset.delete()
    logger.info("Deleted %d raw payload record(s) older than %d days.", deleted, days)
    return deleted


def _payload_size(payload) -> int:
    try:
        return len(json.dumps(payload, default=str).encode())
    except TypeError, ValueError:
        return 0
