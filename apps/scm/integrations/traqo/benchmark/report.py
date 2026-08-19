"""Rendering a comparison result — for a person to read, and for a file to keep.

The human report is written so that information loss is the thing you notice first: a
field one provider supplied and the other did not is printed as "no UN/LOCODE" rather
than left blank, because a blank column reads as "nothing to see".

The JSON report carries measurements only. No raw provider payloads, no credentials, no
event bodies — enough to keep a run for later analysis, not enough to couple the
benchmark to anyone's schema.
"""

from __future__ import annotations

from apps.scm.tracking.models import TrackingEvent

from .matching import AMBIGUOUS, CANDIDATE_ONLY, MATCHED, REFERENCE_ONLY, EventComparison
from .runner import ComparisonResult

# What a verdict is called in the report, from the reader's point of view.
_VERDICT_LABELS = {
    MATCHED: "matched",
    REFERENCE_ONLY: "{reference} only",
    CANDIDATE_ONLY: "{candidate} only",
    AMBIGUOUS: "ambiguous",
}

_ABSENT = "—"


def _when(value, *, with_time: bool = True) -> str:
    if value is None:
        return _ABSENT
    return value.strftime("%Y-%m-%d %H:%M") if with_time else value.strftime("%Y-%m-%d")


def _count(part: int, whole: int) -> str:
    return f"{part}/{whole}"


def _percent(value: float | None) -> str:
    return _ABSENT if value is None else f"{value}%"


def _delta(comparison: EventComparison) -> str:
    minutes = comparison.delta_minutes
    if minutes is None:
        return _ABSENT
    if abs(minutes) < 60:
        return f"{minutes:+.0f} min"
    return f"{minutes / 60:+.1f} h"


def _event_label(event: TrackingEvent) -> str:
    """The milestone, or the provider's own wording when we could not classify it."""
    if event.event_type != TrackingEvent.EventType.UNKNOWN:
        return event.get_event_type_display()
    return f"unclassified ({event.carrier_reference or event.event_code or 'no code'})"


def render_text(result: ComparisonResult, *, verbose: bool = False) -> str:
    """Return the human-readable benchmark report."""
    reference = result.reference_provider_code
    candidate = result.candidate_provider_code
    lines: list[str] = []

    lines.append("=" * 78)
    lines.append(f"{candidate.upper()} vs {reference.upper()} — benchmark")
    lines.append(f"Container: {result.container_number}   Team: {result.team_slug}")
    lines.append(f"Mode: {result.mode}   Sealine: {result.sealine}   Run at: {_when(result.run_at)}")
    lines.append("=" * 78)

    if result.is_sandbox:
        lines.append("")
        lines.append(
            "NOTE: sandbox mode returns fixed demo data for one container regardless of what was "
            "asked. These numbers exercise the benchmark machinery; they say nothing about "
            f"{candidate}'s real data quality."
        )

    lines.extend(_events_section(result))
    lines.extend(_side_by_side_section(result, verbose=verbose))
    lines.extend(_location_section(result))
    lines.extend(_vessel_section(result))
    lines.extend(_eta_section(result))
    lines.extend(_freshness_section(result))
    lines.extend(_snapshot_section(result))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------


def _events_section(result: ComparisonResult) -> list[str]:
    reference = result.reference_summary
    candidate = result.candidate_summary
    matches = result.match_summary
    lines = ["", "EVENTS", "-" * 78]
    lines.append(f"{'':26}{result.reference_provider_code:>14}{result.candidate_provider_code:>14}")
    for label, left, right in (
        ("total events", reference.total, candidate.total),
        ("actual", reference.actual, candidate.actual),
        ("estimated", reference.estimated, candidate.estimated),
        ("planned", reference.planned, candidate.planned),
        ("requested", reference.requested, candidate.requested),
        ("unknown time type", reference.unknown_time_type, candidate.unknown_time_type),
        ("unclassified milestone", reference.unclassified_event_type, candidate.unclassified_event_type),
        ("no timestamp", reference.without_timestamp, candidate.without_timestamp),
    ):
        lines.append(f"{label:26}{left:>14}{right:>14}")

    lines.append("")
    lines.append(f"matched                   {matches.matched}")
    lines.append(f"  of which within 1h      {matches.matched_within_tolerance_minutes}")
    lines.append(f"{result.reference_provider_code} only{'':>18}{matches.reference_only}")
    lines.append(f"{result.candidate_provider_code} only{'':>19}{matches.candidate_only}")
    lines.append(f"ambiguous                 {matches.ambiguous_reference + matches.ambiguous_candidate}")
    lines.append(f"match tolerance           ±{matches.tolerance_hours}h")
    lines.append("")
    lines.append(
        f"benchmark event coverage  {_percent(matches.benchmark_event_coverage_percent)} "
        f"({matches.matched_comparable_reference_events}/{matches.comparable_reference_events} "
        f"classified {result.reference_provider_code} events)"
    )
    lines.append(
        f"raw event coverage        {_percent(matches.raw_event_coverage_percent)} "
        f"({matches.matched}/{matches.total_reference_events} including unclassified)"
    )
    lines.append("  This is coverage of ONE container's journey against ONE reference provider.")
    lines.append("  It is not a statement about carrier coverage.")
    if matches.excluded_reference_codes:
        lines.append(f"  Excluded from the classified denominator: {', '.join(matches.excluded_reference_codes)}")
    if matches.location_name_disagreements:
        lines.append(f"  Matched events where the two place names differ: {matches.location_name_disagreements}")
    return lines


