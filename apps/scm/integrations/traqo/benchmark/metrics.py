"""Benchmark metrics: what each provider supplied, and what the other did not.

Every number here is counted from canonical ``TrackingEvent`` rows that were already
stored by each provider's own ingestion. Nothing is inferred across providers: if Traqo
supplied a place name and Maersk supplied a place name *and* a UN/LOCODE, Traqo is
credited with the name and not with the code. That asymmetry is the measurement, so
closing it would destroy the experiment.

Two honesty constraints run through the module.

*Coverage is scoped, twice over.* It is coverage of one container's journey as recorded
by one reference provider — never a statement about carrier coverage in general. And it
is reported against two denominators: every reference event, and only those the
reference provider's own events could be classified into an operational milestone.
Document milestones a transport document was drafted or released are real events, but
they are not movements of a box and no aggregator claims to carry them; scoring Traqo
against them would understate it as surely as excluding them silently would flatter it.
Both numbers are printed, and the excluded codes are named.

*Freshness cannot be measured by a first look.* ``received_at`` records when Container
SCM learned of an event, so on the run that first ingests a provider it records when
the experiment started, not when the provider knew. Those runs are flagged
``first_observation`` and their lag figures are labelled backfill artefacts. What a
single run *can* measure is which provider knows about the more recent milestone, which
is reported separately as milestone recency.
"""

from __future__ import annotations

import statistics
from dataclasses import asdict, dataclass, field

from apps.scm.tracking.models import TrackingEvent

from .matching import AMBIGUOUS, CANDIDATE_ONLY, MATCHED, REFERENCE_ONLY, EventComparison

_TimeType = TrackingEvent.EventTimeType


def _percentage(part: int, whole: int) -> float | None:
    """Return part/whole as a percentage, or None when there is nothing to divide."""
    if not whole:
        return None
    return round(100.0 * part / whole, 1)


@dataclass
class ProviderEventSummary:
    """How many events of each kind one provider supplied for this container."""

    provider_code: str
    total: int = 0
    actual: int = 0
    estimated: int = 0
    planned: int = 0
    requested: int = 0
    unknown_time_type: int = 0
    unclassified_event_type: int = 0
    without_timestamp: int = 0
    # Milestones this provider's events were classified into, for a readable diff.
    milestones: list[str] = field(default_factory=list)

    @property
    def comparable(self) -> int:
        """Events classified into an operational milestone — see the module docstring."""
        return self.total - self.unclassified_event_type

    def as_dict(self) -> dict:
        return {**asdict(self), "comparable": self.comparable}


def summarise_provider_events(provider_code: str, events: list[TrackingEvent]) -> ProviderEventSummary:
    """Count one provider's events by classification, without interpreting them."""
    summary = ProviderEventSummary(provider_code=provider_code, total=len(events))
    for event in events:
        if event.event_time_type == _TimeType.ACTUAL:
            summary.actual += 1
        elif event.event_time_type == _TimeType.ESTIMATED:
            summary.estimated += 1
        elif event.event_time_type == _TimeType.PLANNED:
            summary.planned += 1
        elif event.event_time_type == _TimeType.REQUESTED:
            summary.requested += 1
        else:
            summary.unknown_time_type += 1

        if event.event_type == TrackingEvent.EventType.UNKNOWN:
            summary.unclassified_event_type += 1
        if event.event_datetime is None:
            summary.without_timestamp += 1

    summary.milestones = sorted(
        {event.event_type for event in events if event.event_type != TrackingEvent.EventType.UNKNOWN}
    )
    return summary


@dataclass
class MatchSummary:
    """The pairing outcome, and the coverage it implies for this journey only."""

    matched: int = 0
    matched_within_tolerance_minutes: int = 0
    reference_only: int = 0
    candidate_only: int = 0
    ambiguous_reference: int = 0
    ambiguous_candidate: int = 0
    tolerance_hours: int = 0
    comparable_reference_events: int = 0
    total_reference_events: int = 0
    matched_comparable_reference_events: int = 0
    # Reference codes excluded from the comparable denominator, named so the exclusion
    # is visible rather than assumed.
    excluded_reference_codes: list[str] = field(default_factory=list)
    location_name_disagreements: int = 0

    @property
    def benchmark_event_coverage_percent(self) -> float | None:
        """Matched share of the reference provider's *classified* events.

        Benchmark event coverage for one container's journey. Not carrier coverage,
        not a provider-wide figure.
        """
        return _percentage(self.matched_comparable_reference_events, self.comparable_reference_events)

    @property
    def raw_event_coverage_percent(self) -> float | None:
        """Matched share of every reference event, including unclassified ones."""
        return _percentage(self.matched, self.total_reference_events)

    def as_dict(self) -> dict:
        return {
            **asdict(self),
            "benchmark_event_coverage_percent": self.benchmark_event_coverage_percent,
            "raw_event_coverage_percent": self.raw_event_coverage_percent,
        }


