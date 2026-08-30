"""Subtracting one benchmark snapshot from a later one.

A single observation of a provider says what it knows. Two, separated by time, say how
it behaves — whether its ETA moves, whether it corrects itself, whether the identities
it puts on its rows survive a refetch. That second thing is what Phase 2.2 needs and
what no amount of analysis of one payload can supply.

So this is a diff of two JSON files produced by :mod:`.snapshot`, and nothing else. It
does not fetch, does not read the database, and does not decide which provider is
right. Given T0 and T1 it reports four things:

*ETA drift, per provider, against that provider's own earlier value.* Never one
provider's new ETA against the other's old one — that would report a disagreement
between providers as a delay. And never across a changed ETA target: if a provider's
ETA meant a port arrival at T0 and an inland delivery at T1, the difference between the
two numbers is not drift, it is a change of subject, and it is reported as one.

*New events, per provider.* Paired on the snapshot fingerprint — provider, milestone,
time, place — because that is the only handle available until a provider is shown to
supply a stable one.

*Corrections.* A fingerprint that vanished while a similar one appeared at the same
milestone is a provider changing its mind about when something happened. Reported as a
correction rather than as one deletion and one addition, because that is what it is.

*Event identity stability.* Traqo's ``idx``, ``event_id``, ``name``, ``creation`` and
``modified``, matched on the semantic identity of the event that carried them, so the
question "does ``name`` survive a refetch" gets an answer per event instead of a guess.

Freshness gets a verdict here that it cannot get at T0: which provider's timeline grew
first is a genuine measurement once there are two observations.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .eta_target import UNKNOWN
from .snapshot import SNAPSHOT_SCHEMA

# Verdicts for one provider's ETA between two runs.
ETA_UNCHANGED = "unchanged"
ETA_DELAYED = "delayed"
ETA_IMPROVED = "improved"
ETA_APPEARED = "appeared"  # no ETA at T0, one at T1
ETA_WITHDRAWN = "withdrawn"  # an ETA at T0, none at T1
ETA_ABSENT = "absent"  # neither run had one
ETA_TARGET_CHANGED = "target_changed"  # both had one, but for different milestones

# Verdicts for whether a provider's own row identities survived a refetch.
IDENTITY_STABLE = "stable"
IDENTITY_UNSTABLE = "unstable"
IDENTITY_UNKNOWN = "unknown"  # nothing to compare — one side has no identity sample


class SnapshotMismatchError(ValueError):
    """Raised when two files cannot honestly be compared."""


@dataclass
class EtaDrift:
    """How one provider's forecast moved between two observations."""

    provider: str
    verdict: str
    previous_eta_at: str | None = None
    current_eta_at: str | None = None
    previous_target: str = ""
    current_target: str = ""
    drift_hours: float | None = None

    def as_dict(self) -> dict:
        return {
            "provider": self.provider,
            "verdict": self.verdict,
            "previous_eta_at": self.previous_eta_at,
            "current_eta_at": self.current_eta_at,
            "previous_target": self.previous_target,
            "current_target": self.current_target,
            "drift_hours": self.drift_hours,
        }


@dataclass
class ProviderEventDelta:
    """What changed in one provider's timeline between two observations."""

    provider: str
    previous_total: int = 0
    current_total: int = 0
    new_actual: list[dict] = field(default_factory=list)
    new_forecast: list[dict] = field(default_factory=list)
    disappeared: list[dict] = field(default_factory=list)
    corrections: list[dict] = field(default_factory=list)
    latest_actual_advanced_to: str | None = None

    @property
    def timeline_grew(self) -> bool:
        return bool(self.new_actual)

    def as_dict(self) -> dict:
        return {
            "provider": self.provider,
            "previous_total": self.previous_total,
            "current_total": self.current_total,
            "new_actual": self.new_actual,
            "new_forecast": self.new_forecast,
            "disappeared": self.disappeared,
            "corrections": self.corrections,
            "latest_actual_advanced_to": self.latest_actual_advanced_to,
            "timeline_grew": self.timeline_grew,
        }


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError, TypeError:
        return None


