"""Reading Traqo's shipment-level ETA as a provider observation.

Traqo states one ETA for the whole shipment, in ``data.eta``, and it is **not** an
event: the production benchmark's ``events_table`` contained ten rows and every one was
``is_actual: 1``. So there is nothing to classify as a forecast and nothing to
fingerprint — synthesising an event from the field would invent a movement Traqo never
reported. It is read here as a provider ETA observation instead, and the canonical ETA
layer decides what to do with it.

What Traqo's ``eta`` is an ETA *for* is not stated, and the benchmark says not to guess:
its value equalled the last event in the list — the empty container's return to the
Gothenburg depot — not the vessel's arrival at the POD eleven days earlier. It is
therefore recorded as provider-defined.

The timestamp is the same naive local string the events use, so it goes through the same
chain: the destination's row in ``locations_table``, its published IANA zone, then UTC.
Where that chain breaks the value is kept as sent and the reason travels with it, which
is exactly what happened on the benchmark container — its destination, BORAAS, publishes
no timezone.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from apps.scm.tracking.eta_observations import ETA_TARGET_PROVIDER_DEFINED, ProviderEtaObservation

from . import PROVIDER_CODE
from .mapper import TZ_OFFSET_SUPPLIED, _index_locations, _location_zone, _parse_timestamp

logger = logging.getLogger(__name__)

# Provider fields worth keeping next to the forecast, because they change what the
# forecast means. `eta_warning` and `eta_reliable` are Traqo's own caveats; `status` and
# `is_delayed` are its view of the journey, which is not Container SCM's view of it.
_CONTEXT_FIELDS = ("eta_warning", "status", "is_delayed", "is_active", "shipment_uid")


def _destination_location_id(data: dict, locations: dict[str, dict]) -> str | None:
    """Return the ``location_id`` of the place Traqo names as the destination.

    Matched by name, because ``destination`` is a label and carries no id. The last row
    of ``locations_table`` is not assumed to be it: row order is Traqo's, not a promise.
    """
    wanted = str(data.get("destination") or "").strip().casefold()
    if not wanted:
        return None
    for location_id, location in locations.items():
        if str(location.get("location") or "").strip().casefold() == wanted:
            return location_id
    logger.debug("Traqo: destination %r is not listed in locations_table.", data.get("destination"))
    return None


def read_traqo_eta_observation(payload: dict, *, observed_at: datetime) -> ProviderEtaObservation | None:
    """Read one Traqo response's ETA, or None when it states none.

    ``observed_at`` is when Container SCM received the response. It is passed in rather
    than read from the clock so the caller's one ingest has one observation time, and so
    a re-read of a stored payload can say when the payload was actually received.
    """
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    if not isinstance(data, dict):
        return None

    parsed, has_offset = _parse_timestamp(data.get("eta"))
    if parsed is None:
        return None

    locations = _index_locations(data)
    location_id = _destination_location_id(data, locations)
    location = locations.get(location_id or "") or {}

    if has_offset:
        eta_at = parsed.astimezone(UTC)
        zone_name, status = str(parsed.tzinfo or ""), TZ_OFFSET_SUPPLIED
    else:
        zone, zone_name, status = _location_zone({"location_id": location_id}, locations)
        # No zone claimed for the destination, so the instant is kept as Traqo sent it
        # rather than shifted by a guessed offset — the same rule the events follow.
        eta_at = parsed.replace(tzinfo=zone or UTC).astimezone(UTC)

    context = {key: data.get(key) for key in _CONTEXT_FIELDS if data.get(key) is not None}
    context["provider_timestamp"] = str(data.get("eta"))
    context["timezone"] = zone_name
    context["timezone_status"] = status

    return ProviderEtaObservation(
        provider_code=PROVIDER_CODE,
        observed_at=observed_at,
        eta_at=eta_at,
        # The provider's local date, which is the date it means whether or not its zone
        # could be resolved.
        eta_date=parsed.date(),
        target=ETA_TARGET_PROVIDER_DEFINED,
        target_name=str(data.get("destination") or ""),
        target_unlocode=str(location.get("locode") or ""),
        provider_updated_at=str(data.get("last_updated_at") or ""),
        reliable=data.get("eta_reliable") if isinstance(data.get("eta_reliable"), bool) else None,
        context=context,
    )
