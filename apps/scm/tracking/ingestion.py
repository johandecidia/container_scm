"""Persistence of normalised carrier events as TrackingEvent rows.

The carrier layer produces :class:`NormalisedTrackingEvent` DTOs; this module is
the only place that turns them into database rows. It owns two responsibilities:

Fingerprinting
    Every event gets a stable ``event_fingerprint``. When the carrier supplies an
    event ID, the fingerprint is derived from it, so a corrected event updates in
    place. When it does not, the fingerprint is derived from the fields that
    identify the event — team, provider, reference, classification, time, place
    and vessel/voyage — which is strong enough that re-processing the same payload
    cannot create duplicates.

Idempotent writes
    Writes go through ``get_or_create`` guarded by the unique constraint on
    (team, provider, event_fingerprint), so two workers processing the same
    payload concurrently end up with one row, not two.
"""

from __future__ import annotations

import hashlib
import logging
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING

from django.db import IntegrityError, transaction
from django.utils import timezone

from .models import TrackingEvent
from .statuses import (
    normalize_dcsa_event_type,
    normalize_event_time_type,
    normalize_transport_mode,
)

if TYPE_CHECKING:
    from apps.scm.integrations.carriers.dcsa.schemas import NormalisedTrackingEvent
    from apps.teams.models import Team

    from .models import TrackingProvider, TrackingRawPayload, TrackingSubscription

logger = logging.getLogger(__name__)

# Fields that identify an event when the carrier gives us no event ID.
_FINGERPRINT_VERSION = "v1"


def _coordinate(value: str | None) -> Decimal | None:
    """Convert a raw coordinate string to a Decimal, or None when unusable.

    A malformed coordinate is dropped rather than guessed — the original value
    stays available in the event's raw data.
    """
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value)).quantize(Decimal("0.000001"))
    except InvalidOperation, ValueError, TypeError:
        logger.debug("Ignoring unparseable coordinate value: %r", value)
        return None


def build_event_fingerprint(
    *,
    team_id: int,
    provider_code: str,
    source_event_id: str = "",
    reference: str = "",
    carrier_event_type: str = "",
    event_code: str = "",
    event_time_type: str = "",
    event_datetime=None,
    location_unlocode: str = "",
    location_name: str = "",
    vessel_imo: str = "",
    vessel_name: str = "",
    voyage_number: str = "",
) -> str:
    """Return the stable deduplication hash for a carrier event.

    With a ``source_event_id`` the hash covers only team, provider and that ID, so
    a carrier correcting an event's time or place updates the existing row instead
    of adding a near-duplicate.

    Without one, the hash covers the identifying fields of the event. Two syncs of
    the same payload therefore produce the same fingerprint, while a genuinely
    different event produces a different one.
    """
    if source_event_id:
        parts = [_FINGERPRINT_VERSION, "id", str(team_id), provider_code, source_event_id]
    else:
        parts = [
            _FINGERPRINT_VERSION,
            "fields",
            str(team_id),
            provider_code,
            reference,
            carrier_event_type,
            event_code,
            event_time_type,
            event_datetime.isoformat() if event_datetime else "",
            location_unlocode or location_name,
            vessel_imo or vessel_name,
            voyage_number,
        ]
    joined = "|".join(part.strip().upper() for part in parts)
    return hashlib.sha256(joined.encode()).hexdigest()


def build_event_defaults(
    normalised: NormalisedTrackingEvent,
    *,
    subscription: TrackingSubscription | None = None,
    shipment=None,
    container=None,
    raw_payload: TrackingRawPayload | None = None,
    received_at=None,
) -> dict:
    """Map a NormalisedTrackingEvent onto TrackingEvent field values.

    Keeps both the internal classification and the carrier's own wording, so a gap
    in the mapping tables never destroys what the carrier reported.
    """
    event_time_type = normalize_event_time_type(normalised.event_classifier)
    return {
        "event_type": normalize_dcsa_event_type(
            normalised.event_type,
            normalised.event_code,
            normalised.description,
        ),
        "carrier_event_type": normalised.event_type[:60],
        "event_code": normalised.event_code[:100],
        "event_time_type": event_time_type,
        "status": (normalised.description or normalised.event_code)[:200],
        "description": normalised.description,
        "carrier_description": normalised.description,
        "location_name": (normalised.location_name or normalised.facility_name)[:200],
        "location_unlocode": normalised.location_unlocode[:10],
        "location_latitude": _coordinate(normalised.latitude),
        "location_longitude": _coordinate(normalised.longitude),
        "vessel_name": normalised.vessel_name[:200],
        "vessel_imo": normalised.vessel_imo[:20],
        "voyage_number": normalised.voyage_number[:50],
        "transport_mode": normalize_transport_mode(normalised.transport_mode),
        "equipment_reference": normalised.container_number[:20],
        "event_datetime": normalised.event_datetime,
        "event_timezone": normalised.event_datetime_timezone[:50],
        "received_at": received_at or timezone.now(),
        "source_event_id": normalised.raw_event_id[:200],
        "raw_data": normalised.raw_payload or {},
        "shipment": shipment,
        "container": container,
        "subscription": subscription,
        "raw_payload": raw_payload,
    }