def _eta_drift(*, provider: str, previous: dict, current: dict, eta_key: str, target_key: str) -> EtaDrift:
    """Compare one provider's ETA across two snapshots of the same container."""
    previous_at = _parse(previous.get(eta_key))
    current_at = _parse(current.get(eta_key))
    previous_target = ((previous.get(target_key) or {}).get("target")) or UNKNOWN
    current_target = ((current.get(target_key) or {}).get("target")) or UNKNOWN

    # dict[str, Any] for the same reason as in eta_target.compare_etas: the values
    # have several types and this is unpacked into EtaDrift's distinct fields.
    common: dict[str, Any] = {
        "provider": provider,
        "previous_eta_at": previous.get(eta_key),
        "current_eta_at": current.get(eta_key),
        "previous_target": previous_target,
        "current_target": current_target,
    }

    if previous_at is None and current_at is None:
        return EtaDrift(verdict=ETA_ABSENT, **common)
    if previous_at is None:
        return EtaDrift(verdict=ETA_APPEARED, **common)
    if current_at is None:
        return EtaDrift(verdict=ETA_WITHDRAWN, **common)

    if previous_target != current_target:
        # Both runs have a number, and subtracting them would produce one. It is
        # withheld: the provider changed what it was forecasting, and a drift figure
        # across that change would be read as the arrival moving when it did not.
        return EtaDrift(verdict=ETA_TARGET_CHANGED, **common)

    drift = round((current_at - previous_at).total_seconds() / 3600.0, 2)
    verdict = ETA_UNCHANGED if drift == 0 else (ETA_DELAYED if drift > 0 else ETA_IMPROVED)
    return EtaDrift(verdict=verdict, drift_hours=drift, **common)


def _by_fingerprint(side: dict) -> dict[str, dict]:
    return {event["fingerprint"]: event for event in side.get("events") or [] if event.get("fingerprint")}


def _milestone_key(event: dict) -> tuple:
    """The part of an event's identity a timestamp correction would not change."""
    return (
        event.get("event_type") or "",
        event.get("event_time_type") or "",
        (event.get("location_name") or "").strip().upper(),
    )


def _event_delta(provider: str, previous: dict, current: dict) -> ProviderEventDelta:
    """Pair one provider's two timelines, separating additions from corrections."""
    before = _by_fingerprint(previous)
    after = _by_fingerprint(current)

    added = [event for fingerprint, event in after.items() if fingerprint not in before]
    removed = [event for fingerprint, event in before.items() if fingerprint not in after]

    # A milestone that is present on both sides under different fingerprints has been
    # re-timestamped, not replaced. Matching them keeps one real correction from being
    # counted as a new event plus a lost one.
    corrections: list[dict] = []
    removed_by_milestone: dict[tuple, list[dict]] = {}
    for event in removed:
        removed_by_milestone.setdefault(_milestone_key(event), []).append(event)

    still_added: list[dict] = []
    for event in added:
        matches = removed_by_milestone.get(_milestone_key(event))
        if matches:
            was = matches.pop(0)
            corrections.append(
                {
                    "event_type": event.get("event_type"),
                    "event_time_type": event.get("event_time_type"),
                    "location_name": event.get("location_name"),
                    "previous_event_datetime": was.get("event_datetime"),
                    "current_event_datetime": event.get("event_datetime"),
                    "shift_hours": _shift_hours(was.get("event_datetime"), event.get("event_datetime")),
                }
            )
            continue
        still_added.append(event)

    remaining_removed = [event for events in removed_by_milestone.values() for event in events]

    previous_latest = (previous.get("latest_actual") or {}).get("event_datetime")
    current_latest = (current.get("latest_actual") or {}).get("event_datetime")

    return ProviderEventDelta(
        provider=provider,
        previous_total=previous.get("total") or 0,
        current_total=current.get("total") or 0,
        new_actual=[event for event in still_added if event.get("event_time_type") == "actual"],
        new_forecast=[event for event in still_added if event.get("event_time_type") != "actual"],
        disappeared=remaining_removed,
        corrections=corrections,
        latest_actual_advanced_to=current_latest if current_latest != previous_latest else None,
    )


def _shift_hours(previous: str | None, current: str | None) -> float | None:
    before, after = _parse(previous), _parse(current)
    if before is None or after is None:
        return None
    return round((after - before).total_seconds() / 3600.0, 2)


def _identity_key(entry: dict) -> tuple:
    """Identify a provider row by what it describes, not by the ids under test."""
    return (
        str(entry.get("timestamp") or ""),
        str(entry.get("event_code") or ""),
        str(entry.get("location") or ""),
    )


# Fields whose stability across a refetch is the question. `creation` and `modified`
# are included because a provider that rebuilds its child rows every sync will move
# them even when nothing about the event changed — which is itself the answer.
_IDENTITY_FIELDS = ("event_id", "name", "creation", "modified", "idx")


