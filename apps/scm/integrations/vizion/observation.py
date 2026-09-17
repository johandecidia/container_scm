"""Comparing two fetches of one Vizion reference — the Phase 1B identity experiment.

Phase 1A left one question deliberately unresolved, because the documentation cannot
answer it: **is a Vizion milestone `id` stable across fetches?**

It matters because Vizion reuses a single milestone for both the ETA and the ATA — it
flips ``planned`` true→false and the classifier EST→ACT. Three outcomes are possible and
they call for different fingerprint strategies:

======================================  ================================================
observation                             what it would mean
======================================  ================================================
ids identical across fetches, and the   The id is a real identity. An id-keyed
EST→ACT flip keeps its id               fingerprint would model the flip as one row
                                        correcting itself, which is richer than the two
                                        rows the field-based fingerprint produces.
ids identical, but the flip issues a     The id identifies an *observation*, not an
new id                                   event. Field-based fingerprinting is right, and
                                         two rows is the honest representation.
ids differ between fetches               The id is per-sync scratch. Adopting it would
                                         duplicate the entire history every poll. This is
                                         what Traqo's ``name`` field turned out to be.
======================================  ================================================

So this module measures, and does not decide. It changes no fingerprint, writes nothing,
and has no opinion: :func:`compare_fetches` reports what the two payloads actually did and
:attr:`FetchComparison.identity_verdict` states which of the three cases the evidence
supports — including ``INCONCLUSIVE``, which is the correct answer when nothing moved
between the two fetches and is the *expected* answer for a first same-day run.

Everything here is read-only measurement apparatus. Deleting this module leaves the
integration working.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .mapper import _dict, _text, read_latest_payload

logger = logging.getLogger(__name__)

# What the evidence supports about milestone identity.
IDENTITY_STABLE_AND_REUSED = "STABLE_ID_REUSED_ON_FLIP"
IDENTITY_STABLE_NEW_ID_ON_FLIP = "STABLE_ID_REPLACED_ON_FLIP"
IDENTITY_UNSTABLE = "UNSTABLE_ID_PER_FETCH"
IDENTITY_INCONCLUSIVE = "INCONCLUSIVE"

# Milestone fields whose arrival or change is worth reporting. Chosen because each one is
# a canonical field: an enrichment here is a canonical improvement, not provider trivia.
_ENRICHABLE = ("vessel", "vessel_imo", "vessel_mmsi", "voyage")
_ENRICHABLE_LOCATION = ("unlocode", "facility", "timezone")


def _milestone_key(milestone: dict) -> tuple[str, str, str, str]:
    """Identify a milestone by what it *is*, independently of any provider id.

    Deliberately not the id — the whole point is to compare id behaviour against an
    identity that does not depend on the id. Uses the DCSA classification, the leg and the
    place, but **not** the timestamp or the classifier, so a milestone whose time moves or
    which flips EST→ACT is still recognised as the same milestone.

    The place component prefers the UN/LOCODE and falls back to the name, which is enough
    for an exact match. It is not enough on its own when a locode *appears* between two
    fetches, so :func:`_match` also tries a place-insensitive pass — see there.
    """
    journey_event = _dict(milestone, "journey_event")
    location = _dict(milestone, "location")
    return (
        _text(journey_event, "journey_type").upper(),
        _text(journey_event, "event_type").upper(),
        _text(_dict(milestone, "shipment_location"), "type_code").upper(),
        _text(location, "unlocode").upper() or _text(location, "name").upper(),
    )


def _places(milestone: dict) -> set[str]:
    """Every name this milestone's place answers to, for a tolerant place comparison."""
    location = _dict(milestone, "location")
    return {value.upper() for value in (_text(location, "unlocode"), _text(location, "name")) if value}


