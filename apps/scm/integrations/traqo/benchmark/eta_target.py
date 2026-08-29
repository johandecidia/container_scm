"""What a provider's ETA is an ETA *for*, and whether two providers' ETAs can be subtracted.

An ETA is meaningless without its target. "14 July" is a different claim if it means the
vessel berthing at Gothenburg, the box reaching an inland yard in Borås, or the empty
returning to a depot — and those three are eleven days apart on the one production
journey Container SCM has measured. Subtracting two providers' ETAs without first
establishing that they forecast the same milestone produces a number that looks like
disagreement and is actually a category error.

So this module answers two questions and refuses to answer a third.

**What does Traqo's ``data.eta`` target?** Decided from Traqo's own payload only, by
testing the value against every milestone Traqo itself publishes: the POD phase in
``voyage_plan_table``, the post-POD phase, the named ``destination``, and the last entry
in ``events_table``. Maersk is never consulted — using the reference provider to supply
the candidate's missing semantics would make the candidate look as though it had stated
something it did not, which is the one result this experiment must not manufacture.

**Are two ETAs comparable?** Only when both name the same target *and* that target is
something more specific than "the provider did not say". Two PROVIDER_DEFINED ETAs are
not comparable to each other: neither has said what it means, and matching ignorance is
not agreement.

**What is the true arrival time?** Not answered. Nothing here reconciles, averages or
prefers a provider.

Evidence from the one production payload available (CPWU2588297, a DELIVERED journey)::

    data.eta                 2026-07-13 13:15:00
    voyage_plan pod          2026-07-01 15:09:00   @ Goteborg      (11.9 days earlier)
    voyage_plan postpod      2026-07-13 09:30:12   @ BORAAS        (3h45m earlier)
    destination              BORAAS
    last events_table row    2026-07-13 13:15:00   @ Goteborg  GTIN/CER  (exact match)

The exact match is with the empty container's return to the Gothenburg depot — not the
POD arrival, and not the inland destination Traqo itself names. On a finished journey
``data.eta`` has become a restatement of the final event, so it classifies as
PROVIDER_DEFINED: Traqo supplies a value and does not say what future milestone it
forecasts. Whether an *active* journey behaves the same way is exactly what a live
observation is needed to establish, and this module reports UNKNOWN rather than guessing
when the payload gives it nothing to test against.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

# The vocabulary Phase 2.2 asks for. Deliberately coarse: a finer scheme would imply
# the providers make finer statements than they do.
PORT_ARRIVAL = "PORT_ARRIVAL"  # the vessel reaching the port of discharge
FINAL_DESTINATION = "FINAL_DESTINATION"  # arrival at the last place on the itinerary
POST_POD_DELIVERY = "POST_POD_DELIVERY"  # a post-discharge inland leg, short of the final place
PROVIDER_DEFINED = "PROVIDER_DEFINED"  # the provider supplies a value and does not say what it means
UNKNOWN = "UNKNOWN"  # not enough in the payload to test anything

# Targets specific enough that two providers naming the same one are talking about the
# same milestone. PROVIDER_DEFINED and UNKNOWN are not: they record an absence.
_COMPARABLE_TARGETS = (PORT_ARRIVAL, FINAL_DESTINATION, POST_POD_DELIVERY)

# How close ``data.eta`` must sit to a published milestone to be called that milestone.
# One hour, not one day: the production payload's candidates are 3h45m and 11.9 days
# apart, so a day-wide window would have matched the wrong one.
MATCH_TOLERANCE = timedelta(hours=1)

# Voyage-plan phase names Traqo uses, in itinerary order.
PHASE_POD = "pod"
PHASE_POST_POD = "postpod"


@dataclass(frozen=True)
class EtaTargetReading:
    """What a provider's ETA appears to target, and the evidence for saying so."""

    target: str
    reason: str
    # Every milestone the value was tested against, so the classification can be
    # audited without re-reading the payload.
    candidates: dict[str, str | None]
    matched_milestone: str = ""
    matched_location: str = ""

    @property
    def is_specific(self) -> bool:
        """True when the target is precise enough to compare against another provider's."""
        return self.target in _COMPARABLE_TARGETS

    def as_dict(self) -> dict:
        return {
            "target": self.target,
            "reason": self.reason,
            "matched_milestone": self.matched_milestone,
            "matched_location": self.matched_location,
            "is_specific": self.is_specific,
            "tested_against": self.candidates,
        }


def _parse(value) -> datetime | None:
    from ..mapper import _parse_timestamp

    parsed, _ = _parse_timestamp(value)
    return parsed


def _location_name(data: dict, location_id) -> str:
    for location in data.get("locations_table") or []:
        if isinstance(location, dict) and str(location.get("location_id")) == str(location_id):
            return str(location.get("location") or "")
    return ""


