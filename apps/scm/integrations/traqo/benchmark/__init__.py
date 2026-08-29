"""Benchmark apparatus for the Traqo experiment — measurement only, never production.

Compares what two tracking providers stored for the same container, so the question
"is this provider's data good enough to rely on" can be answered with evidence instead
of impressions.

Everything here is read-only with respect to already-ingested events. The one write in
the whole package is the candidate provider's ordinary Phase 1 ingestion, which is how
its data gets into canonical form in the first place. No event is altered, merged or
enriched from another provider: the asymmetries between providers are the result.

There is no database model, no schedule and no UI. A run produces a printed report and,
optionally, a JSON file. Deleting this package leaves the tracking domain untouched.
"""

from .candidates import (
    CandidateAssessment,
    assess_candidate,
    assess_reference_candidates,
    choose_candidate,
)
from .eta_target import (
    EtaTargetReading,
    compare_etas,
    read_carrier_eta_target,
    read_traqo_eta_target,
)
from .matching import (
    AMBIGUOUS,
    CANDIDATE_ONLY,
    DEFAULT_TOLERANCE_HOURS,
    MATCHED,
    REFERENCE_ONLY,
    EventComparison,
    match_events,
)
from .report import render_json, render_text
from .runner import REFERENCE_PROVIDER_CODE, ComparisonResult, compare_providers
from .snapshot import SNAPSHOT_SCHEMA, build_snapshot

__all__ = [
    "AMBIGUOUS",
    "CANDIDATE_ONLY",
    "DEFAULT_TOLERANCE_HOURS",
    "MATCHED",
    "REFERENCE_ONLY",
    "REFERENCE_PROVIDER_CODE",
    "SNAPSHOT_SCHEMA",
    "CandidateAssessment",
    "ComparisonResult",
    "EtaTargetReading",
    "EventComparison",
    "assess_candidate",
    "assess_reference_candidates",
    "build_snapshot",
    "choose_candidate",
    "compare_etas",
    "compare_providers",
    "match_events",
    "read_carrier_eta_target",
    "read_traqo_eta_target",
    "render_json",
    "render_text",
]