def _identity_stability(previous: dict, current: dict) -> dict:
    """Report whether the candidate's own row identities survived the refetch."""
    before = {_identity_key(entry): entry for entry in previous.get("candidate_event_identity") or []}
    after = {_identity_key(entry): entry for entry in current.get("candidate_event_identity") or []}
    shared = [key for key in before if key in after]

    if not before or not after:
        return {
            "verdict": IDENTITY_UNKNOWN,
            "reason": "one of the two runs recorded no provider identity sample",
            "compared_events": 0,
            "fields": {},
        }
    if not shared:
        return {
            "verdict": IDENTITY_UNKNOWN,
            "reason": "the two runs share no event that could carry a comparable identity",
            "compared_events": 0,
            "fields": {},
        }

    fields: dict[str, dict] = {}
    for name in _IDENTITY_FIELDS:
        changed = [
            {
                "event": {"timestamp": key[0], "event_code": key[1], "location": key[2]},
                "previous": before[key].get(name),
                "current": after[key].get(name),
            }
            for key in shared
            if before[key].get(name) != after[key].get(name)
        ]
        fields[name] = {
            "stable": not changed,
            "changed_events": len(changed),
            "examples": changed[:3],
        }

    unstable = [name for name, report in fields.items() if not report["stable"]]
    return {
        "verdict": IDENTITY_UNSTABLE if unstable else IDENTITY_STABLE,
        "reason": (
            f"{', '.join(unstable)} changed on at least one event that both runs describe"
            if unstable
            else "every recorded identity field held for every event both runs describe"
        ),
        "compared_events": len(shared),
        "unstable_fields": unstable,
        "fields": fields,
    }


def _freshness_verdict(reference: ProviderEventDelta, candidate: ProviderEventDelta) -> dict:
    """Say which provider's timeline grew, which is measurable only across two runs."""
    if reference.timeline_grew and candidate.timeline_grew:
        first = "both"
        note = "both providers reported new observed events between the two runs"
    elif reference.timeline_grew:
        first = reference.provider
        note = f"only {reference.provider} reported new observed events; {candidate.provider} did not"
    elif candidate.timeline_grew:
        first = candidate.provider
        note = f"only {candidate.provider} reported new observed events; {reference.provider} did not"
    else:
        first = "neither"
        note = (
            "neither provider reported a new observed event between the two runs, so this "
            "interval cannot rank them on freshness"
        )
    return {
        "reported_new_movement_first": first,
        "note": note,
        "reference_new_actual": len(reference.new_actual),
        "candidate_new_actual": len(candidate.new_actual),
    }


def compare_snapshots(previous: dict, current: dict) -> dict:
    """Diff two snapshots of the same container, or refuse to.

    Raises :class:`SnapshotMismatchError` when the two files describe different
    containers or an unreadable schema. Comparing the wrong pair would produce a
    plausible report about nothing, which is the failure mode worth being loud about.
    """
    for label, snapshot in (("previous", previous), ("current", current)):
        if not isinstance(snapshot, dict) or not snapshot.get("container"):
            raise SnapshotMismatchError(f"The {label} snapshot has no container — it is not a benchmark snapshot.")

    if previous["container"] != current["container"]:
        raise SnapshotMismatchError(
            f"These snapshots describe different containers ({previous['container']} and "
            f"{current['container']}), so there is no drift to measure."
        )

    unknown_schemas = {
        snapshot.get("schema")
        for snapshot in (previous, current)
        if snapshot.get("schema") not in (None, SNAPSHOT_SCHEMA)
    }
    if unknown_schemas:
        raise SnapshotMismatchError(
            f"Unrecognised snapshot schema(s): {', '.join(sorted(str(s) for s in unknown_schemas))}. "
            f"This build reads {SNAPSHOT_SCHEMA}."
        )

    reference_provider = (current.get("reference") or {}).get("provider") or "reference"
    candidate_provider = (current.get("candidate") or {}).get("provider") or "candidate"

    reference_drift = _eta_drift(
        provider=reference_provider,
        previous=previous.get("reference") or {},
        current=current.get("reference") or {},
        eta_key="current_eta_at",
        target_key="eta_target",
    )
    candidate_drift = _eta_drift(
        provider=candidate_provider,
        previous=previous.get("candidate") or {},
        current=current.get("candidate") or {},
        eta_key="provider_eta_at",
        target_key="eta_target",
    )

    reference_delta = _event_delta(reference_provider, previous.get("reference") or {}, current.get("reference") or {})
    candidate_delta = _event_delta(candidate_provider, previous.get("candidate") or {}, current.get("candidate") or {})

    return {
        "container": current["container"],
        "previous_run_at": previous.get("run_at"),
        "current_run_at": current.get("run_at"),
        "interval_hours": _shift_hours(previous.get("run_at"), current.get("run_at")),
        "journey_state": {"previous": previous.get("journey_state"), "current": current.get("journey_state")},
        "eta_drift": {
            "reference": reference_drift.as_dict(),
            "candidate": candidate_drift.as_dict(),
        },
        "events": {
            "reference": reference_delta.as_dict(),
            "candidate": candidate_delta.as_dict(),
        },
        "event_identity": _identity_stability(previous, current),
        "freshness": _freshness_verdict(reference_delta, candidate_delta),
    }