def _match(first_index: dict[tuple, dict], second_index: dict[tuple, dict]):
    """Pair up the milestones the two fetches have in common.

    Returns ``(pairs, added, removed)`` where each pair is ``(key, first, second)``.

    Two passes, because one is not sufficient and the reason is itself a finding.

    The exact pass matches identical keys. The second pass then retries the leftovers on
    the DCSA classification and leg alone, accepting a pair whose places *overlap* on
    either the locode or the name. That is what lets a milestone which gained a UN/LOCODE
    between fetches — or whose place Vizion renamed, the "Yantian versus Shenzhen" problem
    the Traqo benchmark hit — be recognised as the same milestone rather than reported as
    one removed and one added.

    Without it, the instrument would mistake provider enrichment for journey progress,
    which is precisely the confusion Phase 1B exists to avoid.
    """
    pairs = [(key, first_index[key], second_index[key]) for key in sorted(set(first_index) & set(second_index))]

    unmatched_first = {key: value for key, value in first_index.items() if key not in second_index}
    unmatched_second = {key: value for key, value in second_index.items() if key not in first_index}

    for second_key, second in list(unmatched_second.items()):
        for first_key, first in list(unmatched_first.items()):
            if first_key[:3] != second_key[:3]:
                continue
            if not (_places(first) & _places(second)):
                continue
            pairs.append((second_key, first, second))
            del unmatched_first[first_key]
            del unmatched_second[second_key]
            break

    return pairs, tuple(sorted(unmatched_second)), tuple(sorted(unmatched_first))


def _classifier(milestone: dict) -> str:
    stated = _text(_dict(milestone, "journey_event"), "event_classifier").upper()
    if stated:
        return stated
    planned = milestone.get("planned")
    if planned is True:
        return "EST"
    if planned is False:
        return "ACT"
    return ""


def _index(payload: dict) -> dict[tuple, dict]:
    """Index a payload's milestones by identity. Later duplicates win, and are counted."""
    indexed: dict[tuple, dict] = {}
    for milestone in payload.get("milestones") or []:
        if isinstance(milestone, dict):
            indexed[_milestone_key(milestone)] = milestone
    return indexed


@dataclass(frozen=True)
class MilestoneChange:
    """One milestone that differed between the two fetches."""

    key: tuple
    first_id: str = ""
    second_id: str = ""
    first_classifier: str = ""
    second_classifier: str = ""
    first_timestamp: str = ""
    second_timestamp: str = ""
    enriched_fields: tuple[str, ...] = field(default_factory=tuple)

    @property
    def id_changed(self) -> bool:
        return bool(self.first_id) and bool(self.second_id) and self.first_id != self.second_id

    @property
    def classifier_changed(self) -> bool:
        return self.first_classifier != self.second_classifier

    @property
    def timestamp_changed(self) -> bool:
        return self.first_timestamp != self.second_timestamp

    @property
    def is_forecast_realised(self) -> bool:
        """True when a forecast became an observation — the EST→ACT flip."""
        return self.first_classifier in ("EST", "PLN") and self.second_classifier == "ACT"

    def as_dict(self) -> dict:
        return {
            "milestone": " / ".join(part or "—" for part in self.key),
            "first_id": self.first_id,
            "second_id": self.second_id,
            "id_changed": self.id_changed,
            "first_classifier": self.first_classifier,
            "second_classifier": self.second_classifier,
            "classifier_changed": self.classifier_changed,
            "first_timestamp": self.first_timestamp,
            "second_timestamp": self.second_timestamp,
            "timestamp_changed": self.timestamp_changed,
            "forecast_realised": self.is_forecast_realised,
            "enriched_fields": list(self.enriched_fields),
        }


