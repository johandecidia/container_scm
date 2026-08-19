"""Pairing one provider's canonical events against another's, for the benchmark only.

This is measurement apparatus, not a cross-provider event identity model. Nothing here
is persisted, nothing is written, and no event is altered: two lists of already-stored
:class:`~apps.scm.tracking.models.TrackingEvent` rows go in, and a verdict per event
comes out. Delete this package and the tracking domain is unchanged.

Why the constraints are what they are
-------------------------------------
``event_time_type`` is a hard partition. An estimated arrival and an actual arrival are
different facts about the world, and letting them pair would report a provider as
having observed something it only forecast — which is the single most misleading thing
this benchmark could do.

``event_type`` is a hard partition too. A gate-in is not a gate-out.

Within a partition, time proximity proposes and identity disposes. A UN/LOCODE or an
IMO that both providers state and that *disagree* disqualifies the pair outright: those
are identities, and two different places cannot be one event. A place *name* that
disagrees does not disqualify, because spelling and choice of name ("Yantian" versus
"Shenzhen", "Göteborg" versus "Gothenburg") is exactly the kind of provider difference
this benchmark exists to measure, and treating it as proof of a different event would
manufacture both a false MAERSK_ONLY and a false TRAQO_ONLY out of one real event. Such
disagreements are counted and reported instead.

Ambiguity is preserved, never resolved. When two candidates are indistinguishable by
every discriminator available — same score, same time distance — both events are
reported AMBIGUOUS rather than one being picked. A guess here would silently move a
count from one column to another in the result the whole experiment turns on.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from apps.scm.tracking.models import TrackingEvent

# An event a provider gave no time for cannot be placed in the journey, so it is listed
# after everything that can be.
_UNDATED_SORTS_LAST = datetime.max.replace(tzinfo=UTC)

# How far apart two reports of the same event may be and still be considered the same
# event. Providers timestamp differently — a terminal's local clock, a carrier's
# batch — so some tolerance is required; 24 hours is wide enough for that and narrow
# enough that consecutive legs of a real journey do not collide.
DEFAULT_TOLERANCE_HOURS = 24

# A match this close is reported separately: it says the two providers agree on when
# the event happened, not merely that both know of it.
TIGHT_TOLERANCE_MINUTES = 60

# Verdicts. Named for the benchmark's question rather than for the providers, so the
# engine can compare any candidate against any reference.
MATCHED = "matched"
REFERENCE_ONLY = "reference_only"
CANDIDATE_ONLY = "candidate_only"
AMBIGUOUS = "ambiguous"

# Evidence weights. Only identity that *both* events state can contribute — an absent
# field is never evidence, for or against.
_SCORE_UNLOCODE = 4
_SCORE_VESSEL_IMO = 3
_SCORE_LOCATION_NAME = 2
_SCORE_VESSEL_NAME = 2
_SCORE_VOYAGE = 1


def _fold(value: str) -> str:
    """Return a place or vessel name reduced to what is comparable across providers.

    Case, accents, punctuation and a trailing country qualifier are presentation, not
    identity: "Göteborg" and "GOTEBORG" are the same port, and so are "Caucedo" and
    "Caucedo, Dominican Republic".
    """
    text = (value or "").strip()
    if not text:
        return ""
    text = text.split(",")[0]
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(char for char in decomposed if not unicodedata.combining(char))
    return " ".join("".join(char if char.isalnum() else " " for char in stripped).split()).upper()


@dataclass(frozen=True)
class FieldDifference:
    """One field on which two matched events disagree, or which only one supplied."""

    field: str
    reference: str = ""
    candidate: str = ""

    @property
    def missing_from_candidate(self) -> bool:
        return bool(self.reference) and not self.candidate

    @property
    def missing_from_reference(self) -> bool:
        return bool(self.candidate) and not self.reference

    def as_dict(self) -> dict:
        return {
            "field": self.field,
            "reference": self.reference,
            "candidate": self.candidate,
            "missing_from_candidate": self.missing_from_candidate,
            "missing_from_reference": self.missing_from_reference,
        }


@dataclass
class EventComparison:
    """One row of the comparison: a pairing verdict and what the two sides said."""

    verdict: str
    reference_event: TrackingEvent | None = None
    candidate_event: TrackingEvent | None = None
    score: int = 0
    delta_seconds: int | None = None
    differences: list[FieldDifference] = field(default_factory=list)
    # Populated for AMBIGUOUS: how many candidates were indistinguishable.
    ambiguous_with: int = 0

    @property
    def event(self) -> TrackingEvent:
        """Whichever side exists — every comparison has at least one."""
        return self.reference_event or self.candidate_event

    @property
    def delta_minutes(self) -> float | None:
        return None if self.delta_seconds is None else round(self.delta_seconds / 60.0, 1)

    @property
    def is_tight(self) -> bool:
        """True when the two providers agree on the time, not merely on the event."""
        return self.delta_seconds is not None and abs(self.delta_seconds) <= TIGHT_TOLERANCE_MINUTES * 60

    @property
    def missing_from_candidate(self) -> list[str]:
        """Fields the reference supplied and the candidate did not."""
        return [difference.field for difference in self.differences if difference.missing_from_candidate]


def _identity_conflict(reference: TrackingEvent, candidate: TrackingEvent) -> bool:
    """True when both events state an identity and the identities differ.

    Only UN/LOCODE and IMO count: they are codes, assigned, and unambiguous. A name is
    not an identity — see the module docstring.
    """
    both_unlocodes = reference.location_unlocode and candidate.location_unlocode
    if both_unlocodes and reference.location_unlocode.strip().upper() != candidate.location_unlocode.strip().upper():
        return True
    both_imos = reference.vessel_imo and candidate.vessel_imo
    return bool(both_imos and reference.vessel_imo.strip() != candidate.vessel_imo.strip())


def _score(reference: TrackingEvent, candidate: TrackingEvent) -> int:
    """Return how much shared identity supports pairing these two events."""
    score = 0
    both_unlocodes = reference.location_unlocode and candidate.location_unlocode
    if both_unlocodes and reference.location_unlocode.strip().upper() == candidate.location_unlocode.strip().upper():
        score += _SCORE_UNLOCODE
    if reference.vessel_imo and candidate.vessel_imo and reference.vessel_imo.strip() == candidate.vessel_imo.strip():
        score += _SCORE_VESSEL_IMO
    if _fold(reference.location_name) and _fold(reference.location_name) == _fold(candidate.location_name):
        score += _SCORE_LOCATION_NAME
    if _fold(reference.vessel_name) and _fold(reference.vessel_name) == _fold(candidate.vessel_name):
        score += _SCORE_VESSEL_NAME
    if reference.voyage_number and reference.voyage_number.strip() == candidate.voyage_number.strip():
        score += _SCORE_VOYAGE
    return score


def _differences(reference: TrackingEvent, candidate: TrackingEvent) -> list[FieldDifference]:
    """List every field on which a matched pair does not say the same thing.

    Includes fields only one side supplied, which is the point: information loss is the
    benchmark's subject, and it shows up here as a difference with one side empty.
    """
    comparable = (
        ("location_name", reference.location_name, candidate.location_name),
        ("location_unlocode", reference.location_unlocode, candidate.location_unlocode),
        ("latitude", _coordinate(reference.location_latitude), _coordinate(candidate.location_latitude)),
        ("longitude", _coordinate(reference.location_longitude), _coordinate(candidate.location_longitude)),
        ("vessel_name", reference.vessel_name, candidate.vessel_name),
        ("vessel_imo", reference.vessel_imo, candidate.vessel_imo),
        ("voyage_number", reference.voyage_number, candidate.voyage_number),
        ("transport_mode", reference.transport_mode, candidate.transport_mode),
        ("event_code", reference.event_code, candidate.event_code),
        ("carrier_event_type", reference.carrier_event_type, candidate.carrier_event_type),
        ("description", reference.description, candidate.description),
    )
    differences = []
    for name, left, right in comparable:
        left_value = (left or "").strip()
        right_value = (right or "").strip()
        if left_value != right_value:
            differences.append(FieldDifference(field=name, reference=left_value, candidate=right_value))
    return differences


def _coordinate(value) -> str:
    return "" if value is None else str(value)


@dataclass(frozen=True)
class _Pair:
    """A viable pairing under consideration."""

    reference_index: int
    candidate_index: int
    score: int
    delta_seconds: int

    @property
    def rank(self) -> tuple[int, int]:
        """Best first: most shared identity, then closest in time."""
        return (-self.score, abs(self.delta_seconds))


def _partition(event: TrackingEvent) -> tuple[str, str]:
    """The hard partition an event may only be matched within."""
    return (event.event_type, event.event_time_type)


def _viable_pairs(
    reference_events: list[TrackingEvent],
    candidate_events: list[TrackingEvent],
    *,
    tolerance: timedelta,
) -> list[_Pair]:
    """Return every pairing not excluded by partition, time window or identity."""
    pairs: list[_Pair] = []
    for reference_index, reference in enumerate(reference_events):
        if reference.event_datetime is None:
            continue
        for candidate_index, candidate in enumerate(candidate_events):
            if candidate.event_datetime is None:
                continue
            if _partition(reference) != _partition(candidate):
                continue
            delta = candidate.event_datetime - reference.event_datetime
            if abs(delta) > tolerance:
                continue
            if _identity_conflict(reference, candidate):
                continue
            pairs.append(
                _Pair(
                    reference_index=reference_index,
                    candidate_index=candidate_index,
                    score=_score(reference, candidate),
                    delta_seconds=int(delta.total_seconds()),
                )
            )
    return pairs


def _indistinguishable(pair: _Pair, pairs: list[_Pair], claimed_reference: set, claimed_candidate: set) -> list[_Pair]:
    """Return the other still-available pairs this one cannot be told apart from.

    Two pairs sharing an event are indistinguishable when every discriminator agrees:
    the same amount of shared identity and the same distance in time. Anything else has
    a best answer, and the caller takes it.
    """
    rivals = []
    for other in pairs:
        if other is pair:
            continue
        if other.reference_index in claimed_reference or other.candidate_index in claimed_candidate:
            continue
        shares_event = other.reference_index == pair.reference_index or other.candidate_index == pair.candidate_index
        if not shares_event:
            continue
        if other.score == pair.score and abs(other.delta_seconds) == abs(pair.delta_seconds):
            rivals.append(other)
    return rivals


def match_events(
    reference_events: list[TrackingEvent],
    candidate_events: list[TrackingEvent],
    *,
    tolerance_hours: int = DEFAULT_TOLERANCE_HOURS,
) -> list[EventComparison]:
    """Pair two providers' events for one container and return a verdict per event.

    Every event on both sides appears in the result exactly once, under one of
    MATCHED, REFERENCE_ONLY, CANDIDATE_ONLY or AMBIGUOUS. An event with no timestamp
    cannot be placed in time and is reported as its provider's own — never matched on
    the strength of its type alone.

    Read-only: the events passed in are not modified, saved or merged.
    """
    tolerance = timedelta(hours=max(int(tolerance_hours), 0))
    pairs = sorted(_viable_pairs(reference_events, candidate_events, tolerance=tolerance), key=lambda p: p.rank)

    comparisons: list[EventComparison] = []
    claimed_reference: set[int] = set()
    claimed_candidate: set[int] = set()
    ambiguous_reference: dict[int, int] = {}
    ambiguous_candidate: dict[int, int] = {}

    for pair in pairs:
        if pair.reference_index in claimed_reference or pair.candidate_index in claimed_candidate:
            continue
        if pair.reference_index in ambiguous_reference or pair.candidate_index in ambiguous_candidate:
            continue

        rivals = _indistinguishable(pair, pairs, claimed_reference, claimed_candidate)
        if rivals:
            # Indistinguishable candidates: report the ambiguity rather than guess.
            count = len(rivals) + 1
            ambiguous_reference[pair.reference_index] = count
            ambiguous_candidate[pair.candidate_index] = count
            for rival in rivals:
                ambiguous_reference.setdefault(rival.reference_index, count)
                ambiguous_candidate.setdefault(rival.candidate_index, count)
            continue

        reference = reference_events[pair.reference_index]
        candidate = candidate_events[pair.candidate_index]
        claimed_reference.add(pair.reference_index)
        claimed_candidate.add(pair.candidate_index)
        comparisons.append(
            EventComparison(
                verdict=MATCHED,
                reference_event=reference,
                candidate_event=candidate,
                score=pair.score,
                delta_seconds=pair.delta_seconds,
                differences=_differences(reference, candidate),
            )
        )

    for index, event in enumerate(reference_events):
        if index in claimed_reference:
            continue
        if index in ambiguous_reference:
            comparisons.append(
                EventComparison(verdict=AMBIGUOUS, reference_event=event, ambiguous_with=ambiguous_reference[index])
            )
            continue
        comparisons.append(EventComparison(verdict=REFERENCE_ONLY, reference_event=event))

    for index, event in enumerate(candidate_events):
        if index in claimed_candidate:
            continue
        if index in ambiguous_candidate:
            comparisons.append(
                EventComparison(verdict=AMBIGUOUS, candidate_event=event, ambiguous_with=ambiguous_candidate[index])
            )
            continue
        comparisons.append(EventComparison(verdict=CANDIDATE_ONLY, candidate_event=event))

    return sorted(comparisons, key=_sort_key)


def _sort_key(comparison: EventComparison):
    """Chronological, with undated events last so the table reads as a journey."""
    event = comparison.event
    when = event.event_datetime if event is not None else None
    return (when or _UNDATED_SORTS_LAST, comparison.verdict)
