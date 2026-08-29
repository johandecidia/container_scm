"""A T0 observation of both providers, in a form a later run can be compared against.

The Phase 2.2 experiment is a difference over time, and a difference needs a first term.
This module produces it: everything about one container's ETA and forecast state, from
both providers, at one instant — written so a second run weeks later can be subtracted
from it by :mod:`.drift` without either run having to know the other's shape.

Three rules govern what goes in.

*Clocks are kept apart, never subtracted into a single "latency".* An event's own time,
the provider's ``last_updated_at`` and ``last_synced_at``, and the time Container SCM
received the row are four different facts. On a first observation the last of them
records when the experiment started, so it cannot be differenced against a backfilled
event's timestamp and called provider lag. The snapshot stores all four and
:mod:`.metrics` marks the first observation as such.

*Provider event identity is preserved verbatim, and not adopted.* Traqo's ``idx``,
``event_id``, ``name``, ``creation`` and ``modified`` are recorded per event so a repeat
fetch can show whether any of them is stable. Phase 2.1 left ``source_event_id``
deliberately unresolved and this does not change it — collecting the evidence is not the
same as acting on it.

*Structure is counted, not ingested.* ``eta_history_table``, ``voyage_plan_table`` and
``route_json`` are summarised so their usefulness can be argued about later. Container
SCM stays the owner of its own ETA history; none of these becomes a canonical row here.

No credentials, no API key, no headers, and no full provider payload: the response
already lives in ``TrackingRawPayload``, and a benchmark file is not a second copy of it.
"""

from __future__ import annotations

import json
from datetime import datetime

from apps.scm.tracking.models import TrackingEvent

from .eta_target import compare_etas, read_carrier_eta_target, read_traqo_eta_target

# How many events to keep per provider. Enough to see the shape of a journey and its
# forecasts; not a second copy of the timeline, which is in the database already.
_EVENT_LIMIT = 40

# The provider fields that change what an ETA means, kept next to it.
_TRAQO_ETA_CONTEXT = (
    "eta",
    "eta_reliable",
    "eta_warning",
    "remaining_days",
    "total_days",
    "status",
    "is_delayed",
    "is_active",
    "destination",
    "origin",
    "last_updated_at",
    "last_synced_at",
    "closed_at",
)

# Traqo's per-event identity fields — the free experiment of section 17. Recorded, not
# used: whether any of them survives a refetch is unknown until one happens.
_IDENTITY_FIELDS = ("idx", "event_id", "name", "creation", "modified")


def _iso(value) -> str | None:
    return value.isoformat() if value else None


def _event_as_dict(event: TrackingEvent) -> dict:
    """One canonical event, as the fields a later run needs to recognise it by."""
    return {
        "provider": event.provider.code,
        "event_type": event.event_type,
        "event_time_type": event.event_time_type,
        "event_code": event.event_code,
        "carrier_reference": event.carrier_reference,
        "event_datetime": _iso(event.event_datetime),
        "received_at": _iso(event.received_at),
        "location_name": event.location_name,
        "location_unlocode": event.location_unlocode,
        "vessel_name": event.vessel_name,
        "voyage_number": event.voyage_number,
        "transport_mode": event.transport_mode,
        "description": event.description,
        # The fingerprint the drift comparison pairs runs on. Not an identity Traqo
        # supplied — a hash of what it said, which is the only stable handle available
        # until a refetch proves otherwise.
        "fingerprint": event_fingerprint(event),
    }


def event_fingerprint(event: TrackingEvent) -> str:
    """Return a run-to-run handle for one canonical event.

    Field-based on purpose. Neither the database id nor Traqo's ``name`` can serve:
    the first is local and says nothing about whether the provider changed its mind,
    and the second appears to be reassigned when Traqo rebuilds its child rows. Time,
    milestone and place are what a human would use to say "that is the same event",
    so a corrected timestamp shows up as a new fingerprint — which is exactly what the
    comparison needs to notice.
    """
    return "|".join(
        [
            event.provider.code,
            event.event_type,
            event.event_time_type,
            event.event_datetime.isoformat() if event.event_datetime else "",
            (event.location_name or "").strip().upper(),
        ]
    )