def summarise_matches(
    comparisons: list[EventComparison],
    *,
    reference_events: list[TrackingEvent],
    tolerance_hours: int,
) -> MatchSummary:
    """Count the pairing verdicts and derive this journey's benchmark coverage."""
    summary = MatchSummary(
        tolerance_hours=tolerance_hours,
        total_reference_events=len(reference_events),
        comparable_reference_events=sum(
            1 for event in reference_events if event.event_type != TrackingEvent.EventType.UNKNOWN
        ),
        excluded_reference_codes=sorted(
            {
                event.carrier_reference or event.event_code or "(no code)"
                for event in reference_events
                if event.event_type == TrackingEvent.EventType.UNKNOWN
            }
        ),
    )

    for comparison in comparisons:
        if comparison.verdict == MATCHED:
            summary.matched += 1
            if comparison.is_tight:
                summary.matched_within_tolerance_minutes += 1
            if comparison.reference_event.event_type != TrackingEvent.EventType.UNKNOWN:
                summary.matched_comparable_reference_events += 1
            if any(
                difference.field == "location_name" and difference.reference and difference.candidate
                for difference in comparison.differences
            ):
                summary.location_name_disagreements += 1
        elif comparison.verdict == REFERENCE_ONLY:
            summary.reference_only += 1
        elif comparison.verdict == CANDIDATE_ONLY:
            summary.candidate_only += 1
        elif comparison.verdict == AMBIGUOUS:
            if comparison.reference_event is not None:
                summary.ambiguous_reference += 1
            else:
                summary.ambiguous_candidate += 1

    return summary


@dataclass
class LocationMetrics:
    """How precisely one provider said where each of its events happened."""

    provider_code: str
    events: int = 0
    with_location_name: int = 0
    with_unlocode: int = 0
    with_coordinates: int = 0
    distinct_unlocodes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            **asdict(self),
            "location_name_percent": _percentage(self.with_location_name, self.events),
            "unlocode_percent": _percentage(self.with_unlocode, self.events),
            "coordinates_percent": _percentage(self.with_coordinates, self.events),
            # TrackingEvent folds a facility name into location_name (see
            # tracking/ingestion.build_event_defaults), so facility richness cannot be
            # measured from canonical rows. Stated rather than reported as zero.
            "facility_information": "not represented separately in TrackingEvent",
        }


def location_metrics(provider_code: str, events: list[TrackingEvent]) -> LocationMetrics:
    """Count the location detail present on one provider's events."""
    metrics = LocationMetrics(provider_code=provider_code, events=len(events))
    for event in events:
        if event.location_name.strip():
            metrics.with_location_name += 1
        if event.location_unlocode.strip():
            metrics.with_unlocode += 1
        if event.location_latitude is not None and event.location_longitude is not None:
            metrics.with_coordinates += 1
    metrics.distinct_unlocodes = sorted({e.location_unlocode.strip().upper() for e in events if e.location_unlocode})
    return metrics


@dataclass
class VesselMetrics:
    """Whether a provider says which ship carried which leg, or only which voyage.

    Event-level and journey-level are separated deliberately. A provider that names one
    vessel for the whole shipment tells us less than one that names a vessel per leg,
    and on a transshipment journey the difference is the difference between knowing
    where a box is and guessing.
    """

    provider_code: str
    events: int = 0
    vessel_mode_events: int = 0
    with_vessel_name: int = 0
    with_vessel_imo: int = 0
    with_voyage: int = 0
    with_transport_mode: int = 0
    distinct_vessel_names: list[str] = field(default_factory=list)
    distinct_vessel_imos: list[str] = field(default_factory=list)
    distinct_voyages: list[str] = field(default_factory=list)

    @property
    def attributes_vessels_to_legs(self) -> bool:
        """True when at least one event names the vessel that carried it."""
        return self.with_vessel_name > 0 or self.with_vessel_imo > 0

    def as_dict(self) -> dict:
        return {
            **asdict(self),
            "vessel_name_percent": _percentage(self.with_vessel_name, self.events),
            "vessel_imo_percent": _percentage(self.with_vessel_imo, self.events),
            "voyage_percent": _percentage(self.with_voyage, self.events),
            "vessel_name_percent_of_vessel_legs": _percentage(self.with_vessel_name, self.vessel_mode_events),
            "attributes_vessels_to_legs": self.attributes_vessels_to_legs,
        }