def _side_by_side_section(result: ComparisonResult, *, verbose: bool) -> list[str]:
    reference = result.reference_provider_code
    candidate = result.candidate_provider_code
    lines = ["", "EVENT BY EVENT", "-" * 78]
    lines.append(f"{'when':17}{'event':26}{'verdict':18}{'Δ time':>12}")
    lines.append("-" * 78)

    for comparison in result.comparisons:
        event = comparison.event
        verdict = _VERDICT_LABELS[comparison.verdict].format(reference=reference, candidate=candidate)
        lines.append(
            f"{_when(event.event_datetime):17}{_event_label(event)[:25]:26}{verdict:18}{_delta(comparison):>12}"
        )
        if comparison.verdict == MATCHED and comparison.differences:
            lines.extend(_matched_detail(comparison, reference=reference, candidate=candidate, verbose=verbose))
        elif comparison.verdict == AMBIGUOUS:
            lines.append(
                f"{'':4}indistinguishable from {comparison.ambiguous_with - 1} other event(s) — not forced into a match"
            )
        elif verbose:
            lines.append(f"{'':4}{_single_side_detail(event)}")
    return lines


# Fields whose absence on one side is the information loss this benchmark is about.
_DETAIL_FIELDS = (
    "location_name",
    "location_unlocode",
    "latitude",
    "longitude",
    "vessel_name",
    "vessel_imo",
    "voyage_number",
)


def _matched_detail(comparison: EventComparison, *, reference: str, candidate: str, verbose: bool) -> list[str]:
    """Show what the two providers said differently about one matched event."""
    interesting = [difference for difference in comparison.differences if verbose or difference.field in _DETAIL_FIELDS]
    if not interesting:
        return []

    lines = []
    for difference in interesting:
        left = difference.reference or f"no {difference.field.replace('_', ' ')}"
        right = difference.candidate or f"no {difference.field.replace('_', ' ')}"
        marker = "  <- lost" if difference.missing_from_candidate else ""
        lines.append(f"{'':4}{difference.field:20}{reference}: {left:28}{candidate}: {right}{marker}")
    return lines


def _single_side_detail(event: TrackingEvent) -> str:
    parts = [
        event.location_name or "no location name",
        event.location_unlocode or "no UN/LOCODE",
        event.vessel_name or "no vessel",
        event.vessel_imo or "no IMO",
        event.event_time_type,
    ]
    return " · ".join(parts)


def _location_section(result: ComparisonResult) -> list[str]:
    lines = ["", "LOCATION QUALITY", "-" * 78]
    for metrics in (result.reference_location, result.candidate_location):
        lines.append(f"{metrics.provider_code}:")
        lines.append(f"{'':4}{'location name':20}{_count(metrics.with_location_name, metrics.events)}")
        lines.append(f"{'':4}{'UN/LOCODE':20}{_count(metrics.with_unlocode, metrics.events)}")
        lines.append(f"{'':4}{'coordinates':20}{_count(metrics.with_coordinates, metrics.events)}")
        if metrics.distinct_unlocodes:
            lines.append(f"{'':4}{'ports named':20}{', '.join(metrics.distinct_unlocodes)}")
        else:
            lines.append(f"{'':4}{'ports named':20}no UN/LOCODE supplied — place names only")
    lines.append("")
    lines.append("Facility detail is folded into location_name by tracking ingestion, so it cannot be")
    lines.append("measured separately from canonical rows. Place names were not geocoded.")
    return lines


