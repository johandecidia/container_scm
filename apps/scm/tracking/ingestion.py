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

Location resolution
    The reported place is handed to
    :func:`apps.scm.containers.location_resolver.resolve_location` and the answer is
    stored alongside the carrier's own wording. This is the right seam for it
    because it is the *only* path carrier events take: Maersk, CMA CGM, MSC, Traqo
    and Vizion all arrive here as a ``NormalisedTrackingEvent``, so one wiring
    covers every provider and no adapter needs a place-name rule of its own.

    Resolution never blocks ingestion. An unresolvable location is ordinary — a
    carrier naming a facility MCR has never recorded — and the event is stored with
    its evidence intact and no canonical link. A resolver that raised would lose
    real tracking data over master data that is merely incomplete, so it is called
    defensively and a failure costs the link, not the event.

Physical interpretation
    A stored event is offered to
    :func:`apps.scm.tracking.physical_movements.interpret_tracking_event_safely`,
    which decides — for the very few event types that say so unambiguously — whether
    it also describes a physical movement.

    This module does not decide that, and must not. Ingestion's job is to record
    what a carrier said; whether what it said becomes MCR's belief about where a box
    is belongs to one place, and that place is the interpretation layer. The call is
    here only because this is the single write path every provider passes through.
"""

from __future__ import annotations

import hashlib
import logging
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING

from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.scm.containers.location_resolver import LocationQuery, LocationResolution, resolve_location

from .models import TrackingEvent
from .physical_movements import interpret_tracking_event_safely
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


def build_location_query(normalised: NormalisedTrackingEvent, *, provider_code: str) -> LocationQuery:
    """Describe a normalised event's place in the resolver's own terms.

    The translation from a carrier DTO to a :class:`LocationQuery` happens here and
    only here, which is what keeps the resolver free of every provider's schema.

    ``facility_name`` stands in when there is no ``location_name`` — the same
    precedence ``build_event_defaults`` uses for the stored ``location_name``, so
    the string that gets resolved is the string that gets displayed.
    """
    return LocationQuery(
        source=provider_code,
        name=normalised.location_name or normalised.facility_name,
        unlocode=normalised.location_unlocode,
        latitude=_coordinate(normalised.latitude),
        longitude=_coordinate(normalised.longitude),
    )


def resolve_event_location(
    *,
    team: Team,
    provider_code: str,
    normalised: NormalisedTrackingEvent,
) -> LocationResolution:
    """Resolve a normalised event's location, never raising.

    A resolver failure must not cost the event. Tracking evidence is the thing that
    cannot be recovered — the carrier will not re-send it — whereas a missing
    canonical link is repaired the next time the event is refreshed or re-parsed.
    """
    try:
        return resolve_location(team, build_location_query(normalised, provider_code=provider_code))
    except Exception:  # noqa: BLE001 — a resolver fault must not lose tracking evidence
        logger.warning(
            "Could not resolve location %r for provider=%s; storing the event unresolved.",
            normalised.location_name or normalised.facility_name,
            provider_code,
            exc_info=True,
        )
        return LocationResolution()


def build_event_defaults(
    normalised: NormalisedTrackingEvent,
    *,
    subscription: TrackingSubscription | None = None,
    shipment=None,
    container=None,
    raw_payload: TrackingRawPayload | None = None,
    received_at=None,
    resolution: LocationResolution | None = None,
) -> dict:
    """Map a NormalisedTrackingEvent onto TrackingEvent field values.

    Keeps both the internal classification and the carrier's own wording, so a gap
    in the mapping tables never destroys what the carrier reported.

    ``resolution`` is passed in rather than computed here so this stays a pure
    mapper with no queries in it. Omitting it leaves the canonical location fields
    untouched, which is what a caller that has not resolved anything should do —
    writing "unresolved" without having looked would be a claim, not an absence.
    """
    event_time_type = normalize_event_time_type(normalised.event_classifier)
    location_fields = {}
    if resolution is not None:
        location_fields = {
            "location": resolution.location,
            "location_resolution_status": resolution.status,
            "location_resolution_method": resolution.method,
        }
    return {
        **location_fields,
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

    A stored event is then offered to the physical interpretation layer, on refresh
    as well as on first sight — a carrier that corrects an event's place, or an
    operator who records the alias that finally resolves it, should see the movement
    appear on the next sync rather than never.
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
        resolution=resolve_event_location(team=team, provider_code=provider.code, normalised=normalised),
    )
    event, created = upsert_event(team=team, provider=provider, fingerprint=fingerprint, defaults=defaults)
    interpret_tracking_event_safely(team, event)
    return event, created


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
    "location_resolution_status",
    "location_resolution_method",
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

    # The canonical location is re-derived rather than merely filled in, and it is
    # the one link allowed to be cleared. It is a function of the event's evidence
    # and the current alias table, so an operator who records an alias sees the next
    # refresh pick it up — and one who removes a wrong alias sees the wrong link go,
    # which is the whole point of having recorded it explicitly. Compared by id so a
    # refresh does not fetch the related row to find out nothing changed.
    if "location" in defaults:
        new_location = defaults["location"]
        new_location_id = new_location.pk if new_location is not None else None
        if event.location_id != new_location_id:
            event.location = new_location
            changed.append("location")

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

    Returns {"created": int, "updated": int, "failed": int, "fingerprints": list[str]}.
    One malformed event does not stop the rest of the batch — it is logged and counted
    as failed.

    ``fingerprints`` are the events this batch actually wrote. A re-parse needs them to
    tell an event it replaced from one it merely left alone; nothing else reads them.
    """
    created = 0
    updated = 0
    failed = 0
    fingerprints: list[str] = []
    for normalised in events:
        try:
            event, was_created = persist_normalised_event(
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
        fingerprints.append(event.event_fingerprint)
        if was_created:
            created += 1
        else:
            updated += 1
    return {"created": created, "updated": updated, "failed": failed, "fingerprints": fingerprints}