def _provider_events(events: list[TrackingEvent]) -> dict:
    """Split one provider's events into what it observed and what it forecasts."""
    actual = [event for event in events if event.event_time_type == TrackingEvent.EventTimeType.ACTUAL]
    forecast = [event for event in events if event.event_time_type != TrackingEvent.EventTimeType.ACTUAL]
    dated_actual = [event for event in actual if event.event_datetime is not None]
    latest_actual = max(dated_actual, key=lambda event: event.event_datetime) if dated_actual else None

    return {
        "total": len(events),
        "actual": len(actual),
        "forecast": len(forecast),
        "latest_actual": _event_as_dict(latest_actual) if latest_actual else None,
        # Forecasts in itinerary order: this is the list section 11 compares between
        # providers, so it must read as a plan and not as a query result.
        "forecast_events": [
            _event_as_dict(event)
            for event in sorted(
                forecast,
                key=lambda event: (event.event_datetime is None, event.event_datetime or datetime.min),
            )[:_EVENT_LIMIT]
        ],
        "events": [
            _event_as_dict(event)
            for event in sorted(
                events,
                key=lambda event: (event.event_datetime is None, event.event_datetime or datetime.min),
            )[:_EVENT_LIMIT]
        ],
    }


def _traqo_structure(data: dict) -> dict:
    """Count the tables Container SCM does not ingest, so their value can be argued later."""
    eta_history = [row for row in (data.get("eta_history_table") or []) if isinstance(row, dict)]
    voyage_plan = [row for row in (data.get("voyage_plan_table") or []) if isinstance(row, dict)]
    logged = sorted(str(row.get("logged_at") or "") for row in eta_history if row.get("logged_at"))

    route = data.get("route_json")
    if isinstance(route, str):
        try:
            route = json.loads(route)
        except ValueError, TypeError:
            route = None
    segments = [segment for segment in (route or []) if isinstance(segment, dict)]

    return {
        "eta_history_table": {
            "rows": len(eta_history),
            "earliest_logged_at": logged[0] if logged else None,
            "latest_logged_at": logged[-1] if logged else None,
            "distinct_eta_values": sorted({str(row.get("eta")) for row in eta_history if row.get("eta")}),
            # One row, or several rows all logged at the fetch instant, is a snapshot of
            # the current ETA rather than a record of how it moved.
            "appears_to_be_a_snapshot": len({str(row.get("eta")) for row in eta_history if row.get("eta")}) <= 1,
            "fields": sorted(eta_history[0].keys()) if eta_history else [],
        },
        "voyage_plan_table": {
            "rows": len(voyage_plan),
            "phases": [str(row.get("phase") or "") for row in voyage_plan],
            "forecast_rows": sum(1 for row in voyage_plan if row.get("is_actual") in (0, False, "0")),
            "rows_with_predictive_eta": sum(1 for row in voyage_plan if row.get("predictive_eta")),
            "entries": [
                {
                    "phase": row.get("phase"),
                    "date": row.get("date"),
                    "is_actual": row.get("is_actual"),
                    "predictive_eta": row.get("predictive_eta"),
                    "location_id": row.get("location_id"),
                    "location": _location_name(data, row.get("location_id")),
                    "timezone": _location_timezone(data, row.get("location_id")),
                }
                for row in voyage_plan
            ],
        },
        "route_json": {
            "present": bool(segments),
            "segments": len(segments),
            "segment_types": [str(segment.get("type") or "") for segment in segments],
            # route_json is the only place Traqo publishes UN/LOCODEs; locations_table
            # does not carry them, which is why every Traqo event reaches TrackingEvent
            # without one.
            "supplies_unlocodes": any(
                (segment.get(end) or {}).get("locode") for segment in segments for end in ("from", "to")
            ),
        },
        "events_table": {
            "rows": len(data.get("events_table") or []),
            "forecast_rows": sum(
                1
                for row in (data.get("events_table") or [])
                if isinstance(row, dict) and row.get("is_actual") in (0, False, "0")
            ),
        },
        "locations_table": {
            "rows": len(data.get("locations_table") or []),
            "rows_without_timezone": sum(
                1 for row in (data.get("locations_table") or []) if isinstance(row, dict) and not row.get("timezone")
            ),
        },
    }


def _location_name(data: dict, location_id) -> str:
    for location in data.get("locations_table") or []:
        if isinstance(location, dict) and str(location.get("location_id")) == str(location_id):
            return str(location.get("location") or "")
    return ""


def _location_timezone(data: dict, location_id) -> str:
    for location in data.get("locations_table") or []:
        if isinstance(location, dict) and str(location.get("location_id")) == str(location_id):
            return str(location.get("timezone") or "")
    return ""


def _identity_sample(data: dict) -> list[dict]:
    """Preserve each Traqo event's identity fields, for the repeat-fetch comparison."""
    sample = []
    for row in data.get("events_table") or []:
        if not isinstance(row, dict):
            continue
        entry = {field: row.get(field) for field in _IDENTITY_FIELDS}
        # Enough context to tell which event an identity belonged to, so a changed
        # `name` can be attributed rather than merely counted.
        entry["timestamp"] = row.get("timestamp")
        entry["event_code"] = row.get("event_code")
        entry["location"] = row.get("location")
        sample.append(entry)
    return sample[:_EVENT_LIMIT]