@dataclass(frozen=True)
class FetchComparison:
    """What changed between two fetches of one Vizion reference."""

    container_number: str
    first_update_count: int = 0
    second_update_count: int = 0
    first_milestone_count: int = 0
    second_milestone_count: int = 0
    ids_present_first: int = 0
    ids_present_second: int = 0
    common_milestones: int = 0
    added: tuple[tuple, ...] = field(default_factory=tuple)
    removed: tuple[tuple, ...] = field(default_factory=tuple)
    changes: tuple[MilestoneChange, ...] = field(default_factory=tuple)
    order_changed: bool = False
    new_update_ids: tuple[str, ...] = field(default_factory=tuple)

    @property
    def unchanged_milestones(self) -> tuple[MilestoneChange, ...]:
        return tuple(
            change
            for change in self.changes
            if not (change.id_changed or change.classifier_changed or change.timestamp_changed)
        )

    @property
    def ids_stable(self) -> bool | None:
        """Whether a milestone that did not otherwise change kept its id.

        None when no milestone carried an id in both fetches, so there is nothing to
        compare — which is itself a finding, and a different one from "unstable".
        """
        comparable = [
            change for change in self.changes if change.first_id and change.second_id and not change.classifier_changed
        ]
        if not comparable:
            return None
        return all(not change.id_changed for change in comparable)

    @property
    def forecast_realisations(self) -> tuple[MilestoneChange, ...]:
        """Milestones that flipped from forecast to observation between the fetches."""
        return tuple(change for change in self.changes if change.is_forecast_realised)

    @property
    def identity_verdict(self) -> str:
        """Which milestone-identity hypothesis this evidence supports.

        INCONCLUSIVE is a real and expected answer: two fetches minutes apart on a
        journey that did not move prove nothing about what happens when it does.
        """
        if self.ids_stable is False:
            return IDENTITY_UNSTABLE
        if self.ids_stable is None:
            return IDENTITY_INCONCLUSIVE

        flips = self.forecast_realisations
        if not flips:
            # Ids held, but nothing flipped, so the question the strategy turns on —
            # what happens to the id when a forecast is realised — is untouched.
            return IDENTITY_INCONCLUSIVE
        if all(flip.id_changed for flip in flips):
            return IDENTITY_STABLE_NEW_ID_ON_FLIP
        if all(not flip.id_changed for flip in flips):
            return IDENTITY_STABLE_AND_REUSED
        return IDENTITY_INCONCLUSIVE

    @property
    def recommendation(self) -> str:
        """What the verdict implies for the fingerprint. Advice, never applied."""
        verdict = self.identity_verdict
        if verdict == IDENTITY_UNSTABLE:
            return (
                "Keep the field-based fingerprint. Milestone ids changed between fetches, so adopting "
                "one as source_event_id would duplicate the whole history on every poll."
            )
        if verdict == IDENTITY_STABLE_AND_REUSED:
            return (
                "Consider an id-keyed fingerprint in Phase 2. Ids are stable AND survive the EST→ACT "
                "flip, so the flip could update one row instead of producing two — but note this "
                "would also overwrite the forecast, losing what the provider predicted. That is a "
                "trade-off to decide, not an obvious win."
            )
        if verdict == IDENTITY_STABLE_NEW_ID_ON_FLIP:
            return (
                "Keep the field-based fingerprint. Ids are stable but a realised forecast is issued a "
                "new id, so the id identifies an observation rather than an event — which is exactly "
                "what the two-row representation already models."
            )
        return (
            "Inconclusive, as expected from fetches taken close together. Re-run once the journey has "
            "actually moved — specifically after a forecast milestone has been realised."
        )

    def as_dict(self) -> dict:
        return {
            "container_number": self.container_number,
            "first_update_count": self.first_update_count,
            "second_update_count": self.second_update_count,
            "first_milestone_count": self.first_milestone_count,
            "second_milestone_count": self.second_milestone_count,
            "ids_present_first": self.ids_present_first,
            "ids_present_second": self.ids_present_second,
            "common_milestones": self.common_milestones,
            "added": [" / ".join(p or "—" for p in key) for key in self.added],
            "removed": [" / ".join(p or "—" for p in key) for key in self.removed],
            "order_changed": self.order_changed,
            "new_update_ids": list(self.new_update_ids),
            "ids_stable": self.ids_stable,
            "identity_verdict": self.identity_verdict,
            "recommendation": self.recommendation,
            "forecast_realisations": [change.as_dict() for change in self.forecast_realisations],
            "changed_milestones": [
                change.as_dict()
                for change in self.changes
                if change.id_changed or change.classifier_changed or change.timestamp_changed or change.enriched_fields
            ],
        }