def _vessel_section(result: ComparisonResult) -> list[str]:
    lines = ["", "VESSEL / VOYAGE QUALITY", "-" * 78]
    for metrics in (result.reference_vessel, result.candidate_vessel):
        lines.append(f"{metrics.provider_code}:")
        lines.append(f"{'':4}{'event vessel name':22}{_count(metrics.with_vessel_name, metrics.events)}")
        lines.append(f"{'':4}{'event vessel IMO':22}{_count(metrics.with_vessel_imo, metrics.events)}")
        lines.append(f"{'':4}{'event voyage':22}{_count(metrics.with_voyage, metrics.events)}")
        lines.append(f"{'':4}{'transport mode':22}{_count(metrics.with_transport_mode, metrics.events)}")
        lines.append(f"{'':4}{'vessel-mode events':22}{metrics.vessel_mode_events}")
        lines.append(
            f"{'':4}{'journey vessels':22}"
            f"{', '.join(metrics.distinct_vessel_names) if metrics.distinct_vessel_names else _ABSENT}"
        )
        lines.append(
            f"{'':4}{'journey IMOs':22}"
            f"{', '.join(metrics.distinct_vessel_imos) if metrics.distinct_vessel_imos else _ABSENT}"
        )
        if not metrics.attributes_vessels_to_legs and metrics.events:
            lines.append(f"{'':4}LIMITATION: no event names the vessel that carried it, so a leg cannot be")
            lines.append(f"{'':4}attributed to a ship. On a transshipment journey this is material.")
    return lines


def _eta_section(result: ComparisonResult) -> list[str]:
    eta = result.eta
    lines = ["", "ETA", "-" * 78]
    for provider_eta in (eta.reference, eta.candidate):
        if not provider_eta.has_eta:
            lines.append(f"{provider_eta.provider_code}: no live arrival forecast (none given, or already arrived)")
            continue
        lines.append(
            f"{provider_eta.provider_code}: {_when(provider_eta.eta_at)} "
            f"({provider_eta.event_time_type}) at {provider_eta.location_name or _ABSENT}"
            f" / {provider_eta.location_unlocode or 'no UN/LOCODE'}"
        )
        lines.append(f"{'':4}learned at {_when(provider_eta.received_at)}, from {provider_eta.source_event_code}")

    difference = eta.difference_hours
    lines.append(f"difference: {_ABSENT if difference is None else f'{difference:+.2f} h'}")
    if eta.eta_history:
        lines.append(f"canonical ETA history rows retained for drift analysis: {len(eta.eta_history)}")
    else:
        lines.append("no canonical ETA history for this container (none recorded, or not on a shipment)")
    return lines


def _freshness_section(result: ComparisonResult) -> list[str]:
    freshness = result.freshness
    lines = ["", "FRESHNESS", "-" * 78]
    lines.append(f"matched actual events compared: {freshness.matched_actual_events}")
    lines.append(
        f"{freshness.reference_provider} median reporting lag (received - event): "
        f"{_hours_label(freshness.reference_median_reporting_lag_hours)}"
    )
    lines.append(
        f"{freshness.candidate_provider} median reporting lag (received - event): "
        f"{_hours_label(freshness.candidate_median_reporting_lag_hours)}"
    )
    lines.append(
        f"median provider observation lag (candidate received - reference received): "
        f"{_hours_label(freshness.median_observation_lag_hours)}"
    )
    lines.append(
        f"newest observed milestone — {freshness.reference_provider}: "
        f"{_when(freshness.reference_latest_actual_event_at)}, "
        f"{freshness.candidate_provider}: {_when(freshness.candidate_latest_actual_event_at)}"
    )
    lines.append(f"milestone recency gap: {_hours_label(freshness.milestone_recency_gap_hours)}")
    if freshness.candidate_payload_last_updated_at:
        lines.append(
            f"{freshness.candidate_provider} payload last_updated_at: {freshness.candidate_payload_last_updated_at}"
        )
    for note in freshness.notes:
        lines.append(f"NOTE: {note}")
    return lines


def _hours_label(value: float | None) -> str:
    return _ABSENT if value is None else f"{value:+.2f} h"


