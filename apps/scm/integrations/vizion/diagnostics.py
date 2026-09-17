"""Developer-facing diagnostics for the Vizion POC — measurement only, never production.

Two questions, answered separately because they have different subjects.

:class:`VizionDiagnostic`
    What did Vizion say about *this* container, and what survived normalisation? Built
    from one run's raw response and the DTOs mapped from it, so raw and canonical sit
    side by side and a mapping loss is visible rather than inferred.

:class:`ProviderComparison`
    What does Container SCM now hold for this container, per provider? Read from
    canonical rows only — no HTTP, no provider client, no second copy of anybody's
    parsing. A provider that is not configured simply has no rows and is reported as
    absent rather than as zero, because those are different claims.

There is no database model, no schedule, no UI and no routing. Deleting this module
leaves the tracking domain untouched.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from apps.scm.integrations.carriers.dcsa.schemas import DcsaEventClassifier, NormalisedTrackingEvent

from .mapper import _dict, _text
from .schemas import VizionReference

# Providers worth showing a column for, in the order a reader compares them. Only those
# with stored rows are printed; the list is the vocabulary, not an assertion that each
# is configured.
COMPARISON_PROVIDER_CODES = ("vizion", "traqo", "maersk", "cma_cgm", "one", "msc", "hapag_lloyd")


def _location_summary(location: dict) -> str:
    """Render one top-level Vizion location as "Name (UNLOCODE)", or "—"."""
    if not location:
        return "—"
    name = _text(location, "name", "city")
    unlocode = _text(location, "unlocode")
    if name and unlocode:
        return f"{name} ({unlocode})"
    return name or unlocode or "—"


@dataclass
class VizionDiagnostic:
    """One container's Vizion run, raw and normalised, for eyeball comparison."""

    container_number: str
    aci_state: str = ""
    last_update_status: str = ""
    resolved_carrier: str = ""
    carrier_name: str = ""
    used_aci: bool | None = None
    reference_id: str = ""
    reference_active: bool | None = None
    journey_status: str = ""
    origin: str = "—"
    destination: str = "—"
    inland_origin: str = "—"
    inland_destination: str = "—"
    container_iso: str = ""

    updates_returned: int = 0
    milestones_raw: int = 0
    events_normalised: int = 0
    events_classified: int = 0
    events_actual: int = 0
    events_estimated: int = 0
    events_planned: int = 0
    events_unclassified_time: int = 0

    locations_with_unlocode: int = 0
    locations_with_coordinates: int = 0
    events_with_vessel: int = 0
    events_with_imo: int = 0
    events_with_voyage: int = 0
    events_with_mmsi: int = 0

    distinct_voyages: tuple[str, ...] = field(default_factory=tuple)
    distinct_vessels: tuple[str, ...] = field(default_factory=tuple)
    transshipment_legs: int = 0

    latest_actual_description: str = ""
    latest_actual_at: datetime | None = None
    eta_at: datetime | None = None
    eta_target: str = ""
    eta_location: str = ""
    eta_vessel: str = ""
    eta_imo: str = ""
    eta_voyage: str = ""

    raw_response_available: bool = False

    @property
    def normalisation_result(self) -> str:
        """A one-line verdict on whether normalisation kept what Vizion sent."""
        if not self.milestones_raw:
            return "no milestones returned — nothing to normalise"
        if self.events_normalised != self.milestones_raw:
            return f"LOSS: {self.milestones_raw} milestone(s) in, {self.events_normalised} event(s) out"
        unclassified = self.events_normalised - self.events_classified
        detail = f"{self.events_normalised}/{self.milestones_raw} milestones mapped"
        if unclassified:
            return f"{detail}; {unclassified} could not be classified (carrier code preserved)"
        return f"{detail}; all classified"

    def as_dict(self) -> dict:
        return {
            "container_number": self.container_number,
            "aci_state": self.aci_state,
            "last_update_status": self.last_update_status,
            "resolved_carrier": self.resolved_carrier,
            "carrier_name": self.carrier_name,
            "used_aci": self.used_aci,
            "reference_id": self.reference_id,
            "reference_active": self.reference_active,
            "journey_status": self.journey_status,
            "origin": self.origin,
            "destination": self.destination,
            "inland_origin": self.inland_origin,
            "inland_destination": self.inland_destination,
            "container_iso": self.container_iso,
            "updates_returned": self.updates_returned,
            "milestones_raw": self.milestones_raw,
            "events_normalised": self.events_normalised,
            "events_classified": self.events_classified,
            "events_actual": self.events_actual,
            "events_estimated": self.events_estimated,
            "events_planned": self.events_planned,
            "events_unclassified_time": self.events_unclassified_time,
            "locations_with_unlocode": self.locations_with_unlocode,
            "locations_with_coordinates": self.locations_with_coordinates,
            "events_with_vessel": self.events_with_vessel,
            "events_with_imo": self.events_with_imo,
            "events_with_voyage": self.events_with_voyage,
            "events_with_mmsi": self.events_with_mmsi,
            "distinct_voyages": list(self.distinct_voyages),
            "distinct_vessels": list(self.distinct_vessels),
            "transshipment_legs": self.transshipment_legs,
            "latest_actual_description": self.latest_actual_description,
            "latest_actual_at": self.latest_actual_at.isoformat() if self.latest_actual_at else "",
            "eta_at": self.eta_at.isoformat() if self.eta_at else "",
            "eta_target": self.eta_target,
            "eta_location": self.eta_location,
            "eta_vessel": self.eta_vessel,
            "eta_imo": self.eta_imo,
            "eta_voyage": self.eta_voyage,
            "raw_response_available": self.raw_response_available,
            "normalisation_result": self.normalisation_result,
        }


