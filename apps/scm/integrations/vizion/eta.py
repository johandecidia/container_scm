"""Reading Vizion's ETA as a provider observation.

Vizion has no ETA *field*. It publishes the arrival at the destination port as an
ordinary milestone and marks it a forecast::

    journey_event: {journey_type: TRANSPORT, event_type: ARRI, event_classifier: EST}
    shipment_location: {type_code: "POD"}
    planned: true

and the *same* milestone becomes the actual arrival when the vessel berths: ``planned``
flips to false and the classifier to ACT.

Two things follow, and they pull in opposite directions.

*The forecast needs no special handling to be usable.* Because it is a real milestone, it
reaches ``TrackingEvent`` as an ESTIMATED VESSEL_ARRIVED row through the ordinary mapper,
and ``get_container_tracking_eta_event`` — the canonical arrival-forecast selector — finds
it with no Vizion-specific code at all. This is the opposite of Traqo, whose events were
all actual and whose ETA existed only as a top-level field.

*But an event is not a history.* When the forecast moves, the mapper produces a second
event rather than editing the first, and nothing records that *this provider* changed its
mind. ``ETAHistory`` is where that belongs, and it is per-source by design, so the
observation is read here and recorded alongside the events — exactly as Traqo's is.

**What the ETA is for is stated, not guessed.** Traqo's ``data.eta`` had to be recorded as
``provider_defined`` because its value turned out to be the last event in the list rather
than any arrival. Vizion says which milestone it is: an ARRI at the POD. So it is recorded
as :data:`ETA_TARGET_VESSEL_ARRIVAL_POD`, which is a *specific* target — and that matters
downstream, because the benchmark refuses to subtract two ETAs unless both name the same
one. Where Vizion does not label the leg as POD, the target degrades to
``provider_defined`` rather than being assumed.
"""

from __future__ import annotations

import logging
from datetime import datetime

from apps.scm.integrations.carriers.dcsa.schemas import DcsaEventClassifier
from apps.scm.tracking.eta_observations import (
    ETA_TARGET_PROVIDER_DEFINED,
    ETA_TARGET_VESSEL_ARRIVAL_POD,
    ProviderEtaObservation,
)

from . import PROVIDER_CODE
from .mapper import _dict, _parse_timestamp, _text

logger = logging.getLogger(__name__)

# The DCSA shape of "the vessel reaches the port of discharge".
_TRANSPORT = "TRANSPORT"
_ARRIVED = "ARRI"
_POD = "POD"

_FORECAST_CLASSIFIERS = (DcsaEventClassifier.ESTIMATED, DcsaEventClassifier.PLANNED)


def _is_forecast(milestone: dict, journey_event: dict) -> bool:
    """True when this milestone is a forecast rather than an observation.

    The DCSA classifier decides where there is one; the ``planned`` boolean only stands
    in when there is not. An unstated forecast is not treated as one — reading silence as
    "estimated" would turn an unclassified arrival into an ETA.
    """
    classifier = _text(journey_event, "event_classifier").upper()
    if classifier:
        return classifier in _FORECAST_CLASSIFIERS
    return milestone.get("planned") is True


def _is_pod_arrival(milestone: dict, journey_event: dict) -> bool:
    """True when this milestone is the vessel's arrival at the port of discharge."""
    if _text(journey_event, "journey_type").upper() != _TRANSPORT:
        return False
    if _text(journey_event, "event_type").upper() != _ARRIVED:
        return False
    return _text(_dict(milestone, "shipment_location"), "type_code").upper() == _POD


def _is_any_arrival(journey_event: dict) -> bool:
    """True for a transport arrival anywhere, POD or not."""
    return (
        _text(journey_event, "journey_type").upper() == _TRANSPORT
        and _text(journey_event, "event_type").upper() == _ARRIVED
    )


def read_vizion_eta_observation(payload: dict, *, observed_at: datetime) -> ProviderEtaObservation | None:
    """Read one Vizion payload's arrival forecast, or None when it states none.

    ``observed_at`` is when Container SCM received the response. It is passed in rather
    than read from the clock so one ingest has one observation time, and so a re-read of
    a stored payload can say when the payload was actually received.

    The POD arrival is preferred and gives a specific target. Where no milestone is
    labelled POD, the **latest** forecast arrival is used and the target degrades to
    ``provider_defined``: it is still Vizion's view of when the box next arrives
    somewhere, and saying so is honest, whereas calling an unlabelled leg the POD would
    invent a claim Vizion did not make.
    """
    if not isinstance(payload, dict):
        return None

    milestones = [item for item in (payload.get("milestones") or []) if isinstance(item, dict)]

    pod: list[dict] = []
    other: list[dict] = []
    for milestone in milestones:
        journey_event = _dict(milestone, "journey_event")
        if not _is_forecast(milestone, journey_event):
            continue
        if _is_pod_arrival(milestone, journey_event):
            pod.append(milestone)
        elif _is_any_arrival(journey_event):
            other.append(milestone)

    chosen, target = (pod, ETA_TARGET_VESSEL_ARRIVAL_POD) if pod else (other, ETA_TARGET_PROVIDER_DEFINED)
    if not chosen:
        return None

    # The latest forecast arrival, because an earlier leg's estimate is not the journey's
    # ETA. Milestones with an unreadable timestamp cannot be ordered and are dropped here
    # rather than sorted to an arbitrary end.
    dated = [(milestone, _parse_timestamp(milestone.get("timestamp"))) for milestone in chosen]
    # A second name rather than reassigning `dated`: the filtered list is the one that
    # is known to hold a timestamp on every row, and rebinding would leave that fact
    # invisible to both a reader and a type checker.
    usable = [(milestone, when) for milestone, when in dated if when is not None]
    if not usable:
        return None
    milestone, eta_at = max(usable, key=lambda pair: pair[1])

    location = _dict(milestone, "location")
    if target == ETA_TARGET_PROVIDER_DEFINED:
        logger.info(
            "Vizion: no POD-labelled forecast arrival for %s; recording the latest forecast arrival "
            "as provider-defined.",
            _text(payload, "container_id"),
        )

    return ProviderEtaObservation(
        provider_code=PROVIDER_CODE,
        observed_at=observed_at,
        eta_at=eta_at,
        # The date at the arrival place, which is the date the business plans around.
        # Vizion's timestamps carry an offset, so this is exact rather than approximate.
        eta_date=eta_at.date(),
        target=target,
        target_name=_text(location, "name"),
        target_unlocode=_text(location, "unlocode"),
        provider_updated_at=_text(payload, "updated_at"),
        # Vizion states no reliability flag, and None means "not stated" rather than
        # "unreliable" — the ETA writer reads it as medium confidence on that basis.
        reliable=None,
        context={
            "eta_source": _text(milestone, "source"),
            "eta_vessel": _text(milestone, "vessel"),
            "eta_vessel_imo": _text(milestone, "vessel_imo"),
            "eta_voyage": _text(milestone, "voyage"),
            "eta_milestone_description": _text(milestone, "description"),
            "shipment_location_type_code": _text(_dict(milestone, "shipment_location"), "type_code"),
            "provider_timestamp": str(milestone.get("timestamp") or ""),
        },
    )