def _snapshot_section(result: ComparisonResult) -> list[str]:
    matches = result.match_summary
    reference = result.reference_provider_code
    candidate = result.candidate_provider_code
    lines = ["", "=" * 78, "SNAPSHOT", "=" * 78]
    lines.append(f"Container: {result.container_number}   Mode: {result.mode}")
    lines.append("")
    lines.append("EVENTS")
    lines.append(f"{reference} events:{'':>12}{result.reference_summary.total}")
    lines.append(f"{candidate} events:{'':>13}{result.candidate_summary.total}")
    lines.append(f"Matched:{'':>21}{matches.matched}")
    lines.append(f"{reference} only:{'':>14}{matches.reference_only}")
    lines.append(f"{candidate} only:{'':>15}{matches.candidate_only}")
    lines.append(f"Ambiguous:{'':>19}{matches.ambiguous_reference + matches.ambiguous_candidate}")
    lines.append(f"Benchmark event coverage: {_percent(matches.benchmark_event_coverage_percent)}")
    lines.append("")
    lines.append("LOCATIONS")
    lines.append(
        f"{reference} UN/LOCODE:{'':>9}"
        f"{_count(result.reference_location.with_unlocode, result.reference_location.events)}"
    )
    lines.append(
        f"{candidate} UN/LOCODE:{'':>10}"
        f"{_count(result.candidate_location.with_unlocode, result.candidate_location.events)}"
    )
    lines.append("")
    lines.append("VESSEL")
    lines.append(
        f"{reference} event vessel:{'':>6}"
        f"{_count(result.reference_vessel.with_vessel_name, result.reference_vessel.events)}"
    )
    lines.append(
        f"{candidate} event vessel:{'':>7}"
        f"{_count(result.candidate_vessel.with_vessel_name, result.candidate_vessel.events)}"
    )
    lines.append("")
    lines.append("ETA")
    lines.append(f"{reference}: {_when(result.eta.reference.eta_at, with_time=False)}")
    lines.append(f"{candidate}: {_when(result.eta.candidate.eta_at, with_time=False)}")
    lines.append(f"Difference: {_hours_label(result.eta.difference_hours)}")
    lines.append("")
    lines.append("FRESHNESS")
    lines.append(f"Observed {candidate} observation lag: {_hours_label(result.freshness.median_observation_lag_hours)}")
    lines.append(f"Milestone recency gap: {_hours_label(result.freshness.milestone_recency_gap_hours)}")
    if result.freshness.first_observation:
        lines.append("First observation of the candidate — lag figures are backfill artefacts.")
    for note in result.notes:
        lines.append(f"NOTE: {note}")
    return lines


# ---------------------------------------------------------------------------
# JSON
# ---------------------------------------------------------------------------


def _comparison_as_dict(comparison: EventComparison) -> dict:
    """One comparison as measurements only — no raw provider payload, no secrets."""

    def side(event: TrackingEvent | None) -> dict | None:
        if event is None:
            return None
        return {
            "provider": event.provider.code,
            "event_type": event.event_type,
            "event_time_type": event.event_time_type,
            "carrier_event_type": event.carrier_event_type,
            "event_code": event.event_code,
            "event_datetime": event.event_datetime.isoformat() if event.event_datetime else None,
            "received_at": event.received_at.isoformat() if event.received_at else None,
            "location_name": event.location_name,
            "location_unlocode": event.location_unlocode,
            "latitude": str(event.location_latitude) if event.location_latitude is not None else None,
            "longitude": str(event.location_longitude) if event.location_longitude is not None else None,
            "vessel_name": event.vessel_name,
            "vessel_imo": event.vessel_imo,
            "voyage_number": event.voyage_number,
            "transport_mode": event.transport_mode,
            "description": event.description,
        }

    return {
        "verdict": comparison.verdict,
        "score": comparison.score,
        "delta_seconds": comparison.delta_seconds,
        "delta_minutes": comparison.delta_minutes,
        "within_one_hour": comparison.is_tight,
        "ambiguous_with": comparison.ambiguous_with,
        "reference": side(comparison.reference_event),
        "candidate": side(comparison.candidate_event),
        "differences": [difference.as_dict() for difference in comparison.differences],
        "missing_from_candidate": comparison.missing_from_candidate,
    }


def render_json(result: ComparisonResult) -> dict:
    """Return the benchmark result as a JSON-serialisable dict.

    Measurements only: enough to keep a run for later analysis, and deliberately not the
    provider payloads — those already live in TrackingRawPayload, and a benchmark file is
    not a place for them.
    """
    by_verdict = {MATCHED: [], REFERENCE_ONLY: [], CANDIDATE_ONLY: [], AMBIGUOUS: []}
    for comparison in result.comparisons:
        by_verdict[comparison.verdict].append(_comparison_as_dict(comparison))

    return {
        "container": result.container_number,
        "team": result.team_slug,
        "run_at": result.run_at.isoformat(),
        "mode": result.mode,
        "sealine": result.sealine,
        "reference_provider": result.reference_provider_code,
        "candidate_provider": result.candidate_provider_code,
        "reference_summary": result.reference_summary.as_dict(),
        "candidate_summary": result.candidate_summary.as_dict(),
        "match_summary": result.match_summary.as_dict(),
        "matched_events": by_verdict[MATCHED],
        "reference_only_events": by_verdict[REFERENCE_ONLY],
        "candidate_only_events": by_verdict[CANDIDATE_ONLY],
        "ambiguous_events": by_verdict[AMBIGUOUS],
        "location_metrics": {
            "reference": result.reference_location.as_dict(),
            "candidate": result.candidate_location.as_dict(),
        },
        "vessel_metrics": {
            "reference": result.reference_vessel.as_dict(),
            "candidate": result.candidate_vessel.as_dict(),
        },
        "eta_metrics": result.eta.as_dict(),
        "freshness_metrics": result.freshness.as_dict(),
        "candidate_ingest": {
            "events_created": result.candidate_ingest_created,
            "events_updated": result.candidate_ingest_updated,
        },
        "notes": result.notes,
    }
