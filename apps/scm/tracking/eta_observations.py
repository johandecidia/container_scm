"""Provider ETA observations, and the one place an ETA change is written.

An ETA is a Container SCM concept. A provider's ETA is an *observation* of it: Maersk
says one thing, Traqo says another, and a predictive model may later say a third. This
module takes an observation from any source and records it in the existing
``ETAHistory``. Nothing in it is provider-shaped — the provider only appears as a
``source`` string on the row.

Two functions:

``record_eta_change``               writes the history row. ``update_shipment_eta``
                                    calls it too, so there is exactly one place an
                                    ETAHistory row is created and the fields cannot
                                    drift between the two paths.
``record_provider_eta_observation`` the intake: decides whether an observation is worth
                                    recording, then routes it through the shipment ETA
                                    writer when the journey has a shipment with no
                                    forecast yet, or straight to history otherwise.

What it deliberately does not do: fetch, schedule, rank providers, or merge two
providers' forecasts into one number. Two sources observing the same journey leave two
attributable trails. Which of them the product should believe is a precedence question,
and answering it is not this module's job.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from apps.scm.shipments.models import Shipment

from .models import ETAHistory
from .selectors import has_journey_arrived

logger = logging.getLogger(__name__)


# What a provider's ETA is an ETA *for*. Providers disagree about this, and several do
# not say at all, so it is recorded rather than assumed: Traqo's `eta` on the benchmark
# container equalled the final empty-return event, not the vessel's arrival at the POD.
ETA_TARGET_UNKNOWN = ""  # the provider does not say, and it cannot be inferred
ETA_TARGET_PROVIDER_DEFINED = "provider_defined"  # the provider's own milestone, whatever it is
ETA_TARGET_VESSEL_ARRIVAL_POD = "vessel_arrival_pod"  # stated to be the vessel reaching the POD
ETA_TARGET_FINAL_DELIVERY = "final_delivery"  # stated to be arrival at the final place


@dataclass(frozen=True)
class ProviderEtaObservation:
    """One provider's statement about when a journey will arrive.

    ``observed_at`` is when Container SCM learned this, which is not the forecast and
    not the provider's own clock either — ``provider_updated_at`` keeps that, verbatim
    and unparsed, because a provider's infrastructure timezone says nothing about the
    journey.

    ``eta_at`` is the forecast to the hour where it can be placed in time; ``eta_date``
    is the date the business plans around. Both are given by the caller rather than
    derived from each other, because a provider whose zone is unknown has a meaningful
    local date and no meaningful instant.
    """

    provider_code: str
    observed_at: datetime
    eta_at: datetime | None = None
    eta_date: date | None = None
    target: str = ETA_TARGET_UNKNOWN
    target_name: str = ""
    target_unlocode: str = ""
    provider_updated_at: str = ""
    reliable: bool | None = None
    context: dict = field(default_factory=dict)

    @property
    def has_eta(self) -> bool:
        return self.eta_at is not None or self.eta_date is not None

    def as_provider_metadata(self) -> dict:
        """Return the provider's own framing of this forecast, for the history row.

        Kept because it is what makes the row interpretable later: the same date from a
        provider that calls its forecast unreliable is not the same claim. The full
        response is not duplicated here — it is already in ``TrackingRawPayload``.
        """
        # Annotated because the reliability flag and the provider context added below
        # are not strings, and an inferred dict[str, str] would reject both.
        metadata: dict[str, Any] = {
            "provider": self.provider_code,
            "observed_at": self.observed_at.isoformat(),
            "eta_target": self.target,
            "provider_updated_at": self.provider_updated_at,
        }
        if self.reliable is not None:
            metadata["eta_reliable"] = self.reliable
        if self.context:
            metadata["provider_context"] = self.context
        return metadata


def record_eta_change(
    *,
    team,
    shipment=None,
    container=None,
    tracking_event=None,
    previous_eta=None,
    new_eta=None,
    previous_eta_at=None,
    new_eta_at=None,
    changed_at,
    received_at,
    source: str = "",
    location_name: str = "",
    location_unlocode: str = "",
    raw_payload: dict | None = None,
) -> ETAHistory:
    """Append one row to the ETA history.

    The single writer, so a shipment forecast and a bare container forecast are stored
    the same way. ``changed_at`` is when the forecast moved and ``received_at`` when we
    heard about it; keeping them apart is what makes provider latency measurable.
    """
    if shipment is None and container is None:
        raise ValueError("An ETA change needs a shipment or a container to be about.")

    from apps.scm.shipments.services import calculate_eta_delta_minutes

    return ETAHistory.objects.create(
        team=team,
        shipment=shipment,
        container=container,
        tracking_event=tracking_event,
        previous_eta=previous_eta,
        new_eta=new_eta,
        previous_eta_at=previous_eta_at,
        new_eta_at=new_eta_at,
        delta_minutes=calculate_eta_delta_minutes(
            previous_eta=previous_eta,
            new_eta=new_eta,
            previous_eta_at=previous_eta_at,
            new_eta_at=new_eta_at,
        ),
        changed_at=changed_at,
        received_at=received_at,
        location_name=location_name[:200],
        location_unlocode=location_unlocode[:10],
        source=source[:100],
        raw_payload=raw_payload or {},
    )


def last_observation_from(team, *, source: str, shipment=None, container=None) -> ETAHistory | None:
    """Return this source's most recent forecast for this journey, if it has made one.

    Per source on purpose. Drift is a question about one provider changing its mind —
    comparing Traqo's new forecast against Maersk's previous one would report a
    disagreement between providers as a delay.
    """
    rows = ETAHistory.objects.filter(team=team, source=source)
    rows = rows.filter(shipment=shipment) if shipment is not None else rows.filter(shipment__isnull=True)
    if container is not None:
        rows = rows.filter(container=container)
    return rows.order_by("-changed_at", "-created_at").first()


# Shipment states where the journey is over, so a forecast of its arrival is answered or
# moot. Checked in addition to the actual arrival milestone, because a shipment can be
# closed out by hand without a carrier event ever saying so.
_CLOSED_SHIPMENT_STATUSES = (
    Shipment.Status.ARRIVED,
    Shipment.Status.PARTIALLY_RECEIVED,
    Shipment.Status.DELIVERED,
    Shipment.Status.CANCELLED,
)


def _journey_is_over(team, shipment, container) -> bool:
    """True when this journey has arrived or been closed, so it has no ETA left."""
    if shipment is not None and shipment.status in _CLOSED_SHIPMENT_STATUSES:
        return True
    return has_journey_arrived(team, shipment=shipment, container=container)


def _is_unchanged(previous: ETAHistory | None, observation: ProviderEtaObservation) -> bool:
    """True when this source is repeating the forecast it already gave.

    Compared on the instant where both have one and on the date otherwise, so a
    provider polled hourly for six weeks leaves one row per actual change rather than
    a thousand identical ones.
    """
    if previous is None:
        return False
    if observation.eta_at is not None and previous.new_eta_at is not None:
        return previous.new_eta_at == observation.eta_at
    return previous.new_eta == observation.eta_date and previous.new_eta_at == observation.eta_at


def record_provider_eta_observation(
    *,
    team,
    observation: ProviderEtaObservation,
    shipment=None,
    container=None,
    user=None,
) -> ETAHistory | None:
    """Record one provider's ETA observation, or return None if there is nothing to record.

    Four reasons to record nothing, in order:

    1. the provider gave no usable ETA — a missing forecast is not news;
    2. the journey has arrived or been closed — a delivered box must not display a
       future arrival, whatever the provider still says;
    3. this provider is repeating its own last forecast;
    4. nothing else, so it is recorded.

    Where the journey is on a shipment that has no forecast at all, the observation goes
    through ``update_shipment_eta`` — the same writer carrier events use — so the cached
    ETA, ``original_eta``, the shipment event and delay detection all follow from it with
    no second code path. Where the shipment already has a forecast, the observation is
    recorded as history against its own source and the cached value is left to whoever
    owns it: displacing another provider's forecast is precedence, which is decided
    elsewhere and not here.
    """
    if not observation.has_eta:
        return None

    if _journey_is_over(team, shipment, container):
        logger.debug(
            "Ignoring %s ETA observation: the journey has already arrived or closed.",
            observation.provider_code,
        )
        return None

    previous = last_observation_from(
        team,
        source=observation.provider_code,
        shipment=shipment,
        container=container,
    )
    if _is_unchanged(previous, observation):
        return None

    metadata = observation.as_provider_metadata()

    if shipment is not None and shipment.eta is None:
        from apps.scm.shipments.services import update_shipment_eta

        update_shipment_eta(
            shipment,
            observation.eta_date,
            source=observation.provider_code,
            confidence="medium" if observation.reliable is None else ("high" if observation.reliable else "low"),
            user=user,
            eta_at=observation.eta_at,
            previous_eta_at=previous.new_eta_at if previous else None,
            location_name=observation.target_name,
            location_unlocode=observation.target_unlocode,
            container=container,
            raw_payload=metadata,
        )
        return last_observation_from(team, source=observation.provider_code, shipment=shipment, container=container)

    return record_eta_change(
        team=team,
        shipment=shipment,
        container=container,
        previous_eta=previous.new_eta if previous else None,
        new_eta=observation.eta_date,
        previous_eta_at=previous.new_eta_at if previous else None,
        new_eta_at=observation.eta_at,
        changed_at=observation.observed_at,
        received_at=observation.observed_at,
        source=observation.provider_code,
        location_name=observation.target_name,
        location_unlocode=observation.target_unlocode,
        raw_payload=metadata,
    )