def _enriched(first: dict, second: dict) -> tuple[str, ...]:
    """Return the canonical fields that gained a value between the two fetches."""
    gained: list[str] = []
    for name in _ENRICHABLE:
        if not _text(first, name) and _text(second, name):
            gained.append(name)
    first_location, second_location = _dict(first, "location"), _dict(second, "location")
    for name in _ENRICHABLE_LOCATION:
        if not _text(first_location, name) and _text(second_location, name):
            gained.append(f"location.{name}")
    first_geo, second_geo = _dict(first_location, "geolocation"), _dict(second_location, "geolocation")
    if not first_geo and second_geo:
        gained.append("location.geolocation")
    return tuple(gained)


def compare_fetches(
    first_updates: list[dict],
    second_updates: list[dict],
    *,
    container_number: str = "",
) -> FetchComparison:
    """Compare two fetches of the same Vizion reference. Reads only; decides nothing.

    Milestones are matched on what they *are* — DCSA type, code, leg and place — never on
    the provider id, because the id's behaviour is the thing under test. Matching on it
    would make the experiment assume its own conclusion.
    """
    first_payload = read_latest_payload(first_updates)
    second_payload = read_latest_payload(second_updates)

    first_index = _index(first_payload)
    second_index = _index(second_payload)
    pairs, added, removed = _match(first_index, second_index)

    changes = tuple(
        MilestoneChange(
            key=key,
            first_id=_text(first, "id"),
            second_id=_text(second, "id"),
            first_classifier=_classifier(first),
            second_classifier=_classifier(second),
            first_timestamp=str(first.get("timestamp") or ""),
            second_timestamp=str(second.get("timestamp") or ""),
            enriched_fields=_enriched(first, second),
        )
        for key, first, second in pairs
    )

    # Ordering is compared over the matched pairs, identified by their ordinal rather than
    # by their key: a pair found by the place-insensitive pass has a different key on each
    # side, so comparing keys would report every enrichment as a reordering. A newly added
    # milestone is excluded, so journey progress does not read as a reorder either.
    ordinal_first = {id(first): index for index, (_, first, _) in enumerate(pairs)}
    ordinal_second = {id(second): index for index, (_, _, second) in enumerate(pairs)}
    first_order = [
        ordinal_first[id(m)]
        for m in first_payload.get("milestones") or []
        if isinstance(m, dict) and id(m) in ordinal_first
    ]
    second_order = [
        ordinal_second[id(m)]
        for m in second_payload.get("milestones") or []
        if isinstance(m, dict) and id(m) in ordinal_second
    ]

    first_update_ids = {str(update.get("id") or "") for update in first_updates if isinstance(update, dict)}
    new_update_ids = tuple(
        sorted(
            str(update.get("id") or "")
            for update in second_updates
            if isinstance(update, dict) and str(update.get("id") or "") not in first_update_ids
        )
    )

    comparison = FetchComparison(
        container_number=container_number,
        first_update_count=len(first_updates),
        second_update_count=len(second_updates),
        first_milestone_count=len(first_payload.get("milestones") or []),
        second_milestone_count=len(second_payload.get("milestones") or []),
        ids_present_first=sum(1 for milestone in first_index.values() if _text(milestone, "id")),
        ids_present_second=sum(1 for milestone in second_index.values() if _text(milestone, "id")),
        common_milestones=len(pairs),
        added=added,
        removed=removed,
        changes=changes,
        order_changed=first_order != second_order,
        new_update_ids=new_update_ids,
    )
    logger.info(
        "Vizion refetch comparison for %s: verdict=%s, %d common milestone(s), %d realisation(s).",
        container_number,
        comparison.identity_verdict,
        comparison.common_milestones,
        len(comparison.forecast_realisations),
    )
    return comparison