def vessel_metrics(provider_code: str, events: list[TrackingEvent]) -> VesselMetrics:
    """Count the vessel, IMO and voyage detail present on one provider's events."""
    metrics = VesselMetrics(provider_code=provider_code, events=len(events))
    vessel_modes = (TrackingEvent.TransportMode.VESSEL, TrackingEvent.TransportMode.BARGE)
    for event in events:
        if event.transport_mode in vessel_modes:
            metrics.vessel_mode_events += 1
        if event.vessel_name.strip():
            metrics.with_vessel_name += 1
        if event.vessel_imo.strip():
            metrics.with_vessel_imo += 1
        if event.voyage_number.strip():
            metrics.with_voyage += 1
        if event.transport_mode.strip():
            metrics.with_transport_mode += 1

    metrics.distinct_vessel_names = sorted({e.vessel_name.strip() for e in events if e.vessel_name.strip()})
    metrics.distinct_vessel_imos = sorted({e.vessel_imo.strip() for e in events if e.vessel_imo.strip()})
    metrics.distinct_voyages = sorted({e.voyage_number.strip() for e in events if e.voyage_number.strip()})
    return metrics


@dataclass
class ProviderEta:
    """One provider's current arrival forecast for this container, if it has one."""

    provider_code: str
    eta_at: object | None = None
    event_time_type: str = ""
    location_name: str = ""
    location_unlocode: str = ""
    received_at: object | None = None
    source_event_code: str = ""

    @property
    def has_eta(self) -> bool:
        return self.eta_at is not None

    def as_dict(self) -> dict:
        return {
            "provider_code": self.provider_code,
            "eta_at": self.eta_at.isoformat() if self.eta_at else None,
            "event_time_type": self.event_time_type,
            "location_name": self.location_name,
            "location_unlocode": self.location_unlocode,
            "received_at": self.received_at.isoformat() if self.received_at else None,
            "source_event_code": self.source_event_code,
        }


@dataclass
class EtaMetrics:
    """The two providers' forecasts, and how far apart they are."""

    reference: ProviderEta
    candidate: ProviderEta
    # Rows already recorded by the canonical ETA history, for drift analysis later.
    eta_history: list[dict] = field(default_factory=list)

    @property
    def difference_hours(self) -> float | None:
        if not (self.reference.has_eta and self.candidate.has_eta):
            return None
        return round((self.candidate.eta_at - self.reference.eta_at).total_seconds() / 3600.0, 2)

    def as_dict(self) -> dict:
        return {
            "reference": self.reference.as_dict(),
            "candidate": self.candidate.as_dict(),
            "difference_hours": self.difference_hours,
            "eta_history": self.eta_history,
        }


def eta_for_provider(*, team, container, provider) -> ProviderEta:
    """Return what this container's ETA would be if only this provider existed.

    Uses the canonical selector, so the benchmark cannot drift from the rule the rest of
    the system applies — including that an actual arrival retires the forecast.
    """
    from apps.scm.tracking.selectors import get_container_tracking_eta_event

    event = get_container_tracking_eta_event(team, container, provider=provider)
    if event is None:
        return ProviderEta(provider_code=provider.code)
    return ProviderEta(
        provider_code=provider.code,
        eta_at=event.event_datetime,
        event_time_type=event.event_time_type,
        location_name=event.location_name,
        location_unlocode=event.location_unlocode,
        received_at=event.received_at,
        source_event_code=event.carrier_reference or event.event_code,
    )