def build_snapshot(
    *,
    result,
    candidate_payload: dict | None,
    reference_events: list[TrackingEvent],
    candidate_events: list[TrackingEvent],
    journey_state: str,
    reference_subscription=None,
    candidate_subscription=None,
    eta_history_rows: list[dict] | None = None,
) -> dict:
    """Build the T0 snapshot for one comparison run.

    ``candidate_payload`` may be None when the run did not fetch (``--no-fetch`` with
    nothing stored). The provider-payload sections are then omitted rather than filled
    with zeros, so an absent observation cannot be misread as an empty one.
    """
    data = (candidate_payload or {}).get("data") if isinstance(candidate_payload, dict) else None
    data = data if isinstance(data, dict) else {}

    reference_eta_event = _canonical_eta_event(result, reference_events)
    reference_target = read_carrier_eta_target(reference_eta_event)
    candidate_target = read_traqo_eta_target(candidate_payload or {})

    candidate_eta_at = result.eta.candidate.eta_at
    provider_eta_at = None
    if candidate_payload:
        from ..eta import read_traqo_eta_observation

        observation = read_traqo_eta_observation(candidate_payload, observed_at=result.run_at)
        provider_eta_at = observation.eta_at if observation else None

    comparison = compare_etas(
        reference_provider=result.reference_provider_code,
        candidate_provider=result.candidate_provider_code,
        reference_eta_at=result.eta.reference.eta_at,
        # The top-level provider ETA where there is one: it is the value section 8 asks
        # about, and it is not an event, so it never appears in the event-derived ETA.
        candidate_eta_at=provider_eta_at or candidate_eta_at,
        reference_target=reference_target.target,
        candidate_target=candidate_target.target,
    )

    snapshot = {
        "schema": SNAPSHOT_SCHEMA,
        "run_at": _iso(result.run_at),
        "container": result.container_number,
        "team": result.team_slug,
        "mode": result.mode,
        "sealine": result.sealine,
        "journey_state": journey_state,
        "reference": {
            "provider": result.reference_provider_code,
            **_provider_events(reference_events),
            "current_eta_at": _iso(result.eta.reference.eta_at),
            "eta_event_time_type": result.eta.reference.event_time_type,
            "eta_location_name": result.eta.reference.location_name,
            "eta_location_unlocode": result.eta.reference.location_unlocode,
            "eta_source_event_code": result.eta.reference.source_event_code,
            "eta_received_at": _iso(result.eta.reference.received_at),
            "eta_target": reference_target.as_dict(),
            "last_synced_at": _iso(getattr(reference_subscription, "last_synced_at", None)),
            "subscription_status": getattr(reference_subscription, "status", ""),
            "tracking_status": getattr(reference_subscription, "tracking_status", ""),
        },
        "candidate": {
            "provider": result.candidate_provider_code,
            **_provider_events(candidate_events),
            "provider_eta_at": _iso(provider_eta_at),
            "event_derived_eta_at": _iso(candidate_eta_at),
            "eta_target": candidate_target.as_dict(),
            "provider_context": {field: data.get(field) for field in _TRAQO_ETA_CONTEXT if field in data},
            "last_synced_at": _iso(getattr(candidate_subscription, "last_synced_at", None)),
            "subscription_status": getattr(candidate_subscription, "status", ""),
            "tracking_status": getattr(candidate_subscription, "tracking_status", ""),
            "observed": bool(candidate_payload),
        },
        "eta_comparison": comparison.as_dict(),
        "event_match": result.match_summary.as_dict(),
        "freshness": result.freshness.as_dict(),
        "canonical_eta_history": eta_history_rows or [],
        "provider_structure": _traqo_structure(data) if candidate_payload else None,
        "candidate_event_identity": _identity_sample(data) if candidate_payload else [],
    }
    return snapshot


# Bumped when the snapshot's shape changes in a way a previous file cannot be read
# against. The drift comparison checks it rather than guessing.
SNAPSHOT_SCHEMA = "traqo-benchmark-snapshot/1"


def _canonical_eta_event(result, reference_events: list[TrackingEvent]) -> TrackingEvent | None:
    """Return the reference provider's canonical forecast event, from the run's own ETA.

    Matched back out of the already-computed ``ProviderEta`` rather than re-queried, so
    the snapshot's ETA target cannot describe a different event than its ETA.
    """
    eta_at = result.eta.reference.eta_at
    if eta_at is None:
        return None
    for event in reference_events:
        if event.event_datetime == eta_at and event.event_time_type == result.eta.reference.event_time_type:
            return event
    return None