def _voyage_phase(data: dict, phase: str) -> tuple[datetime | None, str]:
    """Return (date, place) for a named voyage-plan phase, or (None, "")."""
    for row in data.get("voyage_plan_table") or []:
        if isinstance(row, dict) and str(row.get("phase") or "").strip().lower() == phase:
            # predictive_eta, where Traqo supplies one, is its forecast for the phase;
            # `date` is what it has settled on. The forecast is preferred because that
            # is what an ETA would be compared against on an unfinished leg.
            when = _parse(row.get("predictive_eta")) or _parse(row.get("date"))
            return when, _location_name(data, row.get("location_id"))
    return None, ""


def _last_event(data: dict) -> tuple[datetime | None, str, str]:
    """Return (time, place, code) of the chronologically last event Traqo lists."""
    latest: tuple[datetime, str, str] | None = None
    for row in data.get("events_table") or []:
        if not isinstance(row, dict):
            continue
        when = _parse(row.get("timestamp"))
        if when is None:
            continue
        entry = (when, str(row.get("location") or ""), str(row.get("event_code") or ""))
        if latest is None or when > latest[0]:
            latest = entry
    return latest if latest else (None, "", "")


def read_traqo_eta_target(payload: dict) -> EtaTargetReading:
    """Classify what Traqo's ``data.eta`` targets, from Traqo's own payload only.

    Tested in itinerary order — POD, then post-POD, then the named destination's own
    voyage phase — so a value that coincides with two milestones is attributed to the
    earlier, more conservative one rather than the more flattering one.
    """
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    if not isinstance(data, dict):
        return EtaTargetReading(UNKNOWN, "the payload has no shipment data object", {})

    eta = _parse(data.get("eta"))
    pod_at, pod_place = _voyage_phase(data, PHASE_POD)
    post_pod_at, post_pod_place = _voyage_phase(data, PHASE_POST_POD)
    last_at, last_place, last_code = _last_event(data)
    destination = str(data.get("destination") or "").strip()

    candidates = {
        "data.eta": _iso(eta),
        "voyage_plan.pod": _iso(pod_at),
        "voyage_plan.pod_location": pod_place or None,
        "voyage_plan.postpod": _iso(post_pod_at),
        "voyage_plan.postpod_location": post_pod_place or None,
        "destination": destination or None,
        "last_event": _iso(last_at),
        "last_event_location": last_place or None,
        "last_event_code": last_code or None,
    }

    if eta is None:
        return EtaTargetReading(UNKNOWN, "Traqo stated no eta", candidates)

    if pod_at is not None and abs(eta - pod_at) <= MATCH_TOLERANCE:
        return EtaTargetReading(
            PORT_ARRIVAL,
            f"eta matches the voyage plan's pod phase at {pod_place or 'an unnamed place'} "
            f"within {_hours(MATCH_TOLERANCE)}",
            candidates,
            matched_milestone="voyage_plan.pod",
            matched_location=pod_place,
        )

    if post_pod_at is not None and abs(eta - post_pod_at) <= MATCH_TOLERANCE:
        # The post-POD phase is the final place only when Traqo names it as the
        # destination. Otherwise it is an intermediate inland leg, and saying
        # FINAL_DESTINATION would claim more than the payload supports.
        is_final = bool(destination) and _same_place(post_pod_place, destination)
        return EtaTargetReading(
            FINAL_DESTINATION if is_final else POST_POD_DELIVERY,
            f"eta matches the voyage plan's postpod phase at {post_pod_place or 'an unnamed place'}"
            + (f", which Traqo names as the destination ({destination})" if is_final else ""),
            candidates,
            matched_milestone="voyage_plan.postpod",
            matched_location=post_pod_place,
        )

    if last_at is not None and abs(eta - last_at) <= MATCH_TOLERANCE:
        return EtaTargetReading(
            PROVIDER_DEFINED,
            f"eta restates the last event Traqo lists ({last_code or 'no code'} at "
            f"{last_place or 'an unnamed place'}), not a published future milestone — so Traqo "
            "supplies a value without saying what it forecasts",
            candidates,
            matched_milestone="events_table.last",
            matched_location=last_place,
        )

    return EtaTargetReading(
        PROVIDER_DEFINED,
        "eta matches no milestone Traqo publishes — neither voyage plan phase nor its own "
        "last event — so what it forecasts cannot be established from this payload",
        candidates,
    )


