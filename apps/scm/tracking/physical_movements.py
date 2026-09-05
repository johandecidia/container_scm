"""Turning carrier evidence into accepted physical movements — conservatively.

A resolved :class:`~apps.scm.tracking.models.TrackingEvent` location is not a
position. It is a carrier's statement about a place, and this module is the only
thing allowed to decide that a particular statement is strong enough to become a
:class:`~apps.scm.containers.models.ContainerMovement`. Carrier adapters cannot;
ingestion cannot; the resolver certainly cannot.

**Almost nothing qualifies, on purpose.** The normalised event vocabulary is built
for describing a *journey*, and most of it says nothing reliable about where a box
physically is:

.. code-block:: text

    VESSEL_ARRIVED     the ship reached the port; the box is still on it
    DISCHARGED         the box came off; onto which terminal, the event does not say
    LOADED_ON_VESSEL   a departure, whose destination is a forecast
    ETA_UPDATED        a forecast changing
    BOOKING_CREATED    paperwork
    CUSTOMS_HOLD       a status, not a place

Reading any of those as a position is how a container ends up recorded at
Oceanterminalen because a vessel docked at SEGOT. So only :data:`MOVEMENT_BY_EVENT_TYPE`
is acted on, and it currently holds one entry.

**Why GATE_IN and not GATE_OUT.** A gate-in names the facility the box entered, so
the resulting position is exactly what the event says. A gate-out names the facility
it left and says nothing about where it went — acting on it would replace a location
we know with nothing, on a carrier's word, which is the regression LOC-2 exists to
prevent. Departures need somewhere to go, and that is LOC-3's arrival lifecycle.

(``GATE_IN`` is absent from ``transport_status``'s milestone tuples for an unrelated
reason: a box is gated in on export *and* on import, so the code says nothing about
journey progress. It still says exactly which facility the box entered, which is all
this module asks of it.)

Every gate is guarded. An event is interpreted only when it is an observation, not a
forecast; when its place resolved to a canonical location rather than being merely
reported; when it is dated; and when it belongs to a container. Anything less is
still stored as evidence — it just does not move anything.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, cast

from django.db import IntegrityError, transaction

from apps.scm.containers.choices import LocationResolutionStatus, LocationSource, MovementType
from apps.scm.containers.movements import record_container_movement

from .models import TrackingEvent

if TYPE_CHECKING:
    from apps.scm.containers.models import ContainerMovement
    from apps.teams.models import Team

logger = logging.getLogger(__name__)

# The complete set of carrier events that may become physical movements. Adding to
# this table is a domain decision, not a configuration change: each entry asserts
# that the event type names the place the box ended up, unambiguously.
MOVEMENT_BY_EVENT_TYPE: dict[str, str] = {
    TrackingEvent.EventType.GATE_IN: MovementType.GATE_IN,
}


def is_eligible(event: TrackingEvent) -> bool:
    """True when *event* is strong enough to become a movement.

    Five conditions, each of which has to hold on its own:

    *classified* — the event type is one we have decided names a physical position.

    *observed* — ``ACTUAL``, never a forecast. A planned gate-in is a plan.

    *resolved* — the place was tied to a canonical location by the resolver, and
    ``RESOLVED`` specifically: ``AMBIGUOUS`` means the evidence fitted several
    places and the resolver refused to choose, which is not a position either.

    *dated* — without ``event_datetime`` there is no ``occurred_at``, and a movement
    that cannot be placed in time cannot take part in precedence.

    *attributed* — a movement belongs to a container. An event that only names a
    shipment describes several boxes, and picking one would be a guess.
    """
    return (
        event.event_type in MOVEMENT_BY_EVENT_TYPE
        and event.event_time_type == TrackingEvent.EventTimeType.ACTUAL
        and event.location_id is not None
        and event.location_resolution_status == LocationResolutionStatus.RESOLVED
        and event.event_datetime is not None
        and event.container_id is not None
    )


def interpret_tracking_event(team: Team, event: TrackingEvent) -> ContainerMovement | None:
    """Record the movement *event* implies, or None when it implies none.

    Idempotent. One tracking event yields at most one movement — enforced by the
    unique constraint on ``related_tracking_event``, not by comparing text and
    timestamps — so re-ingesting a carrier's payload, however many times, cannot add
    a second row. A concurrent writer that wins the race is detected through that
    constraint and this call simply returns None rather than failing.

    Recording is not accepting: the movement goes into the history and the
    projection then decides whether it is also the container's position. A gate-in
    the carrier reports for 09:30, delivered at 15:10, loses to an operator's 14:32
    observation without any special case here.
    """
    if not is_eligible(event):
        return None

    from apps.scm.containers.models import Container, ContainerMovement

    if ContainerMovement.objects.filter(team=team, related_tracking_event=event).exists():
        return None

    # Non-NULL: `is_eligible` has already established the event names a container.
    container = Container.objects.filter(team=team, pk=cast(int, event.container_id)).first()
    if container is None:
        # The event names a container belonging to somebody else, or one since
        # deleted. Either way there is nothing of ours to move.
        return None

    try:
        with transaction.atomic():
            return record_container_movement(
                team=team,
                container=container,
                movement_type=MOVEMENT_BY_EVENT_TYPE[event.event_type],
                to_location=event.location,
                occurred_at=event.event_datetime,
                source=LocationSource.TRACKING_EVENT,
                related_tracking_event=event,
                notes=event.description or event.status,
            )
    except IntegrityError:
        # Another worker interpreted the same event between the check and the write.
        return None


def interpret_tracking_event_safely(team: Team, event: TrackingEvent) -> ContainerMovement | None:
    """Interpret *event*, never raising.

    Called from the ingestion path, where the priority is the opposite way round
    from usual: tracking evidence is the thing that cannot be recovered — the
    carrier will not re-send it — while a movement is derived and is recreated the
    next time the event is refreshed. So an interpretation fault costs the movement,
    not the event.
    """
    try:
        return interpret_tracking_event(team, event)
    except Exception:  # noqa: BLE001 — an interpretation fault must not lose tracking evidence
        logger.warning(
            "Could not interpret tracking event %s (%s) as a physical movement.",
            event.pk,
            event.event_type,
            exc_info=True,
        )
        return None