@dataclass
class FreshnessMetrics:
    """Who knew what, when — and what a single run can honestly say about it.

    ``observation_lag_hours`` is the difference between when Container SCM stored the
    candidate's version of an event and when it stored the reference's. It is a
    *provider observation lag* for this installation, not carrier latency, and on a
    first ingest it measures the experiment rather than the provider.
    """

    reference_provider: str
    candidate_provider: str
    matched_actual_events: int = 0
    reference_median_reporting_lag_hours: float | None = None
    candidate_median_reporting_lag_hours: float | None = None
    median_observation_lag_hours: float | None = None
    first_observation: bool = False
    reference_latest_actual_event_at: object | None = None
    candidate_latest_actual_event_at: object | None = None
    candidate_payload_last_updated_at: str = ""
    notes: list[str] = field(default_factory=list)

    @property
    def milestone_recency_gap_hours(self) -> float | None:
        """How far behind the candidate's newest observed milestone is.

        Measurable from one run, unlike the lag figures: it compares what each provider
        knows now, not when we asked. Positive means the candidate is behind.
        """
        if not (self.reference_latest_actual_event_at and self.candidate_latest_actual_event_at):
            return None
        delta = self.reference_latest_actual_event_at - self.candidate_latest_actual_event_at
        return round(delta.total_seconds() / 3600.0, 2)

    def as_dict(self) -> dict:
        return {
            "reference_provider": self.reference_provider,
            "candidate_provider": self.candidate_provider,
            "matched_actual_events": self.matched_actual_events,
            "reference_median_reporting_lag_hours": self.reference_median_reporting_lag_hours,
            "candidate_median_reporting_lag_hours": self.candidate_median_reporting_lag_hours,
            "median_observation_lag_hours": self.median_observation_lag_hours,
            "first_observation": self.first_observation,
            "reference_latest_actual_event_at": (
                self.reference_latest_actual_event_at.isoformat() if self.reference_latest_actual_event_at else None
            ),
            "candidate_latest_actual_event_at": (
                self.candidate_latest_actual_event_at.isoformat() if self.candidate_latest_actual_event_at else None
            ),
            "milestone_recency_gap_hours": self.milestone_recency_gap_hours,
            "candidate_payload_last_updated_at": self.candidate_payload_last_updated_at,
            "notes": self.notes,
        }


def _hours(later, earlier) -> float | None:
    if later is None or earlier is None:
        return None
    return (later - earlier).total_seconds() / 3600.0


def _median(values: list[float]) -> float | None:
    usable = [value for value in values if value is not None]
    return round(statistics.median(usable), 2) if usable else None


def _latest_actual(events: list[TrackingEvent]):
    dated = [
        event.event_datetime
        for event in events
        if event.event_time_type == _TimeType.ACTUAL and event.event_datetime is not None
    ]
    return max(dated) if dated else None


def freshness_metrics(
    comparisons: list[EventComparison],
    *,
    reference_provider: str,
    candidate_provider: str,
    reference_events: list[TrackingEvent],
    candidate_events: list[TrackingEvent],
    first_observation: bool,
    candidate_payload_last_updated_at: str = "",
) -> FreshnessMetrics:
    """Measure reporting lag per provider, and the observation lag between them."""
    metrics = FreshnessMetrics(
        reference_provider=reference_provider,
        candidate_provider=candidate_provider,
        first_observation=first_observation,
        reference_latest_actual_event_at=_latest_actual(reference_events),
        candidate_latest_actual_event_at=_latest_actual(candidate_events),
        candidate_payload_last_updated_at=candidate_payload_last_updated_at,
    )

    reference_lags: list[float] = []
    candidate_lags: list[float] = []
    observation_lags: list[float] = []

    for comparison in comparisons:
        if comparison.verdict != MATCHED:
            continue
        reference = comparison.reference_event
        candidate = comparison.candidate_event
        if reference.event_time_type != _TimeType.ACTUAL:
            continue
        metrics.matched_actual_events += 1
        reference_lags.append(_hours(reference.received_at, reference.event_datetime))
        candidate_lags.append(_hours(candidate.received_at, candidate.event_datetime))
        observation_lags.append(_hours(candidate.received_at, reference.received_at))

    metrics.reference_median_reporting_lag_hours = _median(reference_lags)
    metrics.candidate_median_reporting_lag_hours = _median(candidate_lags)
    metrics.median_observation_lag_hours = _median(observation_lags)

    if first_observation:
        metrics.notes.append(
            f"{candidate_provider} events were first stored during this run, so its reporting lag and the "
            "observation lag are backfill artefacts of when the benchmark started — not provider latency. "
            "Only repeated runs over time can measure that."
        )
    if metrics.milestone_recency_gap_hours is not None and metrics.milestone_recency_gap_hours > 0:
        metrics.notes.append(
            f"{candidate_provider}'s newest observed milestone is "
            f"{metrics.milestone_recency_gap_hours}h behind {reference_provider}'s. This is measurable from a "
            "single run and does indicate staleness."
        )
    return metrics