def read_carrier_eta_target(event) -> EtaTargetReading:
    """Classify what a carrier's canonical forecast event targets, from its own type.

    A carrier ETA reaches Container SCM as a ``TrackingEvent``, so unlike Traqo's bare
    ``data.eta`` it arrives already classified. VESSEL_ARRIVED states that a vessel
    reaches the named place, which is a port arrival and nothing broader. ETA_UPDATED
    states only that the arrival estimate moved, without saying which arrival, so it
    stays PROVIDER_DEFINED rather than being read as a POD arrival by association.
    """
    from apps.scm.tracking.models import TrackingEvent

    if event is None:
        return EtaTargetReading(UNKNOWN, "no canonical forecast event for this provider", {})

    candidates = {
        "event_type": event.event_type,
        "event_time_type": event.event_time_type,
        "event_code": event.carrier_reference or event.event_code or None,
        "location": event.location_name or None,
        "unlocode": event.location_unlocode or None,
        "event_datetime": _iso(event.event_datetime),
    }

    if event.event_type == TrackingEvent.EventType.VESSEL_ARRIVED:
        return EtaTargetReading(
            PORT_ARRIVAL,
            f"a forecast vessel arrival at {event.location_name or 'an unnamed port'}"
            + (f" ({event.location_unlocode})" if event.location_unlocode else ""),
            candidates,
            matched_milestone="vessel_arrived",
            matched_location=event.location_name,
        )

    return EtaTargetReading(
        PROVIDER_DEFINED,
        f"the forecast arrives as {event.event_type}, which states that an arrival estimate "
        "exists without saying which milestone it is for",
        candidates,
        matched_milestone=event.event_type,
        matched_location=event.location_name,
    )


def _same_place(left: str, right: str) -> bool:
    from .matching import _fold

    return bool(left) and bool(right) and _fold(left) == _fold(right)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _hours(delta: timedelta) -> str:
    return f"{int(delta.total_seconds() // 3600)}h"


@dataclass(frozen=True)
class EtaComparison:
    """Whether two providers' ETAs may be subtracted, and the result if they may."""

    reference_provider: str
    candidate_provider: str
    reference_target: str
    candidate_target: str
    reference_eta_at: datetime | None = None
    candidate_eta_at: datetime | None = None
    comparable: bool = False
    verdict: str = ""
    difference_hours: float | None = None

    def as_dict(self) -> dict:
        return {
            "reference_provider": self.reference_provider,
            "candidate_provider": self.candidate_provider,
            "reference_target": self.reference_target,
            "candidate_target": self.candidate_target,
            "reference_eta_at": _iso(self.reference_eta_at),
            "candidate_eta_at": _iso(self.candidate_eta_at),
            "comparable": self.comparable,
            "verdict": self.verdict,
            "difference_hours": self.difference_hours,
        }


NOT_COMPARABLE_DIFFERENT_TARGETS = "NOT COMPARABLE — DIFFERENT ETA TARGETS"
NOT_COMPARABLE_UNKNOWN_TARGET = "NOT COMPARABLE — AT LEAST ONE ETA TARGET IS UNSTATED"
NOT_COMPARABLE_MISSING_ETA = "NOT COMPARABLE — AT LEAST ONE PROVIDER GAVE NO ETA"
COMPARABLE = "COMPARABLE — SAME TARGET"


def compare_etas(
    *,
    reference_provider: str,
    candidate_provider: str,
    reference_eta_at: datetime | None,
    candidate_eta_at: datetime | None,
    reference_target: str,
    candidate_target: str,
) -> EtaComparison:
    """Decide whether two ETAs may be subtracted, and subtract them only if they may.

    ``difference_hours`` is left None whenever the answer would mislead. A number that
    reads as provider disagreement, when the two providers were forecasting different
    milestones all along, is worse than no number: it would be quoted.
    """
    common = {
        "reference_provider": reference_provider,
        "candidate_provider": candidate_provider,
        "reference_target": reference_target,
        "candidate_target": candidate_target,
        "reference_eta_at": reference_eta_at,
        "candidate_eta_at": candidate_eta_at,
    }

    if reference_eta_at is None or candidate_eta_at is None:
        return EtaComparison(**common, comparable=False, verdict=NOT_COMPARABLE_MISSING_ETA)

    if reference_target != candidate_target:
        return EtaComparison(**common, comparable=False, verdict=NOT_COMPARABLE_DIFFERENT_TARGETS)

    if reference_target not in _COMPARABLE_TARGETS:
        # Same label, but the label is "unstated". Two providers that both decline to
        # say what they forecast have not agreed on anything.
        return EtaComparison(**common, comparable=False, verdict=NOT_COMPARABLE_UNKNOWN_TARGET)

    return EtaComparison(
        **common,
        comparable=True,
        verdict=COMPARABLE,
        difference_hours=round((candidate_eta_at - reference_eta_at).total_seconds() / 3600.0, 2),
    )