def persist_normalised_event(
    *,
    team: Team,
    provider: TrackingProvider,
    normalised: NormalisedTrackingEvent,
    subscription: TrackingSubscription | None = None,
    shipment=None,
    container=None,
    raw_payload: TrackingRawPayload | None = None,
) -> tuple[TrackingEvent, bool]:
    """Store one normalised carrier event, returning (event, created).

    Idempotent: the same carrier event processed any number of times yields one
    row. A concurrent writer that wins the race is detected through the unique
    constraint and its row is returned instead.
    """
    reference = normalised.container_number or (subscription.tracking_reference if subscription else "")
    fingerprint = build_event_fingerprint(
        team_id=team.pk,
        provider_code=provider.code,
        source_event_id=normalised.raw_event_id,
        reference=reference,
        carrier_event_type=normalised.event_type,
        event_code=normalised.event_code,
        event_time_type=normalize_event_time_type(normalised.event_classifier),
        event_datetime=normalised.event_datetime,
        location_unlocode=normalised.location_unlocode,
        location_name=normalised.location_name,
        vessel_imo=normalised.vessel_imo,
        vessel_name=normalised.vessel_name,
        voyage_number=normalised.voyage_number,
    )
    defaults = build_event_defaults(
        normalised,
        subscription=subscription,
        shipment=shipment,
        container=container,
        raw_payload=raw_payload,
    )
    return upsert_event(team=team, provider=provider, fingerprint=fingerprint, defaults=defaults)


def upsert_event(
    *,
    team: Team,
    provider: TrackingProvider,
    fingerprint: str,
    defaults: dict,
) -> tuple[TrackingEvent, bool]:
    """Create or refresh the event identified by ``fingerprint``.

    This is the single write path for tracking events: every caller goes through
    it so deduplication and concurrency behaviour cannot diverge between the
    carrier pipeline and other ingestion sources.
    """
    try:
        with transaction.atomic():
            event, created = TrackingEvent.objects.get_or_create(
                team=team,
                provider=provider,
                event_fingerprint=fingerprint,
                defaults=defaults,
            )
    except IntegrityError:
        # A concurrent worker inserted the same event between our check and write.
        event = TrackingEvent.objects.get(team=team, provider=provider, event_fingerprint=fingerprint)
        return event, False

    if not created:
        _update_existing_event(event, defaults)
    return event, created


# Fields refreshed when a carrier re-sends an event we already have. The links to
# shipment/container/subscription are only filled in, never cleared, so a later
# payload that lacks them cannot orphan an event.
_REFRESHABLE_FIELDS = (
    "event_type",
    "carrier_event_type",
    "event_code",
    "event_time_type",
    "status",
    "description",
    "carrier_description",
    "location_name",
    "location_unlocode",
    "location_latitude",
    "location_longitude",
    "vessel_name",
    "vessel_imo",
    "voyage_number",
    "transport_mode",
    "equipment_reference",
    "event_datetime",
    "event_timezone",
    "received_at",
    "source_event_id",
    "raw_data",
)
_LINK_FIELDS = ("shipment", "container", "subscription", "raw_payload")


def _update_existing_event(event: TrackingEvent, defaults: dict) -> None:
    """Refresh a known event with the carrier's latest version of it."""
    changed: list[str] = []
    for name in _REFRESHABLE_FIELDS:
        if name in defaults and getattr(event, name) != defaults[name]:
            setattr(event, name, defaults[name])
            changed.append(name)
    for name in _LINK_FIELDS:
        if defaults.get(name) is not None and getattr(event, f"{name}_id") is None:
            setattr(event, name, defaults[name])
            changed.append(name)
    if changed:
        event.save(update_fields=[*changed, "updated_at"])


def persist_normalised_events(
    *,
    team: Team,
    provider: TrackingProvider,
    events: list[NormalisedTrackingEvent],
    subscription: TrackingSubscription | None = None,
    shipment=None,
    container=None,
    raw_payload: TrackingRawPayload | None = None,
) -> dict:
    """Store a batch of normalised events.

    Returns {"created": int, "updated": int}. One malformed event does not stop the
    rest of the batch — it is logged and counted as failed.
    """
    created = 0
    updated = 0
    failed = 0
    for normalised in events:
        try:
            _event, was_created = persist_normalised_event(
                team=team,
                provider=provider,
                normalised=normalised,
                subscription=subscription,
                shipment=shipment,
                container=container,
                raw_payload=raw_payload,
            )
        except Exception:  # noqa: BLE001 — one bad event must not lose the others
            failed += 1
            logger.warning(
                "Could not persist tracking event for provider=%s reference=%s",
                provider.code,
                normalised.container_number or normalised.raw_event_id,
                exc_info=True,
            )
            continue
        if was_created:
            created += 1
        else:
            updated += 1
    return {"created": created, "updated": updated, "failed": failed}