def _count_transshipment_legs(payload: dict) -> int:
    """Count transport arrivals that are neither the POL nor the POD.

    Vizion labels each transport milestone's leg through ``shipment_location.type_code``
    (PRE / POL / POD / PDE / RTP). An arrival at a code that is not POD is a call at an
    intermediate port — a transshipment. Counted and reported here rather than mapped to
    ``TRANSSHIPMENT_ARRIVED`` on the event, because doing that would need the canonical
    classifier to consult a provider-specific field. See the README's canonical gaps.
    """
    legs = 0
    for milestone in payload.get("milestones") or []:
        if not isinstance(milestone, dict):
            continue
        journey_event = _dict(milestone, "journey_event")
        if _text(journey_event, "journey_type").upper() != "TRANSPORT":
            continue
        if _text(journey_event, "event_type").upper() != "ARRI":
            continue
        code = _text(_dict(milestone, "shipment_location"), "type_code").upper()
        if code and code not in ("POD", "PDE"):
            legs += 1
    return legs


def build_diagnostic(
    *,
    container_number: str,
    reference: VizionReference | None,
    payload: dict,
    events: list[NormalisedTrackingEvent],
    updates_returned: int = 0,
    eta_observation=None,
) -> VizionDiagnostic:
    """Build the POC readout for one container from its raw payload and mapped events."""
    payload = payload if isinstance(payload, dict) else {}
    milestones = [item for item in (payload.get("milestones") or []) if isinstance(item, dict)]

    actual = [event for event in events if event.event_classifier == DcsaEventClassifier.ACTUAL]
    # Paired with its timestamp so the sort key cannot be None — the filter above
    # already guarantees that, but only the pairing says so.
    dated_actual = [(event.event_datetime, event) for event in actual if event.event_datetime is not None]
    latest = max(dated_actual, key=lambda pair: pair[0])[1] if dated_actual else None

    diagnostic = VizionDiagnostic(
        container_number=container_number,
        aci_state=reference.aci_state if reference else "",
        last_update_status=reference.last_update_status if reference else "",
        resolved_carrier=reference.carrier_identifier if reference else "",
        carrier_name=reference.carrier_name if reference else "",
        used_aci=reference.used_aci if reference else None,
        reference_id=reference.reference_id if reference else "",
        reference_active=reference.active if reference else None,
        journey_status=_text(payload, "status") or (reference.last_update_status if reference else ""),
        origin=_location_summary(_dict(payload, "origin_port")),
        destination=_location_summary(_dict(payload, "destination_port")),
        inland_origin=_location_summary(_dict(payload, "inland_origin")),
        inland_destination=_location_summary(_dict(payload, "inland_destination")),
        container_iso=_text(payload, "container_iso"),
        updates_returned=updates_returned,
        milestones_raw=len(milestones),
        events_normalised=len(events),
        events_classified=sum(1 for event in events if event.event_code),
        events_actual=len(actual),
        events_estimated=sum(1 for event in events if event.event_classifier == DcsaEventClassifier.ESTIMATED),
        events_planned=sum(1 for event in events if event.event_classifier == DcsaEventClassifier.PLANNED),
        events_unclassified_time=sum(1 for event in events if not event.event_classifier),
        locations_with_unlocode=sum(1 for event in events if event.location_unlocode),
        locations_with_coordinates=sum(1 for event in events if event.latitude and event.longitude),
        events_with_vessel=sum(1 for event in events if event.vessel_name),
        events_with_imo=sum(1 for event in events if event.vessel_imo),
        events_with_voyage=sum(1 for event in events if event.voyage_number),
        # MMSI has no canonical field, so it is counted from where it is preserved.
        events_with_mmsi=sum(1 for event in events if _text(event.raw_payload, "vessel_mmsi")),
        distinct_voyages=tuple(sorted({event.voyage_number for event in events if event.voyage_number})),
        distinct_vessels=tuple(sorted({event.vessel_name for event in events if event.vessel_name})),
        transshipment_legs=_count_transshipment_legs(payload),
        latest_actual_description=latest.description if latest else "",
        latest_actual_at=latest.event_datetime if latest else None,
        raw_response_available=bool(payload),
    )

    if eta_observation is not None:
        diagnostic.eta_at = eta_observation.eta_at
        diagnostic.eta_target = eta_observation.target
        diagnostic.eta_location = eta_observation.target_name
        diagnostic.eta_vessel = str(eta_observation.context.get("eta_vessel") or "")
        diagnostic.eta_imo = str(eta_observation.context.get("eta_vessel_imo") or "")
        diagnostic.eta_voyage = str(eta_observation.context.get("eta_voyage") or "")

    return diagnostic


# ---------------------------------------------------------------------------
# Cross-provider comparison
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProviderColumn:
    """What one provider has stored, canonically, for one container."""

    provider_code: str
    configured: bool
    events: int = 0
    classified: int = 0
    actual: int = 0
    forecast: int = 0
    with_unlocode: int = 0
    with_coordinates: int = 0
    with_vessel: int = 0
    with_imo: int = 0
    with_voyage: int = 0
    distinct_voyages: int = 0
    eta_at: datetime | None = None
    latest_event_at: datetime | None = None

    def as_dict(self) -> dict:
        return {
            "provider_code": self.provider_code,
            "configured": self.configured,
            "events": self.events,
            "classified": self.classified,
            "actual": self.actual,
            "forecast": self.forecast,
            "with_unlocode": self.with_unlocode,
            "with_coordinates": self.with_coordinates,
            "with_vessel": self.with_vessel,
            "with_imo": self.with_imo,
            "with_voyage": self.with_voyage,
            "distinct_voyages": self.distinct_voyages,
            "eta_at": self.eta_at.isoformat() if self.eta_at else "",
            "latest_event_at": self.latest_event_at.isoformat() if self.latest_event_at else "",
        }


@dataclass(frozen=True)
class ProviderComparison:
    """Side-by-side canonical coverage for one container, per provider."""

    container_number: str
    columns: tuple[ProviderColumn, ...] = field(default_factory=tuple)

    @property
    def present(self) -> tuple[ProviderColumn, ...]:
        """Only the providers that actually have rows — the rest are not comparable."""
        return tuple(column for column in self.columns if column.configured)

    def as_dict(self) -> dict:
        return {
            "container_number": self.container_number,
            "providers": [column.as_dict() for column in self.present],
        }


def compare_stored_providers(*, team, container, provider_codes=COMPARISON_PROVIDER_CODES) -> ProviderComparison:
    """Compare what each provider has stored for one container. Reads canonical rows only.

    No provider is called and no event is altered, merged or enriched from another — the
    asymmetries between providers *are* the result. A provider with no rows is reported
    as not configured rather than as a row of zeroes, because "we never asked" and "we
    asked and got nothing" are different findings.
    """
    from apps.scm.tracking.models import TrackingEvent
    from apps.scm.tracking.selectors import get_container_tracking_eta_event

    columns: list[ProviderColumn] = []
    for code in provider_codes:
        events = list(
            TrackingEvent.objects.filter(team=team, container=container, provider__code=code).select_related("provider")
        )
        if not events:
            columns.append(ProviderColumn(provider_code=code, configured=False))
            continue

        provider = events[0].provider
        eta_event = get_container_tracking_eta_event(team, container, provider=provider)
        dated = [event.event_datetime for event in events if event.event_datetime is not None]

        columns.append(
            ProviderColumn(
                provider_code=code,
                configured=True,
                events=len(events),
                classified=sum(1 for event in events if not event.is_unclassified),
                actual=sum(1 for event in events if event.is_actual),
                forecast=sum(1 for event in events if event.is_estimated),
                with_unlocode=sum(1 for event in events if event.location_unlocode),
                with_coordinates=sum(
                    1
                    for event in events
                    if event.location_latitude is not None and event.location_longitude is not None
                ),
                with_vessel=sum(1 for event in events if event.vessel_name),
                with_imo=sum(1 for event in events if event.vessel_imo),
                with_voyage=sum(1 for event in events if event.voyage_number),
                distinct_voyages=len({event.voyage_number for event in events if event.voyage_number}),
                eta_at=eta_event.event_datetime if eta_event else None,
                latest_event_at=max(dated) if dated else None,
            )
        )

    return ProviderComparison(container_number=container.container_id, columns=tuple(columns))
